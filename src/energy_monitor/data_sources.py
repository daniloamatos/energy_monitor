"""Catálogo editável, cache atômico e consultas aos CSV públicos CCEE/ONS."""

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from queue import Queue, Empty
from threading import Lock
from contextlib import closing
from itertools import chain
from datetime import date, datetime, time, timedelta
from email.utils import parsedate_to_datetime
import json
import csv
import hashlib
import os
from pathlib import Path
from time import perf_counter
import re
from zoneinfo import ZoneInfo

import pandas as pd
import numpy as np
import requests

from .preview import COLUMNS, ID_COLUMN, ALERT_COLUMN, SUBMARKETS, AlertCollection, alert_cells
from .progress import Progress
from .number_format import count, number as format_number
from .daily_cache import read_daily, prepare_indexes
from .cancellation import check_cancelled, OperationCancelled
from .diagnostics import Diagnostics, safe_error, utcnow
from collections import Counter
import sqlite3
import sys
from .generations import active_path, prepare, publish, cleanup_candidate
from .daily_cache import index_csv

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config" / "sources.json"
CACHE = ROOT / "data" / "sources"


def read_workers(settings):
    workers = settings.get("read_workers", 2)
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 4:
        raise ValueError("Configure read_workers com um inteiro entre 1 e 4.")
    return workers


def download_workers(settings):
    workers = settings.get("download_workers", 1)
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 6:
        raise ValueError("Configure download_workers com um inteiro entre 1 e 6.")
    return workers


@dataclass(frozen=True)
class Resource:
    id: str
    source: str
    start: date
    end: date
    mutable: bool | str
    url: str
    granularity: str = "hourly"

    def changes(self, today):
        if self.mutable is False:
            return False
        if self.source == "CCEE":
            return self.start.year == self.end.year == today.year
        return ((self.start.year, self.start.month)
                == (self.end.year, self.end.month)
                == (today.year, today.month))


def catalog(path=CONFIG, cache=None):
    data = json.loads(Path(path).read_text(encoding="utf-8"))

    def parse(items):
        resources, seen = [], set()
        for item in items:
            operational = {key: value for key, value in item.items() if key != 'provenance'}
            operational.setdefault('granularity', item.get('provenance', {}).get('granularity', 'weekly' if 'semanal' in item['id'] else 'hourly'))
            resource = Resource(**(operational | {key: date.fromisoformat(item[key]) for key in ("start", "end")}))
            if (not re.fullmatch(r"[A-Za-z0-9_-]+", resource.id) or resource.id in seen
                    or resource.source not in ("CCEE", "ONS") or resource.granularity not in ("weekly", "hourly") or resource.end < resource.start
                    or not resource.url.startswith("https://")
                    or resource.mutable not in (True, False, "current_month", "current_year")):
                raise ValueError(f"Fonte inválida ou repetida: {resource.id}")
            seen.add(resource.id)
            resources.append(resource)
        return resources

    resources = parse(data["resources"])
    if cache is not None:
        from .discovery import read_state, merge
        merged, _ = merge(data['resources'], read_state(cache)['resources'], data.get('excluded_resources', []))
        resources = parse(merged)
    return data, resources


class Schedule:
    """Uma execução por horário; atrasos durante trabalho são atendidos ao liberar."""

    def __init__(self, settings):
        self.zone = ZoneInfo(settings["timezone"])
        self.times = sorted(time.fromisoformat(value) for value in settings["update_times"])
        if not self.times or any(value.tzinfo is not None for value in self.times):
            raise ValueError("Configure ao menos um horário local sem deslocamento de fuso em update_times.")
        self.next = None

    def due(self, now=None):
        now = now or datetime.now(self.zone)
        now = now.astimezone(self.zone)
        if self.next is None:
            self.next = self._after(now)
            return False
        if now < self.next:
            return False
        self.next = self._after(now)
        return True

    def _after(self, now):
        for offset in (0, 1):
            day = now.date() + timedelta(days=offset)
            for value in self.times:
                candidate = datetime.combine(day, value, self.zone)
                if candidate > now:
                    return candidate
        raise ValueError("Configure ao menos um horário de atualização.")


class SourceStore:
    def __init__(self, config=CONFIG, cache=CACHE, session=None):
        self.config = Path(config)
        self.cache = Path(cache)
        self.session = session or requests.Session()
        self.warnings = []
        from .query_cache import QueryCache
        self.query_cache = QueryCache()

    def path(self, resource_id):
        return active_path(self.cache, resource_id)

    def missing_resources(self, today=None, *, start=None, end=None, source=None):
        """Return catalog resources without a local copy, optionally for one query."""
        settings, resources = catalog(self.config, self.cache)
        today = today or datetime.now(ZoneInfo(settings["timezone"])).date()
        return [resource for resource in resources
                if resource.start <= today
                and (source is None or resource.source == source)
                and (start is None or resource.end >= start)
                and (end is None or resource.start <= end)
                and not self.path(resource.id).is_file()]

    def sync(self, today=None, progress=None, cancelled=lambda: False, *, current_only=False,
             file_result=None, missing_ids=None):
        settings, resources = catalog(self.config, self.cache)
        today = today or datetime.now(ZoneInfo(settings["timezone"])).date()
        self.cache.mkdir(parents=True, exist_ok=True)
        warnings = []
        diagnostics = Diagnostics(self.cache, **settings.get('logging', {}))
        operation = getattr(self, 'operation_id', None)
        outcomes = {source: Counter() for source in ('CCEE', 'ONS')}
        def record(resource, result, **kwargs):
            try:
                diagnostics.resource(resource, result, operation=operation, **kwargs)
            except (OSError, sqlite3.Error):
                print('Diagnóstico degradado: estado operacional indisponível.', file=sys.stderr)
        started = perf_counter()
        timings = [f"Verificação iniciada: {datetime.now().astimezone().isoformat(timespec='seconds')}"]
        if file_result:
            for resource in resources:
                if resource.start <= today and not resource.changes(today) and self.path(resource.id).is_file():
                    file_result(resource.id, "reused", "Histórico local preservado sem consulta remota.")
                    outcomes[resource.source]["reused"] += 1
        selected_missing = None if missing_ids is None else set(missing_ids)
        def should_sync(resource):
            if resource.start > today:
                return False
            installed = self.path(resource.id).is_file()
            if installed:
                return resource.changes(today)
            if selected_missing is not None:
                return resource.id in selected_missing
            return not current_only or resource.changes(today)
        resources = [r for r in resources if should_sync(r)]
        self.completed_sync = False
        processed_resources = 0
        started_sources = set()
        total_resources = len(resources)
        workers = download_workers(settings) if len(resources) > 1 else 1

        diag_lock = Lock()
        def safe_source_start(source):
            with diag_lock:
                if source not in started_sources:
                    started_sources.add(source)
                    try:
                        diagnostics.source_start(source, operation, 'current' if current_only else 'current_and_missing')
                    except (OSError, sqlite3.Error):
                        pass

        def process_one(item_info):
            number, resource = item_info
            if cancelled():
                return
            target = self.path(resource.id)
            installed = target.is_file()
            if installed and not resource.changes(today):
                return
            if progress and workers == 1:
                action = "Verificando atualização" if installed else "Instalando arquivo ausente"
                progress(Progress(f"{action}: {resource.id} · {number + 1}/{total_resources}", number, total_resources))
            def report(message):
                if progress:
                    progress(message if isinstance(message, Progress) else Progress(
                        f"{message} · recurso {number + 1}/{total_resources}", number, total_resources))
            resource_started = perf_counter()
            safe_source_start(resource.source)
            with diag_lock:
                record(resource, "running")
            try:
                outcome = self._sync_one(resource, today, cancelled,
                                         settings.get("request_headers", {}).get(resource.source, {}), report)
                outcome = outcome or ("updated" if installed else "installed")
                with diag_lock:
                    outcomes[resource.source][outcome] += 1
                metadata = json.loads(self.path(resource.id).with_suffix('.json').read_text())
                with diag_lock:
                    record(resource, outcome, metadata=metadata, duration=perf_counter() - resource_started)
                if file_result:
                    file_result(resource.id, outcome)
            except OperationCancelled as error:
                with diag_lock:
                    outcomes[resource.source]['cancelled'] += 1
                    record(resource, 'cancelled', error=error, duration=perf_counter() - resource_started)
                raise
            except (requests.RequestException, OSError, ValueError, csv.Error, sqlite3.Error) as error:
                with diag_lock:
                    outcomes[resource.source]['failed'] += 1
                    record(resource, 'failed', error=error, duration=perf_counter() - resource_started)
                    cached = self.path(resource.id)
                    warnings.append(f"{resource.id}: {safe_error(error)}. " + (
                        "Cache local preservado." if cached.exists() else "Sem cópia local."))
                if file_result:
                    file_result(resource.id, "failed", safe_error(error))
            finally:
                nonlocal processed_resources
                with diag_lock:
                    processed_resources += 1
                    timings.append(f"{resource.id}: {perf_counter() - resource_started:.3f} s")
                if progress:
                    progress(Progress(f"Processados {processed_resources}/{total_resources} arquivos", processed_resources, total_resources))

        to_process = [(n, r) for n, r in enumerate(resources)
                      if not (r.start > today or (current_only and not r.changes(today)) or
                              (self.path(r.id).is_file() and not r.changes(today)))]

        if workers > 1 and len(to_process) > 1:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="energy-sync") as executor:
                futures = [executor.submit(process_one, item) for item in to_process]
                try:
                    for future in futures:
                        check_cancelled(cancelled)
                        future.result()
                except OperationCancelled:
                    executor.shutdown(wait=False, cancel_futures=True)
                    for source, counts in outcomes.items():
                        try:
                            diagnostics.source(source, counts, operation=operation, scope='current' if current_only else 'current_and_missing')
                        except (OSError, sqlite3.Error):
                            pass
                    raise
        else:
            for item in to_process:
                if cancelled():
                    break
                try:
                    process_one(item)
                except OperationCancelled:
                    for source, counts in outcomes.items():
                        try:
                            diagnostics.source(source, counts, operation=operation, scope='current' if current_only else 'current_and_missing')
                        except (OSError, sqlite3.Error):
                            pass
                    raise
        self.completed_sync = processed_resources == len(resources)
        timings.append(f"Total: {perf_counter() - started:.3f} s; falhas: {len(warnings)}")
        try:
            (self.cache / "sync-timing.log").write_text("\n".join(timings) + "\n", encoding="utf-8")
            (self.cache / "sync-errors.log").write_text("\n".join(warnings), encoding="utf-8")
        except OSError:
            print('Diagnóstico degradado: logs da última sincronização indisponíveis.', file=sys.stderr)
        for source, counts in outcomes.items():
            if cancelled() and counts and not self.completed_sync:
                counts['cancelled'] += 1
            try:
                diagnostics.source(source, counts, operation=operation, scope='current' if current_only else 'current_and_missing')
            except (OSError, sqlite3.Error):
                print('Diagnóstico degradado: resumo operacional indisponível.', file=sys.stderr)
        self.warnings = warnings
        if progress:
            progress(Progress("Verificação dos arquivos concluída", 1, 1))
        return warnings

    def _sync_one(self, resource, today, cancelled=lambda: False, request_headers=None, progress=None):
        started = perf_counter()

        def report(message):
            if progress:
                progress(f"{message}: {resource.id}")

        def unchanged():
            report(f"Sem alterações ({format_number(perf_counter() - started, 1)} s)")

        target = self.path(resource.id)
        was_installed = target.is_file()
        metadata_path = target.with_suffix(".json")
        if target.exists() and not resource.changes(today):
            return
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            metadata = {}
        trusted = (target.is_file() and target.stat().st_size > 0 and metadata.get("url_sha256") == hashlib.sha256(resource.url.encode()).hexdigest()
                   and metadata.get("schema_version") == 1
                   and metadata.get("validated_size") == target.stat().st_size
                   and metadata.get("validated_mtime_ns") == target.stat().st_mtime_ns)
        headers = dict(request_headers or {})
        if trusted:
            if metadata.get("etag"):
                headers["If-None-Match"] = metadata["etag"]
            if metadata.get("last_modified"):
                headers["If-Modified-Since"] = metadata["last_modified"]
        # Sem ETag/Last-Modified, o GET baixa uma cópia nova em vez de presumir igualdade.
        with self.session.get(resource.url, headers=headers, stream=True, timeout=(15, 120)) as response:
            check_cancelled(cancelled)
            if response.status_code == 304 and trusted and ("If-None-Match" in headers or "If-Modified-Since" in headers):
                unchanged()
                return "unchanged"
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError(f"Resposta inesperada: HTTP {response.status_code}")
            content_type = str(response.headers.get('Content-Type', '')).lower()
            if any(value in content_type for value in ('text/html', 'application/json', 'application/xml', 'text/xml')):
                raise ValueError('Resposta de erro incompatível com CSV.')
            chunks = iter(response.iter_content(4 * 1024 * 1024))
            first = next(chunks, b'')
            check_cancelled(cancelled)
            if not first or first.lstrip(b'\xef\xbb\xbf \r\n\t')[:1] in (b'<', b'{', b'['):
                raise ValueError('Corpo vazio ou resposta de erro incompatível com CSV.')
            saved_etag = metadata.get("etag") if trusted else None
            remote_etag = response.headers.get("ETag")
            if saved_etag and remote_etag:
                if saved_etag == remote_etag:
                    unchanged()
                    return "unchanged"
            elif trusted and metadata.get("last_modified") and response.headers.get("Last-Modified"):
                try:
                    installed = parsedate_to_datetime(metadata["last_modified"])
                    remote = parsedate_to_datetime(response.headers["Last-Modified"])
                    if remote <= installed:
                        unchanged()
                        return "unchanged"
                except (TypeError, ValueError, OverflowError):
                    pass  # Data não verificável: baixa novamente, conforme a regra de atualização.
            temporary = prepare(self.cache, resource.id)
            digest = hashlib.sha256()
            try:
                total = int(response.headers.get("Content-Length", "0"))
            except (ValueError, TypeError):
                total = 0
            received = 0
            last_report = perf_counter()

            def report_download():
                amount = format_number(received / 1024 ** 2, 1)
                if total > 0:
                    amount += f" / {format_number(total / 1024 ** 2, 1)}"
                if progress and total > 0:
                    progress(Progress(f"Baixando ({amount} MB): {resource.id}", received, total))
                else:
                    report(f"Baixando ({amount} MB)")

            report_download()
            try:
                with temporary.open("wb", buffering=4 * 1024 * 1024) as output:
                    for chunk in chain((first,), chunks):
                        check_cancelled(cancelled)
                        output.write(chunk)
                        digest.update(chunk)
                        received += len(chunk)
                        now = perf_counter()
                        if now - last_report >= 0.25:
                            report_download()
                            last_report = now
                if 'Content-Length' in response.headers and str(response.headers.get('Content-Encoding', 'identity')).lower() in ('', 'identity') and received != total:
                    raise ValueError('Transferência incompleta: comprimento divergente.')
                report_download()
                report("Validando CSV")
                index_csv(temporary, resource.source, cancelled=cancelled, progress=progress, resource=resource)
                import sqlite3
                with closing(sqlite3.connect(temporary.with_suffix('.daily.sqlite'))) as connection:
                    coverage = json.loads(connection.execute('SELECT summary FROM metadata').fetchone()[0])
                check_cancelled(cancelled)
                stamp = utcnow()
                same_content = metadata.get('sha256') == digest.hexdigest()
                content_changed = not target.exists() or (metadata.get('sha256') is not None and not same_content)
                content_stamp = stamp if content_changed else metadata.get('last_content_update_at')
                metadata = {"url_sha256": hashlib.sha256(resource.url.encode()).hexdigest(), "etag": response.headers.get("ETag"),
                            "last_modified": response.headers.get("Last-Modified"),
                            "downloaded_at": datetime.now().astimezone().isoformat(),
                            "sha256": digest.hexdigest(), "validated_size": received, "validated_mtime_ns": temporary.stat().st_mtime_ns,
                            "coverage": coverage, "granularity": resource.granularity,
                            "committed_at": stamp, "last_content_update_at": content_stamp,
                            "operation": getattr(self, 'operation_id', None)}
                report("Instalando CSV")
                check_cancelled(cancelled)
                publish(self.cache, resource.id, temporary, metadata)
                report(f"Arquivo atualizado ({format_number(perf_counter() - started, 1)} s)")
                return 'installed' if not was_installed else ('unchanged' if same_content else ('updated' if content_changed else 'validated'))
            finally:
                cleanup_candidate(temporary)

    def ons_options(self, start, end, progress=None, cancelled=lambda: False):
        """Lê opções reais do período, sem executar uma consulta ou acessar a rede."""
        settings, resources = catalog(self.config, self.cache)
        fields = {"submarkets": "submarket", "states": "state",
                  "plant_types": "plant_type", "fuels": "fuel"}
        options = {key: set() for key in (*fields, "ids", "plants")}
        plant_ids = {}
        resources = [r for r in resources if r.source == "ONS" and r.start <= end and r.end >= start
                     and self.path(r.id).is_file()]
        paths = {r.id: self.path(r.id) for r in resources}
        total_steps = 2 * len(resources)
        def prepare_progress(message, completed):
            if progress:
                progress(Progress(message, completed, total_steps))
        prepare_progress("Verificando índices dos filtros ONS…", 0)
        prepare_indexes([(paths[r.id], r.source) for r in resources],
                        workers=read_workers(settings), cancelled=cancelled, progress=prepare_progress)
        for number, resource in enumerate(resources):
            check_cancelled(cancelled)
            path = paths[resource.id]
            if not path.is_file():
                continue
            def report(message):
                if progress:
                    progress(Progress(f"Filtros {number + 1}/{len(resources)} · {message}",
                                      len(resources) + number, total_steps))
            daily = read_daily(path, "ONS", start, end, progress=report, options=True, cancelled=cancelled)
            for key, column in fields.items():
                options[key].update(value for value in daily[column].unique() if value)
            options["ids"].update(entity[3:] for entity in daily.entity.unique()
                                  if isinstance(entity, str) and entity.startswith("id:"))
            options["plants"].update(value for value in daily.location.unique() if value)
            for row in daily.itertuples(index=False):
                name = row.location
                identifier = row.entity[3:] if isinstance(row.entity, str) and row.entity.startswith("id:") else ""
                if identifier:
                    plant_ids.setdefault(name, set()).add(identifier)
            if progress:
                progress(Progress(f"Filtros: {number + 1}/{len(resources)} arquivos lidos",
                                  len(resources) + number + 1, total_steps))
        result = {key: sorted(values, key=str.casefold) for key, values in options.items()}
        result["plant_ids"] = {name: sorted(ids, key=str.casefold)
                               for name, ids in sorted(plant_ids.items(), key=lambda item: item[0].casefold())}
        return result

    def load(self, query, cancelled=lambda: False, progress=None):
        check_cancelled(cancelled)
        settings, resources = catalog(self.config, self.cache)
        selected = [r for r in resources if r.source == query.source
                    and r.start <= query.end and r.end >= query.start]
        if not selected:
            raise ValueError("Não há fontes configuradas para esse período.")
        cursor = query.start
        for resource in sorted(selected, key=lambda r: r.start):
            if resource.start > cursor:
                raise ValueError("Há um intervalo sem fonte configurada no período solicitado.")
            cursor = max(cursor, resource.end + timedelta(days=1))
        if cursor <= query.end:
            raise ValueError("Configure as fontes restantes do período solicitado.")
        for previous_resource, resource in zip(sorted(selected, key=lambda r: r.start), sorted(selected, key=lambda r: r.start)[1:]):
            if resource.start <= previous_resource.end:
                raise ValueError('Recursos com cobertura sobreposta; resolva o conflito no catálogo.')
        paths = {r.id: self.path(r.id) for r in selected}
        pieces = []
        labels = {}
        entity_codes = {}
        total_steps = 2 * len(selected) + 3
        def report(message, completed):
            if progress:
                progress(Progress(message, completed, total_steps))
        report("Verificando índices locais…", 0)
        plant_name = re.sub(r" \[[^\]]+\]$", "", query.ons_name)
        filters = {"submarket": query.ons_submarket, "state": query.state,
                   "plant_type": query.plant_type, "fuel": query.fuel,
                   "plant_name": plant_name} if query.source == "ONS" else None
        missing = [r for r in selected if not self.query_cache.covers(paths[r.id], r.source,
                   max(query.start, r.start), min(query.end, r.end), query.location, filters)]
        cached = len(selected) - len(missing)
        prepare_indexes([(paths[r.id], r.source) for r in missing],
                        workers=read_workers(settings), cancelled=cancelled,
                        progress=lambda message, completed: report(message, cached + completed))
        for number, resource in enumerate(selected):
            check_cancelled(cancelled)
            path = paths[resource.id]
            if not path.exists():
                raise ValueError(f"Arquivo indisponível: {resource.id}. Verifique a conexão e o link em {self.config}.")
            report(f"Arquivo {number + 1}/{len(selected)} · Lendo {resource.id}", len(selected) + number)
            daily = self.query_cache.read(path, resource.source, max(query.start, resource.start),
                               min(query.end, resource.end), query.location, filters, read_daily,
                               cancelled=cancelled,
                               progress=lambda message: report(f"Arquivo {number + 1}/{len(selected)} · {message}", len(selected) + number))
            if not daily.empty:
                codes, unique = pd.factorize(daily.entity, sort=False)
                mapping = np.fromiter((entity_codes.setdefault(entity, len(entity_codes)) for entity in unique), dtype=np.int64)
                names = daily.loc[~pd.Series(codes, index=daily.index).duplicated()]
                labels.update((entity_codes[entity], label) for entity, label in zip(names.entity, names.location))
                piece = daily[["day", "entity", "sum", "count"]]
                piece['entity'] = mapping[codes]
                pieces.append(piece)
            report(f"Lidos {number + 1}/{len(selected)} arquivos · {count(len(daily))} registros no último arquivo", len(selected) + number + 1)
        if not pieces:
            raise ValueError("Nenhum registro disponível para a consulta.")
        report("Consolidando somas e contagens diárias…", 2 * len(selected))
        combined = pd.concat(pieces, ignore_index=True)
        pieces.clear()
        if combined.duplicated(['day', 'entity']).any():
            totals = combined.groupby(["day", "entity"], sort=False)[["sum", "count"]].sum().reset_index()
        else:
            totals = combined
        del combined
        check_cancelled(cancelled)
        totals["location"] = totals.entity.map(labels)
        totals["value"] = totals["sum"] / totals["count"]
        totals = totals.sort_values(["day", "location"])
        report(f"Calculando variações de {count(len(totals))} registros…", 2 * len(selected) + 1)
        if totals.empty:
            raise ValueError("Nenhum registro disponível para a consulta.")
        from .variation import calculate, REASON_COLUMN
        changes, reasons, previous_positions = calculate(totals.value, totals.entity, return_previous=True)
        frame = pd.DataFrame({COLUMNS[0]: totals.day, COLUMNS[1]: query.source,
                              COLUMNS[2]: totals.location,
                              COLUMNS[3]: totals.value if query.source == "CCEE" else None,
                              COLUMNS[4]: totals.value if query.source == "ONS" else None,
                              COLUMNS[5]: changes})
        frame[REASON_COLUMN] = reasons
        if query.source == "ONS":
            identifiers = {code: entity[3:] if entity.startswith("id:") else "" for entity, code in entity_codes.items()}
            names = {key: re.sub(r" \[[^\]]+\]$", "", name) if identifiers[key] else name
                     for key, name in labels.items()}
            frame[ID_COLUMN] = totals.entity.map(identifiers)
            frame[COLUMNS[2]] = totals.entity.map(names)
        frame.attrs["mode"] = "Dados públicos CCEE/ONS — médias diárias calculadas dos CSV originais"
        frame.attrs['resources'] = [dict(id=r.id, source=r.source, start=r.start.isoformat(), end=r.end.isoformat(), granularity=r.granularity) for r in selected]
        frame.attrs['chart_options'] = settings.get('charts', {})
        frame.attrs['cache'] = str(self.cache.resolve())
        try:
            frame.attrs['diagnostics'] = Diagnostics(self.cache).snapshot(selected, paths)
        except (OSError, sqlite3.Error):
            frame.attrs['diagnostics'] = {'status': 'Diagnóstico indisponível'}
        frame[ALERT_COLUMN] = alert_cells(frame, query).sum(axis=1)
        report("Identificando alertas e preparando a primeira página…", 2 * len(selected) + 2)
        result = frame, AlertCollection(frame, query, cancelled=cancelled, previous=previous_positions)
        check_cancelled(cancelled)
        report(f"Consulta concluída: {count(len(frame))} registros e {count(len(result[1]))} alertas", total_steps)
        return result


class CSVChunks:
    def __init__(self, path):
        self.iterator = _read_csv_chunks(path)

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.iterator)

    def close(self):
        self.iterator.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def read_csv(path):
    return CSVChunks(path)


def _read_csv_chunks(path):
    with Path(path).open('r', encoding='utf-8-sig', newline='') as stream:
        reader = csv.reader(stream, delimiter=';', strict=True)
        headers = next(reader, None)
        if not headers:
            raise ValueError('CSV vazio.')
        headers = [value.strip() for value in headers]
        if len(set(headers)) != len(headers) or any(not value for value in headers):
            raise ValueError('Cabeçalho CSV contém colunas duplicadas ou vazias.')
    # Only the header is inspected in Python; the C parser reads data in blocks.
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter('error', pd.errors.ParserWarning)
        with pd.read_csv(path, sep=';', encoding='utf-8-sig', dtype=str,
                         chunksize=250_000, keep_default_na=False, na_values=[''],
                         on_bad_lines='error', index_col=False, engine='c') as reader:
            for chunk in reader:
                chunk.columns = headers
                yield chunk


def validate_csv(path, source):
    found = False
    reader = read_csv(path)
    try:
        for chunk in reader:
            normalized = normalize(chunk, source)
            found = found or not normalized.empty
        if not found:
            raise ValueError('CSV sem medições válidas.')
    finally:
        reader.close()


def ons_labels(raw):
    """Keep plant names readable and distinguish plants by their ONS identifier."""
    names = raw.nom_usina.str.strip()
    if "id_ons" not in raw:
        return names
    identifiers = raw.id_ons.fillna("").str.strip()
    fallback = names.copy()
    for column in ("nom_estado", "nom_tipousina", "nom_tipocombustivel"):
        if column in raw:
            value = raw[column].fillna("").str.strip()
            fallback += value.map(lambda text: f" | {text}" if text else "")
    return (names + " [" + identifiers + "]").where(identifiers.ne(""), fallback)


def normalize(raw, source, resource=None):
    raw.columns = raw.columns.str.strip()
    if source == "ONS":
        required = {"din_instante", "nom_usina", "val_geracao"}
        if not required.issubset(raw.columns):
            raise ValueError("CSV ONS sem as colunas din_instante, nom_usina e val_geracao.")
        days = pd.to_datetime(raw.din_instante, format="ISO8601", errors="raise").dt.date
        locations = ons_labels(raw)
        if "id_ons" in raw:
            identifiers = raw.id_ons.fillna("").str.strip()
            entities = ("id:" + identifiers).where(identifiers.ne(""), "group:" + locations)
        else:
            entities = locations
        values = raw.val_geracao
    else:
        required = {"DIA", "SUBMERCADO", "PLD_HORA"}
        if not required.issubset(raw.columns):
            raise ValueError("CSV CCEE sem as colunas DIA, SUBMERCADO e PLD_HORA; confira o esquema publicado.")
        if raw.DIA.str.fullmatch(r"\d{1,2}").all():
            if "MES_REFERENCIA" not in raw:
                raise ValueError("CSV CCEE sem MES_REFERENCIA.")
            days = pd.to_datetime(raw.MES_REFERENCIA.str[:6] + raw.DIA.str.zfill(2), format="%Y%m%d").dt.date
        else:
            days = pd.to_datetime(raw.DIA, format="%d/%m/%Y").dt.date
        if "MES_REFERENCIA" in raw and not raw.MES_REFERENCIA.fillna('').eq('').all():
            expected = pd.Series(days, index=raw.index).map(lambda value: value.strftime('%Y%m'))
            if not raw.MES_REFERENCIA.str[:6].eq(expected).all():
                raise ValueError('DIA diverge de MES_REFERENCIA.')
        if 'HORA' in raw:
            hours = pd.to_numeric(raw.HORA, errors='raise')
            if hours.isna().any() or not hours.between(0, 23).all() or not hours.eq(hours.astype(int)).all():
                raise ValueError('HORA inválida para a série horária.')
        mapping = {"SUDESTE": SUBMARKETS[0], "SE": SUBMARKETS[0], "SE/CO": SUBMARKETS[0],
                   "SUDESTE/CENTRO-OESTE": SUBMARKETS[0], "SUL": "Sul", "S": "Sul",
                   "NORDESTE": "Nordeste", "NE": "Nordeste", "NORTE": "Norte", "N": "Norte"}
        locations = raw.SUBMERCADO.str.strip().str.upper().map(mapping)
        if locations.isna().any():
            raise ValueError("CSV CCEE com submercado desconhecido.")
        values = raw.PLD_HORA
        entities = locations
    if days.isna().any():
        raise ValueError("CSV contém datas ausentes.")
    if resource is not None and (days.lt(resource.start).any() or days.gt(resource.end).any()):
        raise ValueError('Datas fora da cobertura declarada do recurso.')
    numbers = pd.to_numeric(values.str.strip().map(
        lambda value: value.replace(".", "").replace(",", ".") if "," in value else value,
        na_action="ignore"), errors="raise")
    if not numbers.dropna().map(lambda v: float('-inf') < v < float('inf')).all():
        raise ValueError("CSV contém valores não finitos.")
    if locations.isna().any() or locations.str.strip().eq("").any():
        raise ValueError("CSV contém entidade ausente.")
    # Ausência de medição não equivale a zero e não participa da média.
    return pd.DataFrame({"day": days, "entity": entities, "location": locations, "value": numbers}).dropna(subset=["value"])
