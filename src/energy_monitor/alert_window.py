"""Janela ampliada com os critérios e motivos dos alertas da última consulta."""

import tkinter as tk
from tkinter.scrolledtext import ScrolledText

from .preview import AlertCollection
from .number_format import compact, count

PAGE_SIZE = 100


class AlertWindow:
    def __init__(self, parent, frame, query, updated, alerts=None):
        self.root = tk.Toplevel(parent)
        self.root.title('Detalhes dos alertas • Energy Monitor')
        self.root.configure(background='#F0F0F0', padx=16, pady=16)
        self.root.transient(parent)
        self.root.resizable(True, True)
        self.root.minsize(560, 480)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)
        self.heading = tk.Label(self.root, anchor='w', padx=12, pady=12,
                                font=('Arial', 16, 'bold'))
        self.heading.grid(row=0, column=0, sticky='ew')
        self.context = tk.Label(self.root, anchor='w', justify='left', background='#F0F0F0',
                                foreground='#330072', font=('Arial', 10), pady=10)
        self.context.grid(row=1, column=0, sticky='ew')
        self.root.bind('<Configure>', lambda event: self.context.configure(wraplength=max(200, event.width - 32))
                       if event.widget == self.root else None)
        self.text = ScrolledText(self.root, wrap='word', font=('Arial', 11), borderwidth=0,
                                 padx=16, pady=12, background='white', foreground='#330072')
        self.text.grid(row=2, column=0, sticky='nsew')
        self.text.tag_configure('heading', font=('Arial', 12, 'bold'), foreground='#B42318', spacing1=14, spacing3=6)
        self.text.tag_configure('detail', spacing3=8)
        navigation = tk.Frame(self.root, background='#F0F0F0', pady=8)
        navigation.grid(row=3, column=0, sticky='ew')
        for column in range(6):
            navigation.columnconfigure(column, weight=1, uniform='navigation')
        tk.Button(navigation, text='Primeira', command=lambda: self.go_to('1')).grid(row=0, column=0, sticky='ew', padx=2, ipady=3)
        self.back_ten = tk.Button(navigation, text='−10', command=lambda: self.move_page(-10))
        self.back_ten.grid(row=0, column=1, sticky='ew', padx=2, ipady=3)
        self.previous = tk.Button(navigation, text='Anterior', command=lambda: self.move_page(-1))
        self.previous.grid(row=0, column=2, sticky='ew', padx=2, ipady=3)
        self.next = tk.Button(navigation, text='Próxima', command=lambda: self.move_page(1))
        self.next.grid(row=0, column=3, sticky='ew', padx=2, ipady=3)
        self.forward_ten = tk.Button(navigation, text='+10', command=lambda: self.move_page(10))
        self.forward_ten.grid(row=0, column=4, sticky='ew', padx=2, ipady=3)
        tk.Button(navigation, text='Última', command=lambda: self.go_to(str(self.pages))).grid(row=0, column=5, sticky='ew', padx=2, ipady=3)
        jump = tk.Frame(navigation, background='#F0F0F0')
        jump.grid(row=1, column=0, columnspan=6, sticky='ew', pady=(8, 0))
        jump.columnconfigure(0, weight=1)
        self.page_label = tk.Label(jump, background='#F0F0F0', foreground='#330072', anchor='w')
        self.page_label.grid(row=0, column=0, sticky='ew')
        tk.Label(jump, text='Ir à página', background='#F0F0F0').grid(row=0, column=1, padx=6)
        self.page_input = tk.Entry(jump, width=9)
        self.page_input.grid(row=0, column=2)
        self.page_input.bind('<Return>', lambda event: self.go_to(self.page_input.get()))
        tk.Button(jump, text='Ir', width=5, command=lambda: self.go_to(self.page_input.get())).grid(row=0, column=3, padx=(6, 0))
        tk.Button(self.root, text='Fechar', command=self.root.destroy, background='#330072',
                  foreground='#F0F0F0', font=('Arial', 11), padx=20, pady=5).grid(row=4, column=0, sticky='e', pady=(8, 0))
        self.root.bind('<Escape>', lambda event: self.root.destroy())
        self.frame = None
        self.refresh(frame, query, updated, alerts)
        width = max(560, min(1000, parent.winfo_width()))
        height = max(480, min(700, parent.winfo_height()))
        x = parent.winfo_rootx() + max(0, (parent.winfo_width() - width) // 2)
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - height) // 2)
        self.root.geometry(f'{width}x{height}+{x}+{y}')
        self.root.lift()
        self.root.focus_set()

    def refresh(self, frame, query, updated, alerts=None):
        if self.frame is frame and self.query == query and self.updated == updated:
            return
        self.frame, self.query, self.updated = frame, query, updated
        alerts = alerts if isinstance(alerts, AlertCollection) else AlertCollection(frame, query)
        self.alerts = alerts
        self.page = 0
        self.heading.configure(
            text=f'ATENÇÃO: {count(len(alerts))} alertas encontrados' if alerts else 'Nenhum alerta encontrado',
            background='#B42318' if alerts else '#E9E3F0',
            foreground='white' if alerts else '#330072')
        price = f'R$ {compact(query.price_limit)}/MWh' if query.price_limit is not None else 'desativado'
        change = f'{compact(query.change_limit)}%' if query.change_limit is not None else 'desativado'
        limits = f'variação absoluta {change}'
        filters = ''
        if query.source == 'CCEE':
            limits = f'preço {price} · {limits}'
        else:
            filters = (f'Submercado: {query.ons_submarket} · Tipo: {query.plant_type}\n'
                       f'Combustível: {query.fuel}\n')
        self.context.configure(text=(
            f"{frame.attrs.get('mode', 'Dados CSV locais')}\n"
            f'{query.source} · {query.start:%d/%m/%Y} a {query.end:%d/%m/%Y} · {query.location}\n'
            f'{filters}'
            f'Última atualização: {updated:%d/%m/%Y %H:%M:%S} (horário local)\n'
            f'Limites aplicados: {limits}'))
        self.render_page()

    def move_page(self, amount):
        self.page = max(0, min(self.pages - 1, self.page + amount))
        self.render_page()

    @property
    def pages(self):
        return max(1, (len(self.alerts) + PAGE_SIZE - 1) // PAGE_SIZE)

    def go_to(self, value):
        try:
            page = int(value)
            if not 1 <= page <= self.pages:
                raise ValueError
        except ValueError:
            self.page_label.configure(text=f'Página inválida (1–{self.pages})')
            return
        self.page = page - 1
        self.render_page()

    def render_page(self):
        alerts = self.alerts
        pages = self.pages
        self.page_label.configure(text=f'Página {self.page + 1} de {pages}')
        self.previous.configure(state='disabled' if self.page == 0 else 'normal')
        self.next.configure(state='disabled' if self.page + 1 >= pages else 'normal')
        self.back_ten.configure(state='disabled' if self.page == 0 else 'normal')
        self.forward_ten.configure(state='disabled' if self.page + 1 >= pages else 'normal')
        self.page_input.delete(0, 'end')
        self.page_input.insert(0, str(self.page + 1))
        self.text.configure(state='normal')
        self.text.delete('1.0', 'end')
        if not alerts:
            self.text.insert('end', 'Nenhum limite ultrapassado nesta consulta.\n\n')
        self.text.insert('end', 'Os motivos abaixo usam os limites da consulta concluída, exibidos acima. '
                         'Alterações nos campos da principal só serão aplicadas ao buscar novamente.\n'
                         'A variação compara registros da mesma entidade dentro do período selecionado; '
                         'o primeiro dia não gera alerta de variação. Igualdade ao limite não dispara alerta.\n\n')
        start = self.page * PAGE_SIZE
        for index, alert in enumerate(alerts.details(start, start + PAGE_SIZE), start=start + 1):
            kind = 'Preço acima do limite' if alert.kind == 'price' else 'Variação acima do limite'
            self.text.insert('end', f'{index}. {kind}\n', 'heading')
            self.text.insert('end', f'Data: {alert.day:%d/%m/%Y} · Fonte: {alert.source}\n'
                             f'{"Submercado" if alert.source == "CCEE" else "Usina"}: {alert.location}\n'
                             f'{alert.explanation}\n\n', 'detail')
        self.text.configure(state='disabled')
        self.text.yview_moveto(0)
