"""Official CKAN discovery, persisted separately from user configuration."""
from calendar import monthrange
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import os
from tempfile import NamedTemporaryFile

from .cancellation import check_cancelled
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

ENDPOINTS = {
    'ONS': ('https://dados.ons.org.br/api/3/action/package_show', 'geracao-usina-2'),
    'CCEE': ('https://dadosabertos.ccee.org.br/api/3/action/package_show', 'pld_horario'),
}


def identity(resource):
    return tuple(resource[key] for key in ('source', 'start', 'end'))


def _named_coverage(text, source):
    text = re.sub(r"[\s-]+", "_", text.lower())
    years = re.findall(r"(?<!\d)(?:19|20)\d{2}(?!\d)", text)
    if not years:
        return None
    year = int(years[0])
    if source == 'CCEE':
        if len(years) > 1 and 'semanal' not in text:
            raise ValueError('Cobertura ambígua')
        return date(year, 1, 1), date(int(years[-1]) if 'semanal' in text else year, 12, 31)
    match = re.search(r'((?:19|20)\d{2})_(\d{2})(?!\d)', text)
    if match:
        month = int(match[2])
        return date(year, month, 1), date(year, month, monthrange(year, month)[1])
    if len(years) != 1:
        raise ValueError('Cobertura ambígua')
    return date(year, 1, 1), date(year, 12, 31)


def candidates(source, payload, dataset):
    if not isinstance(payload, dict) or payload.get('success') is not True:
        raise ValueError('Resposta CKAN inválida')
    result = payload.get('result')
    if not isinstance(result, dict) or result.get('name') != dataset:
        raise ValueError('Conjunto CKAN inesperado')
    resources = result.get('resources')
    if not isinstance(resources, list) or not resources:
        raise ValueError('Catálogo vazio ou inválido')
    accepted, diagnostics = [], []
    for item in resources:
        if not isinstance(item, dict):
            raise ValueError('Recurso CKAN inválido')
        name, url = item.get('name', ''), item.get('url', '')
        formats = {str(item.get(key, '')).strip().lower() for key in ('format', 'mimetype')}
        if not formats.intersection({'csv', 'text/csv', 'application/csv'}):
            continue
        if not isinstance(name, str) or not isinstance(url, str):
            diagnostics.append('Metadados de recurso inválidos')
            continue
        text = (name + ' ' + urlsplit(url).path).lower()
        if 'dicion' in text:
            continue
        if (source == 'ONS' and not re.search(r'gera[cç][aã]o.*usina', text)) or (source == 'CCEE' and 'pld' not in text):
            continue
        if not url.startswith('https://') or not isinstance(item.get('id'), str) or not item['id']:
            diagnostics.append('Identidade ou URL inválida')
            continue
        try:
            named = _named_coverage(name, source)
            # Opaque URLs carry no coverage evidence. Parse only recognizable series paths.
            path = urlsplit(url).path
            from_url = _named_coverage(path, source) if ('pld' in path.lower() or 'usina' in path.lower()) else None
            explicit_start = item.get('temporal_start') or item.get('coverage_start')
            explicit_end = item.get('temporal_end') or item.get('coverage_end')
            explicit = None
            if explicit_start or explicit_end:
                explicit = date.fromisoformat(explicit_start), date.fromisoformat(explicit_end)
            evidence = [value for value in (explicit, named, from_url) if value is not None]
            if not evidence or any(value != evidence[0] for value in evidence):
                raise ValueError('Cobertura ausente ou divergente')
            start, end = evidence[0]
            if start > end:
                raise ValueError('Cobertura invertida')
        except (ValueError, TypeError, OverflowError):
            diagnostics.append('Cobertura inválida ou ambígua: ' + item['id'])
            continue
        accepted.append(dict(id='auto_' + source.lower() + '_' + hashlib.sha256(
            f'{source}:{start}:{end}'.encode()).hexdigest()[:20], source=source,
            start=start.isoformat(), end=end.isoformat(), url=url,
            mutable='current_year' if source == 'CCEE' else 'current_month',
            provenance=dict(dataset=dataset, official_id=item['id'], name=name,
                            granularity='weekly' if 'semanal' in text else 'hourly')))
    if not accepted:
        raise ValueError('Nenhum recurso de dados reconhecido')
    return accepted, diagnostics


def read_state(cache):
    path = Path(cache) / 'discovered-sources.json'
    try:
        state = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(state, dict) or not isinstance(state.get('resources'), list):
            raise ValueError('Estado de descoberta inválido')
        if not isinstance(state.get('providers', {}), dict):
            raise ValueError('Provedores inválidos no estado de descoberta')
        if any(not isinstance(value, dict) for value in state.get('providers', {}).values()):
            raise ValueError('Estado de provedor inválido')
        for item in state['resources']:
            if not isinstance(item, dict) or not all(isinstance(item.get(key), str) for key in ('id', 'source', 'start', 'end', 'url')):
                raise ValueError('Recurso inválido no estado de descoberta')
            if item['source'] not in ENDPOINTS or not re.fullmatch(r'[A-Za-z0-9_-]+', item['id']) or not item['url'].startswith('https://'):
                raise ValueError('Identidade inválida no estado de descoberta')
            if item.get('mutable') not in (True, False, 'current_year', 'current_month') or not isinstance(item.get('provenance', {}), dict):
                raise ValueError('Política ou proveniência inválida')
            if set(item) - {'id', 'source', 'start', 'end', 'url', 'mutable', 'provenance', 'granularity'}:
                raise ValueError('Campos desconhecidos no estado')
            aliases = item.get('provenance', {}).get('aliases', [])
            if not isinstance(aliases, list) or not all(isinstance(value, str) for value in aliases):
                raise ValueError('Aliases inválidos')
            if date.fromisoformat(item['start']) > date.fromisoformat(item['end']):
                raise ValueError('Cobertura inválida no estado de descoberta')
        return state
    except FileNotFoundError:
        return {'resources': [], 'providers': {}}
    except (ValueError, TypeError, KeyError):
        return {'resources': [], 'providers': {}, 'diagnostics': ['Estado automático inválido; configuração preservada.']}


def merge(configured, discovered, exclusions=()):
    chosen = {identity(item): dict(item) for item in configured}
    identifiers = {item['id'] for item in configured}
    conflicts = []
    for item in sorted(discovered, key=lambda value: (identity(value), value['url'])):
        if ({item['id'], item.get('provenance', {}).get('official_id')} | set(item.get('provenance', {}).get('aliases', []))).intersection(exclusions):
            continue
        key = identity(item)
        if key in chosen:
            if chosen[key]['url'] != item['url']:
                conflicts.append(f'Cobertura com URLs distintas: {key}')
            elif 'provenance' in chosen[key]:
                provenance = dict(chosen[key]['provenance'])
                aliases = set(provenance.get('aliases', []))
                official = item.get('provenance', {}).get('official_id')
                if official:
                    aliases.add(official)
                if provenance.get('official_id'):
                    aliases.add(provenance['official_id'])
                provenance['aliases'] = sorted(aliases)
                chosen[key]['provenance'] = provenance
            continue
        if item['id'] in identifiers:
            conflicts.append('ID local já utilizado: ' + item['id'])
            continue
        if any(other['url'] == item['url'] and identity(other) != key for other in chosen.values()):
            conflicts.append('URL compartilhada por coberturas distintas')
            continue
        peers = {other['url'] for other in discovered if identity(other) == key}
        if len(peers) > 1:
            conflicts.append(f'Cobertura ambígua: {key}')
            continue
        chosen[key] = dict(item)
        identifiers.add(item['id'])
    return list(chosen.values()), conflicts


def discover(store, *, dry_run=False, force=False, cancelled=lambda: False, progress=None, clock=None):
    settings = json.loads(store.config.read_text(encoding='utf-8'))
    state = read_state(store.cache)
    diagnostics = [message for message in state.get('diagnostics', []) if message.startswith('Estado automático inválido')]
    now = clock() if clock else datetime.now(timezone.utc)
    for source, (endpoint, dataset) in ENDPOINTS.items():
        check_cancelled(cancelled)
        options = settings.get('discovery', {}).get(source, {})
        if options.get('enabled', True) is False:
            continue
        previous = state.setdefault('providers', {}).get(source, {})
        local = now.astimezone(ZoneInfo(settings.get('timezone', 'America/Sao_Paulo')))
        period = local.strftime('%Y-%m' if source == 'ONS' else '%Y')
        if not force and previous.get('attempted') and previous.get('period') == period:
            try:
                if (now - datetime.fromisoformat(previous['attempted'])).total_seconds() < 3600:
                    continue
            except (ValueError, TypeError):
                pass
        if progress:
            progress('Descobrindo catálogo ' + source)
        try:
            endpoint = options.get('endpoint', endpoint)
            if not isinstance(endpoint, str) or not endpoint.startswith('https://'):
                raise ValueError('Endpoint de catálogo inválido')
            headers = dict(settings.get('request_headers', {}).get(source, {}))
            if previous.get('endpoint') == endpoint:
                if previous.get('etag'):
                    headers['If-None-Match'] = previous['etag']
                if previous.get('last_modified'):
                    headers['If-Modified-Since'] = previous['last_modified']
            response = store.session.get(endpoint, params={'id': dataset}, headers=headers, timeout=(10, 30))
            if response.status_code == 304:
                if (not any(item['source'] == source for item in state['resources'])
                        or not any(key in headers for key in ('If-None-Match', 'If-Modified-Since'))):
                    raise ValueError('HTTP 304 sem catálogo anterior')
                state['providers'][source] = previous | {'attempted': now.isoformat(), 'period': period, 'result': 'unchanged'}
                response.close()
                continue
            response.raise_for_status()
            try:
                found, ignored = candidates(source, response.json(), dataset)
            finally:
                response.close()
            check_cancelled(cancelled)
            merged, conflicts = merge(state['resources'], found)
            state['resources'] = merged
            diagnostics.extend(ignored + conflicts)
            state['providers'][source] = {'attempted': now.isoformat(), 'period': period, 'result': 'success', 'count': len(found), 'endpoint': endpoint, 'etag': response.headers.get('ETag'), 'last_modified': response.headers.get('Last-Modified')}
        except (ValueError, OSError) as error:
            diagnostics.append(source + ': descoberta indisponível (' + type(error).__name__ + ')')
            state['providers'][source] = previous | {'attempted': now.isoformat(), 'period': period, 'result': 'failed'}
        except Exception as error:
            from .cancellation import OperationCancelled
            if isinstance(error, OperationCancelled):
                raise
            import requests
            if not isinstance(error, requests.RequestException):
                raise
            diagnostics.append(source + ': descoberta indisponível (' + type(error).__name__ + ')')
            state['providers'][source] = previous | {'attempted': now.isoformat(), 'period': period, 'result': 'failed'}
    effective, conflicts = merge(settings['resources'], state['resources'], settings.get('excluded_resources', []))
    diagnostics.extend(conflicts)
    state['diagnostics'] = diagnostics
    if not dry_run:
        from .generations import atomic_json
        store.cache.mkdir(parents=True, exist_ok=True)
        check_cancelled(cancelled)
        atomic_json(store.cache / 'discovered-sources.json', state)
    return {'resources': effective, 'diagnostics': diagnostics, 'providers': state['providers']}
