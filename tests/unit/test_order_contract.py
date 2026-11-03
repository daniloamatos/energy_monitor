"""Ordenação global estável, paginação adiada e retenção limitada de posições."""
from datetime import date
from unittest.mock import Mock

import numpy as np
import pandas as pd
import pytest
from hypothesis import given, settings, strategies as st

from energy_monitor.preview import TABLE_COLUMNS
from energy_monitor.query_cache import QueryCache
from energy_monitor.table_order import TableOrder


def table():
    return pd.DataFrame([
        [date(2026, 11, 3), 'ONS', 'beta', None, 20, -5, 'b', 2],
        [date(2026, 11, 1), 'ccee', 'Alpha', 20, None, 5, 'A', 1],
        [date(2026, 11, 1), 'CCEE', 'alpha', 10, 10, 5, 'a', 1],
        [None, None, None, 10, 0, None, None, None],
    ], columns=TABLE_COLUMNS)


def expected(frame, column, ascending):
    key = (lambda series: series.str.casefold()) if column in (1, 2, 6) else None
    return frame.sort_values(TABLE_COLUMNS[column], ascending=ascending, kind='stable', na_position='last', key=key)


@pytest.mark.parametrize('column', range(8))
@pytest.mark.parametrize('ascending', [True, False])
def test_cada_coluna_ordena_globalmente_com_empates_estaveis_e_ausentes_no_fim(column, ascending):
    frame = table()
    actual = TableOrder(frame).sort(column, ascending).iloc[:len(frame)]
    pd.testing.assert_frame_equal(actual, expected(frame, column, ascending))


@given(st.lists(st.one_of(st.none(), st.integers(-100, 100)), max_size=80), st.booleans())
@settings(max_examples=40, deadline=None)
def test_propriedade_ordenacao_numerica_estavel_com_nulos_sempre_ao_final(values, ascending):
    frame = pd.DataFrame({name: pd.Series(values, dtype=float) for name in TABLE_COLUMNS})
    order = TableOrder(frame).sort(3, ascending)
    pd.testing.assert_frame_equal(order.iloc[:len(frame)], expected(frame, 3, ascending))


@pytest.mark.parametrize('column,ascending', [(0, True), (3, True), (3, False)])
def test_primeira_pagina_adiada_completa_ao_navegar_e_equivale_a_ordenacao_integral(column, ascending):
    # O limiar não é configurável. 100.001 inteiros cabem em poucos MiB e são baratos.
    size = 100_001
    frame = pd.DataFrame({TABLE_COLUMNS[0]: pd.date_range('1900-01-01', periods=size, freq='D'),
                          TABLE_COLUMNS[3]: np.arange(size)[::-1] % 103})
    order = TableOrder(frame).sort(column, ascending)
    assert len(order.positions) == 1000
    complete = expected(frame, column, ascending)
    pd.testing.assert_frame_equal(order.iloc[:1000], complete.iloc[:1000])
    assert len(order.positions) == 1000
    pd.testing.assert_frame_equal(order.iloc[1000:2000], complete.iloc[1000:2000])
    assert len(order.positions) == size
    pd.testing.assert_frame_equal(order.iloc[:size], complete)


def test_iteracao_de_exportacao_completa_ordenacao_adiada():
    frame = pd.DataFrame({TABLE_COLUMNS[0]: pd.date_range('2020-01-01', periods=1001)})
    order = TableOrder(frame).sort(0)
    assert len(order.positions) == 1000
    assert list(order.itertuples()) == list(frame.itertuples(index=False, name=None))
    assert len(order.positions) == len(frame)


def test_cache_reutiliza_coluna_e_direcao_sem_recalcular(monkeypatch):
    order = TableOrder(table())
    order.sort(3, True)
    ascending_positions = order.positions
    order.sort(3, False)
    descending_positions = order.positions
    spy = Mock(wraps=order._keys)
    monkeypatch.setattr(order, '_keys', spy)
    order.sort(3, True)
    assert order.positions is ascending_positions
    order.sort(3, False)
    assert order.positions is descending_positions
    spy.assert_not_called()


def test_cache_descarta_menos_recentemente_usado_ao_atingir_teto():
    order = TableOrder(table(), max_cache_bytes=32)  # Cada vetor de 4 int32 ocupa 16 bytes.
    order.sort(3, True).sort(3, False).sort(3, True).sort(4, True)
    assert list(order.orders) == [(3, True), (4, True)]
    assert sum(item.nbytes for item in order.orders.values()) <= 32
    order.sort(3, False)
    assert list(order.orders) == [(4, True), (3, False)]


def test_caches_de_agregados_e_ordenacao_possuem_32mib_independentes():
    aggregates, order = QueryCache(), TableOrder(table())
    assert aggregates.max_bytes == order.max_cache_bytes == 32 * 1024 * 1024
    aggregates.entries['sentinela'] = ('independente',)
    order.sort(3)
    assert list(aggregates.entries) == ['sentinela']
    aggregates.entries.clear()
    assert (3, True) in order.orders


@pytest.mark.parametrize('size', [0, 1, 1000, 1001, 2001])
def test_paginacao_retorna_ultima_pagina_parcial_e_resultado_vazio(size):
    frame = pd.DataFrame({TABLE_COLUMNS[3]: np.arange(size)[::-1]})
    order = TableOrder(frame).sort(3)
    last_start = max(0, ((size-1)//1000)*1000)
    page = order.iloc[last_start:last_start+1000]
    assert len(page) == size-last_start
    assert page[TABLE_COLUMNS[3]].tolist() == list(range(last_start, size))
