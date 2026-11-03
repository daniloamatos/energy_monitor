"""Bounded-memory XLSX export with cancellation and atomic publication."""
from datetime import date, datetime
from itertools import islice
import json
import os
from pathlib import Path
from time import perf_counter
from tempfile import NamedTemporaryFile, SpooledTemporaryFile
from zipfile import ZipFile, ZIP_DEFLATED
from openpyxl.writer.excel import ExcelWriter

import pandas as pd
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.cell.cell import ERROR_CODES
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.worksheet.table import Table, TableStyleInfo
from openpyxl.worksheet.worksheet import Worksheet
from openpyxl.utils import get_column_letter

from .cancellation import check_cancelled
from .progress import Progress

EXCEL_ROWS = 1_048_576
HEADER_FONT = Font(color='F0F0F0', bold=True)
HEADER_FILL = PatternFill('solid', fgColor='330072')


class CancellableOutput:
    def __init__(self, stream, cancelled):
        self.stream, self.cancelled = stream, cancelled

    def write(self, data):
        check_cancelled(self.cancelled)
        return self.stream.write(data)

    def __getattr__(self, key):
        return getattr(self.stream, key)


class LongTexts:
    def __init__(self, cancelled):
        self.stream = SpooledTemporaryFile(max_size=64 * 1024, mode='w+t', encoding='utf-8')
        self.count = 0
        self.rows = 0
        self.cancelled = cancelled

    def add(self, text, origin):
        self.count += 1
        reference = f'T{self.count:06d}'
        offset, part = 0, 0
        while offset < len(text):
            check_cancelled(self.cancelled)
            end = min(len(text), offset + 16000)
            # Stay within Excel's line-feed limit as well as UTF-16 cell capacity.
            boundary = offset
            for _ in range(253):
                boundary = text.find('\n', boundary, end)
                if boundary < 0:
                    break
                boundary += 1
            else:
                end = boundary
            part += 1
            self.stream.write(json.dumps([reference, origin, part, text[offset:end]], ensure_ascii=False) + '\n')
            self.rows += 1
            offset = end
        return f'Texto completo nas abas Textos_Completos. Referência: {reference}.'

    def __iter__(self):
        self.stream.seek(0)
        for line in self.stream:
            yield json.loads(line)


def append_row(sheet, values, *, header=False):
    cells = []
    styles = getattr(sheet.parent, '_energy_formats', None)
    if styles is None:
        styles = {}
        for key, format_string in [('date', 'dd/mm/yyyy'), ('integer', '#,##0'), ('float', '#,##0.00')]:
            prototype = WriteOnlyCell(sheet)
            prototype.number_format = format_string
            prototype.alignment = Alignment(vertical='center')
            styles[key] = prototype._style
        sheet.parent._energy_formats = styles
    row_number = getattr(sheet, '_energy_rows', 0) + 1
    for column, value in enumerate(values, 1):
        if value is None or value is pd.NA or value is pd.NaT:
            value = None
        elif isinstance(value, float) and pd.isna(value):
            value = None
        elif not isinstance(value, (str, int, float, bool, date, datetime)):
            if pd.isna(value):
                value = None
            elif hasattr(value, 'item'):
                value = value.item()
        if isinstance(value, str) and (len(value) > 32767 or value.count('\n') > 253
                or (len(value) > 16383 and len(value.encode('utf-16-le')) // 2 > 32767)):
            journal = getattr(sheet.parent, '_energy_long_texts', None)
            if journal is None:
                raise ValueError('Texto excede o limite de uma célula Excel.')
            value = journal.add(value, f'{sheet.title}!{get_column_letter(column)}{row_number}')
        if not header and (value is None or isinstance(value, bool)
                or (isinstance(value, str) and not value.startswith('=') and value not in ERROR_CODES)):
            cells.append(value)
            continue
        cell = WriteOnlyCell(sheet, value=value)
        if isinstance(value, str):
            cell.data_type = 's'  # Names and identifiers must never become formulas.
        if header:
            cell.fill, cell.font = HEADER_FILL, HEADER_FONT
        elif isinstance(value, (datetime, date)):
            cell._style = styles['date']
        elif isinstance(value, int) and not isinstance(value, bool):
            cell._style = styles['integer']
        elif isinstance(value, float):
            cell._style = styles['float']
        cells.append(cell)
    sheet.append(cells)
    sheet._energy_rows = row_number


def new_sheet(book, name, headers, rows):
    sheet = book.create_sheet(name)
    sheet.freeze_panes = 'A2'
    sheet.sheet_view.showGridLines = False
    sheet.sheet_format.defaultRowHeight = 20
    sheet.row_dimensions[1].height = 30
    sheet.print_options.horizontalCentered = True
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.orientation = 'landscape'
    sheet.page_setup.paperSize = Worksheet.PAPERSIZE_A4
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.print_title_rows = '1:1'
    if rows:
        table = Table(displayName='Tabela_' + name, ref=f'A1:{get_column_letter(len(headers))}{rows + 1}')
        table._initialise_columns()
        for column, label in zip(table.tableColumns, headers):
            column.name = label
        table.tableStyleInfo = TableStyleInfo(name='TableStyleMedium2', showRowStripes=True)
        sheet._tables.add(table)
    sheet.auto_filter.ref = f'A1:{get_column_letter(len(headers))}{rows + 1}'
    for index, label in enumerate(headers, 1):
        dimension = sheet.column_dimensions[get_column_letter(index)]
        dimension.width = min(48, max(18, len(label) + 2))
        if label in ('Submercado / Usina', 'Usina', 'Submercado', 'Motivo da variação', 'Texto'):
            dimension.width = 42 if label != 'Texto' else 100
            dimension.alignment = Alignment(wrap_text=True, vertical='center')
            sheet.sheet_format.defaultRowHeight = 32
        if label == 'Fonte':
            dimension.width = 10
        if label == 'Data':
            dimension.width = 14
        if label == 'ID ONS':
            dimension.width = 18
        if label == 'Alertas' and len(headers) > 2:
            dimension.width = 10
        if label in ('Alertas', 'Valor') and len(headers) <= 2:
            dimension.width = 110 if label == 'Alertas' else 100
            dimension.alignment = Alignment(wrap_text=True, vertical='top')
            sheet.sheet_format.defaultRowHeight = 32
        if label == 'Campo':
            dimension.width = 38
    append_row(sheet, headers, header=True)
    return sheet


def write_parts(book, base, headers, rows, total, capacity, cancelled, report):
    iterator = iter(rows)
    manifest = []
    parts = max(1, (total + capacity - 1) // capacity)
    for part in range(parts):
        check_cancelled(cancelled)
        amount = min(capacity, max(0, total - part * capacity))
        name = base if parts == 1 else f'{base}_{part + 1:03d}'
        sheet = new_sheet(book, name, headers, amount)
        written = 0
        for values in islice(iterator, amount):
            if written % 256 == 0:
                check_cancelled(cancelled)
            append_row(sheet, values)
            written += 1
            if written % 1024 == 0:
                report(name, 1024)
        report(name, written % 1024)
        if written != amount:
            raise ValueError('Contagem de linhas mudou durante a exportação.')
        manifest.append((name, part * capacity + 1 if amount else 0, part * capacity + amount, amount))
    if next(iterator, None) is not None:
        raise ValueError('Iterador contém mais linhas que o snapshot.')
    return manifest


def snapshot_description(frame, source):
    from .preview import COLUMNS
    from zoneinfo import ZoneInfo
    states = [item for item in frame.attrs.get('diagnostics', {}).values() if isinstance(item, dict)]
    def latest(field):
        values = [item.get(field) for item in states if item.get(field)]
        if not values:
            return 'Ainda não registrada'
        return max(datetime.fromisoformat(value) for value in values).astimezone(ZoneInfo('America/Sao_Paulo')).strftime('%d/%m/%Y %H:%M:%S')
    problems = []
    missing = sum((item.get('coverage') or {}).get('missing', 0) for item in states)
    if missing:
        problems.append(f'{missing:,} registros com valores ausentes nos arquivos utilizados; valores ausentes não entram nas médias.'.replace(',', '.'))
    failed = any(item.get('result') in ('failed', 'interrupted', 'cancelled') or item.get('last_error') for item in states)
    unknown = not states or any(not item.get('coverage') for item in states)
    if failed:
        problems.append('Parte dos dados não pôde ser verificada; a consulta utilizou os dados locais disponíveis.')
    if unknown:
        problems.append('A verificação da qualidade ainda não cobre todos os dados utilizados.')
    if failed:
        status = 'Erro'
    elif problems:
        status = 'Aviso'
    elif any(item.get('result') in ('installed', 'updated', 'committed') for item in states):
        status = 'Atualizado'
    else:
        status = 'Sem mudanças'
    dates = frame[COLUMNS[0]]
    period = f'{dates.min():%d/%m/%Y} a {dates.max():%d/%m/%Y}' if len(frame) else 'Sem registros'
    return [('Última verificação', latest('last_attempt_at')), ('Última atualização dos dados', latest('last_content_update_at')),
            ('Período disponível nos registros', period), ('Status dos dados', status),
            ('Problemas encontrados', ' '.join(problems) if problems else 'Nenhum problema identificado nas verificações disponíveis.')]


def export_excel(path, frame, alerts, query, updated, *, progress=None, cancelled=lambda: False,
                 row_limit=EXCEL_ROWS, charts=True):
    from .preview import COLUMNS, ID_COLUMN, ALERT_COLUMN, indicators
    if not 2 <= row_limit <= EXCEL_ROWS:
        raise ValueError('Limite de linhas inválido.')
    check_cancelled(cancelled)
    path = Path(path)
    book = Workbook(write_only=True)
    long_texts = LongTexts(cancelled)
    book._energy_long_texts = long_texts
    temporary = None
    started = perf_counter()
    outcome = 'failed'
    written = 0
    total = len(frame) + len(alerts) + 1

    def report(name, amount):
        nonlocal written
        written += amount
        if progress:
            progress(Progress(f'Exportando {name}: {written}/{total - 1} linhas', written, total))

    try:
        unused = COLUMNS[3] if query.source == 'ONS' else COLUMNS[4]
        columns = [column for column in frame.columns if column != unused]
        if query.source == 'ONS' and ID_COLUMN in columns:
            columns.remove(ID_COLUMN)
            columns.insert(3, ID_COLUMN)
        positions = [frame.columns.get_loc(column) for column in columns]
        rows = (tuple(row[index] for index in positions) for row in frame.itertuples(index=False, name=None))
        headers = [('Usina' if query.source == 'ONS' else 'Submercado') if column == COLUMNS[2] else column for column in columns]
        manifest = write_parts(book, 'Registros', headers, rows, len(frame), row_limit - 1, cancelled, report)
        summary = indicators(frame)
        summary.pop('Preço médio (R$/MWh)' if query.source == 'ONS' else 'Geração média (MW médios)')
        sheet = new_sheet(book, 'Indicadores', ['Indicador', 'Valor'], len(summary))
        for item in summary.items():
            append_row(sheet, item)
        summaries = alerts.iter_summaries() if hasattr(alerts, 'iter_summaries') else iter(alerts)
        manifest += write_parts(book, 'Alertas', ['Alertas'], ((value,) for value in summaries),
                                len(alerts), row_limit - 1, cancelled, report)
        metadata = [
            ('Modo', frame.attrs.get('mode', 'Dados CSV locais')),
            ('Fonte', query.source), ('Data inicial', query.start),
            ('Data final', query.end), ('Filtro', query.location),
            *([('Submercado ONS', query.ons_submarket), ('Estado', query.state),
               ('Tipo de usina', query.plant_type),
               ('Tipo de combustível', query.fuel)] if query.source == 'ONS' else
              [('Limite de preço (R$/MWh)', query.price_limit)]),
            ('Limite do módulo da variação (%)', query.change_limit),
            ('Consulta realizada em (horário local)', updated.strftime('%d/%m/%Y %H:%M:%S')),
            ('Variação', '((atual - anterior) / anterior) × 100; alerta quando o módulo excede estritamente o limite. Ausência de base ou base zero não produz percentual.'),
            ('Ordem', 'Ordenação global da tabela no início da exportação.'),
            *[(f'Parte {name}', f'Linhas {first}–{last}; {amount} registros') for name, first, last, amount in manifest],
        ]
        metadata.extend(snapshot_description(frame, query.source))
        sheet = new_sheet(book, 'Consulta', ['Campo', 'Valor'], len(metadata))
        for item in metadata:
            append_row(sheet, item)
        if charts:
            from .charts import add_charts
            add_charts(book, frame, query, cancelled=cancelled, progress=progress, row_limit=row_limit)
        if long_texts.rows:
            write_parts(book, 'Textos_Completos', ['Referência', 'Origem', 'Parte', 'Texto'],
                        long_texts, long_texts.rows, row_limit - 1, cancelled, lambda *args: None)
        check_cancelled(cancelled)
        if progress:
            progress(Progress('Finalizando arquivo Excel…', written, total))
        with NamedTemporaryFile(dir=path.parent, prefix='.energy-export-', suffix='.xlsx.part', delete=False) as output:
            temporary = Path(output.name)
            guarded = CancellableOutput(output, cancelled)
            archive = ZipFile(guarded, 'w', ZIP_DEFLATED, allowZip64=True)
            try:
                ExcelWriter(book, archive).write_data()
            finally:
                guarded.cancelled = lambda: False
                archive.close()
            output.flush()
            os.fsync(output.fileno())
        check_cancelled(cancelled)
        with ZipFile(temporary) as archive:
            if not {'[Content_Types].xml', 'xl/workbook.xml'}.issubset(archive.namelist()):
                raise ValueError('Estrutura XLSX incompleta.')
        check_cancelled(cancelled)
        os.replace(temporary, path)
        outcome = 'success'
        if progress:
            progress(Progress('Excel publicado.', total, total))
    finally:
        if frame.attrs.get('cache'):
            from .diagnostics import Diagnostics
            Diagnostics(frame.attrs['cache']).event(operation='export', result=outcome if outcome == 'success' else ('cancelled' if cancelled() else 'failed'),
                destination=path.name, records=len(frame), alerts=len(alerts), duration_seconds=perf_counter()-started)
        # openpyxl keeps one XML temporary per write-only worksheet until save.
        for sheet in book.worksheets:
            writer = getattr(sheet, '_writer', None)
            if writer is not None:
                try:
                    if not sheet.closed:
                        sheet.close()
                finally:
                    if os.path.exists(writer.out):
                        writer.cleanup()
        book.close()
        long_texts.stream.close()
        if temporary is not None:
            temporary.unlink(missing_ok=True)
