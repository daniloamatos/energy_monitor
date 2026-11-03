"""Signed percentage changes with explicit validity and no decision rounding."""
import numpy as np
import pandas as pd

REASON_COLUMN = 'Motivo da variação'


def calculate(values, entities, *, return_previous=False):
    numeric = pd.to_numeric(values, errors='coerce')
    codes, _ = pd.factorize(entities, sort=False)
    grouped = pd.DataFrame({'value': numeric.to_numpy(), 'position': np.arange(len(values))}).groupby(codes, sort=False)
    shifted = grouped.shift()
    previous = pd.Series(shifted['value'].to_numpy(), index=values.index)
    exists = pd.Series(shifted['position'].notna().to_numpy(), index=values.index)
    valid = exists & np.isfinite(numeric) & np.isfinite(previous) & previous.ne(0)
    with np.errstate(divide='ignore', invalid='ignore', over='ignore'):
        changes = ((numeric - previous) / previous) * 100
    finite = np.isfinite(changes)
    valid &= finite
    reason = pd.Series('', index=values.index, dtype=object)
    reason.loc[~exists] = 'Sem base de comparação'
    reason.loc[exists & previous.eq(0)] = 'Base anterior zero'
    reason.loc[exists & (~np.isfinite(previous) | ~np.isfinite(numeric))] = 'Medição ou base inválida'
    reason.loc[exists & previous.ne(0) & np.isfinite(previous) & np.isfinite(numeric) & ~finite] = 'Resultado não finito'
    result = changes.where(valid), reason
    if return_previous:
        return (*result, shifted['position'].fillna(-1).to_numpy(dtype=np.int64))
    return result


def predecessor_positions(frame, columns, id_column):
    entities = frame[columns[2]].astype(str)
    if id_column in frame:
        ids = frame[id_column].fillna('')
        entities = ('id:' + ids).where(ids.ne(''), 'group:' + entities)
    ordered = pd.DataFrame({'position': np.arange(len(frame)), 'entity': entities.to_numpy(),
                            'source': frame[columns[1]].to_numpy(), 'day': frame[columns[0]].to_numpy()})
    ordered = ordered.sort_values('day', kind='stable')
    ordered['previous'] = ordered.groupby(['source', 'entity'], sort=False).position.shift().fillna(-1)
    return ordered.sort_values('position')['previous'].to_numpy(dtype=np.int64)


def detail_number(value, signed=False):
    from .number_format import number
    if value is None or not np.isfinite(value):
        return '—'
    if round(float(value), 2) == float(value):
        return number(value, signed=signed)
    return format(float(value), '+.17g' if signed else '.17g').replace('.', ',')
