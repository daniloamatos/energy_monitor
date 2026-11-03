"""Interface desktop do Energy Monitor."""

from datetime import date, datetime, timedelta
from pathlib import Path
from threading import Event
from time import perf_counter

import pandas as pd
import PySimpleGUI as sg
from tkinter import ttk

from .preview import COLUMNS, ID_COLUMN, ALERT_COLUMN, TABLE_COLUMNS, SUBMARKETS, export_excel, indicators, parse_query
from .table_highlights import AlertRows
from .table_order import TableOrder
from .preview import limits_only_changed, reapply_limits, narrower_period, restrict_period, overlapping_period, extend_period, can_filter_snapshot, filter_snapshot
from .progress import Progress
from .number_format import number as format_number, compact, count
from .data_sources import SourceStore, Schedule, catalog, CONFIG, CACHE
from .updater import UpdateExecutor, UpdateResult
from .scheduling import external_enabled
from .diagnostics import Diagnostics, summary as diagnostic_summary, safe_error
from .windows import OwnedWindows, create_main_window
from .dropdown import Dropdown
from .resize import ResizeHandles

PURPLE = "#330072"
BLUE = "#009cde"
BACKGROUND = "#F0F0F0"
ALERT_COLOR = "#B42318"
ALERT_BACKGROUND = "#FFF0ED"
INPUT_HOVER = "#E6F5FC"
PAGE_SIZE = 1000


def should_prompt_missing_files(event, has_local_csv, missing_files):
    """Prompt at startup only when no local CSV can be used as a baseline."""
    return bool(missing_files) and (event == "-VERIFY-" or not has_local_csv)


def add_input_hover(widget):
    """Highlight editable fields without changing their size or event bindings."""
    if isinstance(widget, ttk.Combobox):
        style = ttk.Style(widget)
        original = widget.cget("style") or "TCombobox"
        name = f"EnergyHover.{original}"
        style.map(name, fieldbackground=[
            ("disabled", style.lookup(original, "fieldbackground", ("disabled",))),
            ("active", INPUT_HOVER),
            *style.map(original, "fieldbackground"),
        ])
        widget.configure(style=name)
        widget.bind("<Enter>", lambda event: widget.state(["active"]), add="+")
        widget.bind("<Leave>", lambda event: widget.state(["!active"]), add="+")
    else:
        background = widget.cget("background")
        widget.bind("<Enter>", lambda event: widget.configure(background=INPUT_HOVER), add="+")
        widget.bind("<Leave>", lambda event: widget.configure(background=background), add="+")


def create_window():
    sg.theme_add_new("SimpleEnergy", {
        "BACKGROUND": BACKGROUND, "TEXT": PURPLE,
        "INPUT": "#FFFFFF", "TEXT_INPUT": PURPLE, "SCROLL": BLUE,
        "BUTTON": (BACKGROUND, PURPLE), "PROGRESS": (BLUE, "#DDD4E8"),
        "BORDER": 0, "SLIDER_DEPTH": 0, "PROGRESS_DEPTH": 0,
    })
    sg.theme("SimpleEnergy")
    today = date.today()
    layout = [
        [sg.Text("ENERGY MONITOR", font=("Arial", 18, "bold")),
         sg.Text("CCEE e ONS • Médias diárias", text_color=PURPLE)],
        [sg.Frame("Consulta", [
            [sg.Text("Fonte", size=(10, 1)), sg.Combo(["CCEE", "ONS"], default_value="CCEE", readonly=True, key="-SOURCE-", size=(12, 1)),
             sg.Text("Nome da usina", key="-NAME-LABEL-", size=(12, 1), visible=False),
             sg.Combo(["Todos"], default_value="Todos", readonly=True, key="-ONS-NAME-", size=(27, 1), visible=False),
             sg.Text("Submercado", key="-FILTER-LABEL-", size=(12, 1)),
             sg.Combo(["Todos", *SUBMARKETS], default_value="Todos", readonly=True, key="-LOCATION-", size=(27, 1))],
            [sg.pin(sg.Column([
                [sg.Text("Submercado ONS", size=(15, 1)),
                 sg.Combo(["Todos"], default_value="Todos", readonly=True, key="-ONS-SUBMARKET-", size=(28, 1)),
                 sg.Text("Tipo de usina"),
                 sg.Combo(["Todos"], default_value="Todos", readonly=True, key="-PLANT-TYPE-", size=(25, 1))],
                [sg.Text("Combustível", size=(15, 1)),
                 sg.Combo(["Todos"], default_value="Todos", readonly=True, key="-FUEL-", size=(20, 1)),
                 sg.Text("Estado"),
                 sg.Combo(["Todos"], default_value="Todos", readonly=True, key="-STATE-", size=(12, 1))],
            ], key="-ONS-FILTERS-", visible=False, pad=(0, 0)))],
            [sg.Text("Data inicial", size=(10, 1)), sg.Input((today - timedelta(days=6)).strftime("%d/%m/%Y"), key="-START-", size=(12, 1)),
             sg.Button("Calendário", key="-CAL-START-"),
             sg.Text("Data final"), sg.Input(today.strftime("%d/%m/%Y"), key="-END-", size=(12, 1)),
             sg.Button("Calendário", key="-CAL-END-")],
            [sg.pin(sg.Column([[sg.Text("Preço acima de (R$/MWh)"),
                               sg.Input("200", key="-PRICE-", size=(10, 1))]],
                              key="-PRICE-FILTER-", pad=(0, 0))),
             sg.Text("Limite do módulo da variação (%)"), sg.Input("10", key="-CHANGE-", size=(10, 1)),
             sg.Text("Em branco: sem alerta", font=("Arial", 9),
                     tooltip="Alerta quando |variação percentual| > limite. Igualdade não alerta. Em branco: desativado.")],
        ], expand_x=True)],
        [sg.Button("Buscar", key="-UPDATE-"),
         sg.Button("Exportar Excel", key="-EXPORT-", button_color=(PURPLE, BLUE), disabled=True),
         sg.Push(),
         sg.Button("Situação dos dados", key="-DIAGNOSTICS-"),
         sg.Button("Atualizar dados locais", key="-VERIFY-")],
        [sg.Text("Verificação dos dados locais: aguardando…", key="-UPDATED-", size=(70, 2), expand_x=True)],
        [sg.ProgressBar(100, orientation="h", size=(40, 10), key="-PROGRESS-", expand_x=True)],
        [sg.Text("Verificando os arquivos públicos e o cache local…", key="-STATUS-", size=(100, 1), expand_x=True)],
        [sg.pin(sg.Text("Preço médio: —", key="-PRICE-AVG-", size=(30, 1))),
         sg.pin(sg.Text("Geração média: —", key="-GEN-AVG-", size=(33, 1), visible=False)),
         sg.Text("Maior |variação|: —", key="-MAX-CHANGE-", size=(27, 1))],
        [sg.Text("Nenhuma consulta carregada.", key="-APPLIED-", expand_x=True)],
        [sg.Table([], headings=TABLE_COLUMNS, key="-TABLE-", num_rows=12,
                  auto_size_columns=False, col_widths=[12, 7, 26, 18, 23, 17, 12, 8],
                  justification="left", expand_x=True, expand_y=True,
                  background_color="#FFFFFF", alternating_row_color="#E9E3F0",
                  header_background_color=PURPLE, header_text_color=BACKGROUND,
                  selected_row_colors=(PURPLE, BLUE))],
        [sg.Button("Primeira", key="-PAGE-FIRST-", disabled=True),
         sg.Button("−10", key="-PAGE-BACK10-", disabled=True),
         sg.Button("Anterior", key="-PAGE-PREV-", disabled=True),
         sg.Text("Página 0 de 0", key="-PAGE-INFO-"),
         sg.Button("Próxima", key="-PAGE-NEXT-", disabled=True),
         sg.Button("+10", key="-PAGE-FWD10-", disabled=True),
         sg.Button("Última", key="-PAGE-LAST-", disabled=True),
         sg.Text("Página"), sg.Input("1", key="-PAGE-JUMP-", size=(6, 1)),
         sg.Button("Ir", key="-PAGE-GO-", disabled=True)],
        [sg.Text("Alertas encontrados: —", key="-ALERT-COUNT-", font=("Arial", 15, "bold"),
                 background_color="#E9E3F0", size=(65, 1), expand_x=True,
                 pad=((0, 0), (4, 0))),
         sg.Button("Ampliar alertas", key="-DETAILS-", disabled=True,
                   button_color=("#FFFFFF", ALERT_COLOR), pad=((12, 0), (4, 0)))],
        [sg.Multiline("Atualize os dados para avaliar os limites.", key="-ALERTS-", size=(100, 11),
                      font=("Arial", 11), background_color="#FFFFFF", disabled=True,
                      expand_x=True, pad=(0, 0))],
    ]
    window = create_main_window("Energy Monitor", layout, font=("Arial", 11),
                                resizable=True, margins=(14, 10), element_padding=(4, 2), use_ttk_buttons=False)
    for key in ("-SOURCE-", "-LOCATION-", "-ONS-SUBMARKET-", "-STATE-", "-PLANT-TYPE-", "-FUEL-", "-ONS-NAME-"):
        window[key].selection_popup = Dropdown(
            window[key].Widget, lambda value, key=key: window.write_event_value(key, value))
    for key in ("-START-", "-END-"):
        window[key].bind("<FocusOut>", "-ONS-OPTIONS-")
    window["-PAGE-JUMP-"].bind("<Return>", "-GO-")
    for key in ("-SOURCE-", "-LOCATION-", "-ONS-SUBMARKET-", "-STATE-", "-PLANT-TYPE-", "-FUEL-", "-ONS-NAME-",
                "-START-", "-END-", "-PRICE-", "-CHANGE-", "-PAGE-JUMP-"):
        add_input_hover(window[key].Widget)
    table = window["-TABLE-"].Widget
    for column, minimum in zip(table["columns"], (100, 60, 180, 150, 170, 120, 100, 70)):
        table.column(column, stretch=True, minwidth=minimum)
    table.bind("<Configure>", lambda event: fit_table_columns(table), add="+")
    for index, column in enumerate(table["columns"]):
        table.heading(column, command=lambda index=index: window.write_event_value("-SORT-", index))
    configure_result_source(window, "CCEE")
    window.alert_rows = AlertRows(table)
    window.resize_handles = ResizeHandles(window.TKroot)
    return window


def background_job(function, *args, **kwargs):
    try:
        return True, function(*args, **kwargs)
    except Exception as error:
        return False, safe_error(error)


def table_rows(frame):
    from .variation import REASON_COLUMN
    identifiers = frame[ID_COLUMN] if ID_COLUMN in frame else [""] * len(frame)
    counts = frame[ALERT_COLUMN] if ALERT_COLUMN in frame else [0] * len(frame)
    reasons = frame[REASON_COLUMN] if REASON_COLUMN in frame else [''] * len(frame)
    result = []
    for row, identifier, count, reason in zip(frame[COLUMNS].itertuples(index=False, name=None), identifiers, counts, reasons):
        values = ["—" if pd.isna(value) else format_number(value) for value in row[3:]]
        if pd.isna(row[5]) and reason:
            values[2] = {'Sem base de comparação': 'Sem base', 'Base anterior zero': 'Base zero'}.get(reason, reason)
        result.append([row[0].strftime("%d/%m/%Y"), row[1], row[2], *values,
                       identifier if pd.notna(identifier) else "", int(count)])
    return result


def sort_table(frame, column, ascending=True):
    """Ordena pelos valores originais, antes de formatar datas e números."""
    key = (lambda values: values.str.casefold()) if column in (1, 2, 6) else None
    return frame.sort_values(TABLE_COLUMNS[column], ascending=ascending, kind="stable", na_position="last", key=key)


def table_headings(source):
    headings = list(TABLE_COLUMNS)
    headings[2] = "Usina" if source == "ONS" else "Submercado"
    return headings


def fit_table_columns(table):
    columns = table["displaycolumns"]
    width = table.winfo_width() - 2
    if width <= 0 or not columns:
        return
    minimums = [int(table.column(column, "minwidth")) for column in columns]
    extra, remainder = divmod(max(0, width - sum(minimums)), len(columns))
    for index, (column, minimum) in enumerate(zip(columns, minimums)):
        table.column(column, width=minimum + extra + (index < remainder))


def configure_result_source(window, source):
    table = window["-TABLE-"].Widget
    visible = (0, 1, 2, 6, 4, 5, 7) if source == "ONS" else (0, 1, 2, 3, 5, 7)
    table.configure(displaycolumns=tuple(table["columns"][index] for index in visible))
    fit_table_columns(table)
    window["-PRICE-AVG-"].update(visible=source == "CCEE")
    window["-GEN-AVG-"].update(visible=source == "ONS")


def run(config=CONFIG, cache=CACHE) -> None:
    stopping = Event()
    cancellation = Event()
    export_finished = Event()
    export_finished.set()
    active_operation = None
    store = SourceStore(config, cache)
    updater = UpdateExecutor(store)
    settings, _ = catalog(store.config)
    schedule = Schedule(settings)
    schedule.due()
    window = create_window()
    diagnostic_check = perf_counter()
    diagnostic_stamp = None
    window.write_event_value("-STARTUP-SYNC-", None)
    ons_ids = []
    ons_plant_ids = {}
    owned = OwnedWindows(window.TKroot)
    frame = None
    alerts = []
    query = None
    pending_query = None
    pending_reuse = False
    pending_search_after_sync = False
    updated = None
    busy = False
    progress = 0
    sync_started = 0
    sort_column = None
    sort_ascending = True
    ordered = None
    page = 0
    controls = ("-UPDATE-", "-VERIFY-", "-SOURCE-", "-START-", "-END-", "-LOCATION-",
                "-PRICE-", "-CHANGE-", "-CAL-START-", "-CAL-END-",
                "-ONS-SUBMARKET-", "-STATE-", "-PLANT-TYPE-", "-FUEL-", "-ONS-NAME-")

    def set_busy(value, operation=None):
        nonlocal busy, active_operation
        busy = value
        active_operation = operation if value else None
        if value and operation:
            cancellation.clear()
        for key in controls:
            window[key].update(disabled=value)
        window["-UPDATE-"].update(text="Buscar", button_color=(BACKGROUND, PURPLE))
        window["-VERIFY-"].update(text="Atualizar dados locais", button_color=(BACKGROUND, PURPLE))
        if active_operation and active_operation != "export":
            key = "-UPDATE-" if active_operation == "search" else "-VERIFY-"
            window[key].update(text="Cancelar busca" if active_operation == "search" else "Cancelar atualização",
                               button_color=("#FFFFFF", ALERT_COLOR), disabled=False)
        window["-EXPORT-"].update(text="Cancelar exportação" if active_operation == "export" else "Exportar Excel",
                                   button_color=("#FFFFFF", ALERT_COLOR) if active_operation == "export" else (PURPLE, BLUE),
                                   disabled=(value and active_operation != "export") or frame is None)
        window["-DETAILS-"].update(disabled=value or frame is None)
        if not value:
            window["-PRICE-"].update(disabled=window["-SOURCE-"].get() == "ONS")

    def show_error(message):
        window["-STATUS-"].update(f"Erro: {message}", text_color="#A12636")
        window["-PROGRESS-"].update(0)

    def cancelled():
        return stopping.is_set() or cancellation.is_set()

    def query_progress(message):
        if not cancelled():
            window.write_event_value("-QUERY-PROGRESS-", message)

    def finish_cancel():
        if not cancellation.is_set():
            return False
        operation = active_operation
        set_busy(False)
        window["-PROGRESS-"].update(0)
        window["-STATUS-"].update({"search": "Busca cancelada.", "sync": "Atualização interrompida. Arquivos já publicados permanecem disponíveis.", "export": "Exportação cancelada."}.get(operation, "Operação cancelada."),
                                 text_color=PURPLE)
        return True

    def show_page():
        pages = max(1, (len(ordered) + PAGE_SIZE - 1) // PAGE_SIZE)
        visible = ordered.iloc[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
        window["-TABLE-"].update(values=table_rows(visible))
        window.alert_rows.update(visible, query)
        window["-PAGE-INFO-"].update(f"Página {page + 1} de {pages}")
        window["-PAGE-PREV-"].update(disabled=page == 0)
        window["-PAGE-NEXT-"].update(disabled=page + 1 >= pages)
        for key in ("-PAGE-FIRST-", "-PAGE-BACK10-"):
            window[key].update(disabled=page == 0)
        for key in ("-PAGE-LAST-", "-PAGE-FWD10-"):
            window[key].update(disabled=page + 1 >= pages)
        window["-PAGE-JUMP-"].update(str(page + 1))
        window["-PAGE-GO-"].update(disabled=False)

    try:
        while not owned.closing:
            event, values = window.read(timeout=100)
            if owned.closing or event == sg.WIN_CLOSED:
                break
            if perf_counter() - diagnostic_check >= 5:
                diagnostic_check = perf_counter()
                state_file = store.cache / 'diagnostics.sqlite'
                stamp = state_file.stat().st_mtime_ns if state_file.exists() else None
                if stamp != diagnostic_stamp:
                    diagnostic_stamp = stamp
                    try:
                        window['-UPDATED-'].update(diagnostic_summary(Diagnostics(store.cache).status(reconcile=False), settings['timezone']))
                    except Exception:
                        window['-UPDATED-'].update('Diagnóstico temporariamente indisponível.')
            if event == '-DIAGNOSTICS-':
                try:
                    from .data_health import health_report, show_health
                    show_health(window.TKroot, health_report(store), settings['timezone'])
                except Exception:
                    show_error('Não foi possível consultar a situação dos dados. Tente novamente.')
            if busy and active_operation and event == {"search": "-UPDATE-", "sync": "-VERIFY-", "export": "-EXPORT-"}[active_operation]:
                cancellation.set()
                window[event].update(text="Cancelando…", disabled=True)
                window["-STATUS-"].update("Cancelando operação…", text_color=PURPLE)
                continue
            if not busy and schedule.due():
                window.write_event_value("-SCHEDULE-SYNC-", None)
            if event in ("-SYNC-PROGRESS-", "-QUERY-PROGRESS-"):
                if not cancellation.is_set():
                    message = values[event]
                    window["-STATUS-"].update(str(message), text_color=PURPLE)
                    if isinstance(message, Progress):
                        progress = message.percent
                        window["-PROGRESS-"].Widget.configure(value=progress)
            if event in ("-CAL-START-", "-CAL-END-") and not busy:
                target = "-START-" if event == "-CAL-START-" else "-END-"
                def apply_date(value, target=target):
                    window[target].update(value)
                    window.write_event_value("-ONS-OPTIONS-", None)
                owned.choose_date(values[target], apply_date)
            elif event == "-SOURCE-":
                is_ccee = values["-SOURCE-"] == "CCEE"
                window["-FILTER-LABEL-"].update("Submercado" if is_ccee else "ID ONS", visible=is_ccee)
                window["-NAME-LABEL-"].update(visible=not is_ccee)
                window["-ONS-NAME-"].update(visible=not is_ccee)
                window["-LOCATION-"].update(values=["Todos", *(SUBMARKETS if is_ccee else ons_ids)], value="Todos")
                window["-PRICE-"].update(disabled=not is_ccee)
                window["-PRICE-FILTER-"].update(visible=is_ccee)
                window["-ONS-FILTERS-"].update(visible=not is_ccee)
                if not is_ccee:
                    window.write_event_value("-ONS-OPTIONS-", None)
            elif event in ("-ONS-OPTIONS-", "-START--ONS-OPTIONS-", "-END--ONS-OPTIONS-") and not busy:
                if values["-SOURCE-"] != "ONS":
                    continue
                try:
                    filters_query = parse_query(values | {"-LOCATION-": "Todos", "-PRICE-": "", "-CHANGE-": ""})
                except ValueError:
                    continue
                set_busy(True, "search")
                window["-PROGRESS-"].update(0)
                window["-STATUS-"].update("Lendo filtros disponíveis nos CSV ONS locais…", text_color=PURPLE)
                window.perform_long_operation(
                    lambda: background_job(store.ons_options, filters_query.start, filters_query.end,
                                           progress=query_progress, cancelled=cancelled), "-ONS-OPTIONS-LOADED-")
            elif event == "-ONS-OPTIONS-LOADED-":
                success, result = values[event]
                if finish_cancel():
                    continue
                set_busy(False)
                if not success:
                    show_error(result)
                    continue
                ons_ids = result["ids"]
                ons_plant_ids = result.get("plant_ids", {})
                for key, field in (("-LOCATION-", "ids"), ("-ONS-NAME-", "plants"), ("-ONS-SUBMARKET-", "submarkets"),
                                   ("-STATE-", "states"), ("-PLANT-TYPE-", "plant_types"),
                                   ("-FUEL-", "fuels")):
                    choices = ["Todos", *result[field]]
                    current = window[key].get()
                    window[key].update(values=choices, value=current if current in choices else "Todos")
                window["-STATUS-"].update("Filtros ONS carregados. Use Buscar para consultar os dados.", text_color=PURPLE)
                window["-PROGRESS-"].update(100)
            elif event == "-ONS-NAME-" and not busy:
                choices = ["Todos", *ons_plant_ids.get(values["-ONS-NAME-"], ons_ids)]
                window["-LOCATION-"].update(values=choices,
                          value=values["-LOCATION-"] if values["-LOCATION-"] in choices else "Todos")
            elif event == "-UPDATE-" and not busy:
                try:
                    pending_query = parse_query(values | ({"-PRICE-": ""} if values["-SOURCE-"] == "ONS" else {}))
                except ValueError as error:
                    show_error(str(error))
                    continue
                pending_reuse = ("filter" if frame is not None and can_filter_snapshot(query, pending_query) else
                                 "limits" if frame is not None and limits_only_changed(query, pending_query) else
                                 "subset" if frame is not None and overlapping_period(query, pending_query) and query != pending_query else False)
                missing_for_query = store.missing_resources(start=pending_query.start, end=pending_query.end, source=pending_query.source)
                if missing_for_query:
                    chosen = owned.prompt_download(missing_for_query, title="Arquivos ausentes para a busca")
                    if chosen:
                        pending_search_after_sync = True
                        set_busy(True, "sync")
                        progress = 0
                        window["-PROGRESS-"].update(progress)
                        window["-STATUS-"].update("Baixando arquivos selecionados…", text_color=PURPLE)
                        def download_missing_for_search():
                            return updater.run(origin="search-prompt", current_only=False,
                                               progress=lambda msg: None if cancelled() else window.write_event_value("-SYNC-PROGRESS-", msg),
                                               cancelled=cancelled, missing_ids=chosen)
                        window.perform_long_operation(lambda: background_job(download_missing_for_search), "-SYNCED-")
                        continue
                    else:
                        pending_search_after_sync = False
                        window["-STATUS-"].update("Busca cancelada: arquivos necessários não foram baixados.", text_color=PURPLE)
                        continue
                set_busy(True, "search")
                progress = 0
                window["-PROGRESS-"].update(progress)
                if pending_reuse == "filter":
                    window['-STATUS-'].update('Aplicando filtro ao resultado em memória…', text_color=PURPLE)
                    window.perform_long_operation(lambda: background_job(filter_snapshot, frame, query, pending_query,
                                                                          cancelled=cancelled, progress=query_progress), '-LOADED-')
                elif pending_reuse == "subset":
                    window['-STATUS-'].update("Selecionando período do resultado em memória…", text_color=PURPLE)
                    window.perform_long_operation(lambda: background_job(extend_period, store, frame, query, pending_query,
                                                                          cancelled=cancelled, progress=query_progress, alerts=alerts), "-LOADED-")
                elif pending_reuse:
                    window['-STATUS-'].update('Recalculando alertas do resultado em memória…', text_color=PURPLE)
                    window.perform_long_operation(lambda: background_job(reapply_limits, frame, alerts, query, pending_query,
                                                                          cancelled=cancelled, progress=query_progress), '-LOADED-')
                else:
                    window["-STATUS-"].update("Consultando dados locais; arquivos novos ou alterados são indexados uma vez…", text_color=PURPLE)
                    window.perform_long_operation(lambda: background_job(store.load, pending_query, cancelled=cancelled,
                                                                          progress=query_progress), "-LOADED-")
            elif event in ("-STARTUP-SYNC-", "-SCHEDULE-SYNC-", "-VERIFY-") and not busy:
                current_only = event in ("-STARTUP-SYNC-", "-VERIFY-")
                scheduled = event == "-SCHEDULE-SYNC-"
                origin = {"-VERIFY-": "manual", "-STARTUP-SYNC-": "startup", "-SCHEDULE-SYNC-": "gui-schedule"}[event]
                selected_missing = None
                if not scheduled:
                    all_missing = store.missing_resources()
                    _, resources = catalog(store.config, store.cache)
                    has_local_csv = any(store.path(resource.id).is_file() for resource in resources)
                    if should_prompt_missing_files(event, has_local_csv, all_missing):
                        selected_missing = owned.prompt_download(all_missing, title="Arquivos ausentes para sincronização")
                        if not selected_missing:
                            window["-STATUS-"].update("Atualização cancelada.", text_color=PURPLE)
                            continue
                sync_started = perf_counter()
                set_busy(True, "sync")
                progress = 0
                window["-PROGRESS-"].update(progress)
                window["-STATUS-"].update("Verificando arquivos vigentes…", text_color=PURPLE)
                def synchronize():
                    if scheduled and external_enabled(store):
                        return UpdateResult("deduplicated")
                    return updater.run(origin=origin, scheduled=scheduled,
                        progress=lambda message: None if cancelled() else window.write_event_value("-SYNC-PROGRESS-", message),
                        cancelled=cancelled, current_only=current_only, missing_ids=selected_missing)
                window.perform_long_operation(lambda: background_job(synchronize), "-SYNCED-")
            elif event == "-SYNCED-":
                success, result = values[event]
                elapsed = perf_counter() - sync_started
                if success and result.status in ("success", "failed"):
                    cancellation.clear()
                if finish_cancel():
                    continue
                set_busy(False)
                if not success:
                    show_error(result)
                    continue
                if result.status in ("busy", "deduplicated"):
                    window["-PROGRESS-"].update(0)
                    window["-STATUS-"].update(
                        "Já existe uma atualização em andamento." if result.status == "busy"
                        else "Horário já atendido ou gerenciado pelo agendador externo.", text_color=PURPLE)
                    continue
                checked_at = datetime.now()
                try:
                    window["-UPDATED-"].update(diagnostic_summary(Diagnostics(store.cache).status(reconcile=False), settings['timezone']))
                except Exception:
                    window["-UPDATED-"].update('Diagnóstico temporariamente indisponível.')
                result = result.warnings
                window["-PROGRESS-"].update(100)
                window["-STATUS-"].update(
                    f"Verificação concluída em {format_number(elapsed, 1)} s com {count(len(result))} falhas. Consulte data/sources/sync-errors.log."
                    if result else f"Verificação concluída em {format_number(elapsed, 1)} s. Use Buscar para consultar os arquivos locais.",
                    text_color="#A12636" if result else PURPLE)
                if not result and window["-SOURCE-"].get() == "ONS":
                    window.write_event_value("-ONS-OPTIONS-", None)
                if pending_search_after_sync and not result:
                    pending_search_after_sync = False
                    window.write_event_value("-UPDATE-", values)
            elif event == "-LOADED-":
                success, result = values[event]
                if finish_cancel():
                    continue
                if not success:
                    set_busy(False)
                    show_error(result)
                    continue
                frame, alerts = result
                query = pending_query
                if pending_reuse == "limits":
                    ordered.frame = frame
                    for key in list(ordered.orders):
                        if key[0] == 7:
                            del ordered.orders[key]
                    if sort_column == 7:
                        ordered.sort(sort_column, sort_ascending)
                else:
                    if not pending_reuse:
                        updated = datetime.now()
                    sort_column = None
                    sort_ascending = True
                    ordered = TableOrder(frame)
                    page = 0
                show_page()
                configure_result_source(window, query.source)
                for column, label in zip(window["-TABLE-"].Widget["columns"], table_headings(query.source)):
                    window["-TABLE-"].Widget.heading(column, text=label)
                if pending_reuse != "limits":
                    summary = list(indicators(frame).values())
                for key, label, number, unit in zip(
                    ("-PRICE-AVG-", "-GEN-AVG-", "-MAX-CHANGE-"),
                    ("Preço médio", "Geração média", "Maior |variação|"), summary,
                    ("R$/MWh", "MW médios", "%"),
                ):
                    window[key].update(f"{label}: —" if number is None else f"{label}: {compact(number)} {unit}")
                applied = (f"Exibindo: {query.source} · {query.start:%d/%m/%Y} a {query.end:%d/%m/%Y} · "
                           f"{'Submercado' if query.source == 'CCEE' else 'ID ONS'}: {query.location}")
                if query.source == "ONS":
                    applied += (f" · Submercado: {query.ons_submarket} · Estado: {query.state}"
                                f" · Tipo: {query.plant_type} · Combustível: {query.fuel}"
                                f" · Nome: {query.ons_name}")
                window["-APPLIED-"].update(f"{applied} · {count(len(frame))} registros")
                window["-ALERT-COUNT-"].update(
                    f"ATENÇÃO: {count(len(alerts))} alertas encontrados" if alerts else "Nenhum alerta encontrado",
                    background_color=ALERT_COLOR if alerts else "#E9E3F0",
                    text_color="#FFFFFF" if alerts else PURPLE)
                window["-ALERTS-"].update(
                    ("\n\n".join(alerts[:500]) +
                     (f"\n\nExibindo 500 de {count(len(alerts))} alertas. A exportação contém todos." if len(alerts) > 500 else ""))
                    if alerts else "Nenhum limite ultrapassado nesta consulta.",
                    background_color=ALERT_BACKGROUND if alerts else "#FFFFFF",
                    text_color=ALERT_COLOR if alerts else PURPLE,
                    font=("Arial", 11, "bold") if alerts else ("Arial", 11))
                window["-ALERTS-"].Widget.yview_moveto(0)
                owned.refresh_alerts(frame, query, updated, alerts)
                window["-STATUS-"].update("Consulta local concluída. Filtros e limites serão aplicados na próxima consulta.", text_color=PURPLE)
                window["-PROGRESS-"].update(100)
                set_busy(False)
            elif event == "-SORT-" and not busy and frame is not None:
                column = values[event]
                if not isinstance(column, int) or not 0 <= column < len(TABLE_COLUMNS):
                    continue
                sort_ascending = not sort_ascending if sort_column == column else column != 7
                sort_column = column
                ordered.sort(column, sort_ascending)
                page = 0
                show_page()
                for index, (heading, label) in enumerate(zip(window["-TABLE-"].Widget["columns"], table_headings(query.source))):
                    marker = (" ▲" if sort_ascending else " ▼") if index == column else ""
                    window["-TABLE-"].Widget.heading(heading, text=label + marker)
            elif event in ("-PAGE-PREV-", "-PAGE-NEXT-", "-PAGE-FIRST-", "-PAGE-LAST-",
                           "-PAGE-BACK10-", "-PAGE-FWD10-", "-PAGE-GO-", "-PAGE-JUMP--GO-") and not busy and ordered is not None:
                last = max(0, (len(ordered) - 1) // PAGE_SIZE)
                if event in ("-PAGE-GO-", "-PAGE-JUMP--GO-"):
                    try:
                        target = int(values["-PAGE-JUMP-"]) - 1
                        if not 0 <= target <= last:
                            raise ValueError
                    except ValueError:
                        window["-STATUS-"].update(f"Informe uma página de 1 a {last + 1}.", text_color=ALERT_COLOR)
                        continue
                elif event in ("-PAGE-FIRST-", "-PAGE-LAST-"):
                    target = 0 if event == "-PAGE-FIRST-" else last
                else:
                    target = page + {"-PAGE-PREV-": -1, "-PAGE-NEXT-": 1,
                                     "-PAGE-BACK10-": -10, "-PAGE-FWD10-": 10}[event]
                page = max(0, min(last, target))
                show_page()
            elif event == "-DETAILS-" and not busy and frame is not None:
                owned.show_alerts(frame, query, updated, alerts)
            elif event == "-EXPORT-" and not busy and frame is not None:
                destination = owned.choose_file(f"energy_monitor_{query.source.lower()}_{query.end:%Y%m%d}.xlsx")
                if not destination:
                    continue
                path = Path(destination).expanduser()
                if path.suffix.lower() != ".xlsx":
                    show_error("Escolha um arquivo com extensão .xlsx.")
                    continue
                set_busy(True, "export")
                progress = 0
                window["-PROGRESS-"].update(progress)
                window["-STATUS-"].update("Exportando Excel…", text_color=PURPLE)
                export_finished.clear()
                def export_snapshot():
                    try:
                        return background_job(export_excel, path, ordered, alerts, query, updated,
                                              cancelled=cancelled, progress=query_progress)
                    finally:
                        export_finished.set()
                window.perform_long_operation(export_snapshot, "-EXPORTED-")
            elif event == "-EXPORTED-":
                success, result = values[event]
                if not success and finish_cancel():
                    continue
                set_busy(False)
                if success:
                    window["-PROGRESS-"].update(100)
                    window["-STATUS-"].update(f"Excel exportado: {path}", text_color=PURPLE)
                else:
                    show_error(result)
    finally:
        stopping.set()
        export_finished.wait()
        window.close()
