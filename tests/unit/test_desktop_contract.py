"""Deterministic desktop geometry: resizing and monitor placement without a display."""
from types import SimpleNamespace

import pytest

from energy_monitor.resize import resized_rectangle
from energy_monitor.titlebar import monitor_rectangle
from energy_monitor.windows import primary_monitor


@pytest.mark.parametrize('rectangle,edge,delta,expected', [
    ((100, 100, 400, 300), 'e', (50, 0), (100, 100, 450, 300)),
    ((100, 100, 400, 300), 'w', (50, 0), (150, 100, 350, 300)),
    ((100, 100, 400, 300), 'n', (0, 50), (100, 150, 400, 250)),
    ((100, 100, 400, 300), 's', (0, 50), (100, 100, 400, 350)),
    ((100, 100, 400, 300), 'se', (20, 30), (100, 100, 420, 330)),
    ((100, 100, 400, 300), 'nw', (20, 30), (120, 130, 380, 270)),
])
def test_redimensionar_por_borda_e_canto(rectangle, edge, delta, expected):
    assert resized_rectangle(rectangle, edge, *delta, (10, 10)) == expected


@pytest.mark.parametrize('edge,delta,expected', [
    ('w', (1000, 0), (400, 100, 100, 300)),
    ('n', (0, 1000), (100, 300, 400, 100)),
])
def test_redimensionar_respeita_o_tamanho_minimo(edge, delta, expected):
    # The dragged edge stops at the minimum, so the opposite edge does not move.
    assert resized_rectangle((100, 100, 400, 300), edge, *delta, (100, 100)) == expected


def test_monitor_principal_marcado_vence_a_origem():
    monitors = [SimpleNamespace(x=0, y=0, width=1920, height=1080, is_primary=False),
                SimpleNamespace(x=1920, y=0, width=1280, height=720, is_primary=True)]
    assert primary_monitor(monitors).x == 1920


def test_monitor_principal_sem_marca_usa_o_que_contem_a_origem(monkeypatch):
    # Keep the WSL-to-Windows bridge out of this deterministic geometry test.
    monkeypatch.setattr('energy_monitor.windows.platform.release', lambda: '6.1')
    monitors = [SimpleNamespace(x=-1920, y=0, width=1920, height=1080, is_primary=False),
                SimpleNamespace(x=0, y=0, width=1280, height=720, is_primary=False)]
    assert primary_monitor(monitors).x == 0


def test_maximizar_usa_monitor_de_maior_intersecao_e_desempata_por_distancia():
    monitors = [SimpleNamespace(x=0, y=0, width=1920, height=1080),
                SimpleNamespace(x=1920, y=0, width=1920, height=1080)]
    assert monitor_rectangle((1700, 100, 800, 600), monitors) == (1920, 0, 1920, 1080)
    assert monitor_rectangle((960, 0, 100, 100), monitors) == (0, 0, 1920, 1080)
