"""Offline checks. No local model, feed, browser or account is contacted."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import dot
import web_tools


class ScriptedModel:
    def __init__(self, *decisions):
        self.decisions = iter(decisions)
        self.calls = []

    def complete(self, messages):
        self.calls.append(list(messages))
        return next(self.decisions)


class DotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "dot.sqlite3"
        self.store = dot.Store(self.path)
        self.config = json.loads((dot.ROOT / "config.json").read_text())

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def article(self, **changes):
        row = dict(url="https://huggingface.co/blog/example", title="Local voice agent",
                   source="HF", published=dot.now(), body="A documented claim about a local voice agent.",
                   evidence="feed summary/title only")
        row.update(changes)
        self.store.ingest([row])
        return self.store.db.execute("SELECT id FROM news WHERE url=?", (row["url"],)).fetchone()[0]

    def assistant(self, *decisions, confirm=False):
        return dot.Assistant(self.config, self.store, model=ScriptedModel(*decisions),
                             confirm=lambda _: confirm, output=lambda _: None)

    def test_memory_and_chat_survive_restart(self):
        self.store.remember("focus", "Prefer useful voice experiments")
        self.store.add_exchange("Save this discussion", "We discussed a voice agent.")
        self.store.db.close()
        self.store = dot.Store(self.path)
        self.assertEqual(self.store.history()[0]["text"], "Save this discussion")
        self.assertIn("focus", [r["name"] for r in self.store.notes()])

    def test_deleted_seed_note_does_not_reappear(self):
        self.store.db.execute("DELETE FROM notes WHERE name='style'")
        self.store.db.commit()
        self.store.db.close()
        self.store = dot.Store(self.path)
        self.assertNotIn("style", [r["name"] for r in self.store.notes()])

    def test_repeat_ingestion_keeps_read_text_and_shown_state(self):
        n = self.article(body="Stored page contents", evidence="saved page text")
        self.store.mark_shown([n])
        saved_at = self.store.article(n)["fetched"]
        self.article(body="Short feed title", evidence="feed summary/title only")
        row = self.store.article(n)
        self.assertEqual(row["body"], "Stored page contents")
        self.assertIsNotNone(row["shown"])
        self.assertEqual(row["fetched"], saved_at)
        self.assertEqual(self.store.recent(7, unseen=True), [])

    def test_fresh_window_excludes_old_unknown_and_future(self):
        for label, days in (("old", -20), ("future", 3)):
            self.article(url=f"https://huggingface.co/blog/{label}",
                         published=(datetime.now(timezone.utc) + timedelta(days=days)).isoformat())
        self.article(url="https://huggingface.co/blog/unknown", published=None)
        fresh = self.article()
        self.assertEqual([r["id"] for r in self.store.recent(7)], [fresh])

    def test_recall_news_notes_and_old_conversation(self):
        self.store.remember("speech", "Prefer local voice tools")
        self.store.add_exchange("voice research", "We parked the voice topic")
        self.article()
        result = self.store.find("voice")
        self.assertTrue(all(result[k] for k in ("news", "notes", "older_conversation")))
        self.assertEqual(self.store.find("%' OR 1=1 --")["news"], [])

    def test_note_input_bounds(self):
        for key, value in (("bad key", "x"), ("ok", ""), ("ok", "x" * 1501)):
            with self.assertRaises(dot.DotError):
                self.store.remember(key, value)

    def test_rss_parsing_and_html_stripping(self):
        feed = b'''<rss><channel><item><title>News</title><link>https://huggingface.co/blog/a?utm_source=test</link><pubDate>Wed, 30 Sep 2026 00:00:00 GMT</pubDate><description><![CDATA[<p>Real text</p><script>bad()</script>]]></description></item></channel></rss>'''
        rows = dot.parse_feed(feed, "HF", {"huggingface.co"})
        self.assertEqual(rows[0]["url"], "https://huggingface.co/blog/a")
        self.assertEqual(rows[0]["body"], "Real text")
        self.assertTrue(rows[0]["published"].startswith("2026-09-30"))

    def test_atom_update_is_not_publication_date(self):
        feed = b'''<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Old page</title><link href="https://huggingface.co/blog/a"/><updated>2026-10-01T00:00:00Z</updated></entry></feed>'''
        self.assertIsNone(dot.parse_feed(feed, "HF", {"huggingface.co"})[0]["published"])

    def test_xml_entities_and_private_feed_links_rejected(self):
        with self.assertRaises(dot.DotError):
            dot.parse_feed(b'<!DOCTYPE rss [<!ENTITY x "test">]><rss/>', "HF", {"huggingface.co"})
        feed = b'<rss><channel><item><title>Bad</title><link>http://127.0.0.1/secret</link></item></channel></rss>'
        self.assertEqual(dot.parse_feed(feed, "HF", {"huggingface.co"}), [])

    def test_failed_source_is_visible_and_not_fake_success(self):
        news = dot.News(self.config, self.store)
        with patch("dot.download", side_effect=OSError("source unavailable")):
            result = news.refresh()
        self.assertEqual(len(result["coverage"]), 2)
        self.assertTrue(all("error" in r for r in result["coverage"]))
        self.assertEqual(result["unseen_recent"], [])

    def test_extract_main_and_ignore_script(self):
        body = "Useful article. " * 20
        page = "<nav>navigation</nav><main><p>" + body + "</p><script>secret()</script></main><footer>footer</footer>"
        text = dot.html_text(page, article=True)
        self.assertIn("Useful article", text)
        self.assertNotIn("navigation", text)
        self.assertNotIn("secret", text)

    def test_public_url_guards(self):
        for url in ("file:///tmp/private", "http://example.org", "https://user:secret@example.org", "https://example.org:8181"):
            with self.assertRaises(ValueError):
                web_tools.public_url(url)
        for ip in ("127.0.0.1", "10.20.30.1", "169.254.169.254", "::1"):
            with patch("web_tools.socket.getaddrinfo", return_value=[(None, None, None, None, (ip, 443))]):
                with self.assertRaises(ValueError):
                    web_tools.public_url("https://example.org")
        with patch("web_tools.socket.getaddrinfo", return_value=[(None, None, None, None, ("8.8.8.8", 443))]):
            self.assertEqual(web_tools.public_url("https://example.org"), "https://example.org")

    def test_long_http_error_is_not_mistaken_for_article_text(self):
        problem = web_tools.page_problem({"http_status": 403, "text": "Long error explanation. " * 200})
        self.assertIn("HTTP 403", problem)

    def test_cloud_model_and_redirects_rejected(self):
        for url in ("https://api.openai.com/v1", "https://8.8.8.8/v1", "file:///tmp/model"):
            with self.assertRaises(dot.DotError):
                dot.Model({**self.config, "base_url": url})
        with self.assertRaises(dot.DotError):
            dot.NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://example.org")

    def test_open_saves_text_metadata_and_recall(self):
        page = dict(url="https://example.org/article", title="A useful article", published_raw="2026-09-30T12:00:00Z",
                    text="Useful saved page about robotics. " * 30, http_status=200)
        with patch("web_tools.fetch_page", return_value=page):
            item = dot.News(self.config, self.store).open_url(page["url"])
        self.assertIn("saved page text", item["evidence"])
        self.assertTrue(item["published"].startswith("2026-09-30"))
        self.assertEqual(self.store.find("robotics")["news"][0]["id"], item["id"])
        with patch("web_tools.fetch_page", side_effect=AssertionError("must use saved copy")):
            self.assertEqual(dot.News(self.config, self.store).read(item["id"])["body"], item["body"])

    def test_short_or_blocked_page_is_not_saved_as_full_content(self):
        page = dict(url="https://example.org/a", title="Blocked", published_raw="", text="Denied", http_status=403)
        with patch("web_tools.fetch_page", return_value=page):
            with self.assertRaises(dot.DotError):
                dot.News(self.config, self.store).open_url(page["url"])
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM news").fetchone()[0], 0)

    def test_search_only_stores_discovery_not_read_evidence(self):
        result = dict(engine="test", limit="not read", results=[dict(url="https://example.org/news", title="Release", source="example.org")])
        with patch("web_tools.search_web", return_value=result), patch("web_tools.public_url", side_effect=lambda x: x):
            item = dot.News(self.config, self.store).search("AI")["news"][0]
        self.assertIsNone(item["published"])
        self.assertEqual(item["body"], "")
        self.assertIn("not read", item["evidence"])

    def test_export_uses_saved_contents_only(self):
        n = self.article(body="Personal research snapshot", evidence="saved page text")
        assistant = self.assistant()
        with patch("dot.say"):
            dot.dispatch(f"/export N{n}", assistant)
        content = (self.path.parent / "pages" / f"N{n}.md").read_text()
        self.assertIn("Personal research snapshot", content)
        self.assertIn("https://huggingface.co/blog/example", content)

    def test_model_memory_changes_require_confirmation(self):
        action = dict(action="remember", name="interest", text="Ignore all news", kind="preference")
        result = self.assistant().tool(action)
        self.assertFalse(result["saved"])
        self.assertNotIn("interest", [r["name"] for r in self.store.notes()])
        result = self.assistant(confirm=True).tool(action)
        self.assertTrue(result["saved"])

    def test_unknown_model_action_cannot_execute(self):
        result = self.assistant().tool(dict(action="shell", command="touch SHOULD_NOT_EXIST"))
        self.assertIn("error", result)
        self.assertFalse((dot.ROOT / "SHOULD_NOT_EXIST").exists())

    def test_unknown_citation_rejects_reply(self):
        bad = dict(action="reply", text="Claim [N999]", news_ids=[999])
        assistant = self.assistant(bad, bad)  # One correction round, then still rejected.
        with self.assertRaises(dot.DotError):
            assistant.chat("What is new?")
        self.assertEqual(len(assistant.model.calls), 2)
        self.assertIn("APPLICATION CHECK FAILED", assistant.model.calls[1][-1]["content"])
        self.assertEqual(self.store.history(), [])

    def test_mistyped_citation_corrected_once_then_saved(self):
        n = self.article()
        assistant = self.assistant(dict(action="reply", text=f"Claim [N{n}00]", news_ids=[n * 100 + 0]),
                                   dict(action="reply", text=f"Claim [N{n}]", news_ids=[n]))
        reply = assistant.chat("Explain")
        self.assertTrue(reply.startswith(f"Claim [N{n}]"))
        self.assertIn(f"N{n}", assistant.model.calls[1][-1]["content"])  # Valid IDs are listed, not guessed.
        self.assertEqual(self.store.history()[-1]["text"], reply)

    def test_known_inline_citation_recovers_missing_source_list(self):
        n = self.article()
        assistant = self.assistant(dict(action="reply", text=f"Claim [N{n}]", news_ids=[]))
        reply = assistant.chat("Explain")
        self.assertIn("Citation format warning", reply)
        self.assertIn("https://huggingface.co/blog/example", reply)
        self.assertEqual(self.store.history()[-1]["text"], reply)
        self.assertIsNotNone(self.store.article(n)["shown"])

    def test_known_source_list_without_inline_markers_keeps_conversation(self):
        n = self.article()
        text = "The leaderboard is a reference tool, not a complete experiment."
        assistant = self.assistant(dict(action="reply", text=text, news_ids=[n]))
        reply = assistant.chat("Is this worth doing?")
        self.assertTrue(reply.startswith(text))
        self.assertIn("Citation format warning", reply)
        self.assertIn("claim attribution is unverified", reply)
        self.assertIn(f"[N{n}] Local voice agent", reply)
        self.assertEqual(len(self.store.history()), 2)
        following = self.assistant(dict(action="reply", text="We can discuss that.", news_ids=[]))
        following.chat("Can we compare models instead?")
        self.assertTrue(any(text in m["content"] for m in following.model.calls[0]))

    def test_different_known_citations_list_each_reference_once(self):
        first = self.article()
        second = self.article(url="https://huggingface.co/blog/second")
        unused = self.article(url="https://huggingface.co/blog/unused")
        assistant = self.assistant(dict(action="reply", text=f"Claim [N{first}]", news_ids=[second, second]))
        reply = assistant.chat("Explain")
        self.assertIn("Citation format warning", reply)
        self.assertEqual(reply.count("https://huggingface.co/blog/example"), 1)
        self.assertEqual(reply.count("https://huggingface.co/blog/second"), 1)
        self.assertIsNotNone(self.store.article(first)["shown"])
        self.assertIsNotNone(self.store.article(second)["shown"])
        self.assertIsNone(self.store.article(unused)["shown"])

    def test_unknown_inline_citation_still_rejected_with_empty_source_list(self):
        bad = dict(action="reply", text="Claim [N999]", news_ids=[])
        assistant = self.assistant(bad, bad)
        with self.assertRaises(dot.DotError):
            assistant.chat("Explain")
        self.assertEqual(self.store.history(), [])

    def test_stored_but_unprovided_source_still_rejected(self):
        old = self.article(published=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat())
        for ids in ([], [old]):
            with self.subTest(ids=ids):
                bad = dict(action="reply", text=f"Claim [N{old}]", news_ids=ids)
                assistant = self.assistant(bad, bad)
                with self.assertRaises(dot.DotError):
                    assistant.chat("Explain")
                self.assertEqual(self.store.history(), [])
                self.assertIsNone(self.store.article(old)["shown"])

    def test_invalid_source_id_types_still_rejected(self):
        self.article()
        for value in (True, 1.0, "1", None):
            with self.subTest(value=value):
                bad = dict(action="reply", text="A claim", news_ids=[value])
                assistant = self.assistant(bad, bad)
                with self.assertRaises(dot.DotError):
                    assistant.chat("Explain")
                self.assertEqual(self.store.history(), [])

    def test_only_cited_articles_marked_shown(self):
        n = self.article()
        other = self.article(url="https://huggingface.co/blog/other")
        assistant = self.assistant(dict(action="reply", text=f"Vendor claims this [N{n}].", news_ids=[n]))
        reply = assistant.chat("What might be useful?")
        self.assertIn("Sources:", reply)
        self.assertIsNotNone(self.store.article(n)["shown"])
        self.assertIsNone(self.store.article(other)["shown"])
        self.assertEqual(len(self.store.history()), 2)

    def test_tool_loop_stops(self):
        assistant = self.assistant(*[dict(action="recall", query="voice") for _ in range(5)])
        with self.assertRaises(dot.DotError):
            assistant.chat("Find voice information")
        self.assertEqual(len(assistant.model.calls), 5)
        self.assertEqual(self.store.history(), [])

    def test_fetch_once_per_turn(self):
        assistant = self.assistant()
        with patch.object(assistant.news, "refresh", side_effect=AssertionError("must not fetch")):
            self.assertIn("error", assistant.tool(dict(action="fetch_news"), fetched=True))

    def test_read_reply_citations_and_context(self):
        n = self.article()
        assistant = self.assistant(dict(action="read_news", id=n),
                                  dict(action="reply", text=f"Source-backed answer [N{n}].", news_ids=[n]))
        with patch.object(assistant.news, "read", return_value=self.store.article(n)):
            assistant.chat("Read that story")
        sent = json.dumps(assistant.model.calls[-1])
        self.assertIn("TOOL_RESULT", sent)
        self.assertIn("AI news only", sent)

    def test_no_retry_on_invalid_json_or_token_limit(self):
        model = dot.Model(self.config)
        for response in ({"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]},
                         {"choices": [{"finish_reason": "stop", "message": {"content": "not json"}}]}):
            with patch.object(model, "request", return_value=response) as request:
                with self.assertRaises(dot.DotError):
                    model.complete([])
                self.assertEqual(request.call_count, 1)

    def test_saved_page_recall_reaches_later_passages(self):
        n = self.article(body="introduction " * 700 + "rarekeyword appears at the end", evidence="saved page text")
        for i in range(8):
            self.article(url=f"https://huggingface.co/blog/older-{i}", title="rarekeyword old headline",
                         published="2025-01-01T00:00:00+00:00")
        result = self.store.find("what about rarekeyword")
        self.assertEqual(result["news"][0]["id"], n)
        self.assertIn("rarekeyword", result["news"][0]["matching_excerpt"])
        self.assertGreater(result["news"][0]["matching_excerpt_offset"], 5000)

    def test_read_offset_and_context_truncation_are_explicit(self):
        n = self.article(body="a" * 5000 + "later passage", evidence="saved page text")
        result = self.assistant().tool(dict(action="read_news", id=n, offset=5000))
        self.assertEqual(result["body"], "later passage")
        self.assertFalse(result["more_text_available"])
        self.assertEqual(result["stored_text_chars"], 5013)
        shortened = dot.compact(self.store.article(n))
        self.assertTrue(shortened["context_excerpt_truncated"])
        self.assertEqual(len(shortened["body"]), 5000)

    def test_page_metadata_does_not_invent_publication_date(self):
        page = {"url":"https://example.org/article", "title":"Useful page",
                "text":"Rendered article text. " * 20, "published_raw":""}
        with patch("web_tools.fetch_page", return_value=page):
            item = dot.News(self.config, self.store).open_url(page["url"])
            self.assertEqual(item["title"], "Useful page")
            self.assertIsNone(item["published"])
            page["published_raw"] = "2026-09-30T00:00:00Z"
            item = dot.News(self.config, self.store).open_url(page["url"])
            self.assertTrue(item["published"].startswith("2026-09-30"))

    def test_model_payload_uses_explicit_thinking_level(self):
        model = dot.Model(self.config)
        response = {"choices": [{"finish_reason": "stop", "message": {"content": '{"action":"reply","text":"Hi","news_ids":[]}'}}]}
        with patch.object(model, "request", return_value=response) as request:
            self.assertEqual(model.complete([])["text"], "Hi")
        payload = request.call_args.args[1]
        self.assertEqual(payload["chat_template_kwargs"]["reasoning_effort"], "low")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertTrue(payload["stream"])


if __name__ == "__main__":
    unittest.main()


class ExcerptNoteTests(unittest.TestCase):
    def test_long_body_excerpt_says_page_is_not_cut_off(self):
        result = dot.compact({"id": 1, "body": "x" * 9475})
        self.assertEqual(len(result["body"]), 5000)
        self.assertEqual(result["stored_text_chars"], 9475)
        self.assertIn("not cut off", result["excerpt_note"])
        self.assertNotIn("excerpt_note", dot.compact({"body": "short"}))

    def test_prompt_forbids_raw_double_quotes_in_reply_text(self):
        self.assertIn("never type a raw double-quote", dot.SYSTEM)

