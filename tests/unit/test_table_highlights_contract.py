"""Alert rows are highlighted as whole lines without touching non-alert rows."""
from datetime import date

import numpy as np

from energy_monitor.preview import ALERT_COLUMN, COLUMNS
from energy_monitor.table_highlights import AlertRows


class Tree:
    def __init__(self, items):
        self.items = {item: () for item in items}
        self.tags = {}
        self.configured = {}

    def tag_configure(self, tag, **kwargs):
        self.configured[tag] = kwargs

    def get_children(self):
        return tuple(self.items)

    def item(self, item, option=None, **kwargs):
        if 'tags' in kwargs:
            self.tags[item] = kwargs['tags']
            return
        return self.tags.get(item, ())


def test_linha_com_alerta_recebe_destaque_e_linha_sem_alerta_nao(frame_factory, query_factory):
    frame = frame_factory([[date(2026, 11, 1), 'CCEE', 'Sul', 100, np.nan, np.nan],
                           [date(2026, 11, 2), 'CCEE', 'Sul', 120, np.nan, 20],
                           [date(2026, 11, 3), 'CCEE', 'Sul', 80, np.nan, -33.3]])
    frame[ALERT_COLUMN] = [0, 1, 1]
    tree = Tree(('r1', 'r2', 'r3'))
    rows = AlertRows(tree)
    rows.update(frame, query_factory(price_limit=110, change_limit=10))
    assert tree.tags == {'r2': (AlertRows.TAG,), 'r3': (AlertRows.TAG,)}
    assert 'r1' not in tree.tags
    assert tree.configured[AlertRows.TAG]['background'] == '#FCE8E6'


def test_atualizar_destaque_remove_tag_da_linha_que_deixou_de_alertar(frame_factory, query_factory):
    frame = frame_factory([[date(2026, 11, 1), 'CCEE', 'Sul', 120, np.nan, np.nan]])
    frame[ALERT_COLUMN] = [1]
    tree = Tree(('r1',))
    rows = AlertRows(tree)
    rows.update(frame, query_factory(price_limit=110))
    assert tree.tags['r1'] == (AlertRows.TAG,)
    rows.update(frame, query_factory(price_limit=130))
    assert tree.tags['r1'] == ()
