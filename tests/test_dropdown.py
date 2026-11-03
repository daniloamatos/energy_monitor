import unittest

from energy_monitor.dropdown import find_prefix_index


class DropdownTypeaheadTests(unittest.TestCase):
    def test_prefix_search_is_case_insensitive_and_wraps(self):
        values = ("Todos", "Aparecida", "Balbina", "Abaeté")
        self.assertEqual(find_prefix_index(values, "a"), 1)
        self.assertEqual(find_prefix_index(values, "ab", start=1), 3)
        self.assertEqual(find_prefix_index(values, "a", start=2), 3)
        self.assertIsNone(find_prefix_index(values, "xyz"))
