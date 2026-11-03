"""CLI runs headlessly; systemd is entirely simulated, including subprocesses."""
import json
import sys

import pytest

from energy_monitor.__main__ import main
from energy_monitor import scheduling
from energy_monitor.scheduling import SystemdScheduler
from energy_monitor.updater import UpdateExecutor, UpdateResult
from energy_monitor.cancellation import OperationCancelled


def invoke(monkeypatch, store, *arguments):
    monkeypatch.setattr(sys, 'argv', ['energy_monitor', '--config', str(store.config), '--cache', str(store.cache), *arguments])
    with pytest.raises(SystemExit) as result:
        main()
    return result.value.code


@pytest.mark.parametrize('arguments,text', [
    (['--validate-only'], 'Configuração válida'),
    (['diagnostics'], 'resources'),
    (['sources', 'status'], 'providers'),
    (['--update-only'], 'Verificação'),
])
def test_cli_comandos_locais_sem_rede(arguments, text, store_factory, monkeypatch, capsys, http_mock):
    store = store_factory()
    assert invoke(monkeypatch, store, *arguments) == 0
    assert text in capsys.readouterr().out
    assert http_mock.calls == []


@pytest.mark.parametrize('status,code', [('success', 0), ('busy', 2), ('deduplicated', 2), ('failed', 1)])
def test_cli_repassa_codigos_executor(status, code, store_factory, monkeypatch):
    def run(self, **kwargs):
        assert self.store.config == store.config
        assert self.store.cache == store.cache
        assert kwargs['origin'] == 'external'
        assert kwargs['scheduled'] is True
        return UpdateResult(status, [])
    store = store_factory()
    monkeypatch.setattr(UpdateExecutor, 'run', run)
    assert invoke(monkeypatch, store, '--update-only', '--scheduled') == code


@pytest.mark.parametrize('error,code,text', [(OperationCancelled('cancel'), 130, 'cancelada'),
                                           (ValueError('invalid config'), 1, 'Erro:')])
def test_cli_cancelamento_e_erro_tem_saida_explicita(error, code, text, store_factory, monkeypatch, capsys):
    def run(self, **kwargs): raise error
    monkeypatch.setattr(UpdateExecutor, 'run', run)
    assert invoke(monkeypatch, store_factory(), '--update-only') == code
    assert text in capsys.readouterr().out


def test_cli_scheduled_exige_update_only(store_factory, monkeypatch, capsys):
    assert invoke(monkeypatch, store_factory(), '--scheduled') == 2
    assert '--scheduled exige --update-only' in capsys.readouterr().err


@pytest.fixture
def fake_systemd(monkeypatch):
    calls = []
    state = {'enabled': False}
    monkeypatch.setattr(scheduling.sys, 'platform', 'linux')
    monkeypatch.setattr(scheduling.shutil, 'which', lambda _: '/mock/systemctl')
    def command(arguments, *, cwd=None):
        calls.append((arguments, cwd))
        if arguments[0] != 'systemctl':
            assert '--validate-only' in arguments
            return 'Configuração válida'
        if 'enable' in arguments: state['enabled'] = True
        if 'disable' in arguments: state['enabled'] = False
        if 'show' in arguments:
            unit = arguments[3]
            target = state['scheduler'].directory / unit
            return '\n'.join([
                'LoadState=' + ('loaded' if target.is_file() else 'not-found'),
                'ActiveState=' + ('active' if state['enabled'] else 'inactive'),
                'UnitFileState=' + ('enabled' if state['enabled'] else 'disabled'),
                'FragmentPath=' + str(target), 'NeedDaemonReload=no',
                'NextElapseUSecRealtime=' + ('Tue 2026-11-03 15:36:00 UTC' if state['enabled'] else '')])
        return ''
    monkeypatch.setattr(scheduling, '_command', command)
    return state, calls


def test_systemd_instalacao_idempotente_status_remocao_so_em_diretorio_temporario(store_factory, fake_systemd, tmp_path, monkeypatch):
    state, calls = fake_systemd
    python = str(tmp_path / 'venv space' / 'bin' / 'python')
    monkeypatch.setattr(scheduling.sys, 'executable', python)
    store = store_factory()
    sentinel = store.cache / 'dados.csv'; sentinel.write_bytes(b'keep data')
    scheduler = SystemdScheduler(store); state['scheduler'] = scheduler
    assert scheduler.status()['enabled'] is False
    assert scheduler.install()['enabled'] is True
    first = {path.name: path.read_bytes() for path in scheduler.directory.iterdir()}
    assert scheduler.install()['enabled'] is True
    assert {path.name: path.read_bytes() for path in scheduler.directory.iterdir()} == first
    definition = json.loads(scheduler.record.read_text())
    assert definition['python'] == python
    assert definition['config'] == str(store.config.resolve())
    assert definition['cache'] == str(store.cache.resolve())
    assert definition['timezone'] == 'America/Sao_Paulo'
    assert definition['update_times'] == ['12:30', '20:30']
    assert f'"{python}"' in definition['service']
    assert '"--scheduled"' in definition['service']
    assert 'OnCalendar=*-*-* *:*:00 UTC' in definition['timer']
    assert scheduler.remove() == {'enabled': False}
    assert scheduler.remove() == {'enabled': False}
    assert not scheduler.record.exists()
    assert list(scheduler.directory.iterdir()) == []
    assert sentinel.read_bytes() == b'keep data'
    assert any(arguments[0] == python and cwd == store.cache for arguments, cwd in calls)


@pytest.mark.parametrize('platform,binary', [('win32', '/mock/systemctl'), ('linux', None)])
def test_sistema_sem_systemd_informa_limitacao(platform, binary, store_factory, monkeypatch):
    monkeypatch.setattr(scheduling.sys, 'platform', platform)
    monkeypatch.setattr(scheduling.shutil, 'which', lambda _: binary)
    with pytest.raises(RuntimeError, match='systemd --user no Linux'):
        SystemdScheduler(store_factory()).available()


@pytest.mark.parametrize('action,enabled,expected', [('status', False, 2), ('check', False, 1),
                                                    ('status', True, 0), ('check', True, 0)])
def test_cli_schedule_status_check_codigos(action, enabled, expected, store_factory, monkeypatch, capsys):
    monkeypatch.setattr(SystemdScheduler, 'status', lambda self: {'enabled': enabled})
    assert invoke(monkeypatch, store_factory(), 'schedule', action) == expected
    assert json.loads(capsys.readouterr().out)['enabled'] is enabled


def test_cli_schedule_install_e_remove_em_diretorio_temporario(store_factory, fake_systemd, monkeypatch, capsys):
    state, calls = fake_systemd
    store = store_factory()
    scheduler = SystemdScheduler(store)
    state['scheduler'] = scheduler
    assert invoke(monkeypatch, store, 'schedule', 'install') == 0
    assert scheduler.record.exists()
    assert json.loads(capsys.readouterr().out)['enabled'] is True
    assert invoke(monkeypatch, store, 'schedule', 'remove') == 0
    assert not scheduler.record.exists()
    assert json.loads(capsys.readouterr().out)['enabled'] is False
    assert any(arguments[0] == 'systemctl' and 'enable' in arguments for arguments, _ in calls)


def test_cli_sources_discover_dry_run_com_catalogo_simulado(store_factory, http_mock, monkeypatch, capsys):
    store = store_factory(discovery={'ONS': {'enabled': False}})
    monkeypatch.setattr('energy_monitor.data_sources.SourceStore', lambda *args, **kwargs: store)
    payload = {'success': True, 'result': {'name': 'pld_horario', 'resources': [
        {'id': 'official-1', 'name': 'PLD_HORARIO_2026', 'format': 'CSV',
         'url': 'https://example.invalid/PLD_HORARIO_2026.csv'}]}}
    http_mock.queue(payload=payload)
    assert invoke(monkeypatch, store, 'sources', 'discover', '--dry-run') == 0
    data = json.loads(capsys.readouterr().out)
    assert len(data['resources']) == 1
    assert data['resources'][0]['source'] == 'CCEE'
    assert data['resources'][0]['start'] == '2026-01-01'
    assert data['providers']['CCEE']['result'] == 'success'
