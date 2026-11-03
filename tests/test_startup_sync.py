import unittest

from energy_monitor.gui import should_prompt_missing_files


class StartupSyncPromptTests(unittest.TestCase):
    def test_startup_with_any_local_csv_does_not_prompt(self):
        self.assertFalse(should_prompt_missing_files(
            "-STARTUP-SYNC-", has_local_csv=True, missing_files=["future-source"]))

    def test_startup_without_local_csv_prompts_for_missing_files(self):
        self.assertTrue(should_prompt_missing_files(
            "-STARTUP-SYNC-", has_local_csv=False, missing_files=["current-source"]))

    def test_manual_update_keeps_prompt_for_missing_files(self):
        self.assertTrue(should_prompt_missing_files(
            "-VERIFY-", has_local_csv=True, missing_files=["current-source"]))

    def test_no_missing_files_never_prompts(self):
        self.assertFalse(should_prompt_missing_files(
            "-STARTUP-SYNC-", has_local_csv=False, missing_files=[]))
