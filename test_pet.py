"""Offline pet regressions. Native Tk cases need Windows; no microphone/model/web."""
import json
from contextlib import closing
import os
from pathlib import Path
import queue
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import dot
from pet import Backend, Pet


class Model:
    def __init__(self, *decisions):
        self.decisions = list(decisions)
        self.calls = []

    def complete(self, messages, on_text=None):
        self.calls.append(messages)
        value = self.decisions.pop(0)
        if isinstance(value, Exception):
            raise value
        if value.get("action") == "reply" and on_text:
            text = value["text"]
            for start in range(0, len(text), 3):
                on_text(text[start:start + 3])
        return value


def reply(text="Hello from the test"):
    return {"action": "reply", "text": text, "news_ids": []}


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "qa.sqlite3"
        self.config = json.loads((dot.ROOT / "config.json").read_text())
        self.backend = None
        self.seen = []

    def tearDown(self):
        if self.backend:
            self.backend.close()
            self.backend.thread.join(5)
            self.assertFalse(self.backend.thread.is_alive())
        self.temp.cleanup()

    def start(self, *decisions, **kwargs):
        self.backend = Backend(self.config, self.db, model=Model(*decisions), **kwargs)
        self.backend.thread.start()
        self.wait("ready")
        return self.backend

    def wait(self, kind):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            event = self.backend.events.get(timeout=5)
            self.seen.append(event)
            if event[0] == kind:
                return event[1]
        self.fail("Missing " + kind)

    def test_fresh_session_opens_empty_but_keeps_notes_and_old_chat(self):
        store = dot.Store(self.db)
        store.add_exchange("old rehearsal question", "old rehearsal answer")
        store.remember("last-episode", "Episode 03 fixture", "idea")
        notes_before = store.db.execute("SELECT count(*) FROM notes").fetchone()[0]
        old_session = store.session_id
        store.db.close()
        backend = self.start(fresh_session=True)
        text = "".join(str(v) for k, v in self.seen if k == "text")
        self.assertIn("Local Dot", text)  # The startup banner is in the same stream.
        self.assertNotIn("old rehearsal", text)
        backend.close()
        backend.thread.join(5)
        check = dot.Store(self.db)
        try:
            self.assertNotEqual(check.session_id, old_session)
            self.assertEqual(check.history(), [])
            self.assertEqual(check.db.execute("SELECT count(*) FROM notes").fetchone()[0], notes_before)
            self.assertEqual(check.db.execute("SELECT count(*) FROM messages WHERE session_id=?", (old_session,)).fetchone()[0], 2)
        finally:
            check.db.close()
        self.backend = None

    def test_streamed_reply_saved_once_and_reopen(self):
        backend = self.start(reply())
        backend.requests.put("hello")
        self.wait("done")
        chunks = "".join(v for k, v in self.seen if k == "chunk")
        self.assertEqual(chunks, "\nDot> Hello from the test")
        self.assertIn(("status", "Thinking"), self.seen)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT role,text FROM messages").fetchall(),
                [("user", "hello"), ("assistant", "Hello from the test")])

    def test_prior_session_history_is_displayed_without_model_call(self):
        store = dot.Store(self.db)
        store.add_exchange("Old question", "Old answer")
        store.db.close()
        backend = self.start()
        self.assertEqual(backend.model.calls, [])
        text = "".join(v for k, v in self.seen if k == "text")
        self.assertIn("Old question", text)
        self.assertIn("Old answer", text)

    def test_memory_consent_declined(self):
        backend = self.start({"action": "remember", "name": "qa", "text": "test"}, reply("Not saved"))
        backend.requests.put("Remember this")
        self.wait("confirm")
        backend.answers.put(False)
        self.wait("done")
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM notes WHERE name='qa'").fetchone()[0], 0)

    def test_memory_consent_allowed(self):
        backend = self.start({"action": "remember", "name": "qa", "text": "test"}, reply("Saved"))
        backend.requests.put("Remember this")
        self.wait("confirm")
        backend.answers.put(True)
        self.wait("done")
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT text FROM notes WHERE name='qa'").fetchone()[0], "test")

    def test_closing_during_consent_declines_without_deadlock(self):
        backend = self.start({"action": "remember", "name": "qa", "text": "test"})
        backend.requests.put("Remember this")
        self.wait("confirm")
        backend.close()
        self.wait("closed")
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM notes WHERE name='qa'").fetchone()[0], 0)

    def test_network_error_does_not_disable_next_turn(self):
        backend = self.start(OSError("offline fixture"), reply("Recovered"))
        backend.requests.put("first")
        self.wait("done")
        backend.requests.put("second")
        self.wait("done")
        self.assertTrue(any("offline fixture" in v for k, v in self.seen if k == "text"))
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)

    def test_unknown_citation_stream_not_saved(self):
        backend = self.start(reply("Unsupported [N999]"), reply("Unsupported [N999]"))  # still wrong after the one correction round
        backend.requests.put("question")
        self.wait("done")
        self.assertTrue(any("Citation check" in v for k, v in self.seen if k == "text"))
        self.assertTrue(any("not saved" in v for k, v in self.seen if k == "text"))
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 0)

    def test_new_session_retains_old_transcript(self):
        backend = self.start(reply("First"), {"action": "new_session", "title": "Fresh"}, reply("New chat"))
        backend.requests.put("hello")
        self.wait("done")
        backend.requests.put("Start a new session")
        self.wait("done")
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT COUNT(DISTINCT session_id) FROM messages").fetchone()[0], 2)

    def speech_mock(self):
        speech = Mock()
        speech.enabled, speech.state, speech.truncated, speech.error = True, "ready", False, None
        speech.status.return_value = {"enabled": True}
        return speech

    def test_delayed_ack_is_varied_speech_only_not_saved(self):
        speech = self.speech_mock()
        phrases = ("Give me a second.", "One moment.")
        store = dot.Store(self.db)

        class SlowModel(Model):
            def complete(self, messages, on_text=None):
                time.sleep(0.15)  # model silent long enough for the 0.02 s ack to fire
                return super().complete(messages, on_text)
        try:
            app = dot.Assistant(self.config, store, model=SlowModel(reply("Actual answer"), reply("Second")),
                speech=speech, speech_intro=((0.02, phrases),), output=lambda text: None,
                stream_output=lambda text: None)
            app.chat("question")
            first = speech.feed.call_args_list[0]
            self.assertIn(first.args[0].strip(), phrases)
            self.assertEqual(first.kwargs, {"final": True})
            self.assertEqual(app.ack_timers, [])
            self.assertNotIn(first.args[0].strip(), store.history()[-1]["text"])
            speech.feed.reset_mock()
            app.chat("again")
            second = speech.feed.call_args_list[0].args[0].strip()
            self.assertIn(second, phrases)
            self.assertNotEqual(second, first.args[0].strip(), "same acknowledgement twice in a row")
        finally:
            store.db.close()

    def test_no_ack_once_reply_text_has_started(self):
        speech = self.speech_mock()
        store = dot.Store(self.db)
        try:
            app = dot.Assistant(self.config, store, model=Model(reply("Immediate answer")),
                speech=speech, speech_intro=((0.3, ("Give me a second.",)),), output=lambda text: None,
                stream_output=lambda text: None)
            app.chat("question")
            time.sleep(0.4)
            spoken = "".join(call.args[0] for call in speech.feed.call_args_list)
            self.assertNotIn("Give me a second", spoken)
            self.assertTrue(spoken.startswith("Imm"))
        finally:
            store.db.close()

    def test_legacy_string_intro_is_spoken_at_once(self):
        speech = self.speech_mock()
        store = dot.Store(self.db)
        try:
            app = dot.Assistant(self.config, store, model=Model(reply("Actual answer")),
                speech=speech, speech_intro="Let me think. ", output=lambda text: None,
                stream_output=lambda text: None)
            app.chat("question")
            time.sleep(0.05)
            self.assertEqual(speech.begin.call_count, 1)
            self.assertIn(("Let me think. ",), [c.args for c in speech.feed.call_args_list])
        finally:
            store.db.close()

    def test_cancelled_ack_callback_cannot_leak_into_next_turn(self):
        store = dot.Store(self.db)
        speech = self.speech_mock()
        try:
            app = dot.Assistant(self.config, store, speech=speech,
                                speech_intro=((1, ("Old acknowledgement.",)),))
            with patch("dot.threading.Timer") as timer:
                app.schedule_acks()
                old_callback = timer.call_args.args[1]
                old_args = timer.call_args.kwargs["args"]
                app.cancel_acks()
                app.schedule_acks()
                # Timer.cancel cannot retract a callback that has already started.
                old_callback(*old_args)
                speech.feed.assert_not_called()
                app.cancel_acks()
        finally:
            store.db.close()

    def test_tool_speech_throttled_and_no_ack_flag(self):
        backend = Backend(self.config, self.db)
        backend.speech = self.speech_mock()
        backend.turn_started = backend.last_tool_spoken = 100
        for now in (110, 116, 120, 132, 150):
            with patch("pet.time.monotonic", return_value=now):
                backend.output("[browser_view]")
        self.assertEqual(backend.speech.feed.call_count, 2)
        backend.speech_intro = False
        backend.tool_announcements = 0
        with patch("pet.time.monotonic", return_value=200):
            backend.output("[browser_view]")
        self.assertEqual(backend.speech.feed.call_count, 2)

    def test_confirmation_resumes_only_future_speech(self):
        backend = Backend(self.config, self.db)
        backend.speech = Mock()
        backend.answers.put(False)
        self.assertFalse(backend.confirm("Save a fixture?"))
        backend.speech.stop.assert_called_once()
        backend.speech.begin.assert_called_once()

    def test_cancel_before_worker_dequeues_is_not_lost(self):
        backend = self.start(reply())
        backend.cancelled.set()
        backend.requests.put("already cancelled")
        self.wait("done")
        self.assertEqual(backend.model.calls, [])

    def test_action_indicator_is_not_assumed_from_question(self):
        backend = Backend(self.config, self.db)
        backend.output("  [browser_view]")
        self.assertEqual(backend.events.get_nowait(), ("status", "Reading the shared page"))
        backend.output("A claim about searching")
        self.assertEqual(backend.events.get_nowait()[0], "text")


@unittest.skipUnless(os.name == "nt", "native Windows Tk UI")
class UITests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.backend = Mock()
        self.backend.voice = False
        self.backend.interface = {}
        self.backend.events = queue.Queue()
        self.backend.requests = queue.Queue()
        self.backend.answers = queue.Queue()
        self.backend.thread.is_alive.return_value = True
        self.backend.cancelled = threading.Event()
        self.pet = Pet(self.root, self.backend, show_chat=True)
        self.backend.events.put(("ready", ""))
        self.pet.poll()
        self.root.update()
        self.pet.listener = Mock()
        self.pet.listen_ready = True

    def tearDown(self):
        for timer in self.root.tk.call("after", "info"):
            self.root.after_cancel(timer)
        self.root.destroy()

    def draft(self):
        return self.pet.entry.get("1.0", "end").strip()

    def test_enter_submits_once_and_busy_preserves_new_draft(self):
        self.pet.entry.insert("1.0", "first")
        self.assertEqual(self.pet.submit(), "break")
        self.pet.entry.insert("1.0", "second")
        self.pet.submit()
        self.assertEqual(self.backend.requests.get_nowait(), "first")
        self.assertTrue(self.backend.requests.empty())
        self.assertEqual(self.draft(), "second")

    def test_empty_long_and_commands_not_submitted(self):
        for text in ("", "x" * 12001, "/quit"):
            self.pet.set_draft(text)
            self.pet.submit()
        self.assertTrue(self.backend.requests.empty())

    def test_second_gesture_sends_final_voice_and_preserves_typed_draft(self):
        saved = "  Existing words\nnot submitted  "
        self.pet.set_draft(saved)
        self.pet.toggle_listening()
        self.pet.listener.send.assert_called_once_with("begin")
        self.backend.stop_speech.assert_called_once()
        self.pet.on_listen({"event": "listening"})
        self.pet.on_listen({"event": "transcript", "text": "a wrong", "final": False})
        self.pet.on_listen({"event": "transcript", "text": "a correct phrase", "final": False})
        self.assertEqual(self.draft(), "a correct phrase")
        self.pet.submit()
        self.assertTrue(self.backend.requests.empty())
        self.pet.toggle_listening()
        self.pet.on_listen({"event": "transcript", "text": "a correct phrase", "final": True})
        self.pet.on_listen({"event": "capture_finalized"})
        self.assertEqual(self.backend.requests.get_nowait(), "a correct phrase")
        self.assertEqual(self.pet.entry.get("1.0", "end-1c"), saved)
        self.assertTrue(self.pet.busy)
        self.assertFalse(self.pet.pending_draft)
        self.pet.submit()  # Repeat Enter cannot duplicate the voice turn.
        self.assertTrue(self.backend.requests.empty())

    def test_escape_restores_draft_ignores_late_transcript(self):
        self.pet.set_draft("Keep")
        self.pet.toggle_listening()
        self.pet.on_listen({"event": "listening"})
        self.pet.on_listen({"event": "transcript", "text": "discard", "final": False})
        self.pet.cancel_capture()
        self.pet.cancel_capture()  # Repeated Escape must not queue another cancellation.
        self.assertEqual([call.args[0] for call in self.pet.listener.send.call_args_list], ["begin", "cancel"])
        self.pet.on_listen({"event": "transcript", "text": "late", "final": True})
        self.pet.on_listen({"event": "capture_finalized"})
        self.pet.on_listen({"event": "capture_cancelled"})
        self.assertEqual(self.draft(), "Keep")
        self.pet.set_draft("New typing after cancellation")
        self.pet.on_listen({"event": "capture_cancelled"})
        self.assertEqual(self.draft(), "New typing after cancellation")
        self.assertTrue(self.backend.requests.empty())

    def test_key_during_loading_does_not_restart_helper(self):
        self.pet.listen_ready = False
        self.pet.listen_error = None
        self.pet.on_listen({"event": "key"})
        self.pet.listener.start.assert_not_called()
        self.pet.listener.close.assert_not_called()

    def test_specific_microphone_error_survives_helper_exit(self):
        self.pet.on_listen({"event": "error", "detail": "Selected headset disconnected"})
        self.pet.on_listen({"event": "closed", "detail": "Helper stopped"})
        self.assertIn("Selected headset disconnected", self.pet.listen_status.get())

    def test_no_capture_during_generation(self):
        self.pet.busy = True
        self.pet.toggle_listening()
        self.pet.listener.send.assert_not_called()

    def test_mic_failure_restores_draft_and_keeps_typing(self):
        self.pet.set_draft("Keep")
        self.pet.toggle_listening()
        self.pet.on_listen({"event": "error", "detail": "Microphone disconnected"})
        self.assertEqual(self.draft(), "Keep")
        self.assertEqual(self.pet.capture, "idle")
        self.assertFalse(self.pet.listen_ready)
        self.pet.submit()
        self.assertEqual(self.backend.requests.get_nowait(), "Keep")

    def test_consent_buttons_not_enter(self):
        self.backend.events.put(("confirm", "Save this preference?"))
        self.pet.busy = True
        self.pet.poll()
        self.root.update()
        self.assertTrue(self.pet.consent.winfo_ismapped())
        self.pet.submit()
        self.assertTrue(self.backend.answers.empty())
        self.pet.answer(False)
        self.assertFalse(self.backend.answers.get_nowait())

    def test_hide_show_retains_transcript_and_draft(self):
        self.pet.append("Saved screen text")
        self.pet.set_draft("draft")
        self.pet.toggle()
        self.assertEqual(self.pet.panel.state(), "withdrawn")
        self.pet.toggle()
        self.assertEqual(self.draft(), "draft")
        self.assertIn("Saved screen text", self.pet.chat.get("1.0", "end"))

    def test_chat_hidden_by_default_and_dictation_does_not_reopen_it(self):
        for timer in self.root.tk.call("after", "info"):
            self.root.after_cancel(timer)
        self.root.destroy()
        import tkinter as tk
        self.root = tk.Tk()
        self.pet = Pet(self.root, self.backend)
        self.pet.listener, self.pet.listen_ready = Mock(), True
        self.backend.events.put(("ready", ""))
        self.pet.poll()
        self.assertEqual(self.pet.panel.state(), "withdrawn")
        self.pet.toggle_listening()
        self.pet.on_listen({"event": "listening"})
        self.pet.on_listen({"event": "transcript", "text": "hello dot", "final": True})
        self.pet.toggle_listening()
        self.pet.on_listen({"event": "capture_finalized"})
        self.assertEqual(self.pet.panel.state(), "withdrawn", "dictation must not force the chat window open")
        self.assertEqual(self.backend.requests.get_nowait(), "hello dot")  # No Enter or focus change.
        self.assertIn("You> hello dot", self.pet.chat.get("1.0", "end"))
        self.assertEqual(self.pet.mood, "thinking")
        self.assertEqual(self.pet.panel.state(), "withdrawn")
        # Streaming reply and completion change mood; consent is the one case that opens the panel.
        self.backend.events.put(("chunk", "Hi"))
        self.pet.poll()
        self.assertEqual(self.pet.mood, "replying")
        self.backend.events.put(("done", ""))
        self.pet.poll()
        self.assertEqual(self.pet.mood, "done")
        self.assertEqual(self.pet.panel.state(), "withdrawn")
        self.backend.events.put(("confirm", "Save a preference?"))
        self.pet.poll()
        self.assertEqual(self.pet.mood, "needs-you")
        self.assertNotEqual(self.pet.panel.state(), "withdrawn")

    def test_unrequested_completion_is_a_cancellable_draft_not_an_auto_send(self):
        self.pet.toggle_listening()
        self.pet.on_listen({"event": "listening"})
        self.pet.on_listen({"event": "transcript", "text": "discard this", "final": True})
        self.pet.on_listen({"event": "capture_finalized"})
        self.assertTrue(self.pet.pending_draft)
        self.assertEqual(str(self.pet.quick_send["state"]), "normal")
        self.pet.cancel_capture()
        self.assertEqual(self.draft(), "")
        self.assertFalse(self.pet.pending_draft)
        self.assertTrue(self.backend.requests.empty())

    def test_silence_finalizes_a_segment_not_the_voice_turn(self):
        self.pet.on_listen({"event": "key"})
        self.pet.on_listen({"event": "listening"})
        self.pet.on_listen({"event": "transcript", "text": "First sentence.", "final": True})
        self.assertTrue(self.backend.requests.empty())
        self.assertEqual(self.pet.capture, "listening")
        self.pet.on_listen({"event": "transcript", "text": "Second sentence.", "final": True})
        self.pet.on_listen({"event": "key"})
        self.assertTrue(self.backend.requests.empty())
        self.pet.on_listen({"event": "capture_finalized"})
        self.assertEqual(self.backend.requests.get_nowait(), "First sentence. Second sentence.")
        self.assertEqual(self.pet.capture, "idle")  # No automatic next capture.

    def test_empty_and_partial_only_capture_never_send_old_typed_text(self):
        for partial in ("", "Unfinalized preview"):
            with self.subTest(partial=partial):
                self.pet.set_draft("Keep typed words")
                self.pet.toggle_listening()
                self.pet.on_listen({"event": "listening"})
                self.pet.on_listen({"event": "transcript", "text": partial, "final": False})
                self.pet.toggle_listening()
                self.pet.on_listen({"event": "capture_finalized"})
                self.assertEqual(self.draft(), "Keep typed words")
                self.assertFalse(self.pet.pending_draft)
                self.assertTrue(self.backend.requests.empty())

    def test_duplicate_completion_and_late_text_cannot_send_or_replace_next_draft(self):
        self.pet.toggle_listening(); self.pet.on_listen({"event": "listening"})
        self.pet.on_listen({"event": "transcript", "text": "One voice turn", "final": True})
        self.pet.toggle_listening(); self.pet.on_listen({"event": "capture_finalized"})
        self.assertEqual(self.backend.requests.get_nowait(), "One voice turn")
        self.backend.events.put(("done", "")); self.pet.poll()
        self.pet.set_draft("Next typed draft")
        self.pet.on_listen({"event": "transcript", "text": "Late text", "final": True})
        self.pet.on_listen({"event": "capture_finalized"})
        self.assertEqual(self.draft(), "Next typed draft")
        self.assertTrue(self.backend.requests.empty())

    def test_escape_while_finalizing_prevents_submission(self):
        self.pet.set_draft("Keep this")
        self.pet.toggle_listening(); self.pet.on_listen({"event": "listening"})
        self.pet.on_listen({"event": "transcript", "text": "Discard this speech", "final": True})
        self.pet.toggle_listening()
        self.pet.on_listen({"event": "escape"})
        self.pet.on_listen({"event": "capture_finalized"})
        self.pet.on_listen({"event": "capture_cancelled"})
        self.assertTrue(self.backend.requests.empty())
        self.assertEqual(self.draft(), "Keep this")

    def test_voice_does_not_steal_focus_and_enter_in_another_window_does_not_send(self):
        import tkinter as tk
        other = tk.Toplevel(self.root)
        entry = tk.Entry(other); entry.pack()
        other.lift(); entry.focus_force(); self.root.update()
        self.assertEqual(self.root.focus_get(), entry)
        self.pet.panel.withdraw()
        self.pet.set_draft("Unsent typing")
        self.pet.on_listen({"event": "key"}); self.pet.on_listen({"event": "listening"})
        self.pet.on_listen({"event": "transcript", "text": "Hands away from chat", "final": True})
        self.pet.on_listen({"event": "key"}); self.pet.on_listen({"event": "capture_finalized"})
        self.root.update()
        self.assertEqual(self.root.focus_get(), entry)
        self.assertEqual(self.backend.requests.get_nowait(), "Hands away from chat")
        self.backend.events.put(("done", "")); self.pet.poll()
        entry.event_generate("<Return>"); self.root.update()
        self.assertEqual(self.draft(), "Unsent typing")
        self.assertTrue(self.backend.requests.empty())
        self.assertFalse(self.pet.panel_visible())

    def test_quick_second_gesture_finishes_even_before_start_ack(self):
        self.pet.on_listen({"event": "key"}); self.pet.on_listen({"event": "key"})
        self.assertEqual(self.pet.capture, "finalizing")
        self.assertEqual([call.args[0] for call in self.pet.listener.send.call_args_list], ["begin", "end"])
        self.pet.on_listen({"event": "listening"})
        self.pet.on_listen({"event": "transcript", "text": "Quick turn", "final": True})
        self.pet.on_listen({"event": "capture_finalized"})
        self.assertEqual(self.backend.requests.get_nowait(), "Quick turn")

    def test_background_folder_consent_blocks_capture_and_typed_submission(self):
        self.backend.events.put(("confirm", "Allow the brief folder?")); self.pet.poll()
        self.assertFalse(self.pet.busy)  # Local control, not a foreground model turn.
        self.pet.set_draft("yes"); self.pet.submit(); self.pet.on_listen({"event": "key"})
        self.pet.listener.send.assert_not_called()
        self.assertTrue(self.backend.requests.empty())
        self.assertTrue(self.backend.answers.empty())
        self.pet.answer(False)
        self.assertFalse(self.backend.answers.get_nowait())

    def test_busy_or_permission_during_finalization_holds_without_later_auto_send(self):
        for guard in ("busy", "consent"):
            with self.subTest(guard=guard):
                self.pet.set_draft("Keep typed")
                self.pet.toggle_listening(); self.pet.on_listen({"event": "listening"})
                self.pet.on_listen({"event": "transcript", "text": "Hold voice", "final": True})
                if guard == "busy": self.pet.busy = True
                else:
                    self.backend.events.put(("confirm", "Permission needed")); self.pet.poll()
                self.pet.toggle_listening()  # Still releases the mic even with a pending guard.
                self.pet.on_listen({"event": "capture_finalized"})
                self.assertTrue(self.pet.pending_draft)
                self.assertTrue(self.backend.requests.empty())
                self.assertTrue(self.backend.answers.empty())
                if guard == "consent": self.pet.answer(False); self.backend.answers.get_nowait()
                self.backend.events.put(("done", "")); self.pet.poll()
                self.assertTrue(self.backend.requests.empty())
                self.pet.cancel_capture()
                self.assertEqual(self.draft(), "Keep typed")

    def test_invalid_voice_stays_editable_and_preserves_typed_draft(self):
        for text in ("/quit", "x" * 12001):
            with self.subTest(text_length=len(text)):
                self.pet.set_draft("Unsent typing")
                self.pet.toggle_listening(); self.pet.on_listen({"event": "listening"})
                self.pet.on_listen({"event": "transcript", "text": text, "final": True})
                self.pet.toggle_listening(); self.pet.on_listen({"event": "capture_finalized"})
                self.assertTrue(self.backend.requests.empty())
                self.assertEqual(self.draft(), text)
                self.assertTrue(self.pet.pending_draft)
                calls = self.pet.listener.send.call_count
                self.pet.toggle_listening()  # Don't overwrite a held draft and its backup.
                self.assertEqual(self.pet.listener.send.call_count, calls)
                self.pet.cancel_capture()
                self.assertEqual(self.draft(), "Unsent typing")

    def test_closing_ignores_final_transcript_completion(self):
        self.pet.toggle_listening(); self.pet.on_listen({"event": "listening"})
        self.pet.on_listen({"event": "transcript", "text": "Do not send", "final": True})
        self.pet.toggle_listening(); self.pet.closing = True
        self.pet.on_listen({"event": "capture_finalized"})
        self.assertTrue(self.backend.requests.empty())

    def test_hidden_accepted_preview_and_failure_do_not_claim_success(self):
        self.pet.panel.withdraw()
        self.backend.events.put(("accepted", "A checked answer"))
        self.backend.events.put(("done", ""))
        self.pet.poll()
        self.assertIn("A checked answer", self.pet.bubble.get())
        self.backend.events.put(("failed", "Rejected"))
        self.backend.events.put(("done", ""))
        self.pet.poll()
        self.assertEqual(self.pet.mood, "off")
        self.assertNotIn("checked answer", self.pet.bubble.get())

    def test_drowsy_only_when_idle_and_wakes_on_click(self):
        self.pet.set_mood("idle")
        self.pet.last_active = time.monotonic() - 181
        self.pet.animate()
        self.assertEqual(self.pet.mood, "drowsy")
        self.pet.toggle()
        self.assertEqual(self.pet.mood, "idle")
        self.pet.pending_draft = True
        self.pet.last_active = time.monotonic() - 181
        self.pet.animate()
        self.assertEqual(self.pet.mood, "idle")

    def test_listening_props_and_pet_controls_fit(self):
        self.pet.set_mood("listening")
        for frame in range(90):
            self.pet.frame = frame
            self.pet.animate()
            self.assertGreaterEqual(self.pet.canvas.bbox("all")[1], 0)
        self.root.update()
        self.assertLessEqual(self.pet.quick_send.winfo_y() + self.pet.quick_send.winfo_height(),
                             self.pet.HEIGHT)

    def test_voice_error_clears_speaking_and_restores_idle(self):
        self.backend.events.put(("voice", {"event": "play_started"}))
        self.pet.poll()
        self.assertTrue(self.pet.speaking)
        self.backend.events.put(("voice", {"event": "error", "error": "Output disconnected"}))
        self.pet.poll()
        self.assertFalse(self.pet.speaking)
        self.assertNotEqual(self.pet.mood, "replying")

    def test_every_mood_renders_without_error(self):
        for mood in ("drowsy", "idle", "thinking", "working", "replying", "listening", "needs-you", "done", "off"):
            self.pet.set_mood(mood)
            for _ in range(3):
                self.pet.animate()
            self.assertGreater(len(self.pet.canvas.find_all()), 5, mood)
        self.pet.set_mood("done", hold=2)
        self.pet.animate(); self.pet.animate()
        self.assertEqual(self.pet.mood, "idle")

    def test_stop_does_not_cancel_foreground(self):
        self.backend.stop_speech()
        self.assertFalse(self.backend.cancelled.is_set())
        self.pet.busy = True
        self.pet.cancel()
        self.assertTrue(self.backend.cancelled.is_set())

    def test_controls_fit_default_and_minimum_panel_sizes(self):
        for geometry in ("520x600", "420x520"):
            self.pet.panel.geometry(geometry)
            self.root.update()
            for widget in (self.pet.entry, self.pet.send, self.pet.listen_button):
                self.assertTrue(widget.winfo_ismapped(), (geometry, widget))
                bottom = widget.winfo_rooty() + widget.winfo_height()
                self.assertLessEqual(bottom, self.pet.panel.winfo_rooty() + self.pet.panel.winfo_height())

    def test_consent_fits_without_hiding_submit_controls(self):
        self.pet.panel.geometry("420x520")
        self.backend.events.put(("confirm", "Save owner preference: " + "practical experiments " * 7))
        self.pet.poll()
        self.root.update()
        self.assertTrue(self.pet.listen_button.winfo_ismapped())
        self.assertTrue(self.pet.entry.winfo_ismapped())

    def test_visible_errors_and_text_fallback(self):
        self.backend.events.put(("voice", {"event": "error", "error": "Output disconnected"}))
        self.backend.events.put(("fatal", "Endpoint unavailable"))
        self.backend.events.put(("closed", ""))
        self.pet.poll()
        self.assertIn("Output disconnected", self.pet.voice_status.get())
        self.assertIn("Endpoint unavailable", self.pet.chat.get("1.0", "end"))
        self.assertTrue(self.pet.busy)


class ListeningTests(unittest.TestCase):
    def test_missing_environment_no_install_or_process(self):
        from listening import Listening
        with patch("listening.PYTHON") as python, patch("listening.subprocess.Popen") as popen:
            python.is_file.return_value = False
            with self.assertRaisesRegex(OSError, "Dictation is not configured"):
                Listening(lambda event: None).start()
            popen.assert_not_called()

    def test_existing_dictation_conflict_does_not_kill_or_start(self):
        from listening import Listening
        with patch("listening.PYTHON") as python, patch("listening.subprocess.run") as run, patch("listening.subprocess.Popen") as popen:
            python.is_file.return_value = True
            run.return_value = Mock(returncode=0, stdout="2\n")
            with self.assertRaisesRegex(OSError, "Exit Local Dictation"):
                Listening(lambda event: None).start()
            popen.assert_not_called()
            self.assertNotIn("Stop-Process", str(run.call_args))
            import re
            patterns = re.findall(r"-match '([^']+)'", run.call_args.args[0][-1])
            self.assertTrue(re.search(patterns[0], "pythonw.exe"))
            self.assertTrue(re.search(patterns[1], '"C:\\test\\pythonw.exe" -m realtime_stt '))
            self.assertFalse(re.search(patterns[1], 'pythonw.exe pet.py'))

    def test_failed_conflict_check_refuses_hotkey(self):
        from listening import Listening
        with patch("listening.PYTHON") as python, patch("listening.subprocess.run") as run:
            python.is_file.return_value = True
            run.return_value = Mock(returncode=1, stdout="")
            with self.assertRaisesRegex(OSError, "Could not check"):
                Listening(lambda event: None).start()

    def test_dead_helper_is_visible_not_silent(self):
        from listening import Listening
        client = Listening(lambda event: None)
        with self.assertRaisesRegex(OSError, "not running"):
            client.send("begin")


if __name__ == "__main__":
    unittest.main()
