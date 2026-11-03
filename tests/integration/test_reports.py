"""Contracts 17–18: published workbooks and charts, using small exact snapshots."""
from contextlib import closing
from datetime import date
from zipfile import ZipFile

import openpyxl
import pytest

from energy_monitor import exporting
from energy_monitor.cancellation import OperationCancelled
from energy_monitor.preview import COLUMNS, AlertCollection, indicators
from energy_monitor.table_order import TableOrder


def records(book, prefix):
    return [row for sheet in book if sheet.title == prefix or sheet.title.startswith(prefix + '_')
            for row in list(sheet.values)[1:]]


@pytest.fixture
def snapshot(frame_factory):
    # Mean=(30+0+60)/3=30; signed changes have maximum modulus 100.
    return frame_factory([(date(2026, 11, d), 'CCEE', 'Sul', value, None, change)
                          for d, value, change in [(1, 30., None), (2, 0., -100.), (3, 60., None)]])


def test_excel_exporta_conjunto_completo_na_ordem_global_e_metadados(tmp_path, snapshot, query_factory, clock):
    query = query_factory(location='Sul', price_limit=25, change_limit=90)
    alerts = AlertCollection(snapshot, query)
    order = TableOrder(snapshot)
    order.sort(3, ascending=False)
    # Reading one page must not turn the exported result into that page.
    assert order.iloc[:1][COLUMNS[3]].tolist() == [60.]
    destination = tmp_path / 'relatorio.xlsx'
    exporting.export_excel(destination, order, alerts, query, clock(), charts=False)
    with closing(openpyxl.load_workbook(destination)) as book:
        rows = records(book, 'Registros')
        assert [row[3] for row in rows] == [60., 30., 0.]
        assert len(rows) == len(snapshot)
        assert dict(records(book, 'Indicadores')) == {k: v for k, v in indicators(snapshot).items() if 'Geração' not in k}
        metadata = dict(records(book, 'Consulta'))
        assert metadata['Filtro'] == 'Sul'
        assert metadata['Limite de preço (R$/MWh)'] == 25
        assert metadata['Limite do módulo da variação (%)'] == 90
        assert metadata['Data inicial'].date() == query.start
        assert metadata['Data final'].date() == query.end
        assert metadata['Consulta realizada em (horário local)'] == '03/11/2026 15:35:00'
        assert metadata['Última verificação'] == 'Ainda não registrada'
        assert not any(isinstance(v, str) and v.startswith('{') for v in metadata.values())
        sheet = book['Registros']
        assert sheet.freeze_panes == 'A2'
        assert sheet.auto_filter.ref == 'A1:E4'
        assert sheet['A2'].number_format == 'dd/mm/yyyy'
        assert sheet['D2'].number_format == '#,##0.00'
        assert sheet.column_dimensions['C'].width >= 40
        assert list(sheet.tables.values())[0].tableStyleInfo.showRowStripes
        assert 'Preço (R$/MWh)' in list(sheet.values)[0]
        assert 'Geração (MW médios)' not in list(sheet.values)[0]


@pytest.mark.parametrize('count,parts', [(0, [0]), (1, [1]), (2, [2]), (3, [2, 1]), (4, [2, 2])])
def test_excel_divide_registros_e_alertas_independentemente(tmp_path, frame_factory, query_factory, clock, count, parts):
    frame = frame_factory([(date(2026, 11, 1), 'CCEE', f'Sul {i}', float(i), None, None) for i in range(count)])
    destination = tmp_path / 'parts.xlsx'
    exporting.export_excel(destination, frame, [f'Alerta {i}' for i in range(5)], query_factory(), clock(), row_limit=3, charts=False)
    with closing(openpyxl.load_workbook(destination)) as book:
        assert [sheet.max_row - 1 for sheet in book if sheet.title.startswith('Registros')] == parts
        assert [sheet.max_row - 1 for sheet in book if sheet.title.startswith('Alertas')] == [2, 2, 1]
        assert [row[0] for row in records(book, 'Alertas')] == [f'Alerta {i}' for i in range(5)]


@pytest.mark.parametrize('text', ['=SUM(A1:A2)', '#DIV/0!', '+123', '@SUM(A1)'])
def test_excel_preserva_identificadores_literais_sem_executar_formulas(tmp_path, frame_factory, query_factory, clock, text):
    frame = frame_factory([(date(2026, 11, 1), 'ONS', text, None, 0., None)], ids=[text])
    destination = tmp_path / 'literal.xlsx'
    exporting.export_excel(destination, frame, [], query_factory(source='ONS'), clock(), charts=False)
    with closing(openpyxl.load_workbook(destination)) as book:
        assert book['Registros']['C2'].value == text
        assert book['Registros']['C2'].data_type == 's'
        assert book['Registros']['D2'].value == text
        assert book['Registros']['D2'].data_type == 's'
        assert book['Registros']['E1'].value == 'Geração (MW médios)'
        assert book['Registros']['E2'].value == 0


@pytest.mark.parametrize('text,overflow', [('x' * 32767, False), ('x' * 32768, True), ('😀' * 16384, True), ('linha\n' * 254, True)])
def test_excel_texto_longo_reconstroi_conteudo_sem_truncar(tmp_path, snapshot, query_factory, clock, text, overflow):
    destination = tmp_path / 'long.xlsx'
    exporting.export_excel(destination, snapshot, [text], query_factory(), clock(), row_limit=3, charts=False)
    with closing(openpyxl.load_workbook(destination)) as book:
        parts = records(book, 'Textos_Completos')
        if overflow:
            assert 'Referência: T000001' in records(book, 'Alertas')[0][0]
            assert ''.join(row[3] for row in parts) == text
            assert [row[2] for row in parts] == list(range(1, len(parts) + 1))
            assert all(len(row[3].encode('utf-16-le')) // 2 <= 32767 for row in parts)
        else:
            assert parts == []
            assert records(book, 'Alertas')[0][0] == text


@pytest.mark.parametrize('failure', ['replace', 'zip', 'cancel'])
def test_exportacao_falha_preserva_destino_e_remove_temporarios(tmp_path, snapshot, query_factory, clock, monkeypatch, failure):
    destination = tmp_path / 'existente.xlsx'
    original = b'arquivo anterior preservado'
    destination.write_bytes(original)
    state = {'cancel': False, 'writes': 0}
    if failure == 'replace':
        def fail_replace(*args):
            raise OSError('falha injetada na publicação')
        monkeypatch.setattr(exporting.os, 'replace', fail_replace)
    else:
        original_write = exporting.CancellableOutput.write
        def fail_write(self, data):
            state['writes'] += 1
            if state['writes'] == 8:
                if failure == 'zip':
                    raise OSError('falha injetada no ZIP')
                state['cancel'] = True
            return original_write(self, data)
        monkeypatch.setattr(exporting.CancellableOutput, 'write', fail_write)
    with pytest.raises(OperationCancelled if failure == 'cancel' else OSError):
        exporting.export_excel(destination, snapshot, [], query_factory(), clock(), charts=False, cancelled=lambda: state['cancel'])
    assert destination.read_bytes() == original
    assert not list(tmp_path.glob('.energy-export-*'))


def test_excel_publicado_e_zip_integro_com_graficos_nativos(tmp_path, snapshot, query_factory, clock):
    destination = tmp_path / 'charts.xlsx'
    exporting.export_excel(destination, snapshot, [], query_factory(), clock())
    with ZipFile(destination) as archive:
        assert archive.testzip() is None
        assert 'xl/charts/chart1.xml' in archive.namelist()
    with closing(openpyxl.load_workbook(destination)) as book:
        assert {'Registros', 'Indicadores', 'Alertas', 'Consulta', 'Graficos', 'Metodologia_Graficos', 'Dados_Graficos_001'} <= set(book.sheetnames)
        assert len(book['Graficos']._charts) == 1


def test_grafico_ccee_preserva_zero_lacuna_legendas_e_estilos(tmp_path, frame_factory, query_factory, clock):
    frame = frame_factory([(date(2026, 5, d), 'CCEE', name, value, None, None)
                           for name in ['Sul', 'Norte', 'Nordeste', 'Sudeste/Centro-Oeste']
                           for d, value in [(1, 0.), (3, 20.)]])
    query = query_factory(start=date(2026, 5, 1), end=date(2026, 10, 31))
    destination = tmp_path / 'pld.xlsx'
    exporting.export_excel(destination, frame, [], query, clock())
    with closing(openpyxl.load_workbook(destination)) as book:
        data = list(book['Dados_Graficos_001'].values)
        assert data[1][1:] == (0, 0, 0, 0)
        assert data[2][1:] == (None, None, None, None)
        assert data[3][1:] == (20, 20, 20, 20)
        assert len(data) == (query.end - query.start).days + 2
        chart = book['Graficos']._charts[0]
        assert {series.tx.v for series in chart.series} == set(frame[COLUMNS[2]])
        assert len({series.graphicalProperties.line.solidFill.srgbClr for series in chart.series}) == 4
        assert len({series.graphicalProperties.line.prstDash for series in chart.series}) == 4
        assert all(series.graphicalProperties.line.width == 38100 for series in chart.series)
        assert chart.display_blanks == 'gap'
        assert chart.x_axis.number_format.formatCode == '[$-416]mmm/yy'
        assert chart.x_axis.majorGridlines is None
        assert chart.y_axis.majorGridlines.spPr.ln.solidFill.srgbClr == 'E5E7EB'
        title = ''.join(run.t for paragraph in chart.title.tx.rich.p for run in paragraph.r)
        assert title == 'PLD médio diário por submercado — mai–out/2026'


def test_grafico_ons_seleciona_dois_grupos_de_doze_e_preserva_tabela_completa(tmp_path, frame_factory, query_factory, clock):
    frame = frame_factory([(date(2026, 11, 1), 'ONS', f'Usina {i}', None, float(i), None) for i in range(26)],
                          ids=[f'ID{i:02}' if i < 13 else '' for i in range(26)])
    destination = tmp_path / 'ons.xlsx'
    exporting.export_excel(destination, frame, [], query_factory(source='ONS'), clock())
    with closing(openpyxl.load_workbook(destination)) as book:
        methodology = dict(records(book, 'Metodologia_Graficos'))
        assert methodology['Selecionados'] == 12
        assert methodology['Grupos selecionados separadamente'] == 12
        assert methodology['Entidades fora da visão geral'] == 2
        charts = book['Graficos']._charts
        assert [len(chart.series) for chart in charts] == [6, 6, 6, 6]
        assert all(series.tx.v.startswith('ID') for chart in charts[:2] for series in chart.series)
        assert all(series.tx.v.startswith('Grupo ') for chart in charts[2:] for series in chart.series)
        assert 'ID00' not in methodology
        assert len(records(book, 'Registros')) == 26


def test_grafico_ons_id_mantem_identidade_quando_nome_muda(tmp_path, frame_factory, query_factory, clock):
    frame = frame_factory([(date(2026, 11, 1), 'ONS', 'Nome antigo', None, 0., None),
                           (date(2026, 11, 3), 'ONS', 'Nome novo', None, 12., None)], ids=['ONS01', 'ONS01'])
    destination = tmp_path / 'renomeada.xlsx'
    exporting.export_excel(destination, frame, [], query_factory(source='ONS'), clock())
    with closing(openpyxl.load_workbook(destination)) as book:
        assert list(book['Dados_Graficos_001'].values) == [
            ('Data', 'Nome novo [ONS01]'), (clock().replace(year=2026, month=11, day=1, hour=0, minute=0, tzinfo=None), 0),
            (clock().replace(year=2026, month=11, day=2, hour=0, minute=0, tzinfo=None), None),
            (clock().replace(year=2026, month=11, day=3, hour=0, minute=0, tzinfo=None), 12)]
        assert book['Graficos']._charts[0].series[0].tx.v == 'ONS01'


def test_historico_semanal_ccee_fica_na_tabela_sem_grafico_diario(tmp_path, frame_factory, query_factory, clock):
    frame = frame_factory([(date(2020, 1, 1), 'CCEE', 'Sul', 50., None, None)])
    destination = tmp_path / 'weekly.xlsx'
    exporting.export_excel(destination, frame, [], query_factory(start=date(2020, 1, 1), end=date(2020, 1, 7)), clock())
    with closing(openpyxl.load_workbook(destination)) as book:
        assert len(records(book, 'Registros')) == 1
        assert book['Graficos']._charts == []
        assert dict(records(book, 'Metodologia_Graficos'))['Histórico semanal'].startswith('1 registros preservados')
