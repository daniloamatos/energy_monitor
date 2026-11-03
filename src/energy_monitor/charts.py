"""Native Excel charts derived only from the completed query snapshot."""
from datetime import date, timedelta
from math import isfinite

from openpyxl.chart import LineChart, Reference
from openpyxl.chart.axis import DateAxis, ChartLines
from openpyxl.chart.shapes import GraphicalProperties
from openpyxl.drawing.line import LineProperties
from openpyxl.chart.text import RichText
from openpyxl.drawing.text import Paragraph, ParagraphProperties, CharacterProperties, Font
from openpyxl.chart.series import SeriesLabel

from .cancellation import check_cancelled

COLORS = ('0072B2', 'D55E00', '009E73', '8E44AD', 'E69F00', '444444')
DASHES = ('solid', 'dash', 'dot', 'dashDot', 'lgDash', 'lgDashDot')
MONTHS = ('jan', 'fev', 'mar', 'abr', 'mai', 'jun', 'jul', 'ago', 'set', 'out', 'nov', 'dez')

def period_title(start, end):
    first, last = MONTHS[start.month - 1], MONTHS[end.month - 1]
    if start.year != end.year:
        return f'{first}/{start.year}–{last}/{end.year}'
    return f'{first}/{start.year}' if start.month == end.month else f'{first}–{last}/{start.year}'

def chart_font(size):
    return RichText(p=[Paragraph(pPr=ParagraphProperties(defRPr=CharacterProperties(sz=size * 100, latin=Font(typeface='Arial'))))])


def add_charts(book, frame, query, *, cancelled=lambda: False, progress=None, row_limit=1_048_576):
    from .preview import COLUMNS, ID_COLUMN, SUBMARKETS
    from .exporting import append_row, new_sheet
    options = frame.attrs.get('chart_options', {})
    maximum = options.get('max_series', 12)
    panel_size = options.get('series_per_panel', 6)
    if not isinstance(maximum, int) or not 1 <= maximum <= 100 or not isinstance(panel_size, int) or not 1 <= panel_size <= 6:
        raise ValueError('Configure gráficos com 1–100 séries e 1–6 séries por painel.')
    explicit = options.get('ids', [])
    if not isinstance(explicit, list) or not all(isinstance(value, str) for value in explicit):
        raise ValueError('IDs dos gráficos devem ser uma lista de textos.')
    weekly = [(date.fromisoformat(item['start']), date.fromisoformat(item['end']))
              for item in frame.attrs.get('resources', []) if item.get('granularity') == 'weekly']
    indices = {column: frame.columns.get_loc(column) for column in frame.columns}
    metric = COLUMNS[3] if query.source == 'CCEE' else COLUMNS[4]
    omitted_weekly = 0
    totals = {}

    def observations():
        nonlocal omitted_weekly
        for index, row in enumerate(frame.itertuples(index=False, name=None)):
            if index % 1024 == 0:
                check_cancelled(cancelled)
            day, name, value = row[indices[COLUMNS[0]]], row[indices[COLUMNS[2]]], row[indices[metric]]
            if query.source == 'CCEE' and (day.year < 2021 or any(start <= day <= end for start, end in weekly)):
                omitted_weekly += 1
                continue
            if value is None or not isfinite(value):
                continue
            identifier = row[indices[ID_COLUMN]] if ID_COLUMN in indices else ''
            identifier = identifier if isinstance(identifier, str) else ''
            key = identifier if query.source == 'ONS' and identifier else ('group:' + name if query.source == 'ONS' else name)
            label = f'{name} [{identifier}]' if identifier else name
            yield key, label, day, float(value)

    for key, label, day, value in observations():
        record = totals.setdefault(key, [0.0, 0, day, label])
        record[0] += value
        record[1] += 1
        if day >= record[2]:
            record[2], record[3] = day, label
    weekly_count = omitted_weekly
    individuals = [key for key in totals if not key.startswith('group:')]
    groups = [key for key in totals if key.startswith('group:')]
    rank = lambda key: (-totals[key][0] / totals[key][1], key)
    if query.source == 'ONS':
        selected = [key for key in dict.fromkeys(explicit) if key in individuals] if explicit else sorted(individuals, key=rank)[:maximum]
        selected_groups = sorted(groups, key=rank)[:maximum]
    else:
        selected, selected_groups = sorted(individuals), []
    selection = selected + selected_groups
    legend_labels = {key: key if query.source == 'ONS' else totals[key][3] for key in selected}
    legend_labels.update((key, f'Grupo {index}') for index, key in enumerate(selected_groups, 1))
    data = {key: {} for key in selection}
    for key, label, day, value in observations():
        if key in data:
            if day in data[key]:
                raise ValueError('Snapshot contém mais de um valor diário por entidade.')
            data[key][day] = value
    methodology = [
        ('Fonte', query.source), ('Período', f'{query.start.isoformat()} a {query.end.isoformat()}'),
        ('Filtro', query.location),
        *([('Estado ONS', query.state)] if query.source == 'ONS' else []),
        ('Critério', ('Todos os submercados presentes na consulta.' if query.source == 'CCEE' else 'IDs escolhidos pelo usuário.' if explicit else 'Usinas com maior média diária no período; desempate pelo identificador. O critério não é energia total.')),
        ('Usinas/submercados disponíveis', len(individuals)), ('Selecionados', len(selected)),
        ('Grupos sem ID disponíveis', len(groups)), ('Grupos selecionados separadamente', len(selected_groups)),
        ('Entidades fora da visão geral', len(totals) - len(selection)),
        ('Ausências', 'Lacunas preservadas; zeros publicados mantidos; sem interpolação.'),
        ('Cobertura', 'Médias das observações disponíveis; não comprova cobertura integral de cada dia.'),
        ('Histórico semanal', f'{weekly_count} registros preservados nas tabelas; gráfico semanal omitido por metodologia de ponderação não confirmada.'),
        *[(legend_labels[key], f'{totals[key][3]}; {totals[key][1]} dias válidos') for key in selection],
    ]
    notes = new_sheet(book, 'Metodologia_Graficos', ['Campo', 'Valor'], len(methodology))
    for row in methodology:
        append_row(notes, row)
    canvas = book.create_sheet('Graficos')
    canvas.sheet_view.showGridLines = False
    canvas.sheet_format.defaultRowHeight = 18
    for column, width in [('A', 26), ('B', 24), ('C', 48), ('D', 17), ('E', 24)]:
        canvas.column_dimensions[column].width = width
    canvas.freeze_panes = 'A4' 
    append_row(canvas, [f'{query.source} · {query.start:%d/%m/%Y} a {query.end:%d/%m/%Y} · {query.location}'])
    append_row(canvas, [f'Visão geral: {len(selection)} de {len(totals)} entidades. Dados completos nas tabelas.'])
    append_row(canvas, ['Como ler: cada linha acompanha uma entidade; espaço sem linha indica ausência de medição, não zero.'])
    if query.source == 'ONS':
        append_row(canvas, ['Seleção: maiores médias diárias no período; os demais registros continuam na tabela Registros.' if not explicit else 'Seleção: identificadores escolhidos para esta consulta.'])
    if weekly_count:
        append_row(canvas, ['Histórico semanal preservado nas tabelas; gráfico omitido por metodologia não confirmada.'])
    panels = 0
    days = (query.end - query.start).days + 1
    for selected_set in (selected, selected_groups):
        for offset in range(0, len(selected_set), panel_size):
            keys = selected_set[offset:offset + panel_size]
            for first in range(0, days, row_limit - 1):
                check_cancelled(cancelled)
                amount = min(row_limit - 1, days - first)
                panels += 1
                sheet = new_sheet(book, f'Dados_Graficos_{panels:03d}', ['Data', *[totals[key][3] for key in keys]], amount)
                for index in range(amount):
                    if index % 256 == 0:
                        check_cancelled(cancelled)
                    day = query.start + timedelta(days=first + index)
                    append_row(sheet, [day, *[data[key].get(day) for key in keys]])
                chart = LineChart()
                group_label = 'Grupos sem ID' if keys[0].startswith('group:') else 'Visão geral'
                first_day = query.start + timedelta(days=first)
                last_day = first_day + timedelta(days=amount - 1)
                metric_label = 'PLD médio diário' if query.source == 'CCEE' else 'Geração média diária'
                chart.title = (f'PLD médio diário por submercado — {period_title(first_day, last_day)}' if query.source == 'CCEE'
                               else f'{metric_label} — {period_title(first_day, last_day)} — {group_label}')
                for paragraph in chart.title.tx.rich.p:
                    paragraph.pPr = ParagraphProperties(defRPr=CharacterProperties(sz=1400, b=True, latin=Font(typeface='Arial')))
                chart.y_axis.title = 'PLD médio diário (R$/MWh)' if query.source == 'CCEE' else 'Geração média diária (MW médios)'
                chart.x_axis = DateAxis(crossAx=100)
                chart.x_axis.title = 'Data'
                chart.x_axis.number_format = '[$-416]mmm/yy'
                chart.x_axis.txPr = chart_font(12)
                chart.y_axis.txPr = chart_font(12)
                chart.legend.txPr = chart_font(12)
                chart.x_axis.majorGridlines = None
                chart.x_axis.minorGridlines = None
                chart.y_axis.minorGridlines = None
                chart.y_axis.majorGridlines = ChartLines(spPr=GraphicalProperties(ln=LineProperties(solidFill='E5E7EB', w=6350)))
                chart.x_axis.tickLblPos = 'low'
                chart.y_axis.numFmt = '#,##0.##'
                chart.legend.position = 'b'
                chart.legend.overlay = False
                chart.x_axis.majorTimeUnit = 'months'
                chart.x_axis.majorUnit = 1
                for axis in (chart.x_axis, chart.y_axis):
                    for paragraph in axis.title.tx.rich.p:
                        paragraph.pPr = ParagraphProperties(defRPr=CharacterProperties(sz=1300, latin=Font(typeface='Arial')))
                chart.y_axis.crossAx = 500
                chart.display_blanks = 'gap'
                chart.width, chart.height = 28, 14
                chart.add_data(Reference(sheet, min_col=2, max_col=len(keys) + 1, min_row=1, max_row=amount + 1), titles_from_data=True)
                chart.set_categories(Reference(sheet, min_col=1, min_row=2, max_row=amount + 1))
                for index, series in enumerate(chart.series):
                    series.tx = SeriesLabel(v=legend_labels[keys[index]])
                    style_index = SUBMARKETS.index(keys[index]) if query.source == 'CCEE' and keys[index] in SUBMARKETS else index
                    series.graphicalProperties.line.solidFill = COLORS[style_index]
                    series.graphicalProperties.line.prstDash = DASHES[style_index]
                    series.graphicalProperties.line.width = 38100
                    series.smooth = False
                append_row(canvas, [''])
                append_row(canvas, [('Submercados' if query.source == 'CCEE' else f'Painel {panels}'), group_label, f'{first_day:%d/%m/%Y} a {last_day:%d/%m/%Y}'])
                append_row(canvas, ['Cor e traço', 'Legenda do gráfico', 'Entidade', 'Dias com dados', 'Média no período'], header=True)
                color_names = ('Azul', 'Laranja', 'Verde', 'Roxo', 'Amarelo', 'Cinza')
                dash_names = ('contínua', 'tracejada', 'pontilhada', 'traço e ponto', 'traços longos', 'traços longos e ponto')
                for index, key in enumerate(keys):
                    style_index = SUBMARKETS.index(key) if query.source == 'CCEE' and key in SUBMARKETS else index
                    append_row(canvas, [f'{color_names[style_index]} · {dash_names[style_index]}', legend_labels[key],
                                        totals[key][3], totals[key][1], totals[key][0] / totals[key][1]])
                append_row(canvas, ['Eixo vertical: ' + ('preço em R$/MWh.' if query.source == 'CCEE' else 'geração em MW médios; não representa energia acumulada.')])
                anchor = canvas._energy_rows + 1
                canvas.add_chart(chart, f'A{anchor}')
                for _ in range(25):
                    append_row(canvas, [None])
                if progress:
                    progress(f'Gráficos: painel {panels} preparado; {amount} datas.')
