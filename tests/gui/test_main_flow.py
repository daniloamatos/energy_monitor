"""Headless event-loop contracts for the main window's alert summary cap."""
from collections import deque
from datetime import date
from unittest.mock import Mock

import pandas as pd

import pytest

from energy_monitor import gui
from energy_monitor.preview import ALERT_COLUMN, COLUMNS, AlertCollection


class TableWidget:
    columns = tuple(f'c{index}' for index in range(8))

    def __getitem__(self, key):
        return self.columns if key == 'columns' else (0, 1, 2, 3, 5, 7)

    def column(self, column, option=None, **kwargs):
        return 100

    def winfo_width(self):
        return 1000

    def configure(self, **kwargs):
        pass

    def heading(self, *args, **kwargs):
        pass


def test_tela_principal_mostra_no_maximo_500_resumos_de_alerta(store_factory, query_factory, monkeypatch):
    # 501 rows with a price above the limit produce 501 alerts. The complete
    # collection remains available to the exporter and to the alert window.
    frame = pd.DataFrame(
        [[date(2026, 11, 1), 'CCEE', 'Sul', 10.0, None, None] for _ in range(501)],
        columns=COLUMNS)
    frame[ALERT_COLUMN] = 1
    query = query_factory(price_limit=5, change_limit=None)
    alerts = AlertCollection(frame, query)
    assert len(alerts) == 501

    store = store_factory()
    store.load = Mock(return_value=(frame, alerts))
    elements = {}
    window = Mock()
    window.__getitem__ = Mock(side_effect=lambda key: elements.setdefault(key, Mock()))
    elements['-TABLE-'] = Mock()
    elements['-TABLE-'].Widget = TableWidget()
    elements['-SOURCE-'] = Mock()
    elements['-SOURCE-'].get.return_value = 'CCEE'
    window.alert_rows = Mock()
    window.close = Mock()
    events = deque([('-UPDATE-', {'-START-': '01/11/2026', '-END-': '01/11/2026',
                                  '-SOURCE-': 'CCEE', '-LOCATION-': 'Todos',
                                  '-PRICE-': '5', '-CHANGE-': ''})])

    def write_event(event, value):
        if event != '-STARTUP-SYNC-':
            events.append((event, {event: value}))

    def perform(function, event):
        write_event(event, function())

    def read(timeout=None):
        return events.popleft() if events else (gui.sg.WIN_CLOSED, {})

    window.write_event_value.side_effect = write_event
    window.perform_long_operation.side_effect = perform
    window.read.side_effect = read
    owned = Mock(closing=False)
    monkeypatch.setattr(gui, 'SourceStore', lambda *args, **kwargs: store)
    monkeypatch.setattr(gui, 'create_window', lambda: window)
    monkeypatch.setattr(gui, 'OwnedWindows', lambda root: owned)
    monkeypatch.setattr(gui, 'Schedule', lambda settings: Mock(due=lambda: False))

    gui.run(store.config, store.cache)

    alert_text = elements['-ALERTS-'].update.call_args.args[0]
    assert alert_text.count('01/11/2026 · Sul:') == 500
    assert 'Exibindo 500 de 501 alertas' in alert_text
    assert elements['-ALERT-COUNT-'].update.call_args.args[0].startswith('ATENÇÃO: 501 alertas')
    assert len(alerts) == 501
    window.close.assert_called_once()


def test_tabela_pagina_por_1000_salta_dez_e_rejeita_pagina_direta_invalida(store_factory, query_factory, monkeypatch):
    frame = pd.DataFrame(
        [[date(2026, 11, 1), 'CCEE', 'Sul', float(index), None, None] for index in range(2501)],
        columns=COLUMNS)
    frame[ALERT_COLUMN] = 0
    query = query_factory(price_limit=None, change_limit=None)
    alerts = AlertCollection(frame, query)
    store = store_factory()
    store.load = Mock(return_value=(frame, alerts))
    elements = {}
    window = Mock()
    window.__getitem__ = Mock(side_effect=lambda key: elements.setdefault(key, Mock()))
    elements['-TABLE-'] = Mock()
    elements['-TABLE-'].Widget = TableWidget()
    elements['-SOURCE-'] = Mock()
    elements['-SOURCE-'].get.return_value = 'CCEE'
    window.alert_rows = Mock()
    window.close = Mock()
    events = deque([('-UPDATE-', {'-START-': '01/11/2026', '-END-': '01/11/2026',
                                  '-SOURCE-': 'CCEE', '-LOCATION-': 'Todos',
                                  '-PRICE-': '', '-CHANGE-': ''})])
    page_events = deque([
        ('-PAGE-FWD10-', {'-PAGE-JUMP-': '1'}),
        ('-PAGE-FIRST-', {'-PAGE-JUMP-': '1'}),
        ('-PAGE-GO-', {'-PAGE-JUMP-': '3'}),
        ('-PAGE-GO-', {'-PAGE-JUMP-': '0'}),
        ('-PAGE-GO-', {'-PAGE-JUMP-': 'abc'}),
        ('-PAGE-LAST-', {'-PAGE-JUMP-': '1'}),
    ])
    info, table_sizes = [], []
    elements['-PAGE-INFO-'] = Mock()
    elements['-PAGE-INFO-'].update.side_effect = lambda value: info.append(value)
    elements['-TABLE-'].update.side_effect = lambda **kwargs: table_sizes.append(len(kwargs['values']))

    def write_event(event, value):
        if event == '-STARTUP-SYNC-':
            return
        events.append((event, {event: value}))
        if event == '-LOADED-':
            events.extend(page_events)

    def perform(function, event):
        write_event(event, function())

    def read(timeout=None):
        return events.popleft() if events else (gui.sg.WIN_CLOSED, {})

    window.write_event_value.side_effect = write_event
    window.perform_long_operation.side_effect = perform
    window.read.side_effect = read
    owned = Mock(closing=False)
    monkeypatch.setattr(gui, 'SourceStore', lambda *args, **kwargs: store)
    monkeypatch.setattr(gui, 'create_window', lambda: window)
    monkeypatch.setattr(gui, 'OwnedWindows', lambda root: owned)
    monkeypatch.setattr(gui, 'Schedule', lambda settings: Mock(due=lambda: False))

    gui.run(store.config, store.cache)

    assert info == ['Página 1 de 3', 'Página 3 de 3', 'Página 1 de 3',
                    'Página 3 de 3', 'Página 3 de 3']
    assert table_sizes == [1000, 501, 1000, 501, 501]
    assert 'Informe uma página de 1 a 3' in elements['-STATUS-'].update.call_args.args[0]
    window.close.assert_called_once()


def test_busca_com_arquivos_ausentes_aciona_prompt_e_baixa_selecionados(store_factory, monkeypatch):
    store = store_factory()
    missing_mock = Mock(id="GERACAO_USINA-2_2000", source="ONS", start=date(2000, 1, 1), end=date(2000, 12, 31))
    store.missing_resources = Mock(return_value=[missing_mock])

    elements = {}
    window = Mock()
    window.__getitem__ = Mock(side_effect=lambda key: elements.setdefault(key, Mock()))
    elements['-TABLE-'] = Mock()
    elements['-TABLE-'].Widget = TableWidget()
    elements['-SOURCE-'] = Mock()
    elements['-SOURCE-'].get.return_value = 'ONS'
    window.alert_rows = Mock()
    window.close = Mock()

    values_dict = {'-START-': '01/01/2000', '-END-': '31/12/2000',
                   '-SOURCE-': 'ONS', '-LOCATION-': 'Todos',
                   '-SUBMARKET-': 'Todos', '-STATE-': 'Todos',
                   '-PLANT-TYPE-': 'Todos', '-FUEL-': 'Todos',
                   '-PRICE-': '', '-CHANGE-': ''}
    events = deque([
        ('-UPDATE-', values_dict)
    ])

    def write_event(event, value):
        if event != '-STARTUP-SYNC-':
            events.append((event, {event: value, **values_dict}))

    def perform(function, event):
        write_event(event, function())

    def read(timeout=None):
        return events.popleft() if events else (gui.sg.WIN_CLOSED, {})

    window.write_event_value.side_effect = write_event
    window.perform_long_operation.side_effect = perform
    window.read.side_effect = read

    owned = Mock(closing=False)
    owned.prompt_download = Mock(return_value=["GERACAO_USINA-2_2000"])

    updater = Mock()
    updater.run.return_value = gui.UpdateResult("success", [])

    monkeypatch.setattr(gui, 'SourceStore', lambda *args, **kwargs: store)
    monkeypatch.setattr(gui, 'UpdateExecutor', lambda store: updater)
    monkeypatch.setattr(gui, 'create_window', lambda: window)
    monkeypatch.setattr(gui, 'OwnedWindows', lambda root: owned)
    monkeypatch.setattr(gui, 'Schedule', lambda settings: Mock(due=lambda: False))

    gui.run(store.config, store.cache)

    owned.prompt_download.assert_called_with([missing_mock], title="Arquivos ausentes para a busca")
    updater.run.assert_called()
    assert updater.run.call_args.kwargs['missing_ids'] == ["GERACAO_USINA-2_2000"]
    assert any(call[0][0] == "-UPDATE-" for call in window.write_event_value.call_args_list)
    window.close.assert_called_once()
