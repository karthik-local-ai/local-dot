"""Offline session, workspace permission, and concurrency regressions. No model/web calls."""
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import dot
from jobs import Job, Jobs
from runtime import Runtime
from streaming import read_completion
from workspace_tools import Browser, Files, windows_path
from test_dot import ScriptedModel
from test_streaming import completion


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "chat.sqlite3"
        self.store = dot.Store(self.path)
        self.config = json.loads((dot.ROOT / "config.json").read_text())
        self.lines = []
        self.browser = Mock()
        self.browser.status.return_value = {"open": False}
        self.runtime = Runtime(self.store, lambda sid: dot.Store(self.path, sid),
                               lambda *_: {"answer": "Result", "news_ids": []}, self.lines.append,
                               browser=self.browser)
        self.app = dot.Assistant(self.config, self.store, runtime=self.runtime, output=self.lines.append)

    def tearDown(self):
        self.runtime.close()
        self.store.db.close()
        self.temp.cleanup()

    def page(self, url="https://example.org/article", published=None):
        self.store.ingest([dict(url=url, title="Stored public fixture", source="example.org",
                               published=published, body="Public fixture contents " * 20,
                               evidence="saved page text")])
        return self.store.db.execute("SELECT id FROM news WHERE url=?", (url,)).fetchone()[0]

    def test_additive_migration_preserves_original_rows_and_notes(self):
        path = Path(self.temp.name) / "legacy.sqlite3"
        with sqlite3.connect(path) as db:
            db.executescript("CREATE TABLE notes(name TEXT PRIMARY KEY,kind TEXT,text TEXT,updated TEXT);"
                             "CREATE TABLE messages(id INTEGER PRIMARY KEY,role TEXT,text TEXT,created TEXT);")
            db.execute("INSERT INTO notes VALUES ('owner_note','idea','Keep this exactly','2026-10-01')")
            db.execute("INSERT INTO messages VALUES (7,'user','Original conversation','2026-10-01')")
        db.close()
        upgraded = dot.Store(path)
        with sqlite3.connect(path.with_name("legacy-before-sessions.sqlite3")) as backup:
            self.assertNotIn("session_id", [r[1] for r in backup.execute("PRAGMA table_info(messages)")])
            self.assertEqual(backup.execute("SELECT text FROM messages").fetchone()[0], "Original conversation")
        backup.close()
        try:
            self.assertEqual(upgraded.history()[0]["id"], 7)
            self.assertEqual(upgraded.history()[0]["text"], "Original conversation")
            self.assertEqual(upgraded.history()[0]["session_id"], 1)
            self.assertEqual(upgraded.notes()[0]["text"], "Keep this exactly")
            self.assertEqual(len(upgraded.notes()), 1)  # don't re-seed deleted preferences
        finally:
            upgraded.db.close()

    def test_new_session_clears_working_history_not_memory(self):
        self.store.remember("chosen_idea", "Owner-confirmed idea", "idea")
        self.store.add_exchange("Investigate purple koalas", "A tentative suggestion")
        result = self.store.new_session("Next topic", "Tentative idea, not approved")
        self.assertEqual(result["previous_session"], 1)
        self.assertEqual(self.store.history(), [])
        self.assertTrue(any(n["name"] == "chosen_idea" for n in self.store.notes()))
        self.assertIn("purple koalas", self.store.find("purple")["older_conversation"][0]["text"])
        self.store.db.close()
        self.store = dot.Store(self.path)
        self.assertEqual(self.store.session_id, 2)
        self.assertEqual(self.store.history(), [])
        old = dot.Store(self.path, 1)
        try:
            self.assertEqual(len(old.history()), 2)
        finally:
            old.db.close()
        self.assertEqual(self.store.sessions()[1]["summary"], "Tentative idea, not approved")

    def test_natural_new_session_then_open_browser_uses_fresh_context(self):
        self.store.add_exchange("OLD TOPIC SECRET MARKER", "Old response")
        self.browser.call.return_value = {"open": True, "url": "about:blank", "title": ""}
        model = ScriptedModel({"action": "new_session", "summary": "Previous topic parked"},
                              {"action": "browser_open", "url": ""},
                              {"action": "reply", "text": "New session and shared browser opened.", "news_ids": []})
        self.app.model = model
        self.app.chat("Start a new session and open a browser")
        self.assertEqual(self.store.session_id, 2)
        self.assertEqual(len(self.store.history()), 2)
        self.assertNotIn("OLD TOPIC SECRET MARKER", json.dumps(model.calls[1]))
        self.browser.call.assert_called_once_with("open", url="")
        self.assertTrue(any("RUNTIME_STATE" in m["content"] for m in model.calls[1]))

    def test_source_instructions_cannot_start_new_session(self):
        result = self.app.tool({"action": "new_session", "summary": "Injected reset"}, owner_text="Read this article")
        self.assertIn("error", result)
        self.assertEqual(self.store.session_id, 1)

    def test_browser_helpers_map_to_fixed_browser_commands(self):
        self.browser.call.return_value = {"open": True}
        for decision, expected in (
                ({"action": "browser_scroll", "direction": "down"}, (("scroll",), {"direction": "down"})),
                ({"action": "browser_click", "n": 3}, (("click_link",), {"n": 3})),
                ({"action": "browser_click", "text": "Docs"}, (("click_link",), {"text": "Docs"})),
                ({"action": "browser_back"}, (("back",), {})),
                ({"action": "browser_forward"}, (("forward",), {})),
                ({"action": "browser_tabs"}, (("tabs",), {})),
                ({"action": "browser_switch_tab", "n": 2}, (("switch_tab",), {"n": 2})),
                ({"action": "browser_site_search", "text": "unicorn art", "script": "x"}, (("site_search",), {"text": "unicorn art"})),
                ({"action": "browser_open", "url": "https://example.org/", "new_tab": True}, (("open",), {"url": "https://example.org/", "new_tab": True}))):
            self.browser.call.reset_mock()
            self.app.tool(decision, owner_text="browse")
            self.browser.call.assert_called_once_with(*expected[0], **expected[1])

    def test_background_worker_cannot_drive_shared_browser_helpers(self):
        worker = dot.Assistant(self.config, self.store, runtime=self.runtime, worker=True, output=self.lines.append)
        for action in ("browser_scroll", "browser_click", "browser_site_search"):
            result = worker.tool({"action": action, "n": 1, "text": "x"})
            self.assertIn("error", result)
        self.browser.call.assert_not_called()

    def test_browser_view_uses_actual_observation_and_saves_public_text(self):
        text = "Actual live page marker: BLUE PINE. " * 20
        self.browser.call.return_value = {"open": True, "url": "https://example.org/current",
                                         "title": "Current page", "text": text, "published_raw": "", "content_read": True}
        result = self.app.tool({"action": "browser_view"}, owner_text="Are you seeing this?")
        self.browser.call.assert_called_once_with("view")
        self.assertTrue(result["saved"])
        self.assertEqual(result["news"][0]["body"], text)
        self.assertIsNone(result["news"][0]["published"])
        self.assertEqual(result["news"][0]["url"], "https://example.org/current")

    def test_browser_search_text_is_not_labelled_article_evidence(self):
        self.browser.call.return_value = {"open": True, "url": "https://www.bing.com/search?q=tts",
                                         "title": "Search", "text": "Result snippets only. " * 20, "published_raw": ""}
        result = self.app.tool({"action": "browser_view"})
        self.assertIn("NOT ARTICLE EVIDENCE", result["news"][0]["evidence"])

    def test_foreground_search_and_article_use_the_shared_edge_reader(self):
        self.assertIs(self.app.news.browser, self.browser)
        self.browser.call.return_value = {"engine":"Edge", "limit":"Discovery only", "results":[
            {"url":"https://example.org/source", "title":"Source", "source":"example.org"}]}
        with patch("web_tools.public_url", side_effect=lambda u:u), patch("web_tools.search_web") as fallback:
            found = self.app.news.search("Public topic")
            fallback.assert_not_called()
        self.browser.call.assert_called_with("discover", query="Public topic")
        self.browser.call.return_value = {"url":"https://example.org/source", "title":"Source",
            "text":"Rendered source content. "*20, "published_raw":"", "http_status":200}
        with patch("web_tools.fetch_page") as fallback:
            item = self.app.news.open_url(found["news"][0]["url"])
            fallback.assert_not_called()
        self.browser.call.assert_called_with("read_url", url="https://example.org/source")
        self.assertIn("Edge rendered", item["evidence"])

    def test_runtime_profile_is_db_specific_and_not_launched_at_construction(self):
        runtime = Runtime(self.store, lambda sid: dot.Store(self.path, sid), lambda *_:{}, lambda _:None)
        try:
            self.assertEqual(runtime.browser.profile, self.path.with_name("chat-edge-profile"))
            self.assertIsNone(runtime.browser.process)
            self.assertFalse(runtime.browser.profile.exists())
        finally:
            runtime.close()

    def test_cancelled_research_closes_its_own_browser_not_shared_one(self):
        stop = threading.Event(); stop.set()
        with patch("runtime.Browser") as browser:
            with self.assertRaises(dot.DotError):
                dot.research_runner(self.config, self.path)("No web", stop, 1)
            browser.assert_called_once_with(foreground=False)
            browser.return_value.close.assert_called_once()
            browser.return_value.call.assert_not_called()
        self.browser.close.assert_not_called()

    def test_browser_timeout_is_unknown_not_falsely_closed(self):
        browser = Browser()
        with patch.object(browser, "call", side_effect=ValueError("Timed out")):
            status = browser.status()
        self.assertIsNone(status["open"])
        self.assertTrue(status["status_unknown"])

    def test_blank_or_blocked_view_does_not_claim_saved_article(self):
        for text in ("", "Verify you are human. CAPTCHA. " * 12):
            self.browser.call.return_value = {"open": True, "url": "https://example.org/blocked", "text": text}
            result = self.app.tool({"action": "browser_view"})
            self.assertFalse(result["saved"])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM news").fetchone()[0], 0)

    def test_worker_cannot_nest_control_browser_files_or_memory(self):
        worker = dot.Assistant(self.config, self.store, runtime=self.runtime, worker=True, output=lambda _: None,
                               confirm=lambda _: self.fail("Worker must not ask confirmation"))
        for action in ("start_research", "browser_open", "browser_view", "open_file", "read_file", "remember",
                       "new_session", "cancel_task"):
            with self.subTest(action=action):
                self.assertIn("error", worker.tool({"action": action}))
        self.browser.call.assert_not_called()
        self.assertEqual(self.runtime.jobs.status()["active"], 0)

    def test_worker_context_and_recall_do_not_copy_private_chat(self):
        self.store.add_exchange("Private file marker zebra", "Private explanation zebra")
        worker = dot.Assistant(self.config, self.store, worker=True, output=lambda _: None)
        messages, _ = worker.context("Research public TTS")
        self.assertNotIn("Private file marker", json.dumps(messages))
        self.assertEqual(set(worker.tool({"action": "recall", "query": "zebra"})), {"news"})

    def test_original_owner_research_constraints_survive_paraphrase(self):
        owner = "Search the web for a primary TTS source; no stock news."
        self.app.confirm = Mock(return_value=True)
        self.runtime.tool({"action": "start_research", "task": "Find a TTS source"}, self.app, owner)
        self.assertIn(owner, self.runtime.jobs.get(1)["task"])
        self.assertEqual(dot.research_requirements(self.runtime.jobs.get(1)["task"]), (True, False))
        self.assertEqual(dot.research_requirements("Use only saved source N1. No web search."), (False, True))

    def test_required_search_cannot_be_replaced_by_cached_only_reply(self):
        n = self.page(published=dot.now())
        model = ScriptedModel({"action": "reply", "text": "Cached answer masquerading as research", "news_ids": []},
                              {"action": "search_web", "query": "public TTS source"},
                              {"action": "reply", "text": f"Grounded result [N{n}]", "news_ids": [n]})
        worker = dot.Assistant(self.config, self.store, model=model, worker=True, require_search=True, output=lambda _: None)
        with patch.object(worker.news, "search", return_value={"news": [self.store.article(n)]}) as search:
            answer = worker.chat("Search for a TTS source")
        search.assert_called_once_with("public TTS source")
        self.assertIn("APPLICATION CHECK FAILED", json.dumps(model.calls[1]))
        self.assertIn("web search returned 1 discovery records", answer)
        self.assertNotIn("masquerading", answer)
        self.assertEqual(self.store.history(), [])

    def test_unperformed_required_search_never_finishes_successfully(self):
        calls = self.config.get("research_tool_budget", 8) + 1
        model = ScriptedModel(*[{"action": "reply", "text": "I searched", "news_ids": []}] * calls)
        worker = dot.Assistant(self.config, self.store, model=model, worker=True, require_search=True, output=lambda _: None)
        with self.assertRaisesRegex(dot.DotError, "web search was not performed"):
            worker.chat("Search for a public source")
        self.assertEqual(len(model.calls), calls)
        self.assertEqual(self.store.history(), [])

    def test_failed_required_search_has_application_disclosure(self):
        model = ScriptedModel({"action": "search_web", "query": "public topic"},
                              {"action": "reply", "text": "Only cached evidence available.", "news_ids": []})
        worker = dot.Assistant(self.config, self.store, model=model, worker=True, require_search=True, output=lambda _: None)
        with patch.object(worker.news, "search", side_effect=dot.DotError("Search unavailable")):
            answer = worker.chat("Search for a public source")
        self.assertIn("Public web search failed: Search unavailable", answer)
        self.assertIn("not a fresh search result", answer)

    def test_cached_only_worker_cannot_make_web_calls_even_through_read(self):
        n = self.page()
        self.store.db.execute("UPDATE news SET evidence='search result only; page not read' WHERE id=?", (n,))
        self.store.db.commit()
        worker = dot.Assistant(self.config, self.store, worker=True, cached_only=True, output=lambda _: None)
        with patch.object(worker.news, "search") as search, patch.object(worker.news, "open_url") as fetch:
            for decision in ({"action": "search_web", "query": "anything"}, {"action": "open_url", "url": "https://example.org/"},
                             {"action": "read_news", "id": n}, {"action": "fetch_news"}):
                self.assertIn("error", worker.tool(decision))
            search.assert_not_called()
            fetch.assert_not_called()

    def test_cancelled_worker_starts_no_model_call(self):
        event = threading.Event()
        event.set()
        model = Mock()
        worker = dot.Assistant(self.config, self.store, model=model, worker=True, cancelled=event, output=lambda _: None)
        with self.assertRaises(dot.DotError):
            worker.chat("Do research")
        model.complete.assert_not_called()

    def test_worker_does_not_save_fake_user_exchange_or_mark_unseen_read(self):
        n = self.page(published=dot.now())
        model = ScriptedModel({"action": "reply", "text": f"Result [N{n}]", "news_ids": [n]})
        worker = dot.Assistant(self.config, self.store, worker=True, model=model, output=lambda _: None)
        worker.chat("Internal task brief")
        self.assertEqual(self.store.history(), [])
        self.assertIsNone(self.store.article(n)["shown"])
        self.assertEqual(worker.last_reply_ids, [n])

    def test_background_result_can_be_discussed_with_real_old_source_record(self):
        n = self.page()  # undated: absent from the ordinary recent-news list
        self.store.add_assistant(f"Research result [N{n}]\n\nSources:\n[N{n}] Stored public fixture\n  https://example.org/article")
        messages, known = self.app.context("Tell me more about that result")
        self.assertIn(n, known)
        self.assertIn("Public fixture contents", json.dumps(messages))

    def test_one_off_mode_does_not_start_jobs_or_shared_browser(self):
        self.runtime.interactive = False
        for action in ("start_research", "browser_open", "browser_search", "browser_view"):
            self.assertIn("error", self.app.tool({"action": action, "task": "Research", "query": "public"}))
        self.browser.call.assert_not_called()

    def test_runtime_contains_true_worker_slots(self):
        release = threading.Event()
        self.runtime.jobs.run = lambda *args: (release.wait(3), {"answer": "Done", "news_ids": []})[1]
        try:
            self.runtime.jobs.start("First", 1)
            state = self.runtime.status()
            self.assertEqual((state["workers"]["active"], state["workers"]["free"]), (1, 1))
            model = ScriptedModel({"action": "reply", "text": "One worker is running.", "news_ids": []})
            self.app.model = model
            self.app.chat("How many workers?")
            runtime_messages = [m for m in model.calls[0] if m["content"].startswith("RUNTIME_STATE")]
            data = json.loads(runtime_messages[-1]["content"].split("\n", 1)[1])
            self.assertEqual(data["workers"]["active"], 1)
        finally:
            release.set()

    def test_old_session_job_result_is_not_inserted_into_new_chat(self):
        release = threading.Event()
        self.runtime.jobs.run = lambda *args: (release.wait(3), {"answer": "OLD RESULT MARKER", "news_ids": []})[1]
        job = self.runtime.jobs.start("Old-session request", 1)
        self.store.new_session()
        release.set()
        self.runtime.jobs.items[job["id"]].thread.join(3)
        self.runtime.drain()
        self.assertEqual(self.store.history(), [])
        old = dot.Store(self.path, 1)
        try:
            self.assertIn("OLD RESULT MARKER", old.history()[0]["text"])
        finally:
            old.db.close()
        self.assertTrue(any("session 1" in text for text in self.lines))
        self.assertNotIn("OLD RESULT MARKER", "".join(self.lines))

    def test_file_requires_explicit_selection_and_confirmation(self):
        path = Path(self.temp.name) / "notes.txt"
        path.write_text("PRIVATE FILE MARKER", encoding="utf-8")
        files = Files()
        with self.assertRaises(ValueError):
            files.choose(str(path), lambda _: True, owner_text="Read this website", launch=False)
        result = files.choose(str(path), lambda _: False, owner_text=f"Open {path}", launch=False)
        self.assertFalse(result["opened"])
        self.assertEqual(files.selected, {})
        with self.assertRaises(ValueError):
            files.read(str(path))
        opened = files.choose(str(path), lambda _: True, owner_text=f"Open {path}", launch=False)
        self.assertEqual(opened["body"], "PRIVATE FILE MARKER")
        path.write_text("SAVED CHANGE", encoding="utf-8")
        self.assertEqual(files.read(str(path))["body"], "SAVED CHANGE")

    def test_files_never_execute_associations_or_accept_executable(self):
        files = Files()
        path = Path(self.temp.name) / "example.py"
        path.write_text("print('do not execute')", encoding="utf-8")
        with patch("workspace_tools.subprocess.Popen") as popen, patch("workspace_tools.Path.is_file", return_value=True), patch("workspace_tools.windows_path", side_effect=lambda value: "C:/local-dot-test.py" if isinstance(value, Path) else str(value)):
            files.choose(str(path), lambda _: True, explicit=True)
            command = popen.call_args.args[0]
            self.assertTrue(command[0].lower().endswith("notepad.exe"))
            self.assertEqual(command[1], "C:/local-dot-test.py")
        for value in ("relative.txt", "\\\\server\\share\\secret.txt", str(path.with_suffix(".exe")), str(path.with_suffix(".pdf"))):
            with self.assertRaises(ValueError):
                files.choose(value, lambda _: True, explicit=True, launch=False)

    def test_linux_only_path_not_falsely_opened_in_windows_viewer(self):
        path = Path(self.temp.name) / "fixture.txt"
        path.write_text("Fixture", encoding="utf-8")
        with patch("workspace_tools.windows_path", return_value="/tmp/fixture.txt"), patch("workspace_tools.subprocess.Popen") as popen:
            if os.name != "nt":
                with self.assertRaisesRegex(ValueError, "Windows-drive"):
                    Files().choose(str(path), lambda _: True, explicit=True)
                popen.assert_not_called()
            else:
                self.assertEqual(windows_path(path), str(path))

    def test_file_size_binary_and_selection_reset(self):
        path = Path(self.temp.name) / "large.txt"
        path.write_bytes(b"x" * 200001)
        with self.assertRaises(ValueError):
            self.runtime.files.choose(str(path), lambda _: True, explicit=True, launch=False)
        path.write_bytes(b"abc\x00def")
        with self.assertRaises(ValueError):
            self.runtime.files.choose(str(path), lambda _: True, explicit=True, launch=False)
        path.write_text("Allowed text", encoding="utf-8")
        self.runtime.files.choose(str(path), lambda _: True, explicit=True, launch=False)
        self.app.tool({"action": "new_session"}, owner_text="Start a new session")
        self.assertEqual(self.runtime.files.selected, {})

    def test_stream_cancellation_checks_reasoning_and_comment_boundaries(self):
        stop = threading.Event()
        class Cancelling(io.BytesIO):
            def readline(self, size=-1):
                value = super().readline(size)
                stop.set()
                return value
        response = Cancelling(b": heartbeat\n\n" + completion('{"action":"reply","text":"Never shown","news_ids":[]}'))
        with self.assertRaisesRegex(ValueError, "cancelled"):
            read_completion(response, cancelled=stop)

    def test_live_notification_waits_for_foreground_and_saves_once(self):
        self.runtime.live_notifications = True
        job = Job(7, "A bounded request", 1, state="done", answer="COMPLETED RESULT")
        with self.runtime.ui_lock:
            thread = threading.Thread(target=self.runtime.completed, args=(job,))
            thread.start()
            thread.join(.05)
            self.assertTrue(thread.is_alive())
            self.assertEqual(self.lines, [])
        thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(self.lines), 1)
        self.runtime.completed(job)
        self.assertEqual(len(self.lines), 1)
        self.assertEqual(len(self.store.history()), 1)

    def test_close_preserves_completed_unshown_result_without_marking_discussed(self):
        n = self.page()
        job = Job(3, "Old request", 1, state="done", answer="Not yet shown", news_ids=[n])
        self.runtime.completed(job)
        self.runtime.close()
        self.assertIn("Not yet shown", self.store.history()[0]["text"])
        self.assertEqual(self.lines, [])
        self.assertIsNone(self.store.article(n)["shown"])

    def test_cancelled_model_does_not_open_a_connection(self):
        stop = threading.Event()
        stop.set()
        with patch("dot.urllib.request.build_opener") as opener:
            with self.assertRaises(dot.DotError):
                dot.Model(self.config, cancelled=stop).complete([])
            opener.return_value.open.assert_not_called()

    def test_utf16_file_excerpt_offsets(self):
        path = Path(self.temp.name) / "unicode.txt"
        path.write_text("\u03b1" * 6000, encoding="utf-16")
        files = Files()
        first = files.choose(str(path), lambda _: True, explicit=True, launch=False)
        self.assertEqual(len(first["body"]), 5000)
        self.assertTrue(first["more_text_available"])
        second = files.read(str(path), 5000)
        self.assertEqual(len(second["body"]), 1000)
        self.assertFalse(second["more_text_available"])
        for offset in (-1, 6001, True, "1"):
            with self.assertRaises(ValueError):
                files.read(str(path), offset)

    def test_malformed_action_has_no_side_effect(self):
        self.assertIn("error", self.app.tool({"action": ["browser_open"]}))
        self.browser.call.assert_not_called()


class JobTests(unittest.TestCase):
    def test_hard_cap_and_foreground_remains_available(self):
        release = threading.Event()
        started = []
        def work(task, stop, session, progress):
            started.append(task)
            release.wait(4)
            return {"answer": task, "news_ids": []}
        jobs = Jobs(work, lambda _: None)
        try:
            first = jobs.start("One", 1)
            jobs.start("Two", 1)
            self.assertEqual(jobs.status()["active"], 2)
            with self.assertRaises(ValueError):
                jobs.start("Forbidden third", 1)
            self.assertEqual(jobs.next_id, 3)
            self.assertEqual(len(started), 2)
            jobs.cancel_job(first["id"])
            self.assertEqual(jobs.status()["active"], 2)  # not free until actually stopped
        finally:
            release.set()
            jobs.close()
        self.assertEqual(jobs.get(first["id"])["state"], "cancelled")
        self.assertEqual(jobs.status()["active"], 0)

    def test_concurrent_start_race_never_exceeds_two(self):
        release = threading.Event()
        jobs = Jobs(lambda *args: (release.wait(4), {"answer": "Done"})[1], lambda _: None)
        accepted, rejected = [], []
        def attempt(i):
            try:
                accepted.append(jobs.start(f"Task {i}", 1))
            except ValueError:
                rejected.append(i)
        threads = [threading.Thread(target=attempt, args=(i,)) for i in range(10)]
        try:
            for thread in threads: thread.start()
            for thread in threads: thread.join(2)
            self.assertEqual((len(accepted), len(rejected)), (2, 8))
            self.assertEqual(jobs.status()["active"], 2)
        finally:
            release.set()
            jobs.close()

    def test_limit_cannot_be_raised_above_two(self):
        for limit in (0, 3, 8, True, "2"):
            with self.assertRaises(ValueError):
                Jobs(lambda *_: None, lambda _: None, limit=limit)

    def test_failure_and_shutdown_are_truthful(self):
        def fail(*_):
            raise ValueError("Actual failure")
        jobs = Jobs(fail, lambda _: None, limit=1)
        result = jobs.start("One", 1)
        jobs.items[result["id"]].thread.join(2)
        self.assertEqual(jobs.get(result["id"])["state"], "failed")
        self.assertEqual(jobs.get(result["id"])["error"], "Actual failure")
        jobs.close()
        with self.assertRaises(ValueError):
            jobs.start("Must not start", 1)


if __name__ == "__main__":
    unittest.main()
