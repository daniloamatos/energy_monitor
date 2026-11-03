"""Hermetic pytest fixtures: no network, fixed dates, disposable filesystem."""
from collections import deque
from contextlib import contextmanager
from datetime import date, datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import time
from types import SimpleNamespace

import pandas as pd
import pytest
import requests
from freezegun import freeze_time

ROOT = Path(__file__).resolve().parents[1]
REAL_MONOTONIC = time.monotonic
REAL_PERF_COUNTER = time.perf_counter


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    home = tmp_path / 'home'; home.mkdir()
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('USERPROFILE', str(home))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home / 'config'))
    monkeypatch.setenv('XDG_CACHE_HOME', str(home / 'cache'))
    monkeypatch.setenv('XDG_DATA_HOME', str(home / 'data'))
    monkeypatch.setenv('TMPDIR', str(tmp_path))
    monkeypatch.setenv('HYPOTHESIS_STORAGE_DIRECTORY', str(tmp_path / 'hypothesis'))
    monkeypatch.delenv('DISPLAY', raising=False)
    monkeypatch.delenv('WAYLAND_DISPLAY', raising=False)
    def no_network(*args, **kwargs):
        raise AssertionError('Teste tentou usar rede real; injete http_mock.')
    monkeypatch.setattr(requests.sessions.Session, 'request', no_network)
    monkeypatch.setattr(socket.socket, 'connect', no_network)
    monkeypatch.setattr(socket, 'create_connection', no_network)
    protected = (str(ROOT / 'data'), str(ROOT / 'config' / 'sources.json'))
    def guard(path):
        if isinstance(path, (str, bytes, os.PathLike)):
            name = os.path.abspath(os.fsdecode(path))
            assert not any(name == item or name.startswith(item + os.sep) for item in protected), f'Acesso a dados reais: {name}'
    original_open, original_stat = Path.open, Path.stat
    def opened(path, *args, **kwargs):
        guard(path)
        return original_open(path, *args, **kwargs)
    def stat(path, *args, **kwargs):
        guard(path)
        return original_stat(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'open', opened)
    monkeypatch.setattr(Path, 'stat', stat)
    with freeze_time('2026-11-03 15:35:00+00:00'):
        monkeypatch.setattr(time, 'monotonic', REAL_MONOTONIC)
        monkeypatch.setattr(time, 'perf_counter', REAL_PERF_COUNTER)
        yield


@pytest.fixture
def clock():
    class Clock:
        current = datetime(2026, 11, 3, 15, 35, tzinfo=timezone.utc)
        def __call__(self): return self.current
        def set(self, value): self.current = datetime.fromisoformat(value)
    return Clock()


class Response:
    def __init__(self, body=b'', *, status=200, headers=None, payload=None, error=None):
        self.body, self.status_code = body, status
        self.headers, self.payload, self.error = headers or {}, payload, error
        self.stream_reads = 0
        self.closed = False
    def raise_for_status(self):
        if self.error: raise self.error
        if self.status_code >= 400:
            error = requests.HTTPError(f'HTTP {self.status_code}')
            error.response = self
            raise error
    def json(self):
        if isinstance(self.payload, Exception): raise self.payload
        return self.payload
    def iter_content(self, chunk_size):
        self.stream_reads += 1
        for offset in range(0, len(self.body), min(chunk_size, 32)):
            yield self.body[offset:offset + min(chunk_size, 32)]
    def close(self): self.closed = True
    def __enter__(self): return self
    def __exit__(self, *args): self.close()


@pytest.fixture
def http_mock():
    class HTTP:
        def __init__(self): self.responses, self.calls = deque(), []
        def queue(self, body=b'', **kwargs):
            response = Response(body, **kwargs)
            self.responses.append(response)
            return response
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            assert self.responses, 'HTTP simulado recebeu requisição não prevista'
            response = self.responses.popleft()
            if isinstance(response, Exception): raise response
            return response
    return HTTP()


@pytest.fixture
def resource():
    def make(identifier='pld', source='CCEE', start='2026-11-01', end='2026-11-30', mutable=True, **kwargs):
        return dict(id=identifier, source=source, start=start, end=end, mutable=mutable,
                    url=f'https://example.invalid/{identifier}.csv', **kwargs)
    return make


@pytest.fixture
def csv_factory(tmp_path):
    def make(source, rows, name='sample.csv'):
        # Blank measurements are intentional; zero is a published observation.
        if source == 'CCEE':
            columns = ['MES_REFERENCIA', 'SUBMERCADO', 'DIA', 'PLD_HORA']
        else:
            columns = ['din_instante', 'id_ons', 'nom_usina', 'nom_subsistema', 'nom_tipousina', 'nom_tipocombustivel', 'val_geracao']
        frame = pd.DataFrame(rows, columns=columns) if not rows or not isinstance(rows[0], dict) else pd.DataFrame(rows)
        path = tmp_path / name
        frame.to_csv(path, sep=';', index=False, encoding='utf-8')
        return path
    return make


@pytest.fixture
def store_factory(tmp_path, http_mock):
    from energy_monitor.data_sources import SourceStore
    index = 0
    def make(resources=(), **settings):
        nonlocal index
        index += 1
        root = tmp_path / f'installation {index}'; root.mkdir()
        cache = root / 'cache'; cache.mkdir()
        config = root / 'sources.json'
        config.write_text(json.dumps(dict(timezone='America/Sao_Paulo', update_times=['12:30','20:30'],
                                         discovery_enabled=False, resources=list(resources)) | settings), encoding='utf-8')
        return SourceStore(config, cache, http_mock)
    return make


@pytest.fixture
def query_factory():
    from energy_monitor.preview import Query
    def make(source='CCEE', start=date(2026,11,1), end=date(2026,11,3), location='Todos', price_limit=None, change_limit=10, **kwargs):
        return Query(source,start,end,location,price_limit,change_limit,**kwargs)
    return make


@pytest.fixture
def frame_factory():
    from energy_monitor.preview import COLUMNS, ID_COLUMN
    def make(rows, *, ids=None):
        frame = pd.DataFrame(rows, columns=COLUMNS)
        if ids is not None: frame[ID_COLUMN] = ids
        return frame
    return make


@pytest.fixture
def tk_root(tmp_path, monkeypatch):
    if not os.environ.get('ENERGY_MONITOR_GUI_TESTS') == '1':
        pytest.skip('Use ENERGY_MONITOR_GUI_TESTS=1 com xvfb-run para testes de widgets.')
    # DISPLAY is removed by sandbox; opt-in GUI receives only the explicit session value.
    display = os.environ.get('ENERGY_MONITOR_TEST_DISPLAY', ':0')
    monkeypatch.setenv('DISPLAY', display)
    import tkinter as tk
    try:
        root = tk.Tk()
    except tk.TclError as error:
        pytest.skip(f'Display indisponível: {error}')
    root.geometry('1000x800'); root.update()
    yield root
    try: root.destroy()
    except tk.TclError: pass


def pytest_collection_modifyitems(items):
    for item in items:
        path = str(item.path)
        if '/unit/' in path: item.add_marker(pytest.mark.unit)
        elif '/integration/' in path: item.add_marker(pytest.mark.integration)
        elif '/gui/' in path: item.add_marker(pytest.mark.gui)
