from dataclasses import replace
from datetime import date
from pathlib import Path
from unittest.mock import Mock,patch

import pandas as pd
import pytest

from energy_monitor.data_sources import SourceStore,read_daily
from energy_monitor.preview import (COLUMNS, ID_COLUMN, ALERT_COLUMN, AlertCollection, reapply_limits,
    restrict_period, extend_period, filter_snapshot, can_filter_snapshot)
from energy_monitor.query_cache import QueryCache
from energy_monitor.table_order import TableOrder


def identical(actual,expected):
    pd.testing.assert_frame_equal(actual.reset_index(drop=True),expected.reset_index(drop=True),check_dtype=False)


@pytest.fixture
def loaded(tmp_path,resource,store_factory,csv_factory,query_factory):
    store=store_factory([resource()])
    path=csv_factory('CCEE',[['202611','SUL',str(day),str(value)] for day,value in [(1,100),(2,120),(3,60)]])
    store.path('pld').write_bytes(path.read_bytes())
    query=query_factory(price_limit=110,change_limit=30)
    frame,alerts=store.load(query)
    return store,query,frame,alerts


@pytest.mark.parametrize('changes',[{'change_limit':10},{'price_limit':50},{'price_limit':None,'change_limit':None}])
def test_somente_limites_iguais_a_consulta_nova_sem_reler_arquivos(tmp_path,loaded,changes):
    store,q,frame,alerts=loaded;new=replace(q,**changes)
    expected,_=SourceStore(store.config,store.cache,store.session).load(new)
    with patch('energy_monitor.data_sources.read_daily',side_effect=AssertionError('Índice relido')),patch('energy_monitor.data_sources.read_csv',side_effect=AssertionError('CSV relido')):
        actual,new_alerts=reapply_limits(frame,alerts,q,new)
    identical(actual,expected)
    assert new_alerts.previous is alerts.previous
    assert len(new_alerts)==int(actual[ALERT_COLUMN].sum())


def test_reduzir_periodo_equivale_a_busca_nova_e_remove_base_externa(tmp_path,loaded):
    store,q,frame,alerts=loaded;new=replace(q,start=date(2026,11,2))
    expected,_=SourceStore(store.config,store.cache,store.session).load(new)
    with patch('energy_monitor.data_sources.read_daily',side_effect=AssertionError('Índice relido')):
        result,_=restrict_period(frame,q,new,alerts=alerts)
    identical(result,expected)
    assert pd.isna(result[COLUMNS[5]].iloc[0])
    assert result[COLUMNS[5]].iloc[1]==pytest.approx(-50)


def test_ampliar_periodo_le_so_a_borda_e_reconstroi_comparacao(tmp_path,loaded):
    store,q,full,_=loaded;small=replace(q,end=date(2026,11,2))
    frame,alerts=store.load(small)
    original_load=store.load;calls=[]
    def load(query,**kwargs):calls.append((query.start,query.end));return original_load(query,**kwargs)
    with patch.object(store,'load',side_effect=load):
        result,_=extend_period(store,frame,small,q,alerts=alerts)
    assert calls==[(date(2026,11,3),date(2026,11,3))]
    identical(result,full)


@pytest.mark.parametrize('source',['CCEE','ONS'])
def test_filtrar_entidade_em_memoria_equivale_a_consulta_nova(tmp_path,source,resource,store_factory,csv_factory,query_factory):
    identifier='pld' if source=='CCEE' else 'ons'
    store=store_factory([resource(identifier,source)])
    if source=='CCEE':
        rows=[['202611',market,str(day),str(value)] for market,value in [('SUL',100),('N',300)] for day in (1,2)]
        location='Sul'
    else:
        rows=[[f'2026-11-0{day} 00:00:00',identity,'Mesmo nome','NORTE','HIDRO','Água',str(value)]
              for identity,value in [('A',100),('B',300)] for day in (1,2)]
        location='A'
    store.path(identifier).write_bytes(csv_factory(source,rows).read_bytes())
    q=query_factory(source=source,end=date(2026,11,2));frame,_=store.load(q)
    target=replace(q,location=location)
    expected,_=SourceStore(store.config,store.cache,store.session).load(target)
    with patch('energy_monitor.data_sources.read_daily',side_effect=AssertionError('Índice relido')):
        actual,_=filter_snapshot(frame,q,target)
    identical(actual,expected)


def test_atributo_ons_ausente_do_snapshot_consulta_indice(tmp_path,resource,store_factory,csv_factory,query_factory):
    store=store_factory([resource('ons','ONS')])
    rows=[['2026-11-01 00:00:00',identifier,name,market,'HIDRO','Água',str(value)]
          for identifier,name,market,value in [('A','Uma','NORTE',100),('B','Outra','SUL',200)]]
    store.path('ons').write_bytes(csv_factory('ONS',rows).read_bytes())
    q=query_factory(source='ONS',end=date(2026,11,1));store.load(q)
    target=replace(q,ons_submarket='NORTE')
    assert not can_filter_snapshot(q,target)
    with patch('energy_monitor.data_sources.read_daily',wraps=read_daily) as read:
        result,_=store.load(target)
    assert read.call_count==1
    assert result[ID_COLUMN].tolist()==['A']


def test_cache_agregados_descarta_lru_respeita_teto_e_invalida_csv(tmp_path):
    cache=QueryCache(max_bytes=5000)
    calls=[]
    def loader(path,source,start,end,location,filters,**kwargs):
        calls.append(path.name)
        return pd.DataFrame({'day':[start],'entity':['id:A'],'location':['Uma'],'sum':[100.0],'count':[1]})
    paths=[]
    for i in range(3):
        path=tmp_path/f'{i}.csv';path.write_text('x');paths.append(path)
    for path in paths[:2]:cache.read(path,'ONS',date(2026,11,1),date(2026,11,1),'Todos',None,loader)
    assert len(cache.entries)==2
    cache.read(paths[0],'ONS',date(2026,11,1),date(2026,11,1),'Todos',None,loader)
    cache.read(paths[2],'ONS',date(2026,11,1),date(2026,11,1),'Todos',None,loader)
    assert cache.bytes_used<=5000
    assert {Path(key[0]).name for key in cache.entries}=={'0.csv','2.csv'}
    paths[0].write_text('changed')
    cache.read(paths[0],'ONS',date(2026,11,1),date(2026,11,1),'Todos',None,loader)
    assert calls.count('0.csv')==2


def test_caches_de_agregados_e_ordenacao_sao_independentes_e_limitados(tmp_path,frame_factory):
    frame=frame_factory([[date(2026,11,1),'CCEE','Sul',value,None,-value] for value in range(10)])
    order=TableOrder(frame,max_cache_bytes=80)
    cache=QueryCache()
    assert cache.max_bytes==32*1024*1024
    assert TableOrder(frame).max_cache_bytes==32*1024*1024
    order.sort(3);first=order.positions
    order.sort(5);order.sort(3);order.sort(5,False)
    assert (3,True) in order.orders and (5,True) not in order.orders
    assert sum(item.nbytes for item in order.orders.values())<=80
    assert cache.bytes_used==0 and not cache.entries


def test_intervalo_no_cache_nao_rele_indice_e_extensao_le_so_dias_faltantes(tmp_path):
    cache=QueryCache();path=tmp_path/'one.csv';path.write_text('data');calls=[]
    def loader(path,source,start,end,location,filters,**kwargs):
        calls.append((start,end));days=pd.date_range(start,end).date
        return pd.DataFrame({'day':days,'entity':'A','location':'Nome','sum':10.0,'count':1})
    def read(start,end):return cache.read(path,'ONS',start,end,'Todos',None,loader)
    read(date(2026,11,1),date(2026,11,2));read(date(2026,11,1),date(2026,11,3))
    read(date(2026,11,2),date(2026,11,2))
    assert calls==[(date(2026,11,1),date(2026,11,2)),(date(2026,11,3),date(2026,11,3))]
