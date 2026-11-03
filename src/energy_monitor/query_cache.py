"""Bounded LRU of compact daily aggregates, never raw CSV observations."""
from collections import OrderedDict
from datetime import timedelta

import pandas as pd

from .cancellation import check_cancelled


class QueryCache:
    def __init__(self, max_bytes=32 * 1024 * 1024):
        self.max_bytes = max_bytes
        self.entries = OrderedDict()
        self.bytes_used = 0

    def key(self, path, source, location, filters):
        stat = path.stat()
        return (str(path.resolve()), stat.st_size, stat.st_mtime_ns, source, location,
                tuple(sorted((filters or {}).items())))

    def covers(self, path, source, start, end, location, filters):
        try:
            entry = self.entries.get(self.key(path, source, location, filters))
        except OSError:
            return False
        return entry is not None and entry[0] <= start and entry[1] >= end

    def read(self, path, source, start, end, location, filters, loader, *, cancelled=lambda: False, progress=None):
        check_cancelled(cancelled)
        key = self.key(path, source, location, filters)
        entry = self.entries.get(key)
        # Remove invalidated versions of this CSV without retaining stale copies.
        for old in list(self.entries):
            if old[0] == key[0] and old[1:3] != key[1:3]:
                self.bytes_used -= self.entries.pop(old)[3]
        if entry is not None and entry[0] <= start and entry[1] >= end:
            self.entries.move_to_end(key)
            if progress:
                progress('Reutilizando agregados em memória.')
            return self._select(entry[2], start, end)
        spans = [(start, end)]
        pieces = []
        low, high = start, end
        if entry is not None:
            old_start, old_end, packed, _ = entry
            if start <= old_end + timedelta(days=1) and end >= old_start - timedelta(days=1):
                low, high = min(start, old_start), max(end, old_end)
                spans = []
                if start < old_start:
                    spans.append((start, old_start - timedelta(days=1)))
                if end > old_end:
                    spans.append((old_end + timedelta(days=1), end))
                pieces.append(self._select(packed, old_start, old_end))
        for first, last in spans:
            check_cancelled(cancelled)
            pieces.append(loader(path, source, first, last, location, filters,
                                 cancelled=cancelled, progress=progress))
        check_cancelled(cancelled)
        data = pd.concat(pieces, ignore_index=True) if len(pieces) > 1 else pieces[0]
        if self.max_bytes:
            # An oversized candidate is rejected before constructing a retained copy.
            estimate = len(data) * 32 + 2048
            if estimate <= self.max_bytes:
                packed = pd.DataFrame({
                    'day': pd.to_datetime(data.day),
                    'entity': data.entity.astype('category'),
                    'location': data.location.astype('category'),
                    'sum': data['sum'].to_numpy(copy=True),
                    'count': data['count'].to_numpy(copy=True),
                }, copy=False)
                data['entity'] = packed['entity']
                data['location'] = packed['location']
                size = int(packed.memory_usage(index=True, deep=True).sum()) + 2048
                if size <= self.max_bytes:
                    if key in self.entries:
                        self.bytes_used -= self.entries.pop(key)[3]
                    while self.entries and self.bytes_used + size > self.max_bytes:
                        self.bytes_used -= self.entries.popitem(last=False)[1][3]
                    self.entries[key] = (low, high, packed, size)
                    self.bytes_used += size
        check_cancelled(cancelled)
        if low == start and high == end:
            return data
        return data.loc[data.day.between(start, end)].copy()

    @staticmethod
    def _select(packed, start, end):
        result = packed.loc[packed.day.between(pd.Timestamp(start), pd.Timestamp(end))].copy()
        result['day'] = result.day.dt.date
        return result
