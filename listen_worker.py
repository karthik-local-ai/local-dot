"""Owned local ASR helper. Reuses installed dictation code, never its settings writer.
JSON stdin/stdout; no clipboard, Notes, cloud provider or automatic submission.
"""
import json
import os
from pathlib import Path
import sys
import threading

STT_ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else ".")  # dictation_root from config.json
LOCK = threading.Lock()


def emit(event, **data):
    with LOCK:
        print(json.dumps({"event": event, **data}, ensure_ascii=False), flush=True)


def main():
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    sys.path.insert(0, str(STT_ROOT))
    from huggingface_hub import snapshot_download
    from realtime_stt.audio import MicrophoneCapture
    from realtime_stt.pipeline import TranscriptionPipeline
    from realtime_stt.stt.faster_whisper_local import FasterWhisperStreamingSTT
    from realtime_stt.hotkey import GlobalHoldHotkey

    saved = json.loads((STT_ROOT / "settings.json").read_text(encoding="utf-8"))
    # Resolve locally before constructing a provider; missing cache is a visible error.
    model = snapshot_download("Systran/faster-whisper-small", local_files_only=True,
                              allow_patterns=["model.bin", "config.json", "tokenizer.json", "vocabulary.*"])
    stt = FasterWhisperStreamingSTT(model_name=model, device="cuda", compute_type="float16",
        speech_rms_threshold=int(saved.get("speech_rms_threshold", 650)),
        final_silence_ms=int(saved.get("final_silence_ms", 700)),
        vocab_path=STT_ROOT / "vocabulary.txt")
    microphone = MicrophoneCapture(sample_rate=16000, chunk_ms=50, device=saved.get("microphone") or None)
    pipeline = TranscriptionPipeline(microphone, stt, release_microphone_when_idle=True)
    hotkey = GlobalHoldHotkey("ctrl_r", lambda hwnd: emit("key"), lambda: None,
                              lambda: emit("escape"))
    ready = threading.Event()
    def state(name, detail):
        if name == "connected":
            ready.set()
            emit("ready", device=stt.device, microphone=microphone.current_device_name())
        elif name in {"capture_finalized", "capture_cancelled"}:
            hotkey.set_cancel_enabled(False)
            emit(name)
        elif name in {"error", "closed", "loading"}:
            emit(name, detail=detail)
    try:
        pipeline.start(lambda event: emit("transcript", text=event.text, final=event.is_final), state)
        if "--no-hotkey" not in sys.argv:
            hotkey.start()
        for line in sys.stdin:
            command = json.loads(line).get("command")
            if command == "quit":
                break
            try:
                if command == "begin":
                    if not ready.is_set():
                        raise RuntimeError("Local transcription is not ready.")
                    if pipeline.begin_capture():
                        hotkey.set_cancel_enabled(True)
                        emit("listening")
                elif command == "end":
                    if pipeline.end_capture():
                        emit("finalizing")
                elif command == "cancel":
                    if not pipeline.cancel_capture():
                        stt.cancel()
                else:
                    raise ValueError("Unknown listening command")
            except Exception as exc:
                emit("error", detail=str(exc))
    finally:
        hotkey.stop()
        pipeline.stop()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        main()
    except Exception as exc:
        emit("error", detail=str(exc))
        raise SystemExit(1)
