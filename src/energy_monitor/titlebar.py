"""Barra de título com ícone para janelas X11, inclusive no WSLg."""

import ctypes
import ctypes.util
import tkinter as tk

from screeninfo import get_monitors

PURPLE = "#330072"
BLUE = "#009cde"
BACKGROUND = "#F0F0F0"


def monitor_rectangle(rectangle, monitors):
    """Escolhe o monitor de maior interseção, sem usar o tamanho da área virtual."""
    x, y, width, height = rectangle
    if not monitors:
        raise RuntimeError("Nenhum monitor disponível para maximizar a janela.")

    def score(monitor):
        overlap = (max(0, min(x + width, monitor.x + monitor.width) - max(x, monitor.x))
                   * max(0, min(y + height, monitor.y + monitor.height) - max(y, monitor.y)))
        distance = ((x + width / 2 - monitor.x - monitor.width / 2) ** 2
                    + (y + height / 2 - monitor.y - monitor.height / 2) ** 2)
        return overlap, -distance

    monitor = max(monitors, key=score)
    return monitor.x, monitor.y, monitor.width, monitor.height


def hide_x11_decorations(root):
    """Retira só a moldura; mantém a janela gerenciada, inclusive na barra de tarefas."""
    x11 = ctypes.CDLL(ctypes.util.find_library("X11"))
    window_type = ctypes.c_ulong
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XQueryTree.argtypes = [ctypes.c_void_p, window_type, ctypes.POINTER(window_type),
                              ctypes.POINTER(window_type), ctypes.POINTER(ctypes.POINTER(window_type)),
                              ctypes.POINTER(ctypes.c_uint)]
    x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
    x11.XInternAtom.restype = window_type
    x11.XChangeProperty.argtypes = [ctypes.c_void_p, window_type, window_type, window_type,
                                   ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int]
    x11.XFree.argtypes = [ctypes.c_void_p]
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    display = x11.XOpenDisplay(root.winfo_screen().encode())
    if not display:
        raise RuntimeError("Não foi possível configurar a barra de título X11.")
    try:
        screen_root, parent = window_type(), window_type()
        children = ctypes.POINTER(window_type)()
        count = ctypes.c_uint()
        found = x11.XQueryTree(display, root.winfo_id(), ctypes.byref(screen_root),
                               ctypes.byref(parent), ctypes.byref(children), ctypes.byref(count))
        if children:
            x11.XFree(children)
        if not found or not parent.value:
            raise RuntimeError("Não foi possível localizar a moldura da janela X11.")
        atom = x11.XInternAtom(display, b"_MOTIF_WM_HINTS", False)
        # MWM_HINTS_DECORATIONS, funções inalteradas, nenhuma decoração nativa.
        hints = (ctypes.c_ulong * 5)(2, 0, 0, 0, 0)
        x11.XChangeProperty(display, parent, atom, atom, 32, 0,
                            ctypes.cast(hints, ctypes.POINTER(ctypes.c_ubyte)), 5)
    finally:
        x11.XCloseDisplay(display)


class TitleBar:
    def __init__(self, root, title, icon):
        self.root = root
        self.drag = None
        self._restore_rectangle = None
        self._normal_rectangle = self.rectangle()
        self._restore_limits = None
        self._pending_state = None
        hide_x11_decorations(root)
        contents = root.pack_slaves()
        padx, pady = root.cget("padx"), root.cget("pady")
        root.configure(padx=0, pady=0)
        for index, row in enumerate(contents):
            row.pack_configure(padx=padx, pady=(pady if index == 0 else 0,
                                               pady if index == len(contents) - 1 else 0))
        self.frame = tk.Frame(root, background=PURPLE, height=38, borderwidth=0, highlightthickness=0)
        self.frame.pack(side="top", fill="x", padx=0, pady=0,
                        before=contents[0] if contents else None)
        self.frame.pack_propagate(False)
        self.icon = icon.subsample(2, 2)
        self.icon_label = tk.Label(self.frame, image=self.icon, background=PURPLE)
        self.icon_label.pack(side="left", padx=(12, 8))
        self.title_label = tk.Label(self.frame, text=title, background=PURPLE,
                                    foreground=BACKGROUND, font=("Arial", 10, "bold"), anchor="w")
        self.title_label.pack(side="left", fill="both", expand=True)
        self.close_button = self.button("×", self.close, active="#B42318")
        self.maximize_icons = (self.window_icon(False), self.window_icon(True))
        self.maximize_button = self.button("", self.toggle_maximize)
        self.maximize_button.configure(image=self.maximize_icons[0], width=42)
        self.minimize_button = self.button("−", root.iconify)
        root.bind("<Configure>", self.update_state, add="+")
        root.bind("<Destroy>", self.cancel_pending_state, add="+")
        for widget in (self.frame, self.icon_label, self.title_label):
            widget.bind("<ButtonPress-1>", self.start_drag)
            widget.bind("<B1-Motion>", self.drag_window)
            widget.bind("<ButtonRelease-1>", self.stop_drag)
            widget.bind("<Double-Button-1>", lambda event: self.toggle_maximize())

    def button(self, text, command, active=BLUE):
        button = tk.Button(self.frame, text=text, command=command, width=4, borderwidth=0,
                           highlightthickness=0, padx=0, pady=0,
                           relief="flat", background=PURPLE, foreground=BACKGROUND,
                           activebackground=active, activeforeground=BACKGROUND,
                           font=("Arial", 12), takefocus=False)
        button.pack(side="right", fill="y")
        button.bind("<Enter>", lambda event: button.configure(background=active))
        button.bind("<Leave>", lambda event: button.configure(background=PURPLE))
        return button

    @property
    def maximized(self):
        return self._restore_rectangle is not None

    def rectangle(self):
        return (self.root.winfo_rootx(), self.root.winfo_rooty(),
                self.root.winfo_width(), self.root.winfo_height())

    def apply_rectangle(self, rectangle):
        x, y, width, height = rectangle
        self.root.geometry(f"{width}x{height}+{x}+{y}")

    def maximize(self, rectangle):
        target = monitor_rectangle(rectangle, get_monitors())
        self._restore_rectangle = rectangle
        self._restore_limits = self.root.minsize(), self.root.maxsize()
        minimum, maximum = self._restore_limits
        self.root.attributes("-zoomed", False)
        self.root.attributes("-fullscreen", False)
        self.root.minsize(min(minimum[0], target[2]), min(minimum[1], target[3]))
        self.root.maxsize(max(maximum[0], target[2]), max(maximum[1], target[3]))
        self.apply_rectangle(target)
        self.maximize_button.configure(image=self.maximize_icons[1])

    def window_icon(self, restore):
        image = tk.PhotoImage(master=self.root, width=16, height=16)
        rectangles = ((5, 2, 14, 11), (2, 5, 11, 14)) if restore else ((3, 3, 13, 13),)
        for left, top, right, bottom in rectangles:
            image.put(BACKGROUND, to=(left, top, right, top + 1))
            image.put(BACKGROUND, to=(left, bottom - 1, right, bottom))
            image.put(BACKGROUND, to=(left, top, left + 1, bottom))
            image.put(BACKGROUND, to=(right - 1, top, right, bottom))
        return image

    def update_state(self, event):
        if event.widget is not self.root:
            return
        if self.root.attributes("-zoomed") or self.root.attributes("-fullscreen"):
            if self._pending_state is None:
                self._pending_state = self.root.after_idle(self.adopt_native_maximize)
            return
        if not self.maximized:
            self._normal_rectangle = self.rectangle()

    def adopt_native_maximize(self):
        self._pending_state = None
        if self.maximized:
            target = monitor_rectangle(self._restore_rectangle, get_monitors())
            self.root.attributes("-zoomed", False)
            self.root.attributes("-fullscreen", False)
            self.apply_rectangle(target)
        else:
            self.maximize(self._normal_rectangle)

    def cancel_pending_state(self, event):
        if event.widget is self.root and self._pending_state is not None:
            self.root.after_cancel(self._pending_state)
            self._pending_state = None

    def toggle_maximize(self):
        self.drag = None
        if self.maximized:
            rectangle = self._restore_rectangle
            minimum, maximum = self._restore_limits
            self.root.attributes("-zoomed", False)
            self.root.attributes("-fullscreen", False)
            self.root.minsize(*minimum)
            self.root.maxsize(*maximum)
            self.apply_rectangle(rectangle)
            self._normal_rectangle = rectangle
            self._restore_rectangle = None
            self._restore_limits = None
            self.maximize_button.configure(image=self.maximize_icons[0])
        else:
            self.maximize(self.rectangle())
        return "break"

    def start_drag(self, event):
        if not self.maximized:
            self.drag = (event.x_root, event.y_root, self.root.winfo_rootx(), self.root.winfo_rooty())
            event.widget.grab_set()
        return "break"

    def drag_window(self, event):
        if self.drag is not None:
            pointer_x, pointer_y, x, y = self.drag
            self.root.geometry(f"+{x + event.x_root - pointer_x}+{y + event.y_root - pointer_y}")
        return "break"

    def stop_drag(self, event):
        self.drag = None
        event.widget.grab_release()
        return "break"

    def close(self):
        command = self.root.protocol("WM_DELETE_WINDOW")
        if command:
            self.root.tk.call(command)
        else:
            self.root.destroy()
