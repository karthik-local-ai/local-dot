"""Offline streaming checks: synthetic SSE only, no model or web calls."""
import io
import json
from http.client import IncompleteRead
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import dot
from streaming import ReplyText, events, read_completion


def event(value):
    return ("data: " + json.dumps(value, ensure_ascii=False) + "\n\n").encode("utf-8")


def completion(raw, *, finish="stop", done=True, width=5):
    result = event({"choices": [{"index": 0, "delta": {"reasoning": "INTERNAL REASONING"}}]})
    for i in range(0, len(raw), width):
        result += event({"choices": [{"index": 0, "delta": {"content": raw[i:i + width]}}]})
    if finish is not None:
        result += event({"choices": [{"index": 0, "delta": {}, "finish_reason": finish}]})
    result += event({"choices": [], "usage": {"completion_tokens": 12}})
    return result + (b"data: [DONE]\n\n" if done else b"")


class StreamingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "dot.sqlite3"
        self.store = dot.Store(self.path)
        self.config = json.loads((dot.ROOT / "config.json").read_text())
        self.messages = []
        self.chunks = []

    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def model(self, *streams):
        model = dot.Model(self.config)
        responses = [io.BytesIO(data) for data in streams]
        model.opener.open = Mock(side_effect=responses)
        return model, responses

    def app(self, model, confirm=None):
        return dot.Assistant(self.config, self.store, model=model, confirm=confirm,
                             output=self.messages.append, stream_output=self.chunks.append)

    def test_reply_text_decodes_every_character_boundary(self):
        text = 'Hello "world"\nPath C:\\demo\tCafé — 中文 😀 / done.'
        for ascii_only in (True, False):
            with self.subTest(ascii_only=ascii_only):
                chunks = []
                preview = ReplyText(chunks.append)
                value = {"action": "reply", "text": text, "news_ids": []}
                raw = json.dumps(value, ensure_ascii=ascii_only)
                for char in raw:
                    preview.feed(char)
                preview.check(value)
                self.assertEqual("".join(chunks), text)
                self.assertGreater(len(chunks), 5)

    def test_split_escape_and_surrogate_are_held_until_complete(self):
        chunks = []
        preview = ReplyText(chunks.append)
        preview.feed('{"action":"reply","text":"Hi\\')
        self.assertEqual("".join(chunks), "Hi")
        preview.feed('n\\uD83D')
        self.assertEqual("".join(chunks), "Hi\n")
        preview.feed('\\uDE00"}')
        self.assertEqual("".join(chunks), "Hi\n😀")

    def test_tool_text_and_nested_fields_are_never_previewed(self):
        for value in ({"action": "remember", "name": "test", "text": "DO NOT DISPLAY"},
                      {"action": "other", "nested": {"action": "reply", "text": "DO NOT DISPLAY"}}):
            chunks = []
            preview = ReplyText(chunks.append)
            for char in json.dumps(value):
                preview.feed(char)
            self.assertEqual(chunks, [])

    def test_reordered_fields_are_buffered_not_lost(self):
        raw = '{"text":"Buffered answer","news_ids":[],"action":"reply"}'
        model, _ = self.model(completion(raw))
        answer = self.app(model).chat("Say hello")
        self.assertEqual(answer, "Buffered answer")
        self.assertEqual(self.chunks, [])
        self.assertEqual(sum("Dot> Buffered answer" in x for x in self.messages), 1)

    def test_reasoning_and_json_are_hidden(self):
        raw = json.dumps({"action": "reply", "text": "Visible answer", "news_ids": []})
        chunks = []
        preview = ReplyText(chunks.append)
        result = read_completion(io.BytesIO(completion(raw)), preview.feed)
        self.assertEqual("".join(chunks), "Visible answer")
        self.assertNotIn("INTERNAL", "".join(chunks))
        self.assertEqual(result["choices"][0]["message"]["content"], raw)

    def test_sse_comments_crlf_multiline_data_and_final_no_newline(self):
        data = b': heartbeat\r\nevent: message\r\ndata: {"key":\r\ndata: 1}\r\n\r\ndata: [DONE]'
        values = list(events(io.BytesIO(data)))
        self.assertEqual(json.loads(values[0]), {"key": 1})
        self.assertEqual(values[1], "[DONE]")

    def test_stream_must_have_finish_and_done(self):
        for finish, done in (("stop", False), (None, True), (None, False)):
            with self.subTest(finish=finish, done=done):
                with self.assertRaises(ValueError):
                    read_completion(io.BytesIO(completion('{}', finish=finish, done=done)))

    def test_broken_events_error_and_size_limit(self):
        for data in (b'data: not-json\n\n', event({"error": {"message": "failure"}}), event(["not an event"])):
            with self.assertRaises(ValueError):
                read_completion(io.BytesIO(data))
        with self.assertRaises(ValueError):
            list(events(io.BytesIO(b'data: ' + b'x' * 50), max_bytes=20))

    def test_stream_has_no_content_after_finish(self):
        data = event({"choices": [{"delta": {}, "finish_reason": "stop"}]})
        data += event({"choices": [{"delta": {"content": "late"}}]}) + b'data: [DONE]\n\n'
        with self.assertRaises(ValueError):
            read_completion(io.BytesIO(data))

    def test_real_request_shape_streams_and_closes_response(self):
        raw = json.dumps({"action": "reply", "text": "Streaming works.", "news_ids": []})
        model, responses = self.model(completion(raw))
        result = model.complete([], on_text=self.chunks.append)
        self.assertEqual("".join(self.chunks), result["text"])
        req = model.opener.open.call_args.args[0]
        payload = json.loads(req.data)
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["chat_template_kwargs"]["reasoning_effort"], "low")
        self.assertTrue(responses[0].closed)

    def test_reply_saved_once_and_sources_appear_after_stream(self):
        self.store.ingest([dict(url="https://huggingface.co/blog/example", source="HF", title="Example",
                               published=dot.now(), body="Source contents", evidence="saved page text")])
        raw = json.dumps({"action": "reply", "text": "A sourced answer [N1].", "news_ids": [1]})
        model, _ = self.model(completion(raw))
        order = []
        app = dot.Assistant(self.config, self.store, model=model,
                            output=lambda x: order.append(("line", x)),
                            stream_output=lambda x: order.append(("chunk", x)))
        answer = app.chat("Explain the source")
        self.assertEqual("".join(v for k, v in order if k == "chunk"), "\nDot> A sourced answer [N1].")
        source_positions = [i for i, (_, v) in enumerate(order) if "Sources:" in v]
        self.assertEqual(len(source_positions), 1)
        self.assertTrue(all(i < source_positions[0] for i, (k, _) in enumerate(order) if k == "chunk"))
        self.assertEqual(self.store.history()[-1]["text"], answer)
        self.assertEqual(len(self.store.history()), 2)
        self.assertIsNotNone(self.store.article(1)["shown"])

    def test_missing_inline_citations_finish_stream_and_save_with_warning(self):
        self.store.ingest([dict(url="https://huggingface.co/blog/example", source="HF", title="Example",
                               published=dot.now(), body="Source contents", evidence="saved page text")])
        text = "Partially. The leaderboard compares objective metrics, not listener preference."
        raw = json.dumps({"action": "reply", "text": text, "news_ids": [1]})
        model, _ = self.model(completion(raw))
        reply = self.app(model).chat("Can we compare TTS models?")
        self.assertEqual("".join(self.chunks), "\nDot> " + text)
        self.assertEqual(sum("Citation format warning" in x for x in self.messages), 1)
        self.assertEqual(sum("Sources:" in x for x in self.messages), 1)
        self.assertFalse(any("not saved" in x for x in self.messages))
        self.assertEqual(self.store.history()[-1]["text"], reply)
        self.assertEqual(len(self.store.history()), 2)
        self.assertEqual(model.opener.open.call_count, 1)  # no retry/regeneration

    def test_unknown_inline_reference_still_rejects_stream(self):
        raw = json.dumps({"action": "reply", "text": "Unverified [N999]", "news_ids": []})
        model, _ = self.model(completion(raw), completion(raw))  # rejected after one correction round
        with self.assertRaises(dot.DotError):
            self.app(model).chat("Give a source")
        self.assertTrue(any("not saved" in x for x in self.messages))
        self.assertEqual(self.store.history(), [])
        self.assertFalse(any("Sources:" in x for x in self.messages))

    def test_invalid_citation_warns_and_is_not_persisted(self):
        raw = json.dumps({"action": "reply", "text": "Unverified [N999]", "news_ids": [999]})
        model, _ = self.model(completion(raw), completion(raw))  # rejected after one correction round
        with self.assertRaises(dot.DotError):
            self.app(model).chat("Give a source")
        self.assertIn("Unverified", "".join(self.chunks))
        self.assertTrue(any("not saved" in x for x in self.messages))
        self.assertEqual(self.store.history(), [])
        self.assertFalse(any("Sources:" in x for x in self.messages))

    def test_cut_reply_is_not_persisted_and_response_closes(self):
        raw = json.dumps({"action": "reply", "text": "Partial reply", "news_ids": []})
        model, responses = self.model(completion(raw, done=False))
        with self.assertRaises(dot.DotError):
            self.app(model).chat("Say something")
        self.assertIn("Partial", "".join(self.chunks))
        self.assertEqual(self.store.history(), [])
        self.assertTrue(responses[0].closed)
        self.assertEqual(model.opener.open.call_count, 1)

    def test_token_limit_discards_partial_reply(self):
        raw = json.dumps({"action": "reply", "text": "Unfinished", "news_ids": []})
        model, _ = self.model(completion(raw, finish="length"))
        with self.assertRaises(dot.DotError):
            self.app(model).chat("Say something")
        self.assertEqual(self.store.history(), [])
        self.assertTrue(any("not saved" in x for x in self.messages))

    def test_partial_tool_call_never_executes_or_prompts(self):
        raw = json.dumps({"action": "remember", "name": "test", "text": "Must not save", "kind": "preference"})
        model, _ = self.model(completion(raw, done=False))
        confirm = Mock(return_value=True)
        with self.assertRaises(dot.DotError):
            self.app(model, confirm).chat("Remember something")
        confirm.assert_not_called()
        self.assertEqual(self.chunks, [])
        self.assertNotIn("test", [r["name"] for r in self.store.notes()])

    def test_tool_json_hidden_and_decline_followup_streams(self):
        tool = json.dumps({"action": "remember", "name": "test", "text": "PROPOSED NOTE", "kind": "preference"})
        reply = json.dumps({"action": "reply", "text": "Not saved.", "news_ids": []})
        model, _ = self.model(completion(tool), completion(reply))
        confirm = Mock(return_value=False)
        answer = self.app(model, confirm).chat("Propose a preference")
        self.assertEqual(answer, "Not saved.")
        confirm.assert_called_once()
        self.assertNotIn("PROPOSED NOTE", "".join(self.chunks))
        self.assertIn("Not saved.", "".join(self.chunks))
        self.assertEqual(model.opener.open.call_count, 2)

    def test_cancel_closes_connection_and_saves_no_reply(self):
        raw = json.dumps({"action": "reply", "text": "A reply to cancel", "news_ids": []})
        model, responses = self.model(completion(raw))
        def cancel(chunk):
            if not chunk.startswith("\nDot>"):
                raise KeyboardInterrupt
        app = dot.Assistant(self.config, self.store, model=model, output=self.messages.append, stream_output=cancel)
        with self.assertRaises(KeyboardInterrupt):
            app.chat("Say something")
        self.assertTrue(responses[0].closed)
        self.assertEqual(self.store.history(), [])

    def test_cancel_returns_to_prompt_and_once_exits_130(self):
        args = ["--db", str(self.path)]
        with patch("builtins.input", side_effect=["hello", "/quit"]) as inputs, \
             patch.object(dot.Assistant, "chat", side_effect=KeyboardInterrupt), patch("dot.say"):
            self.assertEqual(dot.main(args), 0)
            self.assertEqual(inputs.call_count, 2)
        with patch.object(dot.Assistant, "chat", side_effect=KeyboardInterrupt), patch("dot.say"):
            self.assertEqual(dot.main(args + ["--once", "hello"]), 130)

    def test_broken_chunked_http_is_reported_without_saving(self):
        prefix = event({"choices": [{"delta": {"content": '{"action":"reply","text":"Partial'}}]})
        class Broken(io.BytesIO):
            def readline(self, size=-1):
                if self.tell() == len(prefix):
                    raise IncompleteRead(b"", 1)
                return super().readline(size)
        response = Broken(prefix)
        model = dot.Model(self.config)
        model.opener.open = Mock(return_value=response)
        with self.assertRaises(dot.DotError):
            self.app(model).chat("Say something")
        self.assertTrue(response.closed)
        self.assertEqual(self.store.history(), [])
        self.assertTrue(any("not saved" in x for x in self.messages))

    def test_lone_unicode_surrogate_rejects_final_reply(self):
        raw = json.dumps({"action": "reply", "text": "Before" + chr(0xD83D), "news_ids": []})
        model, _ = self.model(completion(raw))
        with self.assertRaises(dot.DotError):
            self.app(model).chat("Say something")
        self.assertEqual(self.store.history(), [])

    def test_changed_final_action_rejects_preview(self):
        preview = ReplyText(self.chunks.append)
        preview.feed('{"action":"reply","text":"Already shown"')
        with self.assertRaises(ValueError):
            preview.check({"action": "remember", "text": "Already shown"})
        with self.assertRaises(ValueError):
            preview.check({"action": "reply", "text": "Different answer"})


if __name__ == "__main__":
    unittest.main()
