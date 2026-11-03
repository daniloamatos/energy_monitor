"""Tiny source datasets with hand-computed daily sums, counts and identities."""
from contextlib import closing
from datetime import date
import json
from pathlib import Path
import sqlite3
from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from energy_monitor.data_sources import SourceStore, read_csv
from energy_monitor.daily_cache import index_csv, read_daily, index_is_current, fingerprint
from energy_monitor.preview import COLUMNS, ID_COLUMN, ALERT_COLUMN, indicators


def install(store, identifier, path):
    (store.cache / f'{identifier}.csv').write_bytes(path.read_bytes())


@pytest.mark.parametrize('source,identifier,foreign', [('CCEE','pld','ons'),('ONS','ons','pld')])
def test_consulta_nao_abre_nem_verifica_arquivos_da_outra_fonte(tmp_path,monkeypatch,resource,store_factory,csv_factory,query_factory,source,identifier,foreign):
    from energy_monitor.diagnostics import Diagnostics
    from energy_monitor.data_sources import Resource
    store=store_factory([resource('pld'),resource('ons','ONS')])
    rows=[['202611','SUL','1','100']] if source=='CCEE' else [['2026-11-01 00:00:00','A','Usina','NORTE','HIDRO','Água','100']]
    install(store,identifier,csv_factory(source,rows))
    logger=Diagnostics(store.cache)
    for item in (resource('pld'),resource('ons','ONS')):
        logger.resource(Resource(**(item | {'start':date(2026,11,1),'end':date(2026,11,30)})),'running')
    original_open,original_stat=Path.open,Path.stat
    def opened(path,*args,**kwargs):
        assert not path.name.startswith(foreign+'.'), f'Abriu recurso alheio: {path.name}'
        return original_open(path,*args,**kwargs)
    def stat(path,*args,**kwargs):
        assert not path.name.startswith(foreign+'.'), f'Verificou recurso alheio: {path.name}'
        return original_stat(path,*args,**kwargs)
    monkeypatch.setattr(Path,'open',opened);monkeypatch.setattr(Path,'stat',stat)
    frame,_=store.load(query_factory(source=source,end=date(2026,11,1)))
    assert len(frame)==1
    assert set(frame.attrs['diagnostics'])=={identifier}


def test_indice_guarda_soma_contagem_e_pondera_medicoes_entre_blocos(tmp_path,monkeypatch,csv_factory):
    # Same day, three hours: (0+100+200)/3 = 100; two partial means are 50 and 200, whose mean 125 is wrong.
    rows=[{'MES_REFERENCIA':'202611','SUBMERCADO':'SUL','DIA':'1','HORA':str(hour),'PLD_HORA':str(value)}
          for hour,value in [(0,0),(1,100),(2,200)]]
    path=csv_factory('CCEE',rows)
    actual=pd.read_csv
    calls=[]
    def chunks(*args,**kwargs):
        kwargs['chunksize']=2;calls.append(kwargs['chunksize']);return actual(*args,**kwargs)
    monkeypatch.setattr('energy_monitor.data_sources.pd.read_csv',chunks)
    daily=read_daily(path,'CCEE',date(2026,11,1),date(2026,11,1))
    assert calls==[2]
    assert daily['sum'].iloc[0]==pytest.approx(300)
    assert daily['count'].iloc[0]==3
    assert (daily['sum']/daily['count']).iloc[0]==pytest.approx(100)


def test_somas_de_dois_meses_podem_ser_consolidadas_sem_media_de_medias(tmp_path,csv_factory):
    # January has 2 hourly observations: 0,100. February has 1:300. Pooled mean=400/3, not (50+300)/2.
    # This tests index sufficient statistics; UI indicators intentionally average displayed daily records.
    pieces=[]
    for month,values in [('01',[(0,0),(1,100)]),('02',[(0,300)])]:
        rows=[{'MES_REFERENCIA':f'2026{month}','SUBMERCADO':'SUL','DIA':'1','HORA':str(hour),'PLD_HORA':str(value)}
              for hour,value in values]
        path=csv_factory('CCEE',rows,name=f'{month}.csv')
        pieces.append(read_daily(path,'CCEE',date(2026,1,1),date(2026,2,1)))
    daily=pd.concat(pieces)
    assert daily['sum'].sum()/daily['count'].sum()==pytest.approx(400/3)
    assert ((daily['sum']/daily['count']).mean())==pytest.approx(175)


def test_medicao_ausente_e_excluida_e_zero_participa_da_media(tmp_path,csv_factory,resource,store_factory,query_factory):
    store=store_factory([resource()])
    rows=[{'MES_REFERENCIA':'202611','SUBMERCADO':'SUL','DIA':'1','HORA':str(hour),'PLD_HORA':value}
          for hour,value in [(0,''),(1,'0'),(2,'100')]]
    install(store,'pld',csv_factory('CCEE',rows))
    frame,_=store.load(query_factory(end=date(2026,11,1)))
    assert frame[COLUMNS[3]].iloc[0]==pytest.approx(50)
    with closing(sqlite3.connect(store.path('pld').with_suffix('.daily.sqlite'))) as db:
        assert db.execute('SELECT SUM("count") FROM daily').fetchone()[0]==2


def test_usinas_com_mesmo_nome_mantem_ids_distintos_e_nome_novo_nao_quebra_identidade(tmp_path,csv_factory,resource,store_factory,query_factory):
    store=store_factory([resource('ons','ONS')])
    rows=[['2026-11-01 00:00:00','A','Nome','NORTE','HIDRO','Água','100'],
          ['2026-11-01 00:00:00','B','Nome','SUL','TERM','Gás','300'],
          ['2026-11-03 00:00:00','A','Novo nome','NORTE','HIDRO','Água','150']]
    install(store,'ons',csv_factory('ONS',rows))
    frame,alerts=store.load(query_factory(source='ONS'))
    assert set(frame[ID_COLUMN])=={'A','B'}
    a=frame.loc[frame[ID_COLUMN].eq('A')]
    assert a[COLUMNS[4]].tolist()==pytest.approx([100,150])
    assert a[COLUMNS[5]].iloc[-1]==pytest.approx(50)
    assert alerts.detail(0).previous_day==date(2026,11,1)


@pytest.mark.parametrize('filters,expected', [({},[100,200,300]),
    ({'ons_submarket':'NORTE'},[100,200]),({'plant_type':'HIDRO'},[100]),
    ({'ons_submarket':'NORTE','plant_type':'TERM','fuel':'Gás'},[200]),({'location':'C'},[300]),
    ({'state':'AMAZONAS'},[100]),({'state':'PARÁ','plant_type':'TERM'},[200])])
def test_filtros_ons_combinados_e_opcoes_exclusivamente_locais(tmp_path,csv_factory,resource,store_factory,query_factory,filters,expected):
    store=store_factory([resource('ons','ONS')])
    rows=[{'din_instante':'2026-11-01 00:00:00','id_ons':'A','nom_usina':'Uma','nom_subsistema':'NORTE','nom_estado':'AMAZONAS','nom_tipousina':'HIDRO','nom_tipocombustivel':'Água','val_geracao':'100'},
          {'din_instante':'2026-11-01 00:00:00','id_ons':'B','nom_usina':'Duas','nom_subsistema':'NORTE','nom_estado':'PARÁ','nom_tipousina':'TERM','nom_tipocombustivel':'Gás','val_geracao':'200'},
          {'din_instante':'2026-11-01 00:00:00','id_ons':'C','nom_usina':'Outra','nom_subsistema':'SUL','nom_estado':'SÃO PAULO','nom_tipousina':'TERM','nom_tipocombustivel':'Óleo','val_geracao':'300'}]
    install(store,'ons',csv_factory('ONS',rows))
    q=query_factory(source='ONS',end=date(2026,11,1),**filters)
    frame,_=store.load(q)
    assert sorted(frame[COLUMNS[4]])==pytest.approx(expected)
    options=store.ons_options(q.start,q.end)
    assert options['submarkets']==['NORTE','SUL']
    assert options['states']==['AMAZONAS','PARÁ','SÃO PAULO']
    assert options['ids']==['A','B','C']
    assert options['fuels']==['Gás','Água','Óleo'] or set(options['fuels'])=={'Gás','Água','Óleo'}
    assert store.session.calls==[]


@pytest.mark.parametrize('location', ['Todos','Sudeste/Centro-Oeste','Sul','Nordeste','Norte'])
def test_submercados_ccee_disponiveis_sem_misturar_medicoes(tmp_path,csv_factory,resource,store_factory,query_factory,location):
    store=store_factory([resource()])
    install(store,'pld',csv_factory('CCEE',[['202611',market,'1',str(value)] for market,value in [('SE',100),('SUL',200),('NE',300),('N',400)]]))
    frame,_=store.load(query_factory(location=location,end=date(2026,11,1)))
    assert len(frame)==(4 if location=='Todos' else 1)
    if location!='Todos': assert set(frame[COLUMNS[2]])=={location}


def test_reabrir_store_reutiliza_indice_sem_reler_csv(tmp_path,resource,store_factory,csv_factory,query_factory,monkeypatch):
    store=store_factory([resource()]);install(store,'pld',csv_factory('CCEE',[['202611','SUL','1','100']]))
    q=query_factory(end=date(2026,11,1));expected,_=store.load(q)
    def forbidden(*args,**kwargs):raise AssertionError('CSV reprocessado')
    monkeypatch.setattr('energy_monitor.data_sources.read_csv',forbidden)
    reopened=SourceStore(store.config,store.cache,store.session)
    actual,_=reopened.load(q)
    pd.testing.assert_frame_equal(actual,expected)


def test_csv_alterado_invalida_indice_e_indice_corrompido_e_reconstruido_sem_rede(tmp_path,csv_factory):
    path=csv_factory('CCEE',[['202611','SUL','1','100']]);index=index_csv(path,'CCEE')
    assert index_is_current(path)
    path.write_text(path.read_text().replace(';100',';250'),encoding='utf-8')
    assert not index_is_current(path)
    result=read_daily(path,'CCEE',date(2026,11,1),date(2026,11,1))
    assert result['sum'].iloc[0]==pytest.approx(250)
    index.write_bytes(b'corrupt')
    rebuilt=read_daily(path,'CCEE',date(2026,11,1),date(2026,11,1))
    assert rebuilt['sum'].iloc[0]==pytest.approx(250)


def test_indice_versao_um_compativel_nao_e_reconstruido(tmp_path,csv_factory,monkeypatch):
    path=csv_factory('CCEE',[['202611','SUL','1','100']]);target=index_csv(path,'CCEE')
    with closing(sqlite3.connect(target)) as db,db:
        signature=json.loads(fingerprint(path));signature[0]=1
        db.execute('UPDATE metadata SET signature=?',(json.dumps(signature),))
    original=target.read_bytes()
    monkeypatch.setattr('energy_monitor.data_sources.read_csv',lambda *a,**k:pytest.fail('Leu CSV de índice v1 válido'))
    result=read_daily(path,'CCEE',date(2026,11,1),date(2026,11,1))
    assert result['count'].iloc[0]==1
    assert target.read_bytes()==original


def test_busca_cancelada_nao_substitui_resultados_ou_indice(tmp_path,csv_factory):
    from energy_monitor.cancellation import OperationCancelled
    path=csv_factory('CCEE',[['202611','SUL','1','100']]);index=index_csv(path,'CCEE')
    before=index.read_bytes()
    with pytest.raises(OperationCancelled):read_daily(path,'CCEE',date(2026,11,1),date(2026,11,1),cancelled=lambda:True)
    assert index.read_bytes()==before
