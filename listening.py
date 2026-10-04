"""Small pipe client for the existing installed local dictation engine."""
import json
from pathlib import Path
import subprocess
import threading

ROOT = Path(__file__).resolve().parent


def _dictation_config():
    """Optional external dictation engine (not included). Set dictation_python and dictation_root in config.json."""
    try:
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        config = {}
    return Path(config.get("dictation_python") or "missing-python"), config.get("dictation_root") or ""


PYTHON, STT_ROOT = _dictation_config()


class Listening:
    def __init__(self, callback):
        self.callback = callback
        self.process = None
        self.lock = threading.Lock()
        self.log = None
        self.reader = None

    def start(self):
        if not PYTHON.is_file():
            raise OSError("Dictation is not configured (dictation_python in config.json). Typing still works.")
        try:
            check = subprocess.run(["powershell.exe", "-NoProfile", "-Command",
                r"@(Get-CimInstance Win32_Process | Where-Object { $_.Name -match '^python(w)?\.exe$' -and $_.CommandLine -match '-m realtime_stt(?:\s|$)' }).Count"],
                capture_output=True, text=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as exc:
            raise OSError("Dictation conflict check timed out; text input remains available.") from exc
        if check.returncode or not check.stdout.strip().isdigit():
            raise OSError("Could not check for a conflicting dictation app. Close it before retrying.")
        if int(check.stdout.strip()):
            raise OSError("Exit Local Dictation first: it already owns Right Ctrl. Then reopen Dot Pet.")
        (ROOT / ".local").mkdir(exist_ok=True)
        self.log = (ROOT / ".local" / "pet-listening.log").open("a", encoding="utf-8")
        try:
            self.process = subprocess.Popen([str(PYTHON), "-u", str(ROOT / "listen_worker.py"), STT_ROOT],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            self.log.close()
            raise
        self.reader = threading.Thread(target=self.read, name="dot-listening-events", daemon=True)
        self.reader.start()

    def read(self):
        try:
            for line in self.process.stdout:
                self.callback(json.loads(line))
        except (OSError, ValueError) as exc:
            self.callback({"event": "error", "detail": str(exc)})
        finally:
            self.callback({"event": "closed", "detail": "Listening helper stopped"})

    def send(self, command):
        with self.lock:
            if not self.process or self.process.poll() is not None:
                raise OSError("Listening helper is not running")
            self.process.stdin.write(json.dumps({"command": command}) + "\n")
            self.process.stdin.flush()

    def close(self):
        process = self.process
        if process:
            try:
                if process.poll() is None:
                    self.send("quit")
                    process.wait(timeout=15)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()  # Only our owned helper, never the standalone app.
                process.wait(timeout=5)
            if self.reader:
                self.reader.join(timeout=2)
            for pipe in (process.stdin, process.stdout):
                if pipe:
                    pipe.close()
        if self.log:
            self.log.close()
