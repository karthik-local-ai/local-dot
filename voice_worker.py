"""Owned offline Kokoro/audio subprocess. JSON-lines stdio, no port or microphone.

Only the optional voice CLI starts this. Ctrl+Alt+S is a single registered Windows
hotkey (no keyboard hook/logging). Audio exists in memory, not a WAV archive.
"""
from __future__ import annotations

import argparse
from collections import deque
import json
import os
from pathlib import Path
import sys
import threading
from time import perf_counter


class StopKey:
    """Register one hotkey on its own Windows message thread."""
    def __init__(self, callback):
        self.callback = callback
        self.ready = threading.Event()
        self.error = None
        self.thread_id = None
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        if os.name != 'nt':
            raise RuntimeError('The voice helper requires Windows.')
        self.thread.start()
        if not self.ready.wait(5) or self.error:
            raise RuntimeError(self.error or 'Stop-key registration timed out.')

    def run(self):
        import ctypes
        from ctypes import wintypes as w
        user = ctypes.WinDLL('user32', use_last_error=True)
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.GetCurrentThreadId.restype = w.DWORD
        user.RegisterHotKey.argtypes = [w.HWND, ctypes.c_int, w.UINT, w.UINT]
        user.RegisterHotKey.restype = w.BOOL
        user.UnregisterHotKey.argtypes = [w.HWND, ctypes.c_int]
        user.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
        user.GetMessageW.restype = ctypes.c_int
        self.thread_id = kernel.GetCurrentThreadId()
        if not user.RegisterHotKey(None, 1, 0x4000 | 0x0002 | 0x0001, ord('S')):
            self.error = 'Ctrl+Alt+S is unavailable (possibly reserved by another app). Voice will not start.'
            self.ready.set()
            return
        self.ready.set()
        try:
            message = w.MSG()
            while user.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == 0x0312 and message.wParam == 1:
                    self.callback()
        finally:
            user.UnregisterHotKey(None, 1)

    def post(self, message):
        if self.thread_id and self.thread.is_alive():
            import ctypes
            from ctypes import wintypes as w
            user = ctypes.WinDLL('user32', use_last_error=True)
            user.PostThreadMessageW.argtypes = [w.DWORD, w.UINT, w.WPARAM, w.LPARAM]
            user.PostThreadMessageW.restype = w.BOOL
            return bool(user.PostThreadMessageW(self.thread_id, message, 1, 0))
        return False

    def close(self):
        self.post(0x0012)  # WM_QUIT, our own message loop only.
        if self.thread.is_alive():
            self.thread.join(2)


class Engine:
    """One synthesis thread overlaps one playback thread; stop invalidates both queues."""
    def __init__(self, generate, player, emit, recorder=None):
        self.generate, self.player, self.emit = generate, player, emit
        self.recorder = recorder  # Optional take recorder: saves exactly what is played.
        self.cv = threading.Condition()
        self.text, self.audio = deque(), deque()
        self.turn, self.blocked, self.closed = 0, True, False
        self.threads = [threading.Thread(target=self.synthesize, daemon=True),
                        threading.Thread(target=self.play, daemon=True)]
        for thread in self.threads:
            thread.start()

    def current(self, turn):
        return not self.closed and not self.blocked and turn == self.turn

    def begin(self, turn):
        if type(turn) is not int or turn <= self.turn:
            raise ValueError('Voice turn must increase.')
        with self.cv:
            self.turn, self.blocked = turn, False
            self.text.clear()
            self.audio.clear()
            self.player.stop()
            self.cv.notify_all()

    def enqueue(self, turn, seq, text):
        if type(turn) is not int or type(seq) is not int or not isinstance(text, str) or not 1 <= len(text) <= 400:
            raise ValueError('Invalid bounded speech phrase.')
        with self.cv:
            if not self.current(turn):
                return
            if len(self.text) >= 32:
                raise ValueError('Voice text queue full.')
            self.text.append((turn, seq, text))
            self.cv.notify_all()

    def stop(self, reason='requested'):
        started = perf_counter()
        with self.cv:
            acquired = perf_counter()
            self.blocked = True
            self.text.clear()
            self.audio.clear()
            self.player.stop()  # Signal only our output callback, never a microphone or another process.
            self.cv.notify_all()
            self.emit({'event': 'stopped', 'turn': self.turn, 'reason': reason,
                       'lock_wait_seconds': acquired - started,
                       'signal_seconds': perf_counter() - acquired})

    def fail(self, exc):
        self.stop('error')
        self.emit({'event': 'error', 'error': str(exc)[:500]})
        with self.cv:
            self.closed = True
            self.cv.notify_all()

    def synthesize(self):
        try:
            while True:
                with self.cv:
                    self.cv.wait_for(lambda: self.closed or self.text)
                    if self.closed:
                        return
                    turn, seq, text = self.text.popleft()
                started = perf_counter()
                audio = self.generate(text)
                seconds = perf_counter() - started
                with self.cv:
                    self.cv.wait_for(lambda: not self.current(turn) or len(self.audio) < 4)
                    if self.current(turn):
                        self.audio.append((turn, seq, audio, seconds, text))
                        self.emit({'event': 'synthesized', 'turn': turn, 'seq': seq,
                                   'seconds': seconds, 'audio_seconds': len(audio) / 24000})
                        self.cv.notify_all()
        except Exception as exc:
            self.fail(exc)

    def play(self):
        try:
            while True:
                with self.cv:
                    self.cv.wait_for(lambda: self.closed or self.audio)
                    if self.closed:
                        return
                    turn, seq, audio, seconds, text = self.audio.popleft()
                    self.cv.notify_all()
                    if not self.current(turn):
                        continue
                    # Starting is nonblocking and atomic with stop/begin. Never let
                    # old audio start just AFTER a stop won a scheduling race.
                    self.player.start(audio)
                    self.emit({'event': 'play_started', 'turn': turn, 'seq': seq,
                               'synthesis_seconds': seconds, 'output_latency': self.player.latency})
                if self.recorder:
                    try:
                        self.recorder.phrase(turn, seq, text, audio)
                    except Exception as exc:  # Recording must never break speech.
                        self.emit({'event': 'record_error', 'error': str(exc)[:300]})
                underflow = self.player.wait()
                with self.cv:
                    self.player.finish()
                    if self.current(turn):
                        self.emit({'event': 'played', 'turn': turn, 'seq': seq,
                                   'underflow': bool(underflow), 'audio_status': str(underflow)})
                    else:
                        self.emit({'event': 'output_stopped', 'turn': turn, 'seq': seq})
                        if self.recorder:
                            self.recorder.note('stopped_early', turn=turn, seq=seq)
        except Exception as exc:
            self.fail(exc)
        finally:
            self.player.close()

    def close(self):
        self.stop('closed')
        with self.cv:
            self.closed = True
            self.cv.notify_all()
        for thread in self.threads:
            thread.join(5)


class TakeRecorder:
    """Saves each phrase Dot actually starts playing (24 kHz mono WAV) plus a timeline line.

    Only active with --record DIR (demo takes). Never records the microphone.
    """
    def __init__(self, folder):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.timeline = self.folder / 'timeline.jsonl'
        self.count = 0
        self.lock = threading.Lock()

    def note(self, event, **data):
        import time
        with self.lock, self.timeline.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({'t': round(time.time(), 3), 'who': 'dot', 'event': event, **data}, ensure_ascii=False) + '\n')

    def phrase(self, turn, seq, text, audio):
        import time
        import wave
        import numpy as np
        started = time.time()
        with self.lock:
            self.count += 1
            name = f'dot-{self.count:03d}.wav'
        samples = (np.clip(np.asarray(audio, dtype='float32'), -1, 1) * 32767).astype('<i2')
        with wave.open(str(self.folder / name), 'wb') as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(24000)
            handle.writeframes(samples.tobytes())
        self.note('spoke', turn=turn, seq=seq, text=text, file=name, seconds=round(len(samples) / 24000, 3), started=round(started, 3))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--silent-test', action='store_true', help='Output zeros; exercises output timing without audible speech.')
    parser.add_argument('--record', help='Demo takes only: save each played phrase as WAV plus timeline.jsonl in this folder.')
    args = parser.parse_args()
    protocol = sys.stdout
    sys.stdout = sys.stderr  # Library diagnostics must never corrupt the JSON pipe.
    lock = threading.Lock()

    def emit(event):
        with lock:
            protocol.write(json.dumps(event, ensure_ascii=True) + '\n')
            protocol.flush()

    engine = hotkey = None
    try:
        root = Path(__file__).resolve().parent
        os.environ.update(HF_HOME=str(root / '.local' / 'kokoro-cache'), HF_HUB_OFFLINE='1',
                          HF_HUB_DISABLE_IMPLICIT_TOKEN='1', HF_HUB_DISABLE_TELEMETRY='1',
                          TOKENIZERS_PARALLELISM='false')
        import numpy as np
        import sounddevice as sd
        import spacy.util
        import torch
        from kokoro import KPipeline
        if not spacy.util.is_package('en_core_web_sm'):
            raise RuntimeError('Missing en_core_web_sm. No automatic download in voice mode.')
        if not torch.cuda.is_available():
            raise RuntimeError('CUDA unavailable. No CPU fallback in voice mode.')
        host = next(h for h in sd.query_hostapis() if h['name'] == 'Windows WASAPI')
        output_index = host['default_output_device']
        if output_index < 0:
            raise RuntimeError('No Windows default WASAPI output device.')
        output = sd.query_devices(output_index)
        extra = sd.WasapiSettings(auto_convert=True)  # Shared mode; OS resamples 24 kHz, no device-setting write.
        sd.check_output_settings(device=output_index, samplerate=24000, channels=1,
                                 dtype='float32', extra_settings=extra)
        torch.set_num_threads(4)
        torch.set_num_interop_threads(1)
        pipe = KPipeline(lang_code='a', repo_id='hexgrad/Kokoro-82M', device='cuda')
        pipe.load_voice('af_heart')
        if str(pipe.model.device) != 'cuda:0':
            raise RuntimeError('Kokoro is not on cuda:0. No CPU fallback.')
        if pipe.g2p.fallback is None:
            raise RuntimeError('Pronunciation fallback unavailable.')

        def generate(text):
            with torch.inference_mode():
                chunks = [audio.detach().cpu().numpy().copy() for _, _, audio in
                          pipe(text, voice='af_heart', speed=1, split_pattern=r'\n+')]
            if not chunks:
                raise RuntimeError('No voice audio generated.')
            audio = np.concatenate(chunks)
            if not audio.size or not np.isfinite(audio).all():
                raise RuntimeError('Invalid voice audio.')
            return audio

        generate('Hello. This is a short voice warm up.')  # Not played.

        class Player:
            stream = None
            latency = None
            ole = None
            cancel = None

            def start(self, audio):
                if self.ole is None:
                    # WASAPI stream creation on a Python thread needs COM initialized
                    # on THAT thread. Registering it only on main is insufficient.
                    import ctypes
                    ole = ctypes.WinDLL('ole32')
                    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
                    ole.CoInitializeEx.restype = ctypes.c_long
                    if ole.CoInitializeEx(None, 0) < 0:
                        raise RuntimeError('Could not initialize audio-thread COM.')
                    ole.CoUninitialize.argtypes = []
                    ole.CoUninitialize.restype = None
                    self.ole = ole
                self.cancel = cancel = threading.Event()
                self.done = threading.Event()
                self.flags = flags = sd.CallbackFlags()
                self.duration = len(audio) / 24000
                position = 0

                def callback(outdata, frames, timing, status):
                    nonlocal position
                    flags.__ior__(status)
                    outdata.fill(0)
                    if cancel.is_set():
                        raise sd.CallbackAbort  # Silence next block; do not wait in Pa_AbortStream.
                    count = min(frames, len(audio) - position)
                    if not args.silent_test:
                        outdata[:count, 0] = audio[position:position + count]
                    position += count
                    if position >= len(audio):
                        raise sd.CallbackStop

                self.stream = sd.OutputStream(samplerate=24000, channels=1, dtype='float32',
                    callback=callback, finished_callback=self.done.set, latency='low',
                    device=output_index, extra_settings=extra)
                self.stream.start()
                self.latency = self.stream.latency

            def wait(self):
                if not self.done.wait(self.duration + 3):
                    raise RuntimeError('Audio output callback timed out.')
                return self.flags

            def finish(self):
                self.stream.close(ignore_errors=True)
                self.stream = None

            def stop(self):
                if self.cancel is not None:
                    self.cancel.set()

            def close(self):
                if self.stream is not None:
                    self.stream.abort(ignore_errors=True)
                    self.stream.close(ignore_errors=True)
                    self.stream = None
                if self.ole is not None:
                    self.ole.CoUninitialize()  # Same playback thread, paired with initialization.
                    self.ole = None

        engine = Engine(generate, Player(), emit, TakeRecorder(args.record) if args.record else None)
        hotkey = StopKey(lambda: engine.stop('hotkey'))
        hotkey.start()
        emit({'event': 'ready', 'device': 'cuda:0', 'voice': 'af_heart',
              'output': output['name'], 'audio_api': 'Windows WASAPI (shared)',
              'stop_key': 'Ctrl+Alt+S', 'silent_test': args.silent_test})
        while True:
            line = sys.stdin.readline(65537)
            if not line:
                break
            if len(line) > 65536:
                raise ValueError('Voice command too large.')
            command = json.loads(line)
            action = command.get('action')
            if action == 'begin':
                engine.begin(command.get('turn'))
            elif action == 'phrase':
                engine.enqueue(command.get('turn'), command.get('seq'), command.get('text'))
            elif action == 'stop':
                engine.stop()
            elif action == 'close':
                break
            else:
                raise ValueError('Unknown voice command.')
    except Exception as exc:
        emit({'event': 'error', 'error': str(exc)[:500]})
    finally:
        if hotkey:
            hotkey.close()
        if engine:
            engine.close()


if __name__ == '__main__':
    main()
