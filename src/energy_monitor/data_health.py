"""User-facing data health; technical operational records remain in diagnostics."""
from contextlib import closing
from datetime import date, datetime, timedelta
import json
import sqlite3
from zoneinfo import ZoneInfo

from .data_sources import catalog
from .diagnostics import Diagnostics


def health_report(store):
    settings, resources = catalog(store.config, store.cache)
    state = Diagnostics(store.cache).status(reconcile=False)
    result = []
    zone = ZoneInfo(settings['timezone'])

    def stamp(value):
        if not value:
            return 'Ainda não registrado'
        try:
            return datetime.fromisoformat(value).astimezone(zone).strftime('%d/%m/%Y às %H:%M')
        except (ValueError, TypeError):
            return 'Ainda não registrado'

    for source in ('CCEE', 'ONS'):
        operational = state['sources'].get(source, {})
        entries = [value for value in state['resources'].values() if value.get('source') == source]
        changed = max((value.get('last_content_update_at') or '' for value in entries), default='')
        spans, missing_values, duplicates = [], 0, 0
        unchecked, unavailable, failures = 0, 0, 0
        for resource in resources:
            if resource.source != source:
                continue
            try:
                path = store.path(resource.id)
                if not path.is_file():
                    if resource.start <= datetime.now(zone).date():
                        unavailable += 1
                    continue
                coverage = None
                metadata_path = path.with_suffix('.json')
                if metadata_path.is_file():
                    metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
                    coverage = metadata.get('coverage')
                    changed = max(changed, metadata.get('last_content_update_at') or '')
                index = path.with_suffix('.daily.sqlite')
                if coverage is None and index.is_file():
                    with closing(sqlite3.connect(f'{index.resolve().as_uri()}?mode=ro', uri=True)) as db:
                        columns = {row[1] for row in db.execute('PRAGMA table_info(metadata)')}
                        if 'summary' in columns:
                            row = db.execute('SELECT summary FROM metadata').fetchone()
                            coverage = json.loads(row[0]) if row and row[0] else None
                        if not coverage:
                            first = db.execute('SELECT day FROM daily ORDER BY day LIMIT 1').fetchone()
                            last = db.execute('SELECT day FROM daily ORDER BY day DESC LIMIT 1').fetchone()
                            coverage = {'first': first[0] if first else None, 'last': last[0] if last else None}
                if coverage and coverage.get('first') and coverage.get('last'):
                    spans.append((date.fromisoformat(coverage['first']), date.fromisoformat(coverage['last'])))
                if coverage and 'missing' in coverage:
                    missing_values += coverage['missing']
                    duplicates += coverage.get('duplicates', 0)
                else:
                    unchecked += 1
            except (OSError, ValueError, KeyError, sqlite3.Error):
                failures += 1
        problems = []
        if missing_values:
            label = 'registro com valor ausente' if missing_values == 1 else 'registros com valores ausentes'
            problems.append(f'{missing_values:,} {label}. Esses valores não entram nas médias.'.replace(',', '.'))
        if duplicates:
            problems.append(f'{duplicates:,} registros repetidos foram considerados apenas uma vez.'.replace(',', '.'))
        if unavailable:
            problems.append('Parte do histórico ainda não está disponível neste computador.')
        if unchecked:
            problems.append('A conferência de valores ausentes ainda não cobre todo o período.')
        if failures:
            problems.append('Não foi possível conferir uma parte dos dados locais.')
        cursor = None
        for first, last in sorted(spans):
            if cursor is not None and first > cursor + timedelta(days=1):
                problems.append('Há intervalos sem dados entre os períodos disponíveis.')
                break
            cursor = max(cursor, last) if cursor else last
        current = operational.get('result')
        if current == 'failed':
            status = 'Erro'
            problems.insert(0, 'A última verificação não foi concluída. Os dados anteriores continuam disponíveis quando existentes.')
        elif current in ('partial', 'interrupted', 'running') or problems or not current:
            status = 'Aviso'
            if current == 'partial':
                problems.insert(0, 'A última verificação foi concluída apenas parcialmente.')
            elif current == 'interrupted':
                problems.insert(0, 'A última verificação foi interrompida.')
            elif current == 'running':
                problems.insert(0, 'A verificação dos dados está em andamento.')
            elif not current:
                problems.insert(0, 'Ainda não há uma verificação registrada para esta fonte.')
        else:
            counts = operational.get('counts', {})
            status = 'Atualizado' if counts.get('installed', 0) + counts.get('updated', 0) else 'Sem mudanças'
            if counts.get('validated'):
                status = 'Aviso'
                problems.append('Os dados foram conferidos, mas ainda não há registro que permita confirmar quando o conteúdo mudou.')
        period = (f'{min(first for first, _ in spans):%d/%m/%Y} a {max(last for _, last in spans):%d/%m/%Y}'
                  if spans else 'Ainda não confirmado')
        result.append({'source': source, 'status': status,
                       'last_check': stamp(operational.get('finished_at') or operational.get('last_attempt_at')),
                       'last_update': stamp(changed), 'period': period,
                       'problems': problems or ['Nenhum problema identificado nas verificações disponíveis.']})
    return result


def show_health(parent, report, timezone_name='America/Sao_Paulo'):
    import tkinter as tk
    from tkinter.scrolledtext import ScrolledText
    window = tk.Toplevel(parent)
    window.title('Situação dos dados')
    window.transient(parent)
    window.configure(background='#F0F0F0', padx=16, pady=16)
    window.geometry('720x590')
    window.minsize(520, 400)
    window.columnconfigure(0, weight=1)
    window.rowconfigure(1, weight=1)
    tk.Label(window, text='Situação dos dados', anchor='w', background='#F0F0F0',
             foreground='#330072', font=('Arial', 17, 'bold')).grid(row=0, column=0, sticky='ew', pady=(0, 12))
    text = ScrolledText(window, wrap='word', font=('Arial', 11), padx=16, pady=12,
                        background='white', foreground='#252525', borderwidth=0)
    text.grid(row=1, column=0, sticky='nsew')
    text.tag_configure('source', font=('Arial', 15, 'bold'), foreground='#330072', spacing1=12, spacing3=8)
    text.tag_configure('label', font=('Arial', 11, 'bold'))
    for status, color in [('Atualizado', '#176B3A'), ('Sem mudanças', '#176B3A'), ('Aviso', '#815200'), ('Erro', '#B42318')]:
        text.tag_configure(status, font=('Arial', 12, 'bold'), foreground=color)
    for item in report:
        text.insert('end', item['source'] + '\n', 'source')
        text.insert('end', item['status'] + '\n\n', item['status'])
        for label, key in [('Última verificação', 'last_check'), ('Última atualização dos dados', 'last_update'), ('Período disponível', 'period')]:
            text.insert('end', label + ': ', 'label')
            text.insert('end', item[key] + '\n')
        text.insert('end', '\nProblemas encontrados\n', 'label')
        for problem in item['problems']:
            text.insert('end', '• ' + problem + '\n')
        text.insert('end', '\n')
    text.configure(state='disabled')
    tk.Label(window, text=f'Horários em {timezone_name}', background='#F0F0F0', foreground='#555555').grid(row=2, column=0, sticky='w', pady=(8, 0))
    tk.Button(window, text='Fechar', command=window.destroy, padx=18, pady=4).grid(row=3, column=0, sticky='e', pady=(8, 0))
    window.bind('<Escape>', lambda event: window.destroy())
    return window
