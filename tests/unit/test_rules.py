"""Contratos numéricos: valores manuais, sem arredondar decisões de alerta."""
from datetime import date
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings, strategies as st

from energy_monitor.preview import (COLUMNS, SUBMARKETS, AlertCollection, alert_cells,
                                    indicators, parse_limit, parse_query)
from energy_monitor.variation import calculate, predecessor_positions
from energy_monitor.titlebar import monitor_rectangle


@pytest.mark.parametrize('text,expected', [('1,5', 1.5), ('0', 0), ('', None), ('  ', None), ('2.25', 2.25)])
def test_limite_aceita_decimal_e_vazio(text, expected):
    actual = parse_limit(text, 'Limite')
    assert actual is None if expected is None else actual == pytest.approx(expected)


@pytest.mark.parametrize('text', ['-1', 'inf', '-inf', 'nan', 'NaN', 'texto'])
def test_limite_rejeita_negativo_nao_finito_e_texto(text):
    with pytest.raises(ValueError, match='Limite:'):
        parse_limit(text, 'Limite')


def values(**updates):
    return {'-START-': '01/11/2026', '-END-': '03/11/2026', '-SOURCE-': 'CCEE',
            '-LOCATION-': 'Todos', '-PRICE-': '', '-CHANGE-': '1,5'} | updates


@pytest.mark.parametrize('location', ['Todos', *SUBMARKETS])
def test_consulta_aceita_cada_submercado(location):
    query = parse_query(values(**{'-LOCATION-': location}))
    assert (query.start, query.end, query.location) == (date(2026, 11, 1), date(2026, 11, 3), location)
    assert query.change_limit == pytest.approx(1.5)


@pytest.mark.parametrize('updates,reason', [({'-START-': '31/02/2026'}, 'datas válidas'),
    ({'-END-': ''}, 'datas válidas'), ({'-START-': '04/11/2026'}, 'posterior'),
    ({'-LOCATION-': 'Inexistente'}, 'filtro válido'), ({'-SOURCE-': 'Outra'}, 'CCEE ou ONS')])
def test_consulta_rejeita_periodo_e_fonte_invalidos(updates, reason):
    with pytest.raises(ValueError, match=reason):
        parse_query(values(**updates))


def test_ons_preserva_filtros_e_ignora_limite_de_preco():
    query = parse_query(values(**{'-SOURCE-': 'ONS', '-LOCATION-': 'X', '-PRICE-': 'inválido',
                                 '-ONS-SUBMARKET-': 'Sul', '-STATE-': 'AMAZONAS',
                                 '-PLANT-TYPE-': 'Térmica', '-FUEL-': 'Gás'}))
    assert (query.location, query.ons_submarket, query.state, query.plant_type, query.fuel) == (
        'X', 'Sul', 'AMAZONAS', 'Térmica', 'Gás')
    assert query.price_limit is None


@pytest.mark.parametrize('old,current,expected', [(100, 110, 10), (100, 90, -10), (-100, -90, -10), (50, 0, -100)])
def test_variacao_preserva_formula_e_sinal(old, current, expected):
    result, reason = calculate(pd.Series([old, current]), pd.Series(['A', 'A']))
    assert result.iloc[1] == pytest.approx(expected, abs=1e-12)
    assert np.sign(result.iloc[1]) == np.sign(expected)
    assert reason.iloc[1] == ''


@pytest.mark.parametrize('old,current,reason', [(0, 10, 'Base anterior zero'), (np.nan, 10, 'Medição ou base inválida'),
    (10, np.inf, 'Medição ou base inválida'), (1e-308, 1e308, 'Resultado não finito')])
def test_variacao_sem_base_valida_explicita_motivo(old, current, reason):
    result, reasons = calculate(pd.Series([old, current]), pd.Series(['A', 'A']))
    assert result.isna().all()
    assert reasons.tolist() == ['Sem base de comparação', reason]


@given(st.floats(min_value=1, max_value=1e6, allow_nan=False, allow_infinity=False),
       st.floats(min_value=-99, max_value=100, allow_nan=False, allow_infinity=False))
@settings(max_examples=30, deadline=None)
def test_variacao_obedece_formula_para_valores_finitos(base, percent):
    current = base * (1 + percent / 100)
    result, _ = calculate(pd.Series([base, current]), pd.Series(['A', 'A']))
    assert result.iloc[1] == pytest.approx(percent, rel=1e-10, abs=1e-10)


@pytest.mark.parametrize('source', ['CCEE', 'ONS'])
@pytest.mark.parametrize('change,expected', [(10, False), (10-1e-7, False), (10+1e-7, True), (-10-1e-7, True)])
def test_alerta_variacao_estritamente_maior_antes_de_arredondar(source, change, expected, frame_factory, query_factory):
    frame = frame_factory([[date(2026, 11, 1), source, 'A', 100, 100, np.nan],
                           [date(2026, 11, 3), source, 'A', 100+change, 100+change, change]])
    alerts = AlertCollection(frame, query_factory(source=source, change_limit=10))
    assert len(alerts) == int(expected)
    assert bool(alert_cells(frame, query_factory(source=source)).iloc[1, 1]) is expected


@pytest.mark.parametrize('price,expected', [(100, False), (100-1e-7, False), (100+1e-7, True)])
def test_preco_igual_ao_limite_nao_dispara(price, expected, frame_factory, query_factory):
    frame = frame_factory([[date(2026, 11, 1), 'CCEE', 'Sul', price, np.nan, np.nan]])
    assert len(AlertCollection(frame, query_factory(price_limit=100, change_limit=None))) == int(expected)


@pytest.mark.parametrize('price_limit,change_limit,expected', [(None, 10, 1), (100, None, 1), (None, None, 0), (100, 10, 2)])
def test_limite_vazio_desativa_apenas_seu_tipo(price_limit, change_limit, expected, frame_factory, query_factory):
    frame = frame_factory([[date(2026, 11, 1), 'CCEE', 'Sul', 100, np.nan, np.nan],
                           [date(2026, 11, 3), 'CCEE', 'Sul', 120, np.nan, 20]])
    assert len(AlertCollection(frame, query_factory(price_limit=price_limit, change_limit=change_limit))) == expected


def test_detalhe_alerta_inclui_identidade_base_unidade_formula_e_excesso(frame_factory, query_factory):
    frame = frame_factory([[date(2026, 11, 1), 'ONS', 'Nome antigo', np.nan, 100, np.nan],
                           [date(2026, 11, 3), 'ONS', 'Nome novo', np.nan, 80, -20]], ids=['X', 'X'])
    alert = AlertCollection(frame, query_factory(source='ONS')).detail(0)
    assert (alert.location, alert.previous_day, alert.previous_value, alert.current_value) == ('Nome novo [X]', date(2026, 11, 1), 100, 80)
    for text in ['MW médios', '01/11/2026', '((80,00 - 100,00) / 100,00) × 100', '-20,00%', '10,00 pontos percentuais', 'queda da medição', 'estritamente maior']:
        assert text in alert.explanation


def test_anterior_disponivel_respeita_identidade_e_independe_da_ordem_visual(frame_factory):
    frame = frame_factory([[date(2026, 11, 3), 'ONS', 'Novo', np.nan, 120, 20],
                           [date(2026, 11, 1), 'ONS', 'Igual', np.nan, 50, np.nan],
                           [date(2026, 11, 1), 'ONS', 'Antigo', np.nan, 100, np.nan]], ids=['X', 'Y', 'X'])
    assert predecessor_positions(frame, COLUMNS, 'ID ONS').tolist() == [2, -1, -1]


def test_indicadores_usam_todo_resultado_e_preservam_zero(frame_factory):
    from energy_monitor.table_order import TableOrder
    frame = frame_factory([[date(2026, 11, 1), 'CCEE', 'A', 0, 10, np.nan],
                           [date(2026, 11, 2), 'CCEE', 'A', 100, 30, -20],
                           [date(2026, 11, 3), 'CCEE', 'A', np.nan, 50, 10]])
    ordered = TableOrder(frame).sort(3, False)
    ordered.iloc[:1]  # A página visível não define a população dos indicadores.
    assert indicators(ordered) == pytest.approx({'Preço médio (R$/MWh)': 50, 'Geração média (MW médios)': 30, 'Maior variação absoluta (%)': 20})


@pytest.mark.parametrize('rectangle,expected', [((100, 100, 400, 300), (0, 0, 1920, 1080)),
    ((1800, 60, 1000, 800), (1920, 0, 1920, 1080)), ((5000, 0, 100, 100), (1920, 0, 1920, 1080))])
def test_monitor_escolhido_por_intersecao_ou_distancia(rectangle, expected):
    monitors = [SimpleNamespace(x=x, y=0, width=1920, height=1080) for x in [0, 1920]]
    assert monitor_rectangle(rectangle, monitors) == expected


def test_maximizar_sem_monitor_informa_erro():
    with pytest.raises(RuntimeError, match='Nenhum monitor'):
        monitor_rectangle((0, 0, 100, 100), [])


def test_recorte_recalcula_anterior_e_contador_por_linha(frame_factory, query_factory):
    from dataclasses import replace
    from energy_monitor.preview import ALERT_COLUMN, restrict_period
    frame = frame_factory([[date(2026, 11, 1), 'CCEE', 'Sul', 100, np.nan, np.nan],
                           [date(2026, 11, 2), 'CCEE', 'Sul', 120, np.nan, 20],
                           [date(2026, 11, 3), 'CCEE', 'Sul', 180, np.nan, 50]])
    query = query_factory(price_limit=110)
    # A especificação compara somente dentro do período: o primeiro dia recortado perde a base.
    restricted, alerts = restrict_period(frame, query, replace(query, start=date(2026, 11, 2)))
    assert pd.isna(restricted[COLUMNS[5]].iloc[0])
    assert restricted[COLUMNS[5]].iloc[1] == pytest.approx(50)
    assert restricted[ALERT_COLUMN].tolist() == [1, 2]
    assert len(alerts) == 3


def test_detalhe_preco_explica_observado_limite_excesso_e_unidade(frame_factory, query_factory):
    frame = frame_factory([[date(2026, 11, 1), 'CCEE', 'Sul', 120, np.nan, np.nan]])
    detail = AlertCollection(frame, query_factory(price_limit=100)).detail(0)
    assert (detail.source, detail.day, detail.location, detail.kind) == ('CCEE', date(2026, 11, 1), 'Sul', 'price')
    for text in ['Preço observado: R$ 120,00/MWh', 'Limite configurado: R$ 100,00/MWh',
                 'Excesso: R$ 20,00/MWh', 'estritamente maior']:
        assert text in detail.explanation
