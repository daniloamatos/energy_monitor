"""Runtime contracts: real process locks, schedule slots, progress and cancellation."""
from datetime import date, datetime
from pathlib import Path
import subprocess
import sys
import threading
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from energy_monitor.cancellation import OperationCancelled
from energy_monitor.daily_cache import read_daily
from energy_monitor.data_sources import Schedule
from energy_monitor.progress import Progress
from energy_monitor.updater import Ledger, UpdateExecutor

ZONE = ZoneInfo('America/Sao_Paulo')


def test_agenda_so_dispara_no_horario_configurado_e_avanca_para_o_proximo():
    schedule = Schedule({'timezone': 'America/Sao_Paulo', 'update_times': ['12:30', '20:30']})
    assert schedule.due(datetime(2026, 11, 3, 12, 29, tzinfo=ZONE)) is False
    assert schedule.next == datetime(2026, 11, 3, 12, 30, tzinfo=ZONE)
    assert schedule.due(datetime(2026, 11, 3, 12, 30, tzinfo=ZONE)) is True
    assert schedule.next == datetime(2026, 11, 3, 20, 30, tzinfo=ZONE)
    # 15:30 UTC is 12:30 in São Paulo; the configured zone wins over the host zone.
    assert schedule.due(datetime(2026, 11, 4, 15, 30, tzinfo=ZoneInfo('UTC'))) is True


def test_horarios_atendidos_nao_duplicam_e_perdidos_consolidam_em_uma_execucao(store_factory):
    store = store_factory()
    store.sync = Mock(return_value=[])
    now = [datetime(2026, 11, 2, 12, 30, tzinfo=ZONE)]
    executor = UpdateExecutor(store, clock=lambda: now[0])
    assert executor.run(origin='startup', scheduled=True).status == 'success'
    assert store.sync.call_count == 1
    assert executor.run(origin='external', scheduled=True).status == 'deduplicated'
    assert store.sync.call_count == 1
    # Monday 12:30 -> Thursday 21:30 in São Paulo: five slots, one sync.
    now[0] = datetime(2026, 11, 5, 0, 30, tzinfo=ZoneInfo('UTC'))
    assert executor.run(origin='external', scheduled=True).status == 'success'
    assert store.sync.call_count == 2
    details = Ledger(store.cache).latest()
    assert len(details['slots']) == 5
    assert details['slots'][-1] == '2026-11-04T20:30:00-03:00'
    assert executor.run(origin='external', scheduled=True).status == 'deduplicated'


def test_trava_entre_processos_reais_bloqueia_segunda_atualizacao_e_libera_apos_kill(store_factory):
    store = store_factory()
    store.sync = Mock(return_value=[])
    script = """
import sys, time
from datetime import datetime
from zoneinfo import ZoneInfo
from energy_monitor.data_sources import SourceStore
from energy_monitor.updater import UpdateExecutor
store = SourceStore(sys.argv[1], sys.argv[2])
def sync(**kwargs):
    print('locked', flush=True)
    time.sleep(30)
    return []
store.sync = sync
UpdateExecutor(store, clock=lambda: datetime(2026, 11, 3, 12, 30, tzinfo=ZoneInfo('America/Sao_Paulo'))).run()
"""
    child = subprocess.Popen([sys.executable, '-u', '-c', script, str(store.config), str(store.cache)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        ready = []
        reader = threading.Thread(target=lambda: ready.append(child.stdout.readline()), daemon=True)
        reader.start()
        reader.join(timeout=10)
        assert ready == ['locked\n']
        assert UpdateExecutor(store, clock=lambda: datetime(2026, 11, 3, 12, 30, tzinfo=ZONE)).run().status == 'busy'
        store.sync.assert_not_called()
    finally:
        child.kill()
        child.communicate(timeout=10)
    # The OS releases the flock when the process dies; recovery marks the abandoned run.
    assert UpdateExecutor(store, clock=lambda: datetime(2026, 11, 3, 12, 30, tzinfo=ZONE)).run().status == 'success'
    with Ledger(store.cache).connect() as db:
        statuses = [row[0] for row in db.execute('SELECT status FROM runs')]
    assert 'interrupted' in statuses
    assert 'busy' in statuses
    assert 'success' in statuses


def test_progresso_reflete_arquivos_concluidos_e_percentual_nunca_retrocede(store_factory, resource, monkeypatch):
    store = store_factory([resource('a', start='2026-01-01', end='2026-06-30'),
                           resource('b', start='2026-07-01', end='2026-12-31')])

    def sync_one(item, *args, **kwargs):
        target = store.path(item.id)
        target.write_bytes(b'local')
        target.with_suffix('.json').write_text('{}')
        return 'installed'

    monkeypatch.setattr(store, '_sync_one', sync_one)
    messages = []
    assert store.sync(date(2026, 11, 3), progress=messages.append) == []
    progress = [message for message in messages if isinstance(message, Progress)]
    assert progress
    assert [item.percent for item in progress] == sorted(item.percent for item in progress)
    assert progress[-1].percent == 100
    assert any(item.completed == item.total == 2 for item in progress)
    assert any('Processados' in str(item) for item in progress)


def test_cancelamento_cooperativo_na_busca_nao_cria_indice_e_na_atualizacao_libera_trava(
        store_factory, resource, csv_factory, query_factory):
    store = store_factory([resource()])
    store.cache.joinpath('pld.csv').write_bytes(
        csv_factory('CCEE', [['202611', 'SUL', '1', '100']]).read_bytes())
    with pytest.raises(OperationCancelled):
        store.load(query_factory(end=date(2026, 11, 1)), cancelled=lambda: True)
    assert not store.path('pld').with_suffix('.daily.sqlite').exists()
    store.sync = Mock(return_value=[])
    clock = lambda: datetime(2026, 11, 3, 12, 30, tzinfo=ZONE)
    with pytest.raises(OperationCancelled):
        UpdateExecutor(store, clock=clock).run(cancelled=lambda: True)
    assert Ledger(store.cache).latest()['result'] == 'cancelled'
    assert UpdateExecutor(store, clock=clock).run().status == 'success'


def test_duplicatas_diarias_ccee_sem_hora_sao_deduplicadas_e_conflito_impede_indice(
        tmp_path, csv_factory, monkeypatch):
    actual = pd.read_csv
    def chunks(*args, **kwargs):
        kwargs['chunksize'] = 1
        return actual(*args, **kwargs)
    monkeypatch.setattr('energy_monitor.data_sources.pd.read_csv', chunks)
    rows = [['202611', 'SUL', '1', '100'], ['202611', 'SUL', '1', '100']]
    path = csv_factory('CCEE', rows, name='ccee-duplicates.csv')
    daily = read_daily(path, 'CCEE', date(2026, 11, 1), date(2026, 11, 1))
    assert daily['sum'].iloc[0] == pytest.approx(100)
    assert daily['count'].iloc[0] == 1
    conflict = [['202611', 'SUL', '1', '100'], ['202611', 'SUL', '1', '999']]
    path = csv_factory('CCEE', conflict, name='ccee-conflict.csv')
    with pytest.raises(ValueError, match='conflitantes'):
        read_daily(path, 'CCEE', date(2026, 11, 1), date(2026, 11, 1))
    assert not path.with_suffix('.daily.sqlite').exists()


def test_duplicatas_horarias_equivalentes_sao_deduplicadas_e_conflito_impede_indice(tmp_path, csv_factory):
    header = ['2026-11-01 00:00:00', 'A', 'Usina', 'NORTE', 'HIDRO', 'Água', '100']
    second = ['2026-11-01 01:00:00', 'A', 'Usina', 'NORTE', 'HIDRO', 'Água', '200']
    path = csv_factory('ONS', [header, header, second], name='duplicates.csv')
    daily = read_daily(path, 'ONS', date(2026, 11, 1), date(2026, 11, 1))
    assert daily['sum'].iloc[0] == pytest.approx(300)
    assert daily['count'].iloc[0] == 2
    conflict = list(header)
    conflict[-1] = '999'
    path = csv_factory('ONS', [header, conflict], name='conflict.csv')
    with pytest.raises(ValueError, match='conflitantes'):
        read_daily(path, 'ONS', date(2026, 11, 1), date(2026, 11, 1))
    assert not path.with_suffix('.daily.sqlite').exists()
