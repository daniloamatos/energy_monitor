"""Posicionamento e janelas filhas da interface desktop."""

import calendar
from datetime import date, datetime
import json
from pathlib import Path
import platform
import subprocess
import tkinter as tk
from tkinter import filedialog

from screeninfo import get_monitors

PURPLE = "#330072"
BLUE = "#009cde"
BACKGROUND = "#F0F0F0"
ICON_PATH = Path(__file__).with_name("assets") / "energy-monitor.png"
MONTHS = ("Janeiro", "Fevereiro", "Março", "Abril", "Maio", "Junho",
          "Julho", "Agosto", "Setembro", "Outubro", "Novembro", "Dezembro")


def primary_monitor(monitors):
    """Usa a indicação nativa ou converte a posição do principal do Windows no WSL."""
    if len(monitors) == 1:
        return monitors[0]
    marked = next((monitor for monitor in monitors if monitor.is_primary), None)
    if marked is not None:
        return marked
    if 'microsoft' in platform.release().lower():
        screens = windows_screens()
        primary = next(screen for screen in screens if screen['Primary'])
        x = primary['X'] - min(screen['X'] for screen in screens) + min(monitor.x for monitor in monitors)
        y = primary['Y'] - min(screen['Y'] for screen in screens) + min(monitor.y for monitor in monitors)
        match = next((monitor for monitor in monitors if monitor.x == x and monitor.y == y), None)
        if match is None:
            raise RuntimeError('Não foi possível associar o monitor principal do Windows aos monitores do WSL.')
        return match
    return next(
        (monitor for monitor in monitors
         if monitor.x <= 0 < monitor.x + monitor.width
         and monitor.y <= 0 < monitor.y + monitor.height), monitors[0])


def windows_screens():
    command = ('Add-Type -AssemblyName System.Windows.Forms; '
               '[System.Windows.Forms.Screen]::AllScreens | ForEach-Object { '
               '[PSCustomObject]@{Primary=$_.Primary;X=$_.Bounds.X;Y=$_.Bounds.Y;'
               'Width=$_.Bounds.Width;Height=$_.Bounds.Height} } | ConvertTo-Json -Compress')
    try:
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', command],
                                check=True, capture_output=True, text=True, timeout=5)
        screens = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        raise RuntimeError('Não foi possível consultar o monitor principal no Windows. Verifique a integração Windows/WSL.') from error
    return screens if isinstance(screens, list) else [screens]


def initial_window_geometry():
    monitor = primary_monitor(get_monitors())
    width = min(1280, monitor.width - 80)
    height = min(920, monitor.height - 100)
    return ((monitor.x + (monitor.width - width) // 2,
             monitor.y + (monitor.height - height) // 2), (width, height))


def create_main_window(title, layout, **options):
    """Aplica geometria antes do primeiro Map, inclusive durante finalize()."""
    import PySimpleGUI as sg

    location, size = initial_window_geometry()
    original_toplevel = sg.tk.Toplevel

    class PositionedToplevel(original_toplevel):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.withdraw()
            self.geometry(f'{size[0]}x{size[1]}+{location[0]}+{location[1]}')

    # O PySimpleGUI chama update_idletasks antes de aplicar location. Mantém
    # apenas a construção desta janela retirada para evitar um primeiro Map errado.
    sg.tk.Toplevel = PositionedToplevel
    try:
        window = sg.Window(title, layout, location=location, size=size,
                           location_anchor=sg.WIN_ANCHOR_UPPER_LEFT, alpha_channel=0,
                           finalize=True, **options)
    finally:
        sg.tk.Toplevel = original_toplevel
    root = window.TKroot
    root._energy_icon_images = tuple(
        tk.PhotoImage(master=root, file=str(path))
        for path in (ICON_PATH.with_name("energy-monitor-32.png"), ICON_PATH)
    )
    root.iconphoto(True, *root._energy_icon_images)
    if root.tk.call('tk', 'windowingsystem') == 'x11':
        from .titlebar import TitleBar

        root.withdraw()
        root._energy_titlebar = TitleBar(root, title, root._energy_icon_images[0])
    root.minsize(min(800, size[0]), min(600, size[1]))
    timer = None

    def reveal():
        nonlocal timer
        # WSLg confirma a moldura nativa depois de Map. Não mostra a posição
        # provisória; apenas revela a geometria solicitada antes da construção.
        x, y = root.winfo_rootx(), root.winfo_rooty()
        if (root.winfo_ismapped() and 0 <= x - location[0] <= 64
                and 0 <= y - location[1] <= 64):
            timer = None
            root._energy_frame_offset = (x - location[0], y - location[1])
            root.attributes('-alpha', 1)
        else:
            timer = root.after(10, reveal)

    def cancel_reveal(event):
        if event.widget is root and timer is not None:
            root.after_cancel(timer)

    root.bind('<Destroy>', cancel_reveal, add='+')
    root.deiconify()
    reveal()
    return window


class OwnedWindows:
    """Todos os diálogos pertencem ao root; fechá-lo encerra também seus filhos."""

    def __init__(self, root):
        self.root = root
        self.calendar = None
        self.alerts_view = None
        self.closing = False
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<FocusIn>", self.raise_calendar, add="+")

    def raise_calendar(self, event=None):
        if not self.closing and self.calendar is not None and self.calendar.winfo_exists():
            self.calendar.lift()

    def close(self):
        self.closing = True
        # destroy encerra imediatamente os filhos, inclusive diálogos Tcl nativos.
        try:
            self.root.quit()
            self.root.destroy()
        except tk.TclError:
            pass

    def choose_file(self, filename):
        try:
            return filedialog.asksaveasfilename(
                parent=self.root, title="Salvar relatório",
                initialfile=filename, defaultextension=".xlsx",
                filetypes=[("Excel", "*.xlsx")], confirmoverwrite=True,
            )
        except tk.TclError:
            if self.closing:
                return ""
            raise

    def show_alerts(self, frame, query, updated, alerts=None):
        from .alert_window import AlertWindow

        if self.alerts_view is None or not self.alerts_view.root.winfo_exists():
            self.alerts_view = AlertWindow(self.root, frame, query, updated, alerts)
        else:
            self.alerts_view.refresh(frame, query, updated, alerts)
            self.alerts_view.root.deiconify()
            self.alerts_view.root.lift()
            self.alerts_view.root.focus_set()

    def prompt_download(self, missing_resources, title="Baixar arquivos históricos"):
        """Modal overlay placed directly in the center of the main window."""
        if not missing_resources or self.closing:
            return []

        # Overlay Frame within root: guaranteed exact center of main window,
        # perfectly matching choose_date calendar behavior.
        dialog = tk.Frame(
            self.root, background=BACKGROUND, padx=16, pady=16,
            highlightthickness=2, highlightbackground=PURPLE
        )

        tk.Label(dialog, text="Arquivos não encontrados localmente:", anchor="w",
                 background=BACKGROUND, foreground=PURPLE,
                 font=("Arial", 12, "bold")).pack(fill="x", pady=(0, 8))

        container = tk.Frame(dialog, background="white", bd=1, relief="solid")
        container.pack(fill="both", expand=True)

        canvas = tk.Canvas(container, background="white", highlightthickness=0, width=500, height=260)
        scrollbar = tk.Scrollbar(container, orient="vertical", command=canvas.yview)
        scrollable = tk.Frame(canvas, background="white")

        scrollable.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas_window = canvas.create_window((0, 0), window=scrollable, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfig(canvas_window, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def _on_mousewheel(event):
            if event.num == 4:
                canvas.yview_scroll(-1, "units")
            elif event.num == 5:
                canvas.yview_scroll(1, "units")
            elif event.delta:
                canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        for widget in (canvas, scrollable):
            widget.bind("<MouseWheel>", _on_mousewheel)
            widget.bind("<Button-4>", _on_mousewheel)
            widget.bind("<Button-5>", _on_mousewheel)

        vars_by_id = {}
        for r in missing_resources:
            var = tk.BooleanVar(dialog, value=True)
            vars_by_id[r.id] = var
            period = f"{r.start:%d/%m/%Y} a {r.end:%d/%m/%Y}"
            text = f"{r.source} · {r.id} ({period})"
            cb = tk.Checkbutton(scrollable, text=text, variable=var,
                                background="white", foreground=PURPLE,
                                anchor="w", justify="left")
            cb.pack(fill="x", padx=8, pady=3)
            cb.bind("<MouseWheel>", _on_mousewheel)
            cb.bind("<Button-4>", _on_mousewheel)
            cb.bind("<Button-5>", _on_mousewheel)

        controls = tk.Frame(dialog, background=BACKGROUND)
        controls.pack(fill="x", pady=(8, 8))

        def toggle_all(val):
            for v in vars_by_id.values():
                v.set(val)

        tk.Button(controls, text="Selecionar todos", command=lambda: toggle_all(True),
                  background=BACKGROUND, foreground=PURPLE).pack(side="left", padx=(0, 6))
        tk.Button(controls, text="Desmarcar todos", command=lambda: toggle_all(False),
                  background=BACKGROUND, foreground=PURPLE).pack(side="left")

        actions = tk.Frame(dialog, background=BACKGROUND)
        actions.pack(fill="x")

        done = tk.BooleanVar(dialog, False)
        selected = []

        def dismiss():
            dialog.grab_release()
            dialog.place_forget()
            dialog.after_idle(dialog.destroy)
            self.root.focus_set()
            done.set(True)

        def on_confirm():
            nonlocal selected
            selected = [rid for rid, v in vars_by_id.items() if v.get()]
            dismiss()

        def on_cancel():
            nonlocal selected
            selected = []
            dismiss()

        tk.Button(actions, text="Cancelar", command=on_cancel,
                  background=BACKGROUND, foreground=PURPLE, padx=12, pady=4).pack(side="right", padx=(8, 0))
        tk.Button(actions, text="Baixar selecionados", command=on_confirm,
                  background=PURPLE, foreground="white", padx=16, pady=4).pack(side="right")

        dialog.bind("<Escape>", lambda e: on_cancel())

        # Exact center of main window
        dialog.place(in_=self.root, relx=0.5, rely=0.5, anchor="center")
        dialog.lift()
        dialog.grab_set()
        dialog.focus_set()
        self.root.wait_variable(done)
        return selected

    def refresh_alerts(self, frame, query, updated, alerts=None):
        if self.alerts_view is not None and self.alerts_view.root.winfo_exists():
            self.alerts_view.refresh(frame, query, updated, alerts)

    def choose_date(self, value, on_select):
        today = date.today()
        if self.calendar is not None and self.calendar.winfo_exists():
            self.calendar.event_generate("<<DismissCalendar>>")
        try:
            selected = datetime.strptime(value, "%d/%m/%Y").date()
        except ValueError:
            selected = today
        if selected > today:
            selected = today

        # Frame sobreposto à janela principal: sem Toplevel, sem gerenciador de janelas.
        child = self.calendar = tk.Frame(
            self.root, background=BACKGROUND, padx=12, pady=12,
            highlightthickness=2, highlightbackground=PURPLE)

        def dismiss(event=None):
            if not child.winfo_exists():
                return
            child.grab_release()
            child.place_forget()                 # some no mesmo instante
            child.after_idle(child.destroy)      # libera os widgets depois
            if self.calendar is child:
                self.calendar = None
            self.root.focus_set()

        child.bind("<Escape>", dismiss)
        child.bind("<<DismissCalendar>>", dismiss)

        month, year = selected.month, selected.year
        header = tk.Frame(child, background=BACKGROUND)
        header.pack(fill="x")
        title = tk.Button(header, width=22, background=BACKGROUND, foreground=PURPLE,
                          font=("Arial", 11, "bold"), relief="flat", cursor="hand2")
        body = tk.Frame(child, background=BACKGROUND)
        body.pack(pady=8)
        days = tk.Frame(body, background=BACKGROUND)
        days.pack()
        picker = tk.Frame(body, background=BACKGROUND)
        picker_month = tk.IntVar(child, month)
        picker_year = tk.StringVar(child, str(year))
        picker_error = tk.StringVar(child)
        navigation = []

        def show_period_picker():
            if picker.winfo_manager():
                render()
                return
            picker_month.set(month)
            picker_year.set(str(year))
            picker_error.set("")
            days.pack_forget()
            picker.pack()
            year_input.focus_set()
            year_input.selection_range(0, tk.END)

        def apply_period():
            nonlocal month, year
            try:
                new_year = int(picker_year.get().strip())
                if not 1 <= new_year <= today.year:
                    raise ValueError
            except ValueError:
                picker_error.set(f"Informe um ano de 1 a {today.year}.")
                year_input.focus_set()
                return
            if (new_year, picker_month.get()) > (today.year, today.month):
                picker_error.set("Selecione um mês até o mês atual.")
                return
            month, year = picker_month.get(), new_year
            render()
            child.focus_set()

        year_row = tk.Frame(picker, background=BACKGROUND)
        year_row.pack(pady=(0, 8))
        tk.Label(year_row, text="Ano", background=BACKGROUND, foreground=PURPLE).pack(side="left", padx=6)
        year_input = tk.Spinbox(year_row, from_=1, to=today.year, width=7, textvariable=picker_year,
                               font=("Arial", 11))
        year_input.pack(side="left")
        year_input.bind("<Return>", lambda event: apply_period())
        months_grid = tk.Frame(picker, background=BACKGROUND)
        months_grid.pack()
        for index, name in enumerate(MONTHS):
            tk.Radiobutton(months_grid, text=name, variable=picker_month, value=index + 1,
                           indicatoron=False, width=10, background="white", foreground=PURPLE,
                           selectcolor=BLUE).grid(row=index // 3, column=index % 3, padx=2, pady=2)

        def update_month_choices(*args):
            try:
                current_year = int(picker_year.get())
            except ValueError:
                return
            for index, button in enumerate(months_grid.winfo_children(), start=1):
                button.configure(state="disabled" if (current_year, index) > (today.year, today.month) else "normal")
            if current_year == today.year and picker_month.get() > today.month:
                picker_month.set(today.month)

        picker_year.trace_add("write", update_month_choices)
        update_month_choices()
        tk.Label(picker, textvariable=picker_error, background=BACKGROUND,
                 foreground="#A12636").pack()
        picker_actions = tk.Frame(picker, background=BACKGROUND)
        picker_actions.pack()
        tk.Button(picker_actions, text="Aplicar", command=apply_period,
                  background=PURPLE, foreground=BACKGROUND).pack(side="left", padx=4)
        tk.Button(picker_actions, text="Voltar", command=lambda: render(),
                  background=PURPLE, foreground=BACKGROUND).pack(side="left", padx=4)
        for widget in (year_input, *months_grid.winfo_children(), *picker_actions.winfo_children()):
            widget.bind("<Escape>", dismiss)
        title.configure(command=show_period_picker)

        def choose(day):
            if date(year, month, day) > today:
                return
            result = f"{day:02d}/{month:02d}/{year:04d}"
            dismiss()
            self.root.after_idle(lambda: on_select(result))

        def click_day(event, day):
            choose(day)
            return "break"

        def render():
            picker.pack_forget()
            days.pack()
            title.configure(text=f"{MONTHS[month - 1]} {year}")
            for button, delta in navigation:
                target_year, target_month = divmod(year * 12 + month - 1 + delta, 12)
                allowed = 1 <= target_year and (target_year, target_month + 1) <= (today.year, today.month)
                button.configure(state="normal" if allowed else "disabled")
            for widget in days.winfo_children():
                widget.destroy()
            for column, name in enumerate(("Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom")):
                tk.Label(days, text=name, background=PURPLE, foreground=BACKGROUND,
                         width=4).grid(row=0, column=column, padx=1, pady=1)
            for row, week in enumerate(calendar.monthcalendar(year, month), start=1):
                for column, day in enumerate(week):
                    if day:
                        button = tk.Button(
                            days, text=str(day), width=4, borderwidth=0,
                            state="disabled" if date(year, month, day) > today else "normal",
                            background=BLUE if date(year, month, day) == selected else "white",
                            foreground=PURPLE, command=lambda day=day: choose(day))
                        button.grid(row=row, column=column, padx=1, pady=1)
                        button.bind("<ButtonRelease-1>", lambda event, day=day: click_day(event, day))

        def move(amount):
            nonlocal month, year
            absolute = year * 12 + month - 1 + amount
            new_year, new_month = divmod(absolute, 12)
            if 1 <= new_year and (new_year, new_month + 1) <= (today.year, today.month):
                year, month = new_year, new_month + 1
                render()

        for label, delta in (("«", -12), ("‹", -1)):
            button = tk.Button(header, text=label, command=lambda delta=delta: move(delta),
                               background=PURPLE, foreground=BACKGROUND)
            button.pack(side="left")
            navigation.append((button, delta))
        title.pack(side="left")
        for label, delta in (("›", 1), ("»", 12)):
            button = tk.Button(header, text=label, command=lambda delta=delta: move(delta),
                               background=PURPLE, foreground=BACKGROUND)
            button.pack(side="left")
            navigation.append((button, delta))
        tk.Button(child, text="Cancelar", command=dismiss,
                  background=PURPLE, foreground=BACKGROUND).pack()

        render()
        child.place(in_=self.root, relx=0.5, rely=0.5, anchor="center")
        child.lift()
        child.grab_set()      # comportamento modal: cliques fora do calendário são ignorados
        child.focus_set()     # para o Escape funcionar
        return child
