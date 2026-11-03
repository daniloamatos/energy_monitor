"""Contracts 3, 8 and 20: pure navigation plus opt-in Tk ownership/layout."""
from datetime import date
from types import SimpleNamespace
import tkinter as tk
from unittest.mock import Mock

import pytest

from energy_monitor.alert_window import AlertWindow
from energy_monitor.preview import AlertCollection
from energy_monitor import windows


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def button(widget, label):
    return next(child for child in descendants(widget)
                if isinstance(child, (tk.Button, tk.Radiobutton)) and child.cget('text') == label)


@pytest.mark.parametrize('count,pages', [(0, 1), (1, 1), (100, 1), (101, 2), (200, 2)])
def test_alertas_paginacao_limites_sem_display(count, pages):
    view = AlertWindow.__new__(AlertWindow)
    view.alerts = [None] * count
    view.page = 0
    view.render_page = Mock()
    assert view.pages == pages
    view.move_page(10)
    assert view.page == pages - 1
    view.move_page(-10)
    assert view.page == 0
    assert view.render_page.call_count == 2


@pytest.mark.parametrize('value,expected', [('1', 0), ('2', 1), ('0', None), ('3', None), ('abc', None), ('', None)])
def test_alertas_pagina_direta_valida_e_rejeita_entrada_invalida(value, expected):
    view = AlertWindow.__new__(AlertWindow)
    view.alerts, view.page = [None] * 101, 0
    view.page_label = Mock()
    view.render_page = Mock()
    view.go_to(value)
    assert view.page == (expected if expected is not None else 0)
    if expected is None:
        view.page_label.configure.assert_called_once_with(text='Página inválida (1–2)')
        view.render_page.assert_not_called()
    else:
        view.render_page.assert_called_once_with()


@pytest.mark.parametrize('monitors,expected', [
    ([SimpleNamespace(x=0, y=0, width=1920, height=1080, is_primary=True)], ((320, 80), (1280, 920))),
    ([SimpleNamespace(x=-1920, y=0, width=1920, height=1080, is_primary=True),
      SimpleNamespace(x=0, y=0, width=1280, height=720, is_primary=False)], ((-1600, 80), (1280, 920))),
    ([SimpleNamespace(x=0, y=0, width=800, height=600, is_primary=True)], ((40, 50), (720, 500))),
])
def test_janela_centraliza_no_monitor_principal_simulado(monkeypatch, monitors, expected):
    monkeypatch.setattr(windows, 'get_monitors', lambda: monitors)
    assert windows.initial_window_geometry() == expected


def test_selecao_arquivo_pertence_a_janela_principal(monkeypatch, tmp_path):
    root = Mock()
    owner = windows.OwnedWindows(root)
    choose = Mock(return_value=str(tmp_path / 'saida.xlsx'))
    monkeypatch.setattr(windows.filedialog, 'asksaveasfilename', choose)
    assert owner.choose_file('saida.xlsx') == str(tmp_path / 'saida.xlsx')
    assert choose.call_args.kwargs['parent'] is root
    assert choose.call_args.kwargs['confirmoverwrite'] is True


@pytest.mark.gui
def test_calendario_seleciona_dia_aplica_e_fecha(tk_root):
    owner = windows.OwnedWindows(tk_root)
    selected = []
    calendar = owner.choose_date('01/02/2024', selected.append)
    assert calendar.master is tk_root
    button(calendar, '29').invoke()
    tk_root.update()
    assert selected == ['29/02/2024']
    assert owner.calendar is None
    assert not calendar.winfo_exists()


@pytest.mark.gui
def test_calendario_esc_cancela_sem_modificar_campo(tk_root):
    owner = windows.OwnedWindows(tk_root)
    selected = []
    calendar = owner.choose_date('01/02/2024', selected.append)
    tk_root.update()
    calendar.event_generate('<Escape>')
    tk_root.update()
    assert selected == []
    assert owner.calendar is None


@pytest.mark.gui
def test_calendario_navega_virada_de_ano_e_escolhe_mes_ano(tk_root):
    owner = windows.OwnedWindows(tk_root)
    selected = []
    calendar = owner.choose_date('01/12/2023', selected.append)
    button(calendar, '›').invoke()
    assert button(calendar, 'Janeiro 2024')
    button(calendar, 'Janeiro 2024').invoke()
    year = next(widget for widget in descendants(calendar) if isinstance(widget, tk.Spinbox))
    year.delete(0, 'end')
    year.insert(0, '2024')
    button(calendar, 'Fevereiro').invoke()
    button(calendar, 'Aplicar').invoke()
    button(calendar, '29').invoke()
    tk_root.update()
    assert selected == ['29/02/2024']


@pytest.mark.gui
def test_fechar_principal_encerra_calendario_e_alertas(tk_root, frame_factory, query_factory, clock):
    owner = windows.OwnedWindows(tk_root)
    calendar = owner.choose_date('01/11/2026', lambda value: None)
    frame = frame_factory([])
    owner.show_alerts(frame, query_factory(), clock())
    alert_window, calendar = owner.alerts_view.root, calendar
    owner.close()
    # Destroying the root also destroys the Tcl interpreter, so a query on any
    # owned widget must fail. That is the observable proof that the children
    # did not outlive the main window.
    for child in (alert_window, calendar):
        with pytest.raises(tk.TclError):
            child.winfo_exists()
    assert owner.closing


@pytest.mark.gui
def test_alertas_detalhes_legiveis_e_botoes_acessiveis_no_tamanho_minimo(tk_root, frame_factory, query_factory, clock):
    frame = frame_factory([(date(2026, 11, 1), 'CCEE', 'Sul', 100., None, None),
                           (date(2026, 11, 2), 'CCEE', 'Sul', 120., None, 20.)])
    view = AlertWindow(tk_root, frame, query_factory(price_limit=110, change_limit=10), clock())
    view.root.geometry('560x480')
    tk_root.update()
    content = view.text.get('1.0', 'end')
    for expected in ['Sul', 'CCEE', '02/11/2026', '120', '110', '100', '01/11/2026', '10', '%', 'R$']:
        assert expected in content
    assert str(view.root.transient()) == str(tk_root)
    assert tuple(view.root.resizable()) == (1, 1)
    controls = [widget for widget in descendants(view.root) if isinstance(widget, tk.Button)]
    for control in controls:
        assert control.winfo_width() >= 30
        assert 0 <= control.winfo_rootx() - view.root.winfo_rootx()
        assert control.winfo_rootx() + control.winfo_width() <= view.root.winfo_rootx() + view.root.winfo_width()
        assert control.winfo_rooty() + control.winfo_height() <= view.root.winfo_rooty() + view.root.winfo_height()
    view.root.destroy()


@pytest.mark.gui
def test_painel_de_situacao_pertence_a_principal_e_cabe_no_tamanho_minimo(tk_root):
    from energy_monitor.data_health import show_health

    report = [{'source': 'CCEE', 'status': 'Aviso', 'last_check': 'Ainda não registrado',
               'last_update': 'Ainda não registrado', 'period': 'Ainda não confirmado',
               'problems': ['Ainda não há uma verificação registrada para esta fonte.']},
              {'source': 'ONS', 'status': 'Sem mudanças', 'last_check': '03/11/2026 às 12:35',
               'last_update': '03/11/2026 às 12:35', 'period': '01/11/2026 a 02/11/2026',
               'problems': ['Nenhum problema identificado nas verificações disponíveis.']}]
    window = show_health(tk_root, report)
    window.geometry('520x400')
    tk_root.update()
    assert str(window.transient()) == str(tk_root)
    assert tuple(window.minsize()) == (520, 400)
    content = next(widget for widget in descendants(window) if widget.winfo_class() == 'Text')
    text = content.get('1.0', 'end')
    for expected in ('CCEE', 'ONS', 'Aviso', 'Sem mudanças', 'Ainda não registrado', 'Ainda não confirmado'):
        assert expected in text
    window.destroy()
