"""Filtros, indicadores, alertas e exportação das consultas aos CSV locais."""

from dataclasses import dataclass, replace
from collections.abc import Sequence
from datetime import date, datetime
from math import isfinite
from pathlib import Path
import re

import pandas as pd
import numpy as np
from openpyxl.styles import Font, PatternFill
from .cancellation import check_cancelled
from .number_format import number, compact
from .variation import predecessor_positions, detail_number

SUBMARKETS = ("Sudeste/Centro-Oeste", "Sul", "Nordeste", "Norte")
COLUMNS = ["Data", "Fonte", "Submercado / Usina", "Preço (R$/MWh)", "Geração (MW médios)", "Variação (%)"]
ID_COLUMN = "ID ONS"
ALERT_COLUMN = "Alertas"
TABLE_COLUMNS = [*COLUMNS, ID_COLUMN, ALERT_COLUMN]


def alert_cells(frame, query):
    """Use the same strict thresholds as the alert explanations."""
    result = pd.DataFrame(False, index=frame.index, columns=[COLUMNS[3], COLUMNS[5]])
    if query.source == "CCEE" and query.price_limit is not None:
        result[COLUMNS[3]] = frame[COLUMNS[3]].gt(query.price_limit)
    if query.change_limit is not None:
        changes = pd.to_numeric(frame[COLUMNS[5]], errors='coerce')
        result[COLUMNS[5]] = (np.isfinite(changes) & changes.abs().gt(query.change_limit)).fillna(False)
    return result


@dataclass(frozen=True)
class Query:
    source: str
    start: date
    end: date
    location: str
    price_limit: float | None
    change_limit: float | None
    ons_submarket: str = "Todos"
    plant_type: str = "Todos"
    fuel: str = "Todos"
    state: str = "Todos"
    ons_name: str = "Todos"


@dataclass(frozen=True)
class Alert:
    day: date
    source: str
    location: str
    kind: str
    value: float
    limit: float
    previous_value: float | None = None
    previous_day: date | None = None
    current_value: float | None = None

    @property
    def summary(self) -> str:
        prefix = f"{self.day:%d/%m/%Y} · {self.location}"
        if self.kind == "price":
            return f"{prefix}: preço R$ {compact(self.value)}/MWh acima de R$ {compact(self.limit)}/MWh."
        return f"{prefix}: variação {compact(self.value, signed=True)}% supera o limite do módulo de {compact(self.limit)}%."

    @property
    def explanation(self) -> str:
        if self.limit == 0:
            percentages = "Comparação percentual com o limite: não calculável (limite igual a zero).\n"
        else:
            observed = self.value if self.kind == "price" else abs(self.value)
            percentages = (
                f"Percentual do limite atingido: {compact(observed / self.limit * 100)}%\n"
                f"Excesso percentual sobre o limite: {compact((observed - self.limit) / self.limit * 100)}%\n")
        if self.kind == "price":
            return (f"Preço observado: R$ {compact(self.value)}/MWh\n"
                    f"Limite configurado: R$ {compact(self.limit)}/MWh\n"
                    f"Excesso: R$ {compact(self.value - self.limit)}/MWh\n"
                    f"{percentages}"
                    f"Motivo: o preço observado é estritamente maior que o limite configurado "
                    f"({number(self.value)} > {number(self.limit)}).")
        metric, unit = ("Preço", "R$/MWh") if self.source == "CCEE" else ("Geração", "MW médios")
        direction = "aumento da medição" if self.current_value > self.previous_value else "queda da medição"
        return (f"{metric} atual: {detail_number(self.current_value)} {unit}\n"
                f"{metric} anterior ({self.previous_day:%d/%m/%Y}): {detail_number(self.previous_value)} {unit}\n"
                f"Variação observada: {detail_number(self.value, signed=True)}% ({direction})\n"
                f"Limite configurado: {detail_number(self.limit)}% em módulo\n"
                f"Excesso: {detail_number(abs(self.value) - self.limit)} pontos percentuais\n"
                f"{percentages}"
                f"Cálculo: (({detail_number(self.current_value)} - {detail_number(self.previous_value)}) / {detail_number(self.previous_value)}) × 100 "
                f"= {detail_number(self.value, signed=True)}%\n"
                f"Motivo: o módulo da variação é estritamente maior que o limite "
                f"({detail_number(abs(self.value))}% > {detail_number(self.limit)}%). Base anterior disponível dentro do período; sinal preservado pela fórmula.")


def evaluate_alerts(frame: pd.DataFrame, query: Query, cancelled=lambda: False) -> list[Alert]:
    """Compartilha as mesmas regras entre resumo e explicações dos alertas."""
    collection = AlertCollection(frame, query, cancelled=cancelled)
    result = []
    for index in range(len(collection)):
        if index % 1024 == 0:
            check_cancelled(cancelled)
        result.append(collection.detail(index))
    return result


class AlertCollection(Sequence):
    """Store alert positions; format only the details or summaries being displayed."""

    def __init__(self, frame, query, cancelled=lambda: False, *, previous=None):
        check_cancelled(cancelled)
        self.frame = frame
        self.query = query
        self.previous = predecessor_positions(frame, COLUMNS, ID_COLUMN) if previous is None else previous
        flags = alert_cells(frame, query)
        flags[COLUMNS[5]] &= self.previous >= 0
        self.positions = np.flatnonzero(flags.to_numpy().ravel())
        check_cancelled(cancelled)

    def __len__(self):
        return len(self.positions)

    def detail(self, index):
        position, kind = divmod(int(self.positions[index]), 2)
        row = self.frame.iloc[position]
        day, source, location, price, generation, change = (row[column] for column in COLUMNS)
        identifier = row.get(ID_COLUMN, "")
        if pd.notna(identifier) and identifier:
            location = f"{location} [{identifier}]"
        if kind == 0:
            return Alert(day, source, location, "price", price, self.query.price_limit)
        old = self.frame.iloc[self.previous[position]]
        metric = COLUMNS[3] if source == "CCEE" else COLUMNS[4]
        return Alert(day, source, location, "change", change, self.query.change_limit,
                     previous_value=old[metric], previous_day=old[COLUMNS[0]], current_value=row[metric])

    def details(self, start, end):
        return [self.detail(index) for index in range(start, min(end, len(self)))]

    def iter_summaries(self):
        arrays = {column: self.frame[column].to_numpy(copy=False) for column in COLUMNS}
        identifiers = self.frame[ID_COLUMN].fillna("").to_numpy(copy=False) if ID_COLUMN in self.frame else None
        for encoded in self.positions:
            position, kind = divmod(int(encoded), 2)
            day, source, location, price, generation, change = (arrays[column][position] for column in COLUMNS)
            if identifiers is not None and identifiers[position]:
                location = f"{location} [{identifiers[position]}]"
            if kind == 0:
                yield Alert(day, source, location, "price", price, self.query.price_limit).summary
            else:
                old = self.previous[position]
                metric = COLUMNS[3] if source == "CCEE" else COLUMNS[4]
                yield Alert(day, source, location, "change", change, self.query.change_limit,
                            arrays[metric][old], arrays[COLUMNS[0]][old], arrays[metric][position]).summary

    def __getitem__(self, index):
        if isinstance(index, slice):
            return [self.detail(i).summary for i in range(*index.indices(len(self)))]
        return self.detail(index).summary


def limits_only_changed(previous_query, query):
    return (previous_query is not None and previous_query != query
            and replace(previous_query, price_limit=query.price_limit, change_limit=query.change_limit) == query)


def narrower_period(previous_query, query):
    return (previous_query is not None and previous_query.start <= query.start <= query.end <= previous_query.end
            and replace(previous_query, start=query.start, end=query.end,
                        price_limit=query.price_limit, change_limit=query.change_limit) == query)


def can_filter_snapshot(previous_query, query):
    return (previous_query is not None and previous_query.source == query.source
            and previous_query.start <= query.start <= query.end <= previous_query.end
            and previous_query.location == 'Todos' and query.location != 'Todos'
            and replace(previous_query, start=query.start, end=query.end, location=query.location,
                        price_limit=query.price_limit, change_limit=query.change_limit) == query)


def filter_snapshot(frame, previous_query, query, cancelled=lambda: False, progress=None):
    """Filter entity rows already present; do not infer unavailable ONS attributes."""
    if not can_filter_snapshot(previous_query, query):
        raise ValueError('O filtro solicitado não está coberto pelo resultado disponível.')
    check_cancelled(cancelled)
    location = query.location
    mask = frame[COLUMNS[0]].between(query.start, query.end)
    if query.source == 'ONS' and ID_COLUMN in frame:
        identifier = re.search(r" \[([^\]]+)\]$", location)
        if identifier:
            mask &= (frame[ID_COLUMN].fillna('').eq(identifier.group(1))
                     & frame[COLUMNS[2]].eq(location[:identifier.start()]))
        else:
            mask &= (frame[ID_COLUMN].fillna('').eq(location)
                     | frame[COLUMNS[2]].eq(location))
    else:
        mask &= frame[COLUMNS[2]].eq(location)
    selected = frame.loc[mask].copy(deep=False)
    return restrict_period(selected, query, query, cancelled, progress)


def restrict_period(frame, previous_query, query, cancelled=lambda: False, progress=None, *, alerts=None):
    """Derive a smaller period from the displayed snapshot without reading indexes."""
    if not narrower_period(previous_query, query):
        raise ValueError('O período precisa estar contido na consulta anterior, com os mesmos filtros.')
    check_cancelled(cancelled)
    mask = frame[COLUMNS[0]].between(query.start, query.end)
    result = frame.copy(deep=False) if mask.all() else frame.loc[mask].copy(deep=False)
    if result.empty:
        raise ValueError('Nenhum registro disponível para a consulta.')
    from .variation import calculate, REASON_COLUMN
    if isinstance(alerts, AlertCollection) and alerts.frame is frame:
        selected = np.flatnonzero(mask.to_numpy())
        old_previous = alerts.previous[selected]
        previous = np.searchsorted(selected, old_previous)
        valid = (old_previous >= 0) & (previous < len(selected))
        valid[valid] &= selected[previous[valid]] == old_previous[valid]
        previous[~valid] = -1
        result[COLUMNS[5]] = result[COLUMNS[5]].mask(previous < 0)
        result[REASON_COLUMN] = result.get(REASON_COLUMN, pd.Series('', index=result.index)).mask(previous < 0, 'Sem base de comparação')
    else:
        entities = result[COLUMNS[2]].astype(str)
        if ID_COLUMN in result:
            ids = result[ID_COLUMN].fillna('')
            entities = ('id:' + ids).where(ids.ne(''), 'group:' + entities)
        metric = COLUMNS[3] if query.source == 'CCEE' else COLUMNS[4]
        changes, reasons, previous = calculate(result[metric], entities, return_previous=True)
        result[COLUMNS[5]] = changes
        result[REASON_COLUMN] = reasons
    result[ALERT_COLUMN] = alert_cells(result, query).sum(axis=1)
    updated_alerts = AlertCollection(result, query, cancelled=cancelled, previous=previous)
    check_cancelled(cancelled)
    if progress:
        from .progress import Progress
        progress(Progress('Período selecionado do resultado em memória.', 1, 1))
    return result, updated_alerts


def overlapping_period(previous_query, query):
    return (previous_query is not None and previous_query.start <= query.end and query.start <= previous_query.end
            and replace(previous_query, start=query.start, end=query.end,
                        price_limit=query.price_limit, change_limit=query.change_limit) == query)


def extend_period(store, frame, previous_query, query, cancelled=lambda: False, progress=None, *, alerts=None):
    """Keep the displayed overlap and query only missing edges, without a history of frames."""
    if not overlapping_period(previous_query, query):
        raise ValueError('A reutilização exige períodos sobrepostos com os mesmos filtros.')
    if narrower_period(previous_query, query):
        return restrict_period(frame, previous_query, query, cancelled, progress, alerts=alerts)
    from datetime import timedelta
    pieces = [frame.loc[frame[COLUMNS[0]].between(query.start, query.end)]]
    spans = []
    if query.start < previous_query.start:
        spans.append((query.start, previous_query.start - timedelta(days=1)))
    if query.end > previous_query.end:
        spans.append((previous_query.end + timedelta(days=1), query.end))
    resources = {item['id']: item for item in frame.attrs.get('resources', [])}
    diagnostics = dict(frame.attrs.get('diagnostics', {}))
    for first, last in spans:
        check_cancelled(cancelled)
        try:
            extra, _ = store.load(replace(query, start=first, end=last), cancelled=cancelled,
                                  progress=(lambda message: progress(str(message))) if progress else None)
        except ValueError as error:
            if str(error) == 'Nenhum registro disponível para a consulta.':
                continue
            raise
        for identifier, state in extra.attrs.get('diagnostics', {}).items():
            old_generation = diagnostics.get(identifier, {}).get('generation')
            new_generation = state.get('generation') if isinstance(state, dict) else None
            if old_generation and new_generation and old_generation != new_generation:
                return store.load(query, cancelled=cancelled, progress=progress)
        pieces.append(extra)
        resources.update((item['id'], item) for item in extra.attrs.get('resources', []))
        diagnostics.update(extra.attrs.get('diagnostics', {}))
    pieces = [part for part in pieces if not part.empty]
    pieces.sort(key=lambda part: part[COLUMNS[0]].iloc[0])
    combined = pd.concat(pieces, ignore_index=True)
    combined.attrs = dict(frame.attrs)
    combined.attrs['resources'] = list(resources.values())
    combined.attrs['diagnostics'] = diagnostics
    return restrict_period(combined, query, query, cancelled, progress)


def reapply_limits(frame, alerts, previous_query, query, cancelled=lambda: False, progress=None):
    """Reuse the completed data snapshot; only replace threshold-dependent results."""
    if not limits_only_changed(previous_query, query):
        raise ValueError('A reutilização exige os mesmos filtros e período, alterando somente limites.')
    check_cancelled(cancelled)
    result = frame.copy(deep=False)
    result[ALERT_COLUMN] = alert_cells(result, query).sum(axis=1)
    updated_alerts = AlertCollection(result, query, cancelled=cancelled, previous=getattr(alerts, 'previous', None))
    check_cancelled(cancelled)
    if progress:
        from .progress import Progress
        progress(Progress('Limites reaplicados ao resultado em memória.', 1, 1))
    return result, updated_alerts


def parse_limit(value: str, label: str) -> float | None:
    if not value.strip():
        return None
    try:
        result = float(value.strip().replace(",", "."))
    except ValueError:
        raise ValueError(f"{label}: informe um número positivo ou deixe em branco.") from None
    if not isfinite(result) or result < 0:
        raise ValueError(f"{label}: informe um número maior ou igual a zero.")
    return result


def parse_query(values: dict) -> Query:
    try:
        start = datetime.strptime(values["-START-"], "%d/%m/%Y").date()
        end = datetime.strptime(values["-END-"], "%d/%m/%Y").date()
    except ValueError:
        raise ValueError("Use datas válidas no formato DD/MM/AAAA.") from None
    if end < start:
        raise ValueError("A data final deve ser igual ou posterior à inicial.")
    source = values["-SOURCE-"]
    if source not in ("CCEE", "ONS"):
        raise ValueError("Selecione CCEE ou ONS.")
    location = values["-LOCATION-"]
    if not location or (source == "CCEE" and location not in ("Todos", *SUBMARKETS)):
        raise ValueError("Selecione um filtro válido para a fonte.")
    return Query(source, start, end, location,
                 parse_limit(values["-PRICE-"], "Limite de preço") if source == "CCEE" else None,
                 parse_limit(values["-CHANGE-"], "Limite de variação"),
                 values.get("-ONS-SUBMARKET-", "Todos") if source == "ONS" else "Todos",
                 values.get("-PLANT-TYPE-", "Todos") if source == "ONS" else "Todos",
                 values.get("-FUEL-", "Todos") if source == "ONS" else "Todos",
                 values.get("-STATE-", "Todos") if source == "ONS" else "Todos",
                 values.get("-ONS-NAME-", "Todos") if source == "ONS" else "Todos")



def indicators(frame: pd.DataFrame) -> dict[str, float | None]:
    def average(column: str) -> float | None:
        values = frame[column].dropna()
        return float(values.mean()) if not values.empty else None
    changes = pd.to_numeric(frame[COLUMNS[5]], errors="coerce")
    changes = changes[np.isfinite(changes)]
    return {"Preço médio (R$/MWh)": average(COLUMNS[3]),
            "Geração média (MW médios)": average(COLUMNS[4]),
            "Maior variação absoluta (%)": float(changes.abs().max()) if not changes.empty else None}


def export_excel(path: Path, frame: pd.DataFrame, alerts, query: Query, updated: datetime, **kwargs) -> None:
    """Exporta exatamente a última consulta concluída, incluindo seus limites."""
    from .exporting import export_excel as write_excel
    write_excel(path, frame, alerts, query, updated, **kwargs)
