"""Optional phrase speech for reply.text only; no model/server or microphone control."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import threading
from time import perf_counter

ROOT = Path(__file__).resolve().parent


def spoken_text(text):
    """Conservative speech rendering; terminal text/citations remain unchanged."""
    text = re.sub(r'`([^`]*)`', lambda m: m[1] if re.fullmatch(r'[\w -]{1,60}', m[1])
                  else 'code shown on screen', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'\[N\d+\]', '', text)
    text = re.sub(r'https?://\S+|www\.\S+', '', text)
    text = re.sub(r'(?im)^\s*(?:#{1,6}\s*|[-*>]\s+|\d+[.)]\s+)', '', text)
    text = text.replace('*', '').replace('_', ' ')
    text = re.sub(r'[\x00-\x1f\x7f]', ' ', text)
    return ' '.join(text.split()).strip()


class Phrases:
    def __init__(self):
        self.raw, self.carry, self.code = '', '', False

    def feed(self, text, final=False):
        self.raw += text
        result = []
        while self.raw:
            # Fences are handled before sentence boundaries, including split ``` tokens.
            if self.code or self.raw.lstrip().startswith('```'):
                end = self.raw.find('\n')
                if end < 0:
                    if final:
                        self.raw = ''
                    break
                line, self.raw = self.raw[:end], self.raw[end + 1:]
                if line.lstrip().startswith('```'):
                    self.code = not self.code
                continue
            ends = [m.end() for m in re.finditer(r'[.!?]["\u201d\u2019\')*_]*\s+|\n', self.raw)]
            if len(self.raw) > 240:
                cut = self.raw.rfind(' ', 80, 220)
                if cut > 0:
                    ends.append(cut)
            if final:
                ends.append(len(self.raw))
            end = 0
            for possible in sorted(set(ends)):
                candidate = self.raw[:possible]
                if (candidate.count('`') % 2 == 0 and candidate.count('[') == candidate.count(']')
                        and candidate.count('(') == candidate.count(')')):
                    end = possible
                    break
            if not end:
                if final or len(self.raw) > 2000:
                    self.raw = ''  # Unrenderable markup stays on screen only.
                break
            candidate, self.raw = self.raw[:end], self.raw[end:]
            if '|' in candidate:  # Tables/code-like rows are not good speech.
                continue
            cleaned = spoken_text(candidate)
            if cleaned:
                self.carry = (self.carry + ' ' + cleaned).strip()
            if len(self.carry) >= 40:
                result.append(self.carry)
                self.carry = ''
        if final and self.carry:
            result.append(self.carry)
            self.carry = ''
        return result


class Speech:
    """One owned voice process. A stopped reply stays muted until the next turn."""
    def __init__(self, *, silent_test=False, event_callback=None, log_dir=None, record_dir=None):
        self.process = None
        self.reader = None
        self.log = None
        self.lock = threading.RLock()
        self.ready_event, self.idle = threading.Event(), threading.Event()
        self.idle.set()
        self.enabled, self.active, self.closed = True, False, False
        self.state, self.error, self.warning = 'closed', '', ''
        self.turn, self.seq = 0, 0
        self.pending = set()
        self.events = []
        self.timing = {}
        self.meta = {}
        self.phrases = Phrases()
        self.characters = 0
        self.truncated = False
        self.silent_test, self.event_callback = silent_test, event_callback
        self.log_dir = Path(log_dir) if log_dir else ROOT / '.local'
        self.record_dir = Path(record_dir) if record_dir else None  # Demo takes only.

    def start(self, timeout=65):
        if os.name != 'nt':
            raise ValueError('Use the Windows voice launcher for playback and the global stop key.')
        python = ROOT / '.local' / 'kokoro-venv' / 'Scripts' / 'python.exe'
        if not python.is_file():
            raise ValueError('The isolated Kokoro environment is missing. No automatic install.')
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.log = (self.log_dir / 'voice-errors.log').open('w', encoding='utf-8')
        command = [str(python), '-u', str(ROOT / 'voice_worker.py')]
        if self.silent_test:
            command.append('--silent-test')
        if self.record_dir:
            command += ['--record', str(self.record_dir)]
        try:
            self.state = 'warming'
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=self.log, encoding='utf-8', bufsize=1,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.reader = threading.Thread(target=self.read_events, daemon=True)
            self.reader.start()
            if not self.ready_event.wait(timeout) or self.state != 'ready':
                raise ValueError(self.error or 'Voice warm-up timed out.')
        except BaseException:
            self.close()
            raise
        return self.status()

    def read_events(self):
        try:
            for line in self.process.stdout:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError('Voice event must be an object.')
                with self.lock:
                    kind = event.get('event')
                    if kind == 'ready':
                        self.meta, self.state = event, 'ready'
                        self.ready_event.set()
                    elif kind == 'error':
                        self.error, self.state = event.get('error', 'Voice failed.'), 'failed'
                        self.active = False
                        self.pending.clear()
                        self.idle.set()
                        self.ready_event.set()
                    elif event.get('turn') == self.turn:
                        elapsed = perf_counter() - self.started if hasattr(self, 'started') else None
                        event['since_turn_seconds'] = elapsed
                        if kind == 'play_started' and 'first_playback_requested_seconds' not in self.timing:
                            self.timing['first_playback_requested_seconds'] = elapsed
                        if kind == 'stopped':
                            self.active = False
                            self.pending.clear()
                        elif kind == 'played':
                            self.pending.discard(event['seq'])
                            if event.get('underflow'):
                                self.warning = event.get('audio_status', 'Audio callback warning.')
                        if not self.pending:
                            self.idle.set()
                    self.events.append(event)
                    self.events = self.events[-120:]
                if self.event_callback:
                    self.event_callback(event)
                if kind == 'error':
                    self.process.terminate()
                    break
        except (OSError, ValueError, KeyError) as exc:
            self.error = 'Voice event pipe failed: ' + str(exc)
        finally:
            with self.lock:
                if not self.closed and self.state != 'failed':
                    self.state = 'failed'
                    self.error = self.error or 'Voice helper exited.'
                self.active = False
                self.pending.clear()
                self.idle.set()
                self.ready_event.set()

    def send(self, command):
        # Called under lock; bounded text keeps writes small. No UI lock dependency.
        try:
            if self.process is None or self.process.poll() is not None:
                raise OSError('Voice helper is not running.')
            self.process.stdin.write(json.dumps(command, ensure_ascii=True) + '\n')
            self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            self.state, self.error, self.active = 'failed', str(exc), False
            self.pending.clear()
            self.idle.set()

    def begin(self):
        with self.lock:
            self.turn += 1
            self.seq, self.characters, self.truncated = 0, 0, False
            self.phrases = Phrases()
            self.pending.clear()
            self.idle.set()
            self.started, self.timing = perf_counter(), {}
            self.active = self.enabled and self.state == 'ready'
            if self.state == 'ready':
                self.send({'action': 'begin', 'turn': self.turn})

    def feed(self, text, final=False):
        with self.lock:
            if not self.active:
                return
            if text and 'first_text_seconds' not in self.timing:
                self.timing['first_text_seconds'] = perf_counter() - self.started
            for phrase in self.phrases.feed(text, final):
                if self.seq >= 32 or self.characters + len(phrase) > 4000 or len(phrase) > 400:
                    self.truncated = True
                    self.active = False
                    break
                self.seq += 1
                self.characters += len(phrase)
                self.pending.add(self.seq)
                self.idle.clear()
                if self.seq == 1:
                    self.timing['first_phrase_sent_seconds'] = perf_counter() - self.started
                self.send({'action': 'phrase', 'turn': self.turn, 'seq': self.seq, 'text': phrase})

    def finish(self):
        with self.lock:
            self.feed('', final=True)
            self.active = False

    def stop(self):
        with self.lock:
            self.active = False
            self.phrases = Phrases()
            self.pending.clear()
            self.idle.set()
            if self.state == 'ready':
                self.send({'action': 'stop'})

    def set_enabled(self, enabled):
        self.stop()
        self.enabled = bool(enabled)

    def wait_idle(self, timeout=90):
        return self.idle.wait(timeout)

    def status(self):
        with self.lock:
            return {'state': self.state, 'enabled': self.enabled and self.state == 'ready',
                    'accepting_reply_text': self.active,
                    'pending_phrases': len(self.pending), 'output': self.meta.get('output'),
                    'stop_key': self.meta.get('stop_key'), 'error': self.error, 'audio_warning': self.warning,
                    'audio_api': self.meta.get('audio_api'),
                    'last_synthesis': next((dict(e) for e in reversed(self.events) if e.get('event') == 'synthesized'), None),
                    'last_output_completion': next((dict(e) for e in reversed(self.events) if e.get('event') == 'played'), None),
                    'speech_excerpt_limited': self.truncated, 'timing': dict(self.timing),
                    'limit': 'Output only; provisional reply phrases. Timings are playback requests, not audible headset onset.'}

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.stop()
            self.closed = True
            if self.process and self.process.poll() is None:
                self.send({'action': 'close'})
        if self.process:
            try:
                self.process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=5)
            if self.reader:
                self.reader.join(2)
            for stream in (self.process.stdin, self.process.stdout):
                if stream:
                    stream.close()
        if self.log:
            self.log.close()
        self.state = 'closed'
