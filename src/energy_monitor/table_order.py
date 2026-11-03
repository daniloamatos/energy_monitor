"""Sort row positions, materializing only the page being displayed."""
from collections import OrderedDict

import numpy as np
import pandas as pd

from .preview import TABLE_COLUMNS


class PageIndexer:
    def __init__(self, owner):
        self.owner = owner

    def __getitem__(self, selection):
        positions = self.owner.positions
        if positions is None:
            return self.owner.frame.iloc[selection]
        if positions is not None:
            if isinstance(selection, slice):
                forward = (selection.start is None or selection.start >= 0) and (selection.stop is not None and selection.stop >= 0) and selection.step in (None, 1)
                needed = selection.stop if forward else len(self.owner)
            else:
                needed = selection + 1 if isinstance(selection, int) and selection >= 0 else len(self.owner)
            if needed is None or needed > len(positions):
                self.owner._complete()
                positions = self.owner.positions
        return self.owner.frame.iloc[positions[selection]]


class TableOrder:
    def __init__(self, frame, *, max_cache_bytes=32 * 1024 * 1024):
        self.frame = frame
        self.positions = None
        self.iloc = PageIndexer(self)
        self.column = None
        self.orders = OrderedDict()
        self.max_cache_bytes = max_cache_bytes
        self.ascending = True
        self.date_ordered = None

    def __len__(self):
        return len(self.frame)

    @property
    def columns(self):
        return self.frame.columns

    @property
    def attrs(self):
        return self.frame.attrs

    def __getitem__(self, column):
        # Global indicators do not depend on display order.
        return self.frame[column]

    def itertuples(self, index=False, name=None):
        for start in range(0, len(self), 8192):
            yield from self.iloc[start:start + 8192].itertuples(index=index, name=name)

    def _is_date_ordered(self):
        if self.date_ordered is None:
            self.date_ordered = self.frame[TABLE_COLUMNS[0]].is_monotonic_increasing
        return self.date_ordered

    def _keys(self, column, ascending):
        values = self.frame[TABLE_COLUMNS[column]]
        missing = pd.isna(values).to_numpy()
        valid = np.flatnonzero(~missing)
        if column in (0, 1, 2, 6):
            codes, unique = pd.factorize(values, sort=False)
            if column == 0:
                ranks = pd.to_datetime(unique).asi8
            else:
                ranks = pd.Series(unique).str.casefold().rank(method='dense').to_numpy()
            keys = ranks[codes[valid]]
        else:
            keys = values.to_numpy()[valid]
            if keys.dtype.kind not in 'iuf':
                keys = pd.to_numeric(keys).astype(float)
        if not ascending:
            keys = ~keys if keys.dtype.kind in 'iu' else -keys
        return keys, valid, np.flatnonzero(missing)

    def _remember(self, key, positions):
        positions = positions.astype(np.int32 if len(self) <= np.iinfo(np.int32).max else np.int64, copy=False)
        self.orders.pop(key, None)
        while self.orders and sum(value.nbytes for value in self.orders.values()) + positions.nbytes > self.max_cache_bytes:
            self.orders.popitem(last=False)
        if positions.nbytes <= self.max_cache_bytes:
            self.orders[key] = positions
        self.positions = positions

    def sort(self, column, ascending=True):
        self.column, self.ascending = column, ascending
        cache_key = (column, ascending)
        if cache_key in self.orders:
            self.positions = self.orders[cache_key]
            self.orders.move_to_end(cache_key)
            return self
        if column == 0 and ascending and self._is_date_ordered():
            self._remember(cache_key, np.arange(min(len(self), 1000)))
            return self
        keys, valid, missing = self._keys(column, ascending)
        # Display the exact stable first page without sorting millions of unseen rows.
        if len(valid) > 100_000:
            size = 1000
            boundary = np.partition(keys, size - 1)[size - 1]
            lower = np.flatnonzero(keys < boundary)
            equal = np.flatnonzero(keys == boundary)[:size - len(lower)]
            selected = np.concatenate((lower, equal))
            positions = valid[selected[np.argsort(keys[selected], kind='stable')]]
        else:
            positions = np.concatenate((valid[np.argsort(keys, kind='stable')], missing))
        self._remember(cache_key, positions)
        return self

    def _complete(self):
        if self.positions is None or len(self.positions) == len(self):
            return
        if self.column == 0 and self.ascending and self._is_date_ordered():
            self._remember((self.column, self.ascending), np.arange(len(self)))
            return
        keys, valid, missing = self._keys(self.column, self.ascending)
        self._remember((self.column, self.ascending), np.concatenate((valid[np.argsort(keys, kind='stable')], missing)))
