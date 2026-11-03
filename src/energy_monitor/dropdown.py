"""Lista de seleção desenhada na janela principal, sem popup do sistema."""

import tkinter as tk


def find_prefix_index(values, prefix, start=0):
    """Return the first option matching a typed prefix, wrapping around."""
    if not prefix or not values:
        return None
    folded = prefix.casefold()
    for offset in range(len(values)):
        index = (start + offset) % len(values)
        if str(values[index]).casefold().startswith(folded):
            return index
    return None


class Dropdown:
    def __init__(self, combo, on_select):
        self.combo = combo
        self.root = combo.winfo_toplevel()
        self.on_select = on_select
        self.panel = None
        self.listbox = None
        self.search_text = ''
        self.search_after = None
        combo.bind('<ButtonPress-1>', self.toggle)
        combo.bind('<ButtonRelease-1>', lambda event: 'break')
        for key in ('<Down>', '<Up>', '<Alt-Down>', '<F4>', '<space>'):
            combo.bind(key, self.toggle)
        combo.bind('<Escape>', self.close)
        combo.bind('<KeyPress>', self.typeahead)
        self.root.bind('<ButtonPress-1>', self.outside_click, add='+')
        self.root.bind('<Configure>', self.parent_changed, add='+')

    def parent_changed(self, event):
        if event.widget == self.root:
            self.close()

    def outside_click(self, event):
        if self.panel is None:
            return
        widget = event.widget
        while widget is not None:
            if widget in (self.combo, self.panel):
                return
            widget = getattr(widget, 'master', None)
        self.close()

    def toggle(self, event=None):
        if self.combo.instate(['disabled']):
            return 'break'
        if self.panel is not None:
            self.close()
            return 'break'
        active = getattr(self.root, '_energy_dropdown', None)
        if active is not None:
            active.close()
        self.root._energy_dropdown = self
        values = self.combo.cget('values')
        self.panel = tk.Frame(self.root, background='#330072', padx=1, pady=1)
        self.listbox = tk.Listbox(
            self.panel, height=max(1, min(10, len(values))), borderwidth=0, highlightthickness=0,
            exportselection=False, activestyle='none', font=self.combo.cget('font'),
            background='white', foreground='#330072',
            selectbackground='#009cde', selectforeground='#330072',
        )
        scrollbar = tk.Scrollbar(self.panel, orient='vertical', command=self.listbox.yview)
        scrollbar.pack(side='right', fill='y')
        self.listbox.configure(yscrollcommand=scrollbar.set)
        self.listbox.pack(side='left', fill='both', expand=True)
        for value in values:
            self.listbox.insert('end', value)
        if self.combo.get() in values:
            index = values.index(self.combo.get())
            self.listbox.selection_set(index)
            self.listbox.activate(index)
            self.listbox.see(index)
        self.listbox.bind('<ButtonRelease-1>', self.pick)
        self.listbox.bind('<Return>', self.pick)
        self.listbox.bind('<space>', self.pick)
        self.listbox.bind('<Escape>', self.close)
        self.listbox.bind('<KeyPress>', self.typeahead)
        def _on_mousewheel(event):
            if event.num == 4:
                self.listbox.yview_scroll(-1, 'units')
            elif event.num == 5:
                self.listbox.yview_scroll(1, 'units')
            elif event.delta:
                self.listbox.yview_scroll(int(-1 * (event.delta / 120)), 'units')
        self.listbox.bind('<MouseWheel>', _on_mousewheel)
        self.listbox.bind('<Button-4>', _on_mousewheel)
        self.listbox.bind('<Button-5>', _on_mousewheel)
        self.panel.update_idletasks()
        y = self.combo.winfo_rooty() - self.root.winfo_rooty() + self.combo.winfo_height()
        height = min(self.panel.winfo_reqheight(), max(1, self.root.winfo_height() - y))
        self.panel.place(in_=self.combo, x=0, rely=1, width=self.combo.winfo_width(),
                         height=height, bordermode='outside')
        self.panel.lift()
        self.panel.update_idletasks()
        self.listbox.focus_set()
        return 'break'

    def typeahead(self, event):
        if not event.char or not event.char.isprintable():
            return
        if self.panel is None:
            self.toggle()
        if self.search_after is not None:
            self.combo.after_cancel(self.search_after)
        self.search_text += event.char
        self.search_after = self.combo.after(800, self.reset_typeahead)
        values = self.listbox.get(0, 'end')
        current = self.listbox.index('active') if values else 0
        index = find_prefix_index(values, self.search_text, current)
        if index is None:
            self.search_text = event.char
            index = find_prefix_index(values, self.search_text, current)
        if index is not None:
            self.listbox.selection_clear(0, 'end')
            self.listbox.selection_set(index)
            self.listbox.activate(index)
            self.listbox.see(index)
        return 'break'

    def reset_typeahead(self):
        self.search_after = None
        self.search_text = ''

    def pick(self, event):
        if event.type == tk.EventType.ButtonRelease:
            index = self.listbox.nearest(event.y)
        else:
            index = self.listbox.index('active')
        value = self.listbox.get(index)
        # A lista sai da própria superfície da principal antes de aplicar o valor.
        self.close()
        self.combo.set(value)
        self.combo.focus_set()
        self.root.update_idletasks()
        self.on_select(value)
        return 'break'

    def close(self, event=None):
        panel, self.panel = self.panel, None
        if self.search_after is not None:
            self.combo.after_cancel(self.search_after)
            self.search_after = None
        self.search_text = ''
        if panel is not None:
            panel.place_forget()
            panel.destroy()
        if event is not None and event.type == tk.EventType.KeyPress:
            self.combo.focus_set()
        return 'break'
