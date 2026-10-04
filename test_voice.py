"""Voice plumbing checks; no model, microphone or audible output. Native hotkey test is scoped."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import dot
from speech import Phrases, Speech, spoken_text
from voice_worker import Engine, StopKey


def eventually(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(.01)
    return False


class PhraseTests(unittest.TestCase):
    def render(self, text):
        phrases = Phrases()
        chunks = []
        for char in text:
            chunks += phrases.feed(char)
        chunks += phrases.feed('', final=True)
        return chunks

    def test_incremental_sentences_not_words(self):
        text = 'This is a complete useful opening sentence. Here is the second useful sentence.'
        self.assertEqual(self.render(text), [text.split('. ')[0]+'.', text.split('. ')[1]])

    def test_citations_links_urls_and_inline_code(self):
        result = ' '.join(self.render('Read [the source](https://example.com/a) [N12]. Do not pronounce `code.with.dots()` or https://example.com/b.'))
        self.assertIn('the source', result)
        for forbidden in ('https', 'N12', 'code.with', '.com'):
            self.assertNotIn(forbidden, result)

    def test_fenced_code_and_table_stay_on_screen(self):
        result = ' '.join(self.render('Here is the explanation.\n```python\nSECRET = 1.0\nprint(SECRET)\n```\n| hidden | table |\nThis is the remaining plain language explanation.'))
        self.assertNotIn('SECRET', result)
        self.assertNotIn('hidden', result)
        self.assertIn('remaining plain', result)

    def test_inline_code_with_sentence_boundary_does_not_stall(self):
        result = ' '.join(self.render('The example `print("Hi. There.")` is only on screen. The spoken explanation continues normally.'))
        self.assertNotIn('print', result)
        self.assertIn('spoken explanation continues', result)

    def test_long_text_is_bounded(self):
        chunks = self.render('long words with no punctuation ' * 40)
        self.assertTrue(chunks)
        self.assertTrue(all(len(c) <= 300 for c in chunks))

    def test_inline_word_formatting_must_not_remove_negation(self):
        self.assertEqual(spoken_text('This is `not` a passed test.'), 'This is not a passed test.')

    def test_markdown_and_controls(self):
        self.assertEqual(spoken_text('## **Hello** [N1]\x1b\n'), 'Hello')


class ClientTests(unittest.TestCase):
    def client(self):
        client = Speech()
        client.state = 'ready'
        sent = []
        client.send = sent.append
        return client, sent

    def test_stop_mutes_rest_of_reply_not_next_turn(self):
        client, sent = self.client()
        client.begin()
        client.feed('This first complete sentence is long enough to speak. ')
        client.stop()
        count = len(sent)
        client.feed('The rest of this reply must never restart speech. ', final=True)
        self.assertEqual(len(sent), count)
        client.begin()
        client.feed('A newly requested answer may speak normally again. ', final=True)
        self.assertEqual(sent[-1]['action'], 'phrase')
        self.assertEqual(sent[-1]['turn'], 2)

    def test_disabled_voice_stays_silent_but_begin_cancels_old_work(self):
        client, sent = self.client()
        client.set_enabled(False)
        client.begin()
        client.feed('This complete sentence should stay entirely on screen.', final=True)
        self.assertFalse(any(c['action'] == 'phrase' for c in sent))

    def test_voice_excerpt_limit_is_visible_and_bounded(self):
        client, sent = self.client()
        client.begin()
        for _ in range(100):
            client.feed('This is a fairly long sentence that can be read naturally. ')
        self.assertTrue(client.truncated)
        self.assertLessEqual(sum(c['action'] == 'phrase' for c in sent), 32)

    def test_ready_is_not_evidence_of_completed_playback(self):
        client, sent = self.client()
        client.begin()
        self.assertIsNone(client.status()['last_synthesis'])
        self.assertIsNone(client.status()['last_output_completion'])
        client.events.append({'event': 'played', 'turn': 1, 'seq': 1})
        self.assertEqual(client.status()['last_output_completion']['seq'], 1)

    def test_finish_refuses_late_text(self):
        client, sent = self.client()
        client.begin()
        client.feed('A short ending.')
        client.finish()
        count = len(sent)
        client.feed('Late text must not enter an accepted finished reply.', final=True)
        self.assertEqual(len(sent), count)

    def test_pipe_failure_does_not_break_text_caller(self):
        client = Speech()
        client.state = 'ready'
        client.begin()
        self.assertEqual(client.state, 'failed')
        self.assertTrue(client.error)
        self.assertTrue(client.wait_idle(.01))


class FakePlayer:
    latency = .02
    def __init__(self):
        self.started = []
        self.done = threading.Event()
        self.stops = 0
    def start(self, audio):
        self.done.clear()
        self.started.append(audio)
    def wait(self):
        self.done.wait(3)
    def stop(self):
        self.stops += 1
        self.done.set()
    def finish(self):
        pass
    def close(self):
        pass


class TakeRecordingTests(unittest.TestCase):
    def test_played_phrases_are_saved_with_timeline_and_exported(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed in this interpreter")
        import json, tempfile, wave
        from voice_worker import TakeRecorder
        import take_export
        class AutoPlayer(FakePlayer):
            def wait(self):
                time.sleep(.05)
        with tempfile.TemporaryDirectory() as folder:
            player = AutoPlayer()
            engine = Engine(lambda text: np.full(2400, .25, dtype="float32"), player, lambda e: None, TakeRecorder(folder))
            try:
                engine.begin(1)
                engine.enqueue(1, 1, "First sentence.")
                engine.enqueue(1, 2, "Second sentence.")
                self.assertTrue(eventually(lambda: len(player.started) == 2))
                time.sleep(.2)
            finally:
                engine.close()
            lines = [json.loads(line) for line in open(folder + "/timeline.jsonl", encoding="utf-8")]
            spoken = [line for line in lines if line["event"] == "spoke"]
            self.assertEqual([line["text"] for line in spoken], ["First sentence.", "Second sentence."])
            with wave.open(folder + "/dot-001.wav") as clip:
                self.assertEqual((clip.getframerate(), clip.getnchannels(), clip.getnframes()), (24000, 1, 2400))
            audio, transcript, phrases, seconds = take_export.export(folder)
            self.assertEqual(phrases, 2)
            self.assertIn("Second sentence.", transcript.read_text(encoding="utf-8"))
            with wave.open(str(audio)) as full:
                self.assertGreaterEqual(full.getnframes(), 4800)

    def test_no_recorder_means_nothing_written(self):
        import os, tempfile
        with tempfile.TemporaryDirectory() as folder:
            engine = Engine(lambda text: [text], FakePlayer(), lambda e: None)
            try:
                engine.begin(1)
            finally:
                engine.close()
            self.assertEqual(os.listdir(folder), [])


class EngineTests(unittest.TestCase):
    def test_synthesis_overlaps_playback_and_stop_discards_queue(self):
        generated, events = [], []
        player = FakePlayer()
        def generate(text):
            generated.append(text)
            return [text]
        engine = Engine(generate, player, events.append)
        try:
            engine.begin(1)
            for n in range(3):
                engine.enqueue(1, n, str(n))
            self.assertTrue(eventually(lambda: len(player.started) == 1 and len(generated) == 3))
            engine.stop()
            self.assertTrue(eventually(lambda: player.done.is_set()))
            time.sleep(.05)
            self.assertEqual(len(player.started), 1)
            self.assertFalse(engine.audio)
            engine.enqueue(1, 9, 'must stay silent')
            time.sleep(.05)
            self.assertEqual(len(generated), 3)
        finally:
            engine.close()
        self.assertTrue(all(not t.is_alive() for t in engine.threads))

    def test_stop_during_generation_cannot_leak_old_audio_into_new_turn(self):
        release = threading.Event()
        entered = threading.Event()
        player = FakePlayer()
        def generate(text):
            if text == 'old':
                entered.set()
                release.wait(3)
            return [text]
        engine = Engine(generate, player, lambda e: None)
        try:
            engine.begin(1)
            engine.enqueue(1, 1, 'old')
            self.assertTrue(entered.wait(1))
            engine.stop()
            engine.begin(2)
            engine.enqueue(2, 1, 'new')
            release.set()
            self.assertTrue(eventually(lambda: bool(player.started)))
            self.assertEqual(player.started, [['new']])
        finally:
            release.set()
            engine.close()

    def test_engine_error_is_reported(self):
        events = []
        def broken(text):
            raise RuntimeError('fixture CUDA error')
        engine = Engine(broken, FakePlayer(), events.append)
        try:
            engine.begin(1)
            engine.enqueue(1, 1, 'hello')
            self.assertTrue(eventually(lambda: any(e['event'] == 'error' for e in events)))
        finally:
            engine.close()

    def test_invalid_or_stale_commands(self):
        engine = Engine(lambda s: [s], FakePlayer(), lambda e: None)
        try:
            with self.assertRaises(ValueError): engine.begin(True)
            engine.begin(1)
            with self.assertRaises(ValueError): engine.begin(1)
            with self.assertRaises(ValueError): engine.enqueue(1, 1, 'x'*401)
            engine.enqueue(0, 1, 'stale')
            self.assertFalse(engine.text)
        finally:
            engine.close()

    @unittest.skipUnless(os.name == 'nt', 'Native Windows hotkey registration')
    def test_stop_hotkey_message_loop_and_conflict(self):
        received = threading.Event()
        first, second = StopKey(received.set), StopKey(lambda: None)
        try:
            try: first.start()
            except RuntimeError as exc: self.skipTest(str(exc))
            with self.assertRaisesRegex(RuntimeError, 'unavailable'): second.start()
            self.assertTrue(first.post(0x0312))  # Our own WM_HOTKEY, no OS key injection.
            self.assertTrue(received.wait(1))
        finally:
            second.close()
            first.close()
        self.assertFalse(first.thread.is_alive())


class ChatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = dot.Store(Path(self.temp.name)/'qa.sqlite3')
        self.config = json.loads((dot.ROOT/'config.json').read_text())
    def tearDown(self):
        self.store.db.close()
        self.temp.cleanup()

    def assistant(self, model):
        voice = Speech()
        voice.state = 'ready'
        commands = []
        voice.send = commands.append
        return dot.Assistant(self.config, self.store, model=model, output=lambda _: None,
                             stream_output=lambda _: None, speech=voice), voice, commands

    def test_speech_begins_before_reply_completion_but_finish_waits_for_validation(self):
        class Model:
            def complete(inner, messages, on_text):
                self.assertIn('spoken output', messages[0]['content'])
                text = 'A complete useful first sentence can now be spoken. '
                on_text(text)
                self.assertTrue(any(c['action'] == 'phrase' for c in commands))
                self.assertEqual(self.store.history(), [])
                return {'action':'reply', 'text':text+'A short ending.', 'news_ids':[]}
        assistant, voice, commands = self.assistant(Model())
        answer = assistant.chat('hello')
        self.assertTrue(answer.endswith('A short ending.'))
        self.assertEqual(len(self.store.history()), 2)
        self.assertEqual(commands[-1]['text'], 'A short ending.')

    def test_unknown_citation_rejects_and_stops_previously_streamed_speech(self):
        class Model:
            def complete(inner, messages, on_text):
                text = 'This entire spoken preview has not passed final validation. '
                on_text(text)
                return {'action':'reply', 'text':text+'[N999]', 'news_ids':[999]}
        assistant, voice, commands = self.assistant(Model())
        with self.assertRaises(dot.DotError): assistant.chat('hello')
        self.assertEqual(commands[-1]['action'], 'stop')
        self.assertFalse(voice.active)
        self.assertEqual(self.store.history(), [])

    def test_launch_flag_at_chat_prompt_never_calls_qwen(self):
        with patch.object(dot.Model, 'complete', side_effect=AssertionError('No model call')), patch('dot.say') as output:
            assistant = dot.Assistant(self.config, self.store)
            self.assertTrue(dot.dispatch('--voice', assistant))
            self.assertIn('launch option, not chat', output.call_args[0][0])
            self.assertEqual(self.store.history(), [])

    def test_missing_voice_points_to_unambiguous_launcher(self):
        assistant = dot.Assistant(self.config, self.store)
        with self.assertRaisesRegex(dot.DotError, 'DotVoice.cmd from Windows'):
            dot.dispatch('/voice status', assistant)

    def main_output(self, voice=False, fail=False):
        lines = []
        with patch('dot.terminal') as terminal, patch('dot.say', side_effect=lines.append), patch('speech.Speech') as factory:
            terminal.return_value.__enter__.return_value = (lambda _: '/quit', False)
            helper = factory.return_value
            helper.start.return_value = {'output': 'fixture', 'audio_api': 'fixture'}
            helper.status.return_value = {'enabled': True}
            if fail:
                helper.start.side_effect = ValueError('fixture startup failure')
            args = ['--db', str(self.store.path)] + (['--voice'] if voice else [])
            self.assertEqual(dot.main(args), 0)
            if not voice:
                factory.assert_not_called()
        return '\n'.join(lines)

    def test_text_only_banner_explicitly_says_voice_off(self):
        text = self.main_output()
        self.assertIn('Voice: OFF (text-only mode)', text)
        self.assertNotIn('Voice ready:', text)

    def test_voice_banner_requires_successful_start(self):
        text = self.main_output(voice=True)
        self.assertIn('Voice ready:', text)
        self.assertIn('Voice: ON', text)

    def test_failed_start_banner_does_not_imply_voice_available(self):
        text = self.main_output(voice=True, fail=True)
        self.assertIn('fixture startup failure', text)
        self.assertIn('Voice: OFF (text-only mode)', text)
        self.assertNotIn('Voice ready:', text)

    def test_worker_cannot_use_passed_speech(self):
        assistant = dot.Assistant(self.config, self.store, worker=True, speech=Speech())
        self.assertIsNone(assistant.speech)

    def test_interrupt_stops_audio(self):
        class Model:
            def complete(inner, messages, on_text): raise KeyboardInterrupt()
        assistant, voice, commands = self.assistant(Model())
        with self.assertRaises(KeyboardInterrupt): assistant.chat('hello')
        self.assertEqual(commands[-1]['action'], 'stop')


if __name__ == '__main__':
    unittest.main()
