"""Highlight complete alert rows using native Treeview tags."""

from .preview import alert_cells


class AlertRows:
    TAG = "energy-alert-row"

    def __init__(self, table):
        self.table = table
        self.original_tags = {}
        table.tag_configure(self.TAG, background="#FCE8E6", foreground="#8F2520")

    def update(self, frame, query):
        items = self.table.get_children()
        for item in items:
            if self.TAG in self.table.item(item, "tags"):
                self.table.item(item, tags=self.original_tags.get(item, ()))
        self.original_tags.clear()
        flags = alert_cells(frame, query).any(axis=1)
        for item, active in zip(items, flags):
            if active:
                self.original_tags[item] = self.table.item(item, "tags")
                self.table.item(item, tags=(self.TAG,))
