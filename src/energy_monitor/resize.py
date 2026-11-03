"""Áreas de arraste nas quatro bordas e nos quatro cantos da principal."""

import tkinter as tk


def resized_rectangle(rectangle, edge, dx, dy, minimum):
    x, y, width, height = rectangle
    right, bottom = x + width, y + height
    if 'w' in edge:
        x = min(x + dx, right - minimum[0])
        width = right - x
    elif 'e' in edge:
        width = max(minimum[0], width + dx)
    if 'n' in edge:
        y = min(y + dy, bottom - minimum[1])
        height = bottom - y
    elif 's' in edge:
        height = max(minimum[1], height + dy)
    return x, y, width, height


class ResizeHandles:
    def __init__(self, root):
        self.root = root
        self.handles = {}
        self.drag = None
        cursors = {
            'n': 'top_side', 's': 'bottom_side', 'w': 'left_side', 'e': 'right_side',
            'nw': 'top_left_corner', 'ne': 'top_right_corner',
            'sw': 'bottom_left_corner', 'se': 'bottom_right_corner',
        }
        if root.tk.call('tk', 'windowingsystem') == 'win32':
            cursors = {edge: 'size_we' if edge in ('w', 'e') else 'size_ns' if edge in ('n', 's')
                       else 'size_nw_se' if edge in ('nw', 'se') else 'size_ne_sw' for edge in cursors}
        placements = {
            'n': dict(relwidth=1, height=6),
            's': dict(rely=1, anchor='sw', relwidth=1, height=6),
            'w': dict(relheight=1, width=6),
            'e': dict(relx=1, anchor='ne', relheight=1, width=6),
            'nw': dict(width=12, height=12),
            'ne': dict(relx=1, anchor='ne', width=12, height=12),
            'sw': dict(rely=1, anchor='sw', width=12, height=12),
            'se': dict(relx=1, rely=1, anchor='se', width=12, height=12),
        }
        titlebar = getattr(root, '_energy_titlebar', None)
        if titlebar is not None:
            top = titlebar.frame.winfo_reqheight()
            for edge in ('n', 'nw', 'ne'):
                placements[edge]['y'] = top
            for edge in ('w', 'e'):
                placements[edge].update(y=top, height=-top)
        for edge, placement in placements.items():
            handle = tk.Frame(root, background=root.cget('background'), cursor=cursors[edge])
            handle.place(bordermode='outside', **placement)
            handle.lift()
            handle.bind('<ButtonPress-1>', lambda event, edge=edge: self.start(event, edge))
            handle.bind('<B1-Motion>', self.motion)
            handle.bind('<ButtonRelease-1>', self.stop)
            self.handles[edge] = handle

    def start(self, event, edge):
        titlebar = getattr(self.root, '_energy_titlebar', None)
        if self.root.state() != 'normal' or (titlebar is not None and titlebar.maximized):
            return 'break'
        inset_x, inset_y = getattr(self.root, '_energy_frame_offset', (0, 0))
        rectangle = (self.root.winfo_rootx() - inset_x, self.root.winfo_rooty() - inset_y,
                     self.root.winfo_width(), self.root.winfo_height())
        self.drag = (edge, event.x_root, event.y_root, rectangle)
        event.widget.grab_set()
        return 'break'

    def motion(self, event):
        if self.drag is None:
            return 'break'
        edge, pointer_x, pointer_y, rectangle = self.drag
        x, y, width, height = resized_rectangle(
            rectangle, edge, event.x_root - pointer_x, event.y_root - pointer_y, self.root.minsize())
        self.root.geometry(f'{width}x{height}+{x}+{y}')
        return 'break'

    def stop(self, event):
        self.drag = None
        event.widget.grab_release()
        return 'break'
