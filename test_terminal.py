"""Exercise the installed prompt_toolkit adapter with simulated typing, no OS key injection."""
import importlib.util
import sys
import threading
import time
import unittest
from unittest.mock import patch
import dot


@unittest.skipUnless(importlib.util.find_spec("prompt_toolkit"), "Optional Windows terminal package is not installed here")
class TerminalTests(unittest.TestCase):
    def test_background_notice_preserves_unsubmitted_input(self):
        from prompt_toolkit import PromptSession
        from prompt_toolkit.application import create_app_session
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput
        class Output(DummyOutput):
            def __init__(self): self.writes = []
            def write(self, data): self.writes.append(data)
            def write_raw(self, data): self.writes.append(data)
        output = Output()
        with create_pipe_input() as pipe, create_app_session(input=pipe, output=output):
            session = PromptSession(input=pipe, output=output)
            with patch.object(sys.stdin, "isatty", return_value=True), patch.object(sys.stdout, "isatty", return_value=True), patch("prompt_toolkit.PromptSession", return_value=session):
                with dot.terminal() as (prompt, live):
                    self.assertTrue(live)
                    def type_and_notify():
                        time.sleep(.15)
                        pipe.send_text("an unfinished")
                        time.sleep(.15)
                        dot.say("\nDot> background task completed")
                        time.sleep(.35)
                        pipe.send_text(" question\r")
                    thread = threading.Thread(target=type_and_notify, daemon=True)
                    thread.start()
                    answer = prompt("You> ")
                    thread.join(3)
        self.assertEqual(answer, "an unfinished question")
        self.assertIn("background task completed", "".join(output.writes))


if __name__ == "__main__":
    unittest.main()
