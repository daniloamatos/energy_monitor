"""Offline contracts for official discovery, validated updates and data health."""
from datetime import date
import json
import os
from pathlib import Path

import pytest

from energy_monitor import discovery, generations
from energy_monitor.data_sources import catalog
from energy_monitor.data_health import health_report
from energy_monitor.diagnostics import Diagnostics
from energy_monitor.updater import UpdateExecutor

CCEE = b'MES_REFERENCIA;SUBMERCADO;DIA;PLD_HORA\n202611;SUL;1;100\n202611;SUL;2;200\n'


def payload(source, name, *, identifier='official', url=None, format='CSV'):
    return {'success': True, 'result': {'name': discovery.ENDPOINTS[source][1], 'resources': [
        {'id': identifier, 'name': name, 'format': format,
         'url': url or 'https://example.invalid/' + name + '.csv'}]}}


@pytest.mark.parametrize('source,name,start,end', [
    ('ONS', 'GERACAO_USINA_2-2024-02', '2024-02-01', '2024-02-29'),
    ('CCEE', 'PLD_HORARIO_2026', '2026-01-01', '2026-12-31'),
    ('CCEE', 'PLD_HISTORICO_SEMANAL_2001_2020', '2001-01-01', '2020-12-31'),
])
def test_catalogo_identifica_serie_cobertura_e_ano_bissexto(source, name, start, end):
    found, warnings = discovery.candidates(source, payload(source, name), discovery.ENDPOINTS[source][1])
    assert warnings == []
    assert (found[0]['source'], found[0]['start'], found[0]['end']) == (source, start, end)
    assert found[0]['provenance']['official_id'] == 'official'


@pytest.mark.parametrize('change', ['format', 'dataset', 'coverage', 'identity'])
def test_catalogo_rejeita_recurso_nao_identificavel(change):
    data = payload('CCEE', 'PLD_HORARIO_2026')
    item = data['result']['resources'][0]
    if change == 'format': item['format'] = 'XLSX'
    elif change == 'dataset': data['result']['name'] = 'outro'
    elif change == 'coverage': item['coverage_start'], item['coverage_end'] = '2025-01-01', '2025-12-31'
    else: item['id'] = ''
    with pytest.raises(ValueError):
        discovery.candidates('CCEE', data, 'pld_horario')


def test_duplicatas_equivalentes_consolidam_alias_e_conflitos_nao_escolhem_url(resource):
    original = resource() | {'provenance': {'official_id': 'a'}}
    duplicate = original | {'provenance': {'official_id': 'b'}}
    found, warnings = discovery.merge([], [original, duplicate])
    assert len(found) == 1
    assert found[0]['provenance']['aliases'] == ['a', 'b']
    assert warnings == []
    found, warnings = discovery.merge([], [original, duplicate | {'url': 'https://other.invalid/file.csv'}])
    assert found == []
    assert any('ambígua' in message for message in warnings)


def test_descoberta_preserva_configuracao_links_ids_e_exclusoes(store_factory, resource, http_mock, clock):
    custom = resource('personalizado', start='2026-01-01', end='2026-12-31')
    store = store_factory([custom], discovery={'ONS': {'enabled': False}}, excluded_resources=['official'])
    before = store.config.read_bytes()
    http_mock.queue(payload=payload('CCEE', 'PLD_HORARIO_2026'))
    result = discovery.discover(store, clock=clock)
    assert result['resources'] == [custom]
    assert store.config.read_bytes() == before
    assert (store.cache / 'discovered-sources.json').is_file()
    assert catalog(store.config, store.cache)[1][0].id == 'personalizado'


def test_falha_catalogo_mantem_fontes_conhecidas(store_factory, http_mock, clock):
    store = store_factory(discovery={'ONS': {'enabled': False}})
    http_mock.queue(payload=payload('CCEE', 'PLD_HORARIO_2026'), headers={'ETag': 'catalog-v1'})
    first = discovery.discover(store, clock=clock)
    http_mock.queue(status=503)
    failed = discovery.discover(store, force=True, clock=clock)
    assert failed['resources'] == first['resources']
    assert failed['providers']['CCEE']['result'] == 'failed'
    assert http_mock.calls[-1][1]['headers']['If-None-Match'] == 'catalog-v1'


def test_descoberta_desabilitada_nao_acessa_http(store_factory, http_mock, clock):
    store = store_factory(discovery_enabled=False)
    result = UpdateExecutor(store, clock=clock).run()
    assert result.exit_code == 0
    assert http_mock.calls == []


@pytest.mark.parametrize('today,current', [(date(2026, 11, 3), 'nov'), (date(2026, 12, 1), 'dez'), (date(2027, 1, 1), 'jan')])
def test_verificacao_ons_so_mes_vigente(today, current, store_factory, resource, monkeypatch):
    resources = [resource('nov', 'ONS'), resource('dez', 'ONS', '2026-12-01', '2026-12-31'),
                 resource('jan', 'ONS', '2027-01-01', '2027-01-31')]
    store = store_factory(resources)
    seen = []
    def sync_one(item, *args):
        seen.append(item.id)
        target = store.path(item.id); target.write_bytes(b'local')
        target.with_suffix('.json').write_text('{}')
        return 'installed'
    monkeypatch.setattr(store, '_sync_one', sync_one)
    assert store.sync(today, current_only=True) == []
    assert seen == [current]


@pytest.mark.parametrize('today,current', [(date(2026, 11, 3), 'ano2026'), (date(2027, 1, 1), 'ano2027')])
def test_verificacao_ccee_so_ano_vigente(today, current, store_factory, resource, monkeypatch):
    resources = [resource('ano2026', 'CCEE', '2026-01-01', '2026-12-31'),
                 resource('ano2027', 'CCEE', '2027-01-01', '2027-12-31'),
                 resource('ano2025', 'CCEE', '2025-01-01', '2025-12-31', mutable=False)]
    store = store_factory(resources)
    seen = []
    def sync_one(item, *args):
        seen.append(item.id)
        target = store.path(item.id); target.write_bytes(b'local')
        target.with_suffix('.json').write_text('{}')
        return 'installed'
    monkeypatch.setattr(store, '_sync_one', sync_one)
    assert store.sync(today, current_only=True) == []
    assert seen == [current]


def test_historico_presente_reutilizado_ausente_so_sincronizacao_completa(store_factory, resource, monkeypatch):
    resources = [resource('present', start='2025-01-01', end='2025-12-31', mutable=False),
                 resource('missing', start='2024-01-01', end='2024-12-31', mutable=False),
                 resource('future', start='2027-01-01', end='2027-12-31')]
    store = store_factory(resources)
    store.path('present').write_bytes(b'old')
    seen = []
    def sync_one(item, *args):
        seen.append(item.id)
        store.path(item.id).write_bytes(b'new')
        store.path(item.id).with_suffix('.json').write_text('{}')
        return 'installed'
    monkeypatch.setattr(store, '_sync_one', sync_one)
    store.sync(date(2026, 11, 3), current_only=True)
    assert seen == []
    store.sync(date(2026, 11, 3))
    assert seen == ['missing']
    assert store.path('present').read_bytes() == b'old'


def test_etag_304_nao_le_corpo_e_etag_novo_publica(store_factory, resource, http_mock):
    store = store_factory([resource()])
    http_mock.queue(CCEE, headers={'ETag': 'v1', 'Last-Modified': 'Mon, 02 Nov 2026 12:00:00 GMT'})
    assert store.sync(date(2026, 11, 3)) == []
    previous = store.path('pld')
    unchanged = http_mock.queue(status=304)
    assert store.sync(date(2026, 11, 3)) == []
    assert http_mock.calls[-1][1]['headers']['If-None-Match'] == 'v1'
    assert unchanged.stream_reads == 0
    assert store.path('pld') == previous
    http_mock.queue(CCEE.replace(b';200', b';250'), headers={'ETag': 'v2'})
    assert store.sync(date(2026, 11, 3)) == []
    assert store.path('pld') != previous
    assert b';250' in store.path('pld').read_bytes()


@pytest.mark.parametrize('body', [b'wrong;schema\n1;2\n', CCEE.replace(b';100', b';inf'),
    CCEE.replace(b'202611', b'202610'), CCEE.replace(b';1;100', b';32;100'),
    CCEE + b'202611;SUL;1;999\n'])
def test_download_invalido_preserva_geracao_ativa(body, store_factory, resource, http_mock):
    store = store_factory([resource()])
    http_mock.queue(CCEE)
    assert store.sync(date(2026, 11, 3)) == []
    before = store.path('pld')
    snapshot = [path.read_bytes() for path in (before, before.with_suffix('.daily.sqlite'), before.with_suffix('.json'))]
    http_mock.queue(body)
    assert len(store.sync(date(2026, 11, 3))) == 1
    assert store.path('pld') == before
    assert [path.read_bytes() for path in (before, before.with_suffix('.daily.sqlite'), before.with_suffix('.json'))] == snapshot


@pytest.mark.parametrize('stage', ['metadata', 'fsync', 'directory', 'pointer'])
def test_falha_publicacao_preserva_csv_indice_metadados(stage, store_factory, resource, http_mock, monkeypatch):
    store = store_factory([resource()])
    http_mock.queue(CCEE)
    assert store.sync(date(2026, 11, 3)) == []
    previous = store.path('pld')
    files = [previous, previous.with_suffix('.json'), previous.with_suffix('.daily.sqlite'), store.cache / 'pld.active.json']
    snapshot = [path.read_bytes() for path in files]
    old_json, old_replace, old_sync = generations.atomic_json, generations.os.replace, generations.os.fsync
    def atomic(path, value):
        if (stage == 'metadata' and Path(path).parent.name.startswith('.pending-')) or (stage == 'pointer' and str(path).endswith('.active.json')):
            raise OSError('injected publication failure')
        return old_json(path, value)
    def replace(source, target):
        if stage == 'directory' and Path(source).is_dir() and Path(source).name.startswith('.pending-'):
            raise OSError('injected rename failure')
        return old_replace(source, target)
    def fsync(fd):
        # Restrict failure to candidate CSV, after download/index preparation.
        if stage == 'fsync' and '/.pending-' in os.readlink(f'/proc/self/fd/{fd}') and os.readlink(f'/proc/self/fd/{fd}').endswith('.csv'):
            raise OSError('injected fsync failure')
        return old_sync(fd)
    monkeypatch.setattr(generations, 'atomic_json', atomic)
    monkeypatch.setattr(generations.os, 'replace', replace)
    monkeypatch.setattr(generations.os, 'fsync', fsync)
    http_mock.queue(CCEE.replace(b';200', b';250'))
    assert len(store.sync(date(2026, 11, 3))) == 1
    assert store.path('pld') == previous
    assert [path.read_bytes() for path in files] == snapshot
    generations.recover(store.cache)
    assert not list((store.cache / '.generations').glob('*/.pending-*'))
    assert previous.is_file()


def test_recuperacao_remove_so_geracoes_incompletas(tmp_path):
    pending = generations.prepare(tmp_path, 'pld'); pending.write_bytes(b'partial')
    complete = pending.parent.parent / 'completed'; complete.mkdir(); (complete / 'keep').write_bytes(b'complete')
    generations.recover(tmp_path)
    assert not pending.parent.exists()
    assert (complete / 'keep').read_bytes() == b'complete'


@pytest.mark.parametrize('counts,status', [({'installed': 1}, 'Atualizado'), ({'unchanged': 1}, 'Sem mudanças'),
                                         ({'failed': 1}, 'Erro'), ({'installed': 1, 'failed': 1}, 'Aviso')])
def test_situacao_separa_fontes_status_e_horarios(counts, status, store_factory, resource, http_mock):
    store = store_factory([resource()])
    http_mock.queue(CCEE)
    assert store.sync(date(2026, 11, 3)) == []
    Diagnostics(store.cache).source('CCEE', counts, operation='test', scope='current')
    ccee, ons = health_report(store)
    assert ccee['status'] == status
    assert ccee['last_check'] == '03/11/2026 às 12:35'
    assert ccee['last_update'] == '03/11/2026 às 12:35'
    assert ccee['period'] == '01/11/2026 a 02/11/2026'
    assert ons['last_check'] == 'Ainda não registrado'
    assert ons['period'] == 'Ainda não confirmado'
    assert all(value is not None and value != '' for value in ons.values())
    assert all('None' not in str(value) for value in ons.values())


def test_situacao_registra_valor_ausente_e_duplicata_equivalente(store_factory, resource, http_mock):
    missing = (b'din_instante;id_ons;nom_usina;nom_subsistema;nom_tipousina;nom_tipocombustivel;val_geracao\n'
               b'2026-11-01 00:00:00;A;Usina;NORTE;HIDRO;\xc3\x81gua;\n'
               b'2026-11-01 01:00:00;A;Usina;NORTE;HIDRO;\xc3\x81gua;100\n')
    duplicate = (b'din_instante;id_ons;nom_usina;nom_subsistema;nom_tipousina;nom_tipocombustivel;val_geracao\n'
                 b'2026-11-01 00:00:00;A;Usina;NORTE;HIDRO;\xc3\x81gua;100\n'
                 b'2026-11-01 00:00:00;A;Usina;NORTE;HIDRO;\xc3\x81gua;100\n')
    store = store_factory([resource('ons', 'ONS')])
    http_mock.queue(missing)
    assert store.sync(date(2026, 11, 3)) == []
    assert any('valor ausente' in problem for problem in health_report(store)[1]['problems'])
    http_mock.queue(duplicate)
    assert store.sync(date(2026, 11, 3)) == []
    assert any('repetido' in problem for problem in health_report(store)[1]['problems'])
