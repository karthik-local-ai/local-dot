"""Fixed local capabilities: an owned browser child and owner-selected text files."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
import queue
import re
import subprocess
import threading

ROOT = Path(__file__).resolve().parent


def windows_path(path):
    value = str(path)
    if value.startswith("/mnt/") and len(value) > 7 and value[6] == "/":
        return value[5].upper() + ":/" + value[7:]
    return value


class Browser:
    def __init__(self, headless=False, profile=None, foreground=True):
        self.headless, self.profile, self.foreground = headless, profile, foreground
        self.process = None
        self.responses = queue.Queue()
        self.lock = threading.RLock()
        self.sequence = 0

    def _start(self):
        python = Path(sys.executable) if os.name == "nt" else Path(os.environ.get("LOCAL_DOT_WINDOWS_PYTHON", ""))
        if not python.is_file():
            raise ValueError("The browser needs Windows Python. Run Local Dot with Windows Python, or set LOCAL_DOT_WINDOWS_PYTHON under WSL.")
        command = [str(python), windows_path(ROOT / "shared_browser.py")]
        if self.headless:
            command.append("--headless")
        if self.profile:
            command += ["--profile", windows_path(self.profile)]
        if not self.foreground:
            command.append("--background")
        self.responses = queue.Queue()
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1)
        process, responses = self.process, self.responses
        def read():
            try:
                while True:
                    line = process.stdout.readline(1_000_001)
                    if not line:
                        break
                    if len(line) > 1_000_000:
                        raise ValueError("Browser observation too large.")
                    responses.put(json.loads(line))
            except Exception as exc:
                responses.put({"error": "Browser bridge: " + str(exc)})
            finally:
                responses.put({"error": "The owned browser helper exited."})
        threading.Thread(target=read, daemon=True, name="dot-browser-reader").start()

    def call(self, action, timeout=40, **args):
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                if action in {"status", "close", "view", "scroll", "click_link", "back", "forward", "tabs", "switch_tab", "site_search"}:
                    return {"open": False, "note": "No Dot Edge window is open. Ordinary Edge/Chrome profiles are not visible to Dot."}
                self._start()
            self.sequence += 1
            request_id = self.sequence
            self.process.stdin.write(json.dumps({"id": request_id, "action": action, **args}) + "\n")
            self.process.stdin.flush()
            while True:
                try:
                    response = self.responses.get(timeout=timeout)
                except queue.Empty as exc:
                    raise ValueError("Shared browser did not respond in time. No automatic retry.") from exc
                if response.get("id") not in (None, request_id):
                    continue
                if response.get("error"):
                    raise ValueError(response["error"])
                return response["result"]

    def status(self):
        try:
            return self.call("status", timeout=3)
        except (ValueError, OSError) as exc:
            return {"open": None, "status_unknown": True, "error": str(exc)}

    def close(self):
        with self.lock:
            process = self.process
            if not process:
                return
            try:
                if process.poll() is None:
                    self.call("close", timeout=5)
                    process.wait(timeout=5)
            except (ValueError, OSError, subprocess.TimeoutExpired):
                # Only this CLI-owned helper, never an ordinary Chrome or model process.
                process.terminate()
                process.wait(timeout=5)
            finally:
                for pipe in (process.stdin, process.stdout):
                    if pipe:
                        pipe.close()
                self.process = None


class Files:
    EXTENSIONS = {".txt", ".md", ".csv", ".json", ".log", ".py", ".html", ".css", ".js", ".yaml", ".yml", ".toml"}

    def __init__(self):
        self.selected = {}

    @staticmethod
    def path(value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Choose an explicit absolute file path; Dot does not scan your drives.")
        value = value.strip().strip('"')
        if value.startswith(("\\\\", "//")):
            raise ValueError("Network file shares are not supported.")
        if os.name != "nt" and re.match(r"^[A-Za-z]:[\\/]", value):
            value = "/mnt/" + value[0].lower() + "/" + value[3:].replace("\\", "/")
        if os.name == "nt":
            value = windows_path(value)
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("Use an absolute file path.")
        path = path.resolve()
        if str(path).startswith(("\\\\", "//")):
            raise ValueError("Network file shares are not supported, including through links.")
        return path

    def choose(self, value, confirm, owner_text="", explicit=False, launch=True):
        path = self.path(value)
        # A page/model cannot invent a path and ask us to read it without owner selection.
        spelling = str(value).strip().strip('"').replace("\\", "/").casefold()
        if not explicit and spelling not in owner_text.replace("\\", "/").casefold():
            raise ValueError("Please provide the file's full path yourself, or use /file PATH.")
        if path.suffix.lower() not in self.EXTENSIONS:
            raise ValueError("This prototype opens text/code files only, not executables, PDFs, Office files or media.")
        if not explicit and not confirm(f"Read {path} with local Qwen and open it as text? Do not select secrets."):
            return {"opened": False, "reason": "Owner declined; file not read."}
        if not path.is_file():
            raise ValueError("Selected file does not exist.")
        if launch and os.name != "nt" and not re.match(r"^[A-Za-z]:[\\/]", windows_path(path)):
            raise ValueError("The fixed Windows viewer needs a Windows-drive file, such as /mnt/c/...; Linux-only paths are not supported.")
        # Read before launching; binary/oversized files must not trigger an associated program.
        result = self._read(path, 0)
        if launch:
            editor = "C:/Windows/System32/notepad.exe" if os.name == "nt" else "/mnt/c/Windows/System32/notepad.exe"
            if not Path(editor).is_file():
                raise ValueError("The fixed Windows text viewer is unavailable; no alternative program executed.")
            subprocess.Popen([editor, windows_path(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.selected[str(path)] = path
        return {"opened": True, **result, "viewer": "Notepad (text only, never run file associations)"}

    def read(self, value, offset=0):
        path = self.path(value)
        if str(path) not in self.selected:
            raise ValueError("That file has not been selected in this session. Use /file PATH first.")
        return self._read(path, offset)

    def _read(self, path, offset):
        with path.open("rb") as handle:
            data = handle.read(200_001)
        if len(data) > 200_000:
            raise ValueError("File exceeds the 200 KB prototype limit. Select a smaller text excerpt.")
        text = data.decode("utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig")
        if "\x00" in text:
            raise ValueError("This is not a readable text file.")
        if type(offset) is not int or not 0 <= offset <= len(text):
            raise ValueError("File offset is outside the text.")
        return {"path": str(path), "body": text[offset:offset + 5000], "excerpt_offset": offset,
                "total_chars": len(text), "more_text_available": offset + 5000 < len(text),
                "limit": "Private selected-file data. Never send its contents to web search. Disk text only; unsaved editor changes are not observed."}
