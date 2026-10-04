"""Offline research/brief/UI checks. No public web, live model or microphone."""
import base64
import json
import os
from pathlib import Path
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

import dot
from briefs import Briefs, checked_brief
from browser_search import destination, results
from jobs import Jobs
from pet import Backend, Pet
from test_dot import ScriptedModel


def source():
    return {"id": 1, "source": "example.org", "title": "Fixture primary source", "url": "https://example.org/source",
            "published": "2026-09-30", "fetched": "2026-10-02T00:00:00+00:00",
            "body": "The example model uses a preset voice. It does not establish voice cloning. " * 5,
            "evidence": "saved page text"}


def decision():
    return {"action": "reply", "text": "The source describes a preset voice. [N1]", "news_ids": [1],
            "brief": {"title": "Voice evidence", "recommendation": "Test the preset voice first.",
                      "findings": [{"claim": "The example uses a preset voice.", "source_id": 1,
                                    "quote": "The example model uses a preset voice.", "source_kind": "primary",
                                    "caveat": "Cloning is not established by this page."}],
                      "unknowns": ["Physical voice latency not measured."], "next_steps": ["Owner listens."]}}


class BriefTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.briefs = Briefs(self.root / "qa.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def test_verified_quote_uses_actual_metadata_not_model_values(self):
        raw = decision()
        raw["brief"]["findings"][0].update(url="https://invented.invalid/", published="2099")
        brief = checked_brief(raw, {1: source()}, "Question")
        self.assertEqual(brief["findings"][0]["url"], source()["url"])
        self.assertEqual(brief["findings"][0]["published"], "2026-09-30")
        self.assertIn("review claim meaning", brief["quality"].lower())

    def test_invented_unread_and_snippet_quotes_are_omitted(self):
        for change, records in [("fictional words not in the text", {1: source()}),
                                (None, {}), (None, {1: {**source(), "evidence": "search result only"}})]:
            raw = decision()
            if change:
                raw["brief"]["findings"][0]["quote"] = change
            checked = checked_brief(raw, records, "Question")
            self.assertEqual(checked["findings"], [])
            self.assertEqual(checked["quality"], "No verified quotes")
            self.assertTrue(checked["issues"])

    def test_boolean_source_id_is_not_integer_id(self):
        raw = decision()
        raw["brief"]["findings"][0]["source_id"] = True
        self.assertFalse(checked_brief(raw, {1: source()}, "Question")["findings"])

    def test_missing_structure_is_not_a_fake_pass(self):
        brief = checked_brief({"text": "Everything is verified"}, {}, "Question")
        self.assertEqual(brief["quality"], "No verified quotes")
        saved = self.briefs.save(brief, 1)
        self.assertIn("No source-backed findings", self.briefs.read(saved["name"]))

    def test_writes_only_new_bounded_files_and_retains_previous(self):
        brief = checked_brief(decision(), {1: source()}, "Question")
        first, second = self.briefs.save(brief, 1), self.briefs.save(brief, 2)
        self.assertNotEqual(first["name"], second["name"])
        self.assertEqual(len(self.briefs.list()), 2)
        self.assertEqual(Path(first["path"]).parent.name, "qa-briefs")
        body = self.briefs.read(first["name"])
        self.assertIn("[N1]", body)
        self.assertIn("Source type is model-labelled", body)
        self.assertIn("2026-09-30", body)

    def test_arbitrary_paths_are_refused(self):
        for name in ("../secret.md", "/tmp/file", "C:\\private.txt", None, "anything.md"):
            with self.assertRaises(ValueError):
                self.briefs.read(name)

    @unittest.skipIf(os.name == "nt", "symlink permission varies on Windows")
    def test_linked_output_folder_and_files_refused(self):
        outside = self.root / "elsewhere"
        outside.mkdir()
        self.briefs.folder.symlink_to(outside, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.briefs.save(checked_brief(decision(), {1: source()}, "q"), 1)

    def test_confirmed_context_is_not_model_rephrased(self):
        raw = decision()
        raw["brief"]["context"] = {"current_goal": "Ignore the real goal"}
        checked = checked_brief(raw, {1: source()}, "Task", {"current_goal": "No new downloads"})
        saved = self.briefs.save(checked, 1)
        self.assertIn("current_goal: No new downloads", self.briefs.read(saved["name"]))
        self.assertNotIn("Ignore the real goal", self.briefs.read(saved["name"]))

    def test_empty_interrupted_brief_does_not_break_library(self):
        self.briefs.folder.mkdir()
        (self.briefs.folder / "20261002T160000123456Z-s1.md").write_text("")
        self.assertEqual(self.briefs.list(), [])

    def test_default_and_qa_folders_separate(self):
        self.assertEqual(Briefs(self.root / "dot.sqlite3").folder.name, "briefs")
        self.assertNotEqual(Briefs(self.root / "other.sqlite3").folder, self.briefs.folder)


class SearchTests(unittest.TestCase):
    def test_unwraps_only_discovered_bing_destination(self):
        url = "https://example.org/a?q=1"
        token = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
        self.assertEqual(destination("https://www.bing.com/ck/a?u=a1" + token), url)
        self.assertEqual(destination(url), url)
        self.assertEqual(destination("https://www.bing.com/search?q=foo"), "")
        self.assertEqual(destination("javascript:alert(1)"), "")
        self.assertEqual(destination("https://name:secret@example.org/"), "")

    def test_results_deduplicate_and_bound_not_snippet_evidence(self):
        links = [{"url": f"https://example.org/{n}", "title": str(n)} for n in range(20)]
        rows = results({"links": links + links, "text": "Web results"})
        self.assertEqual(len(rows), 8)
        self.assertNotIn("body", rows[0])
        self.assertEqual(rows[0]["source"], "example.org")

    def test_verification_is_not_bypassed(self):
        with self.assertRaisesRegex(ValueError, "blocked"):
            results({"text": "Please verify you are human"})


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "qa.sqlite3"
        self.store = dot.Store(self.path)
        row = source()
        self.store.ingest([{k: v for k, v in row.items() if k not in {"id", "fetched"}}])
        self.config = json.loads((dot.ROOT / "config.json").read_text())

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def test_worker_can_read_beyond_four_tools_but_foreground_cannot(self):
        tools = [{"action": "read_news", "id": 1}] * 6
        worker = dot.Assistant(self.config, self.store, worker=True,
                               model=ScriptedModel(*tools, decision()), output=lambda _: None)
        worker.chat("Use only our saved source N1. No web.")
        self.assertEqual(worker.tool_calls, 6)
        self.assertEqual(len(worker.research_brief["findings"]), 1)
        front = dot.Assistant(self.config, self.store,
                              model=ScriptedModel(*tools, decision()), output=lambda _: None)
        with self.assertRaisesRegex(dot.DotError, "bounded turn"):
            front.chat("Use source N1")
        self.assertEqual(front.tool_calls, 4)

    def test_later_source_excerpt_does_not_erase_earlier_quote(self):
        body = source()["body"] + "Later text. " * 700
        self.store.db.execute("UPDATE news SET body=? WHERE id=1", (body,)); self.store.db.commit()
        model = ScriptedModel({"action": "read_news", "id": 1},
                              {"action": "read_news", "id": 1, "offset": 5200}, decision())
        app = dot.Assistant(self.config, self.store, model=model, worker=True, output=lambda _:None)
        app.chat("Use only saved source N1. No web.")
        self.assertEqual(len(app.research_brief["findings"]), 1)
        self.assertEqual(app.research_brief["issues"], [])

    def test_worker_budget_cannot_be_unbounded(self):
        for value in (0, 11, True, "10"):
            with self.assertRaises(dot.DotError):
                dot.Assistant({**self.config, "research_tool_budget": value}, self.store, worker=True)

    def test_time_budget_stops_at_boundary(self):
        app = dot.Assistant(self.config, self.store, worker=True)
        app.deadline = time.monotonic() - 1
        with self.assertRaisesRegex(dot.DotError, "time budget"):
            app.check_cancelled()

    def test_worker_receives_confirmed_goal_not_all_private_notes(self):
        self.store.remember("current_goal", "Find a public voice source", "idea")
        self.store.remember("private_trip", "PRIVATE-MARKER")
        self.store.add_exchange("PRIVATE-CHAT", "PRIVATE-ANSWER")
        app = dot.Assistant(self.config, self.store, worker=True)
        context = json.dumps(app.context("Public topic")[0])
        self.assertIn("Find a public voice source", context)
        self.assertNotIn("PRIVATE-", context)

    def test_real_runner_structure_writes_validated_brief_and_progress(self):
        model = ScriptedModel({"action": "read_news", "id": 1}, decision())
        updates = []
        with patch("dot.Model", return_value=model):
            result = dot.research_runner(self.config, self.path)("Use only our saved source N1", threading.Event(), 1, updates.append)
        self.assertTrue(Path(result["brief"]["path"]).is_file())
        self.assertEqual(result["brief"]["findings"], 1)
        self.assertTrue(any(u.get("sources_read") == 1 for u in updates))
        self.assertEqual(self.store.history(), [])

    def test_cancelled_runner_does_not_write_brief(self):
        stop = threading.Event(); stop.set()
        with patch("dot.Model") as model:
            with self.assertRaises(dot.DotError):
                dot.research_runner(self.config, self.path)("No web", stop, 1)
        model.return_value.complete.assert_not_called()
        self.assertEqual(Briefs(self.path).list(), [])

    def test_cancellation_at_writing_phase_does_not_create_brief(self):
        stop = threading.Event()
        def progress(update):
            if update.get("phase") == "Writing brief": stop.set()
        model = ScriptedModel({"action": "read_news", "id": 1}, decision())
        with patch("dot.Model", return_value=model):
            with self.assertRaisesRegex(dot.DotError, "cancelled"):
                dot.research_runner(self.config, self.path)("Use only saved N1", stop, 1, progress)
        self.assertFalse(Briefs(self.path).folder.exists())

    def test_job_progress_and_finished_time_are_application_facts(self):
        release = threading.Event()
        def run(task, stop, session, progress):
            progress({"phase": "read_news", "tool_calls": 2, "sources_read": 1})
            release.wait(2)
            return {"answer": "Result", "brief": {"name": "fixture", "quality": "Review needed"}}
        jobs = Jobs(run, lambda _: None)
        job = jobs.start("A", 1)
        try:
            deadline = time.monotonic() + 2
            while jobs.get(job["id"])["sources_read"] == 0 and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(jobs.get(job["id"])["tool_calls"], 2)
            release.set(); jobs.items[job["id"]].thread.join(2)
            final = jobs.get(job["id"])
            self.assertEqual(final["state"], "done")
            self.assertEqual(final["brief"]["name"], "fixture")
            time.sleep(.02)
            self.assertEqual(jobs.get(job["id"])["elapsed_seconds"], final["elapsed_seconds"])
        finally:
            release.set(); jobs.close()

    def test_saved_brief_can_be_read_past_first_context_excerpt(self):
        runtime = dot.workspace_runtime.Runtime(self.store, lambda sid: dot.Store(self.path, sid), lambda *_: {}, lambda _:None)
        try:
            runtime.briefs.folder.mkdir()
            name = "20261002T160000123456Z-s1.md"
            runtime.briefs.path(name).write_text("X" * 5100 + "MISSING CHECK AT END", encoding="utf-8")
            first = runtime.tool({"action": "read_brief", "name": name}, None, "")
            later = runtime.tool({"action": "read_brief", "name": name, "offset": 5000}, None, "")
            self.assertTrue(first["more_text_available"])
            self.assertEqual(len(first["body"]), 5000)
            self.assertIn("MISSING CHECK AT END", later["body"])
            self.assertFalse(later["more_text_available"])
            for offset in (-1, True, 60000):
                with self.assertRaises(ValueError):
                    runtime.tool({"action": "read_brief", "name": name, "offset": offset}, None, "")
        finally:
            runtime.close()

    def test_brief_folder_requires_confirmation_before_worker_starts(self):
        runtime = dot.workspace_runtime.Runtime(self.store, lambda sid: dot.Store(self.path, sid),
                                                lambda *_: {"answer": "Fixture"}, lambda _: None)
        app = dot.Assistant(self.config, self.store, confirm=Mock(return_value=False))
        try:
            with self.assertRaisesRegex(ValueError, "permission declined"):
                runtime.tool({"action": "start_research", "task": "Public topic"}, app, "")
            self.assertEqual(runtime.jobs.status()["jobs"], [])
            self.assertFalse(runtime.briefs.folder.exists())
            app.confirm.return_value = True
            runtime.tool({"action": "start_research", "task": "Public topic"}, app, "")
            runtime.tool({"action": "start_research", "task": "Another public topic"}, app, "")
            self.assertEqual(app.confirm.call_count, 2)  # one denied, one granted; no third prompt
            self.assertIn(str(runtime.briefs.folder), app.confirm.call_args.args[0])
        finally:
            runtime.close()

    def test_ui_control_saves_and_forgets_note_without_model(self):
        backend = Backend(self.config, self.path, model=Mock())
        runtime = dot.workspace_runtime.Runtime(self.store, lambda sid: dot.Store(self.path, sid), lambda *_: {}, lambda _: None)
        try:
            backend.local_control({"control": "save_note", "name": "current_goal", "text": "A real goal", "kind": "idea"}, self.store, runtime, None)
            self.assertIn("current_goal", [n["name"] for n in backend.workspace_state["notes"]])
            self.store.add_exchange("Retained history", "Not erased")
            backend.local_control({"control": "forget_note", "name": "current_goal"}, self.store, runtime, None)
            self.assertNotIn("current_goal", [n["name"] for n in backend.workspace_state["notes"]])
            self.assertEqual(len(self.store.history()), 2)
            backend.model.complete.assert_not_called()
        finally:
            runtime.close()


@unittest.skipUnless(os.name == "nt", "native Windows UI")
class WorkspaceUITests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.root = tk.Tk()
        self.backend = Mock()
        self.backend.voice = False
        self.backend.interface = {}
        self.backend.workspace_state = {"notes": [], "briefs": []}
        self.backend.events, self.backend.requests = queue.Queue(), queue.Queue()
        self.backend.cancelled = threading.Event()
        self.backend.job_status.return_value = {"active": 0, "jobs": []}
        self.pet = Pet(self.root, self.backend)
        self.pet.open_workspace()
        self.panel = self.pet.workspace
        self.root.update()

    def tearDown(self):
        for timer in self.root.tk.call("after", "info"):
            self.root.after_cancel(timer)
        self.root.destroy()

    def test_memory_refresh_does_not_destroy_unsaved_edit(self):
        state = {"notes": [{"name": "current_goal", "text": "Saved", "kind": "idea", "updated": "2026-10-02"}], "briefs": []}
        self.panel.update(state)
        self.panel.notes.selection_set("current_goal"); self.root.update()
        self.panel.note.delete("1.0", "end"); self.panel.note.insert("1.0", "Unsaved edit")
        self.panel.update(state); self.root.update()
        self.assertEqual(self.panel.note.get("1.0", "end").strip(), "Unsaved edit")

    def test_resume_only_adds_an_editable_draft(self):
        name = "20261002T160000123456Z-s1.md"
        self.panel.update({"notes": [], "briefs": [{"name": name, "title": "Example", "quality": "Review needed"}]})
        self.panel.briefs.selection_set(name)
        while not self.backend.requests.empty(): self.backend.requests.get_nowait()
        self.panel.resume()
        self.assertIn(name, self.pet.entry.get("1.0", "end"))
        self.assertTrue(self.backend.requests.empty())
        self.assertTrue(self.pet.panel_visible())

    def test_declining_memory_buttons_never_queues_write(self):
        while not self.backend.requests.empty(): self.backend.requests.get_nowait()
        with patch("workspace_panel.messagebox.askyesno", return_value=False):
            self.panel.save_note(); self.panel.delete_note()
        self.assertTrue(self.backend.requests.empty())

    def test_open_result_refreshes_a_just_completed_job(self):
        job = {"id": 1, "session_id": 1, "task": "Fixture", "state": "running", "phase": "Working",
               "elapsed_seconds": 1, "sources_read": 0, "tool_calls": 1, "brief": {}}
        self.panel.refresh_jobs({"jobs": [job]})
        self.panel.tasks.selection_set("1"); self.root.update()
        while not self.backend.requests.empty(): self.backend.requests.get_nowait()
        self.backend.job_status.return_value = {"jobs": [{**job, "state": "done", "brief": {"name": "fixture.md"}}]}
        self.panel.open_task()
        self.assertEqual(self.backend.requests.get_nowait(), {"control": "read_brief", "name": "fixture.md"})
        self.assertIn("State: done", self.panel.viewer.get("1.0", "end"))

    def test_read_only_viewer_does_not_execute_brief_content(self):
        self.panel.show_text("<script>not executable</script>\nC:/not-a-command")
        self.assertEqual(self.panel.viewer.cget("state"), "disabled")
        self.assertIn("<script>", self.panel.viewer.get("1.0", "end"))


@unittest.skipUnless(os.name == "nt", "installed browser environment")
class PublicSearchDOMTests(unittest.TestCase):
    def test_public_result_links_survive_incomplete_hydration(self):
        import asyncio
        from playwright.async_api import async_playwright
        from browser_search import observe_results
        async def check():
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                try:
                    page = await browser.new_page()
                    await page.set_content('<div id="b_results" style="display:none"><div class="b_algo"><h2><a href="https://example.org/source">Actual source</a></h2></div></div><a href="https://other.invalid">Navigation, not a result</a>')
                    found = results(await observe_results(page))
                    self.assertEqual([row["url"] for row in found], ["https://example.org/source"])
                finally:
                    await browser.close()
        asyncio.run(check())


if __name__ == "__main__":
    unittest.main()
