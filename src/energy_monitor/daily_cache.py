"""Disposable daily CSV indexes; source files remain authoritative."""

from contextlib import closing, contextmanager
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import json
from queue import Queue, Empty
import sqlite3
import tempfile
from threading import Event
from pathlib import Path

import pandas as pd

from .cancellation import check_cancelled
from .number_format import count

FIELDS = {"submarket": "nom_subsistema", "state": "nom_estado",
          "plant_type": "nom_tipousina", "fuel": "nom_tipocombustivel",
          "plant_name": "nom_usina"}
VERSION = 4


def fingerprint(path):
    stat = path.stat()
    return json.dumps([VERSION, stat.st_size, stat.st_mtime_ns])


def index_is_current(path):
    target = path.with_suffix(".daily.sqlite").resolve()
    try:
        with closing(sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True)) as connection:
            row = connection.execute("SELECT signature FROM metadata").fetchone()
            connection.execute("SELECT day, entity FROM daily LIMIT 1").fetchone()
            if not row:
                return False
            stored = json.loads(row[0])
            current = json.loads(fingerprint(path))
            # Older versions remain readable when their daily schema still matches
            # the requested fields. ONS indexes without state are rebuilt on use.
            return (isinstance(stored, list) and len(stored) == 3
                    and stored[0] in (1, 2, VERSION) and stored[1:] == current[1:])
    except (sqlite3.Error, OSError, ValueError, TypeError):
        return False


def _has_state_column(target):
    try:
        with closing(sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True)) as connection:
            return "state" in {row[1] for row in connection.execute("PRAGMA table_info(daily)")}
    except (sqlite3.Error, OSError):
        return False


def prepare_indexes(files, workers=2, cancelled=lambda: False, progress=None):
    """Build independent CSV indexes concurrently, without retaining daily frames."""
    missing = []
    for path, source in files:
        check_cancelled(cancelled)
        if not path.is_file():
            raise ValueError(f"Arquivo indisponível: {path.stem}. Verifique a conexão e o link no catálogo.")
        if not index_is_current(path):
            missing.append((path, source))
    completed = len(files) - len(missing)
    workers = min(workers, len(missing))

    def report(message):
        if progress:
            progress(f"Índices {completed}/{len(files)} · {message}", completed)

    report(f"{len(missing)} CSVs para indexar" + (f" com {workers} threads" if workers else ""))
    if not missing:
        return
    abort = Event()
    messages = Queue()

    def stopped():
        return abort.is_set() or cancelled()

    def build(item):
        path, source = item
        return index_csv(path, source, cancelled=stopped, progress=messages.put)

    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="energy-csv")
    remaining = iter(missing)
    try:
        pending = {executor.submit(build, next(remaining)) for _ in range(workers)}
        while pending:
            check_cancelled(cancelled)
            done, pending = wait(pending, timeout=.05, return_when=FIRST_COMPLETED)
            while True:
                try:
                    report(messages.get_nowait())
                except Empty:
                    break
            for future in done:
                path = future.result()
                completed += 1
                report(f"Pronto: {path.stem}")
                item = next(remaining, None)
                if item is not None:
                    pending.add(executor.submit(build, item))
        check_cancelled(cancelled)
    finally:
        abort.set()
        executor.shutdown(wait=True, cancel_futures=True)


@contextmanager
def cancellable_connection(path, cancelled):
    with closing(sqlite3.connect(path)) as connection:
        connection.set_progress_handler(lambda: int(cancelled()), 1000)
        try:
            yield connection
        except Exception:
            check_cancelled(cancelled)
            raise


def index_csv(path, source, cancelled=lambda: False, progress=None, resource=None, force=False):
    from .data_sources import normalize, read_csv

    target = path.with_suffix(".daily.sqlite")
    check_cancelled(cancelled)
    signature = fingerprint(path)
    if not force and index_is_current(path) and (source != "ONS" or _has_state_column(target)):
        return target
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".sqlite.part", delete=False) as temporary:
        staging = Path(temporary.name)
    try:
        with cancellable_connection(staging, cancelled) as connection, connection:
            connection.execute('PRAGMA synchronous = OFF')
            connection.execute('PRAGMA journal_mode = MEMORY')
            connection.execute('PRAGMA cache_size = -64000')
            connection.execute('PRAGMA temp_store = MEMORY')
            columns = None
            rows = 0
            duplicates = 0
            valid = 0
            connection.execute('CREATE TABLE observations (instant TEXT, entity TEXT, value REAL, submarket TEXT, state TEXT, plant_type TEXT, fuel TEXT, plant_name TEXT, PRIMARY KEY (instant, entity)) WITHOUT ROWID')
            weekly = (getattr(resource, 'granularity', None) == 'weekly' or 'semanal' in path.stem)
            with closing(read_csv(path)) as reader:
                for chunk in reader:
                    check_cancelled(cancelled)
                    columns = list(chunk.columns)
                    daily = normalize(chunk, source, resource=resource)
                    if resource is not None and not daily.empty:
                        if daily.day.min() < resource.start or daily.day.max() > resource.end:
                            raise ValueError('Datas fora da cobertura declarada do recurso.')
                    can_deduplicate = not weekly
                    if can_deduplicate:
                        observations = pd.DataFrame(index=daily.index)
                        if source == 'ONS':
                            observations['instant'] = pd.to_datetime(chunk.din_instante, format='ISO8601').dt.as_unit('ns').astype('int64').reindex(daily.index).astype(str)
                        elif 'HORA' in chunk:
                            observations['instant'] = daily.day.astype(str) + 'T' + pd.to_numeric(chunk.HORA).reindex(daily.index).astype('int64').astype(str)
                        else:
                            # CCEE without HORA still has a daily observation key.
                            observations['instant'] = daily.day.astype(str)
                        observations['entity'] = daily.entity
                        observations['value'] = daily.value
                        for field, column in FIELDS.items():
                            observations[field] = chunk[column].fillna('').str.strip().reindex(daily.index) if column in chunk else ''
                        keys = ['instant', 'entity']
                        distinct = observations.drop_duplicates()
                        if distinct.duplicated(keys).any():
                            raise ValueError('Observações conflitantes para o mesmo instante e entidade.')
                        incoming = distinct.assign(position=distinct.index)
                        incoming.to_sql('incoming', connection, if_exists='replace', index=False, method='multi', chunksize=1000)
                        check_cancelled(cancelled)
                        fields = ' OR '.join(f'i.{field} != o.{field}' for field in FIELDS)
                        conflict = connection.execute(
                            'SELECT 1 FROM incoming i JOIN observations o USING (instant, entity) '
                            f'WHERE i.value != o.value OR {fields} LIMIT 1').fetchone()
                        if conflict:
                            raise ValueError('Observações conflitantes para o mesmo instante e entidade.')
                        keep = pd.read_sql_query(
                            'SELECT i.position FROM incoming i LEFT JOIN observations o USING (instant, entity) '
                            'WHERE o.entity IS NULL', connection)['position']
                        duplicates += len(daily) - len(keep)
                        connection.execute('INSERT OR IGNORE INTO observations '
                                           'SELECT instant, entity, value, submarket, state, plant_type, fuel, plant_name FROM incoming')
                        daily = daily.loc[keep]
                        connection.execute('DROP TABLE incoming')
                    valid += len(daily)
                    for field, column in FIELDS.items():
                        daily[field] = chunk[column].str.strip().reindex(daily.index).fillna("") if column in chunk else ""
                    daily["day"] = daily.day.astype(str)
                    keys = ["day", "entity", "location", *FIELDS]
                    grouped = daily.groupby(keys, dropna=False).value.agg(["sum", "count"]).reset_index()
                    grouped.to_sql("chunks", connection, if_exists="append", index=False)
                    rows += len(chunk)
                    if progress:
                        progress(f"Indexando {path.stem}: {count(rows)} registros lidos")
            if not valid:
                raise ValueError('CSV sem medições válidas.')
            if progress:
                progress(f"Consolidando índice: {path.stem}")
            connection.execute('CREATE TABLE daily AS SELECT day, entity, location, submarket, state, plant_type, fuel, plant_name, '
                               'SUM("sum") AS "sum", SUM("count") AS "count" FROM chunks '
                               'GROUP BY day, entity, location, submarket, state, plant_type, fuel, plant_name')
            connection.execute("DROP TABLE chunks")
            connection.execute("CREATE INDEX daily_date ON daily(day)")
            connection.execute("CREATE INDEX daily_location ON daily(location, day)")
            connection.execute('DROP TABLE observations')
            first, last, days = connection.execute('SELECT MIN(day), MAX(day), COUNT(DISTINCT day) FROM daily').fetchone()
            summary = dict(first=first, last=last, observed_days=days, rows=rows, valid=valid,
                           missing=rows-valid-duplicates, duplicates=duplicates,
                           granularity='weekly' if weekly else 'hourly', continuity='not_verified')
            connection.execute("CREATE TABLE metadata (signature TEXT, columns TEXT, summary TEXT)")
            connection.execute("INSERT INTO metadata VALUES (?, ?, ?)", (signature, json.dumps(columns), json.dumps(summary)))
            if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise ValueError('Índice candidato inválido.')
        if fingerprint(path) != signature:
            raise ValueError("CSV alterado durante a indexação. Repita a consulta.")
        check_cancelled(cancelled)
        staging.replace(target)
    finally:
        staging.unlink(missing_ok=True)
    return target


def _read_daily(path, source, start, end, location="Todos", filters=None, cancelled=lambda: False,
               progress=None, options=False):
    target = index_csv(path.resolve(), source, cancelled, progress)
    if progress:
        progress(f"Aplicando filtros no índice: {path.stem}")
    clauses, values = ["day >= ?", "day <= ?"], [start.isoformat(), end.isoformat()]
    with cancellable_connection(target, cancelled) as connection:
        columns = json.loads(connection.execute("SELECT columns FROM metadata").fetchone()[0])
        if location != "Todos":
            if source == "ONS":
                clauses.append("(entity = ? OR entity = ? OR location = ?)")
                values.extend(("id:" + location, "group:" + location, location))
            else:
                clauses.append("location = ?")
                values.append(location)
        for field, value in (filters or {}).items():
            if value != "Todos":
                if FIELDS[field] not in columns:
                    raise ValueError(f"CSV ONS sem a coluna necessária ao filtro: {FIELDS[field]}.")
                clauses.append(f"{field} = ?")
                values.append(value)
        columns = ("DISTINCT entity, location, submarket, state, plant_type, fuel, plant_name" if options
                   else 'day, entity, location, "sum", "count"')
        result = pd.read_sql_query(f"SELECT {columns} FROM daily WHERE " + " AND ".join(clauses), connection, params=values)
    if not options:
        result["day"] = pd.to_datetime(result.day).dt.date
    check_cancelled(cancelled)
    return result


def read_daily(path, source, start, end, location="Todos", filters=None, cancelled=lambda: False,
               progress=None, options=False):
    try:
        return _read_daily(path, source, start, end, location, filters, cancelled, progress, options)
    except (sqlite3.DatabaseError, pd.errors.DatabaseError):
        check_cancelled(cancelled)
        index_csv(path.resolve(), source, cancelled, progress, force=True)
        return _read_daily(path, source, start, end, location, filters, cancelled, progress, options)
