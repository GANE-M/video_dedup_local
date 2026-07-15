import tempfile
import threading
import time
import unittest
import os
import subprocess
import sys
from pathlib import Path
from unittest import mock

import global_slots


class GlobalSlotTests(unittest.TestCase):
    def test_slot_is_shared_between_independent_python_processes(self) -> None:
        script = (
            "import time; from global_slots import global_asr_slot; "
            "lease=global_asr_slot(1, logger=lambda _message: None); "
            "lease.__enter__(); print('LOCKED', flush=True); time.sleep(0.45); lease.__exit__(None,None,None)"
        )
        with tempfile.TemporaryDirectory() as temp_name:
            environment = dict(os.environ)
            environment["VIDEO_DEDUP_GLOBAL_SLOT_DIR"] = temp_name
            child = subprocess.Popen(
                [sys.executable, "-c", script],
                cwd=Path(__file__).resolve().parent,
                env=environment,
                stdout=subprocess.PIPE,
                text=True,
            )
            self.assertEqual(child.stdout.readline().strip(), "LOCKED")
            started = time.monotonic()
            with mock.patch.dict("os.environ", {"VIDEO_DEDUP_GLOBAL_SLOT_DIR": temp_name}):
                with global_slots.global_asr_slot(1, logger=lambda _message: None, poll_seconds=0.02):
                    waited = time.monotonic() - started
            child.wait(timeout=2)
            child.stdout.close()

        self.assertGreaterEqual(waited, 0.30)

    def test_second_waiter_blocks_until_machine_slot_is_released(self) -> None:
        events: list[str] = []
        acquired = threading.Event()
        with tempfile.TemporaryDirectory() as temp_name, mock.patch.dict(
            "os.environ", {"VIDEO_DEDUP_GLOBAL_SLOT_DIR": temp_name}
        ):
            def waiter() -> None:
                with global_slots.global_asr_slot(1, "ASR-B", events.append, poll_seconds=0.02):
                    acquired.set()

            with global_slots.global_asr_slot(1, "ASR-A", events.append, poll_seconds=0.02):
                thread = threading.Thread(target=waiter)
                thread.start()
                time.sleep(0.08)
                self.assertFalse(acquired.is_set())
                self.assertTrue(any("等待全局 ASR 槽位" in event for event in events))
            thread.join(timeout=2)

        self.assertTrue(acquired.is_set())
        self.assertTrue(any("释放全局 ASR 槽位" in event for event in events))

    def test_slot_files_are_stored_outside_the_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name, mock.patch.dict(
            "os.environ", {"VIDEO_DEDUP_GLOBAL_SLOT_DIR": temp_name}
        ):
            with global_slots.global_asr_slot(2, logger=lambda _message: None):
                files = list((Path(temp_name) / "asr-v1").glob("*.lock"))
        self.assertEqual(len(files), 1)

    def test_llm_slots_use_a_separate_machine_wide_pool(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name, mock.patch.dict(
            "os.environ", {"VIDEO_DEDUP_GLOBAL_SLOT_DIR": temp_name}
        ):
            with global_slots.global_asr_slot(1, logger=lambda _message: None):
                with global_slots.global_llm_slot(1, logger=lambda _message: None):
                    asr_files = list((Path(temp_name) / "asr-v1").glob("*.lock"))
                    llm_files = list((Path(temp_name) / "llm-v1").glob("*.lock"))
        self.assertEqual(len(asr_files), 1)
        self.assertEqual(len(llm_files), 1)


if __name__ == "__main__":
    unittest.main()
