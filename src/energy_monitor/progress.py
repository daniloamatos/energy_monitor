"""Readable progress messages backed by completed work units."""


class Progress(str):
    def __new__(cls, message, completed, total):
        value = super().__new__(cls, message)
        value.completed = completed
        value.total = total
        return value

    @property
    def percent(self):
        return max(0, min(100, int(100 * self.completed / self.total))) if self.total else 0
