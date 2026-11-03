"""Persistent operational state and coordinated, sanitized rotating event logs."""
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from time import monotonic, sleep
from uuid import uuid4
from zoneinfo import ZoneInfo


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def sanitize(text):
    text = str(text)
    text = re.sub(r'https?://[^\s<>"\']+', '[URL removida]', text, flags=re.I)
    text = re.sub(r'(?i)\b(authorization|cookie|password|passwd|token|secret|api[_-]?key)\b\s*[:=]\s*[^\s,;]+', r'\1=[removido]', text)
    text = re.sub(r'(?i)\bBearer\s+\S+', 'Bearer [removido]', text)
    return text[:2000]


def safe_error(error):
    # Library exception messages may embed response bodies, cells, URLs, or credentials.
    response = getattr(error, 'response', None)
    status = getattr(response, 'status_code', None)
    if isinstance(status, int):
        return f'HTTP {status}: consulta não concluída; cópia anterior preservada.'
    message = str(error)
    prefixes = ('CSV ', 'Datas fora ', 'DIA diverge', 'HORA inválida', 'Observações conflitantes',
                'Transferência incompleta', 'Resposta de erro', 'Corpo vazio', 'Índice candidato',
                'Referência de geração', 'Geração ativa', 'Configure ', 'Não há fontes',
                'Há um intervalo', 'Recursos com cobertura', 'Arquivo indisponível', 'Operação cancelada')
    if isinstance(error, ValueError) and message.startswith(prefixes):
        return sanitize(message)
    return f'{type(error).__name__}: operação não concluída; dados anteriores preservados quando disponíveis.'


def sanitize_data(value):
    if isinstance(value, dict):
        return {key: sanitize_data(item) for key, item in value.items()
                if key.lower() not in ('url', 'headers', 'authorization', 'cookie', 'password', 'token', 'secret')}
    if isinstance(value, (list, tuple)):
        return [sanitize_data(item) for item in value]
    return sanitize(value) if isinstance(value, str) else value


class Diagnostics:
    def __init__(self, cache, *, clock=utcnow, max_bytes=10*1024*1024, backups=5):
        self.cache = Path(cache)
        self.clock = clock
        if not isinstance(max_bytes, int) or max_bytes < 256 or not isinstance(backups, int) or not 1 <= backups <= 20:
            raise ValueError('Configure limites válidos de rotação.')
        self.max_bytes, self.backups = max_bytes, backups
        self.path = self.cache / 'diagnostics.sqlite'

    def connect(self):
        self.cache.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.executescript('CREATE TABLE IF NOT EXISTS resources (id TEXT PRIMARY KEY, source TEXT, payload TEXT);'
                                 'CREATE TABLE IF NOT EXISTS sources (source TEXT PRIMARY KEY, payload TEXT);')
        return connection

    def event(self, **data):
        from .updater import UpdateLock
        event = sanitize_data({'timestamp': self.clock(), **data})
        line = json.dumps(event, ensure_ascii=False) + '\n'
        lock = UpdateLock(self.cache / '.log-lock')
        started = monotonic()
        try:
            while not lock.acquire():
                if monotonic() - started > 2:
                    raise OSError('Log ocupado')
                sleep(.01)
            target = self.cache / 'events.jsonl'
            if target.exists() and target.stat().st_size + len(line.encode()) > self.max_bytes:
                (self.cache / f'events.jsonl.{self.backups}').unlink(missing_ok=True)
                for index in range(self.backups - 1, 0, -1):
                    previous = self.cache / f'events.jsonl.{index}'
                    if previous.exists():
                        os.replace(previous, self.cache / f'events.jsonl.{index+1}')
                os.replace(target, self.cache / 'events.jsonl.1')
            with target.open('a', encoding='utf-8') as output:
                output.write(line)
        except OSError:
            print('Diagnóstico degradado: não foi possível gravar o log operacional.', file=sys.stderr)
        finally:
            lock.release()

    def resource(self, resource, result, *, metadata=None, operation=None, error=None, duration=None):
        now = self.clock()
        with closing(self.connect()) as db, db:
            row = db.execute('SELECT payload FROM resources WHERE id=?', (resource.id,)).fetchone()
            state = json.loads(row[0]) if row else {'last_attempt_at': None, 'last_successful_check_at': None,
                                                   'last_content_update_at': None, 'coverage': None}
            started = now if result == 'running' else state.get('last_operation', {}).get('started_at')
            state.update(resource=resource.id, source=resource.source, result=result,
                         last_operation={'id': operation, 'result': result, 'at': now, 'started_at': started,
                                         'duration_seconds': duration, 'stage': 'check' if result == 'running' else 'sync_finished'})
            if result == 'running':
                state['last_attempt_at'] = now
            elif result in ('installed', 'updated', 'unchanged', 'validated'):
                state['last_successful_check_at'] = now
                state['last_error'] = None
                if metadata:
                    state.update(generation=metadata.get('generation'), coverage=metadata.get('coverage'),
                                 last_content_update_at=metadata.get('last_content_update_at'))
            elif result in ('failed', 'cancelled'):
                state['last_error'] = {'code': type(error).__name__, 'message': safe_error(error)}
            db.execute('INSERT OR REPLACE INTO resources VALUES (?, ?, ?)',
                       (resource.id, resource.source, json.dumps(sanitize_data(state), ensure_ascii=False)))
        self.event(resource=resource.id, source=resource.source, operation=operation, result=result,
                   error=safe_error(error) if error else None, generation=(metadata or {}).get('generation'))

    def source_start(self, source, operation, scope):
        with closing(self.connect()) as db, db:
            row = db.execute('SELECT payload FROM sources WHERE source=?', (source,)).fetchone()
            state = json.loads(row[0]) if row else {}
            state.update(source=source, last_attempt_at=self.clock(), result='running', operation=operation, scope=scope)
            db.execute('INSERT OR REPLACE INTO sources VALUES (?, ?)', (source, json.dumps(state)))

    def recover(self):
        if not self.path.exists():
            return
        state = self.status()
        with closing(self.connect()) as db, db:
            for identifier, resource in state['resources'].items():
                if resource.get('result') in ('running', 'committed'):
                    if resource['result'] == 'running':
                        resource['result'] = 'interrupted'
                    db.execute('UPDATE resources SET payload=? WHERE id=?', (json.dumps(resource), identifier))
            for source, item in state['sources'].items():
                if item.get('result') == 'running':
                    item['result'] = 'interrupted'
                    db.execute('UPDATE sources SET payload=? WHERE source=?', (json.dumps(item), source))

    def source(self, source, counts, *, operation, scope):
        now = self.clock()
        attempted = sum(counts.get(key, 0) for key in ('installed', 'updated', 'unchanged', 'validated', 'failed', 'cancelled'))
        if not attempted:
            return
        failures = counts.get('failed', 0) + counts.get('cancelled', 0)
        result = 'partial' if failures and attempted > failures else ('failed' if failures else 'success')
        with closing(self.connect()) as db, db:
            row = db.execute('SELECT payload FROM sources WHERE source=?', (source,)).fetchone()
            state = json.loads(row[0]) if row else {}
            state.update(source=source, last_attempt_at=state.get('last_attempt_at', now), finished_at=now, result=result, counts=counts, scope=scope, operation=operation)
            if not failures:
                state['last_successful_check_at'] = now
            db.execute('INSERT OR REPLACE INTO sources VALUES (?, ?)', (source, json.dumps(state)))
        self.event(source=source, operation=operation, result=result, counts=counts, scope=scope)

    def status(self, *, resource_ids=None, reconcile=True):
        if not self.path.exists():
            return {'resources': {}, 'sources': {}}
        with closing(sqlite3.connect(f'{self.path.resolve().as_uri()}?mode=ro', uri=True)) as db:
            if resource_ids is None:
                rows = db.execute('SELECT id,payload FROM resources')
            elif resource_ids:
                identifiers = tuple(resource_ids)
                rows = db.execute('SELECT id,payload FROM resources WHERE id IN (' + ','.join('?' for _ in identifiers) + ')', identifiers)
            else:
                rows = []
            resources = {row[0]: json.loads(row[1]) for row in rows}
            sources = {row[0]: json.loads(row[1]) for row in db.execute('SELECT source,payload FROM sources')}
        from .generations import active_path
        for identifier, state in (resources.items() if reconcile else ()):

            try:
                metadata = json.loads(active_path(self.cache, identifier).with_suffix('.json').read_text())
            except (OSError, ValueError, KeyError):
                continue
            if metadata.get('generation') and metadata.get('generation') != state.get('generation'):
                state.update(generation=metadata['generation'], coverage=metadata.get('coverage'),
                             last_content_update_at=metadata.get('last_content_update_at'))
                if metadata.get('operation') == state.get('last_operation', {}).get('id'):
                    state['last_successful_check_at'] = metadata.get('committed_at')
                    if state.get('result') == 'running':
                        state['result'] = 'committed'
        return sanitize_data({'resources': resources, 'sources': sources})

    def snapshot(self, resources, paths):
        states = self.status(resource_ids={resource.id for resource in resources}, reconcile=False)['resources']
        result = {}
        for resource in resources:
            try:
                metadata = json.loads(paths[resource.id].with_suffix('.json').read_text())
            except (OSError, ValueError):
                metadata = {}
            state = states.get(resource.id, {})
            if state.get('generation') != metadata.get('generation'):
                state = {}
            result[resource.id] = state | {'generation': metadata.get('generation'),
                'coverage': metadata.get('coverage'), 'last_content_update_at': metadata.get('last_content_update_at'),
                'declared_start': resource.start.isoformat(), 'declared_end': resource.end.isoformat()}
        return sanitize_data(result)


def summary(status, timezone_name='America/Sao_Paulo'):
    def stamp(value):
        return datetime.fromisoformat(value).astimezone(ZoneInfo(timezone_name)).strftime('%d/%m %H:%M') if value else None
    parts = []
    labels = {'success': 'verificação concluída', 'partial': 'verificação parcial',
              'failed': 'falha na verificação', 'running': 'verificação em andamento',
              'interrupted': 'verificação interrompida'}
    for source in ('CCEE', 'ONS'):
        state = status.get('sources', {}).get(source, {})
        result = state.get('result')
        value = state.get('last_successful_check_at') if result == 'success' else state.get('last_attempt_at')
        when = stamp(value)
        message = labels.get(result, 'sem verificação registrada')
        parts.append(f"{source}: {message}" + (f" · {when}" if when else ''))
    return '\n'.join(parts)
