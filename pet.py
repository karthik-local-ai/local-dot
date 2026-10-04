"""Small Windows desktop face for Local Dot; existing backend, no new service.

Typed mode is available for testing. Optional owned local dictation uses Right
Ctrl to start capture and again to finish/send final speech. Typed drafts use Enter.
No idle listening, global Enter interception or clipboard routing.
"""
from __future__ import annotations

import argparse
import json
import math
import queue
import threading
import time
from pathlib import Path

import dot


ACTIVITY = {
    "local model thinking...": "Thinking",
    "search_web": "Searching the public web",
    "browser_search": "Searching in the browser",
    "browser_open": "Opening the shared browser",
    "browser_view": "Reading the shared page",
    "browser_scroll": "Scrolling the page",
    "browser_click": "Opening a link",
    "browser_back": "Going back",
    "browser_forward": "Going forward",
    "browser_tabs": "Checking tabs",
    "browser_switch_tab": "Switching tab",
    "browser_site_search": "Typing in the site's search",
    "open_url": "Reading a public page",
    "read_news": "Reading a saved source",
    "recall": "Checking memory",
    "fetch_news": "Checking feeds",
    "open_file": "Selecting a file",
    "read_file": "Reading a selected file",
    "start_research": "Starting background research",
    "new_session": "Starting a new session",
    "remember": "Checking permission to save",
    "task_status": "Checking research status",
    "task_result": "Reading research result",
    "cancel_task": "Cancelling research",
    "list_sessions": "Checking saved sessions",
    "list_briefs": "Finding saved briefs",
    "read_brief": "Reading a saved brief",
}


# Pet moods. Palettes are deliberately different from the Agent Pet robot: a soft round "dot"
# whose body colour, eyes, mouth and small props change with what the backend is actually doing.
MOODS = {
    #            body        light      dark       label
    "drowsy":    ("#99bdb6", "#d5e6e1", "#314e48", "resting"),
    "idle":      ("#83d9c0", "#c9f3e7", "#1f3f38", ""),
    "thinking":  ("#9b8cf5", "#e3ddff", "#2b2460", "thinking"),
    "working":   ("#f6b26b", "#ffe2bf", "#5a3410", "working"),
    "replying":  ("#5fc3e8", "#cdeefb", "#123a4c", "replying"),
    "listening": ("#ff7f7f", "#ffd6d6", "#5a1e1e", "listening"),
    "needs-you": ("#f58fb4", "#ffdbe8", "#5c1f3a", "needs you"),
    "done":      ("#ffd166", "#fff0c2", "#5a4200", "done"),
    "off":       ("#9aa5b1", "#d9dee4", "#2e3640", "off"),
}
BG = "#17202b"          # transparent colour key of the pet shell
FRAME_MS = 50
DONE_FLASH_FRAMES = 50  # ~2.5 s of "done" before settling back to idle


def _rgb(hex_colour):
    return tuple(int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))


def _hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(v)))) for v in rgb)


def _mix(a, b, t):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


class Backend:
    """All foreground SQLite/browser work lives on one thread, never Tk's thread."""
    def __init__(self, config, db, voice=False, model=None, speech_intro=True, record_dir=None, fresh_session=False):
        self.config, self.db, self.voice, self.model = config, db, voice, model
        self.fresh_session = fresh_session  # Demo takes: open on an empty conversation; notes are kept.
        self.record_dir = Path(record_dir) if record_dir else None  # Demo take: timeline + Dot's spoken WAVs.
        self.speech_intro = speech_intro
        self.interface = {"kind": "desktop pet", "dictation": "off",
                          "controls": "Right Ctrl starts local capture; Right Ctrl again finishes and sends final speech without chat focus. Escape cancels before submission. Typed drafts use Enter. Click Allow/Decline for consent. Ctrl+Alt+S stops speech. No idle listening, silence-triggered sending or acoustic barge-in."}
        self.events, self.requests, self.answers = queue.Queue(), queue.Queue(), queue.Queue()
        self.closing = threading.Event()
        self.cancelled = threading.Event()
        self.speech = None
        self.assistant = None
        self.runtime = None
        self.workspace_state = {}
        self.turn_started = self.last_tool_spoken = 0
        self.tool_announcements = 0
        self.thread = threading.Thread(target=self.run, name="dot-pet-backend", daemon=False)

    def take(self, event, **data):
        """Demo takes only: append a timed line next to Dot's spoken WAVs."""
        if not self.record_dir:
            return
        try:
            self.record_dir.mkdir(parents=True, exist_ok=True)
            who = "you" if event == "asked" else "dot"
            with (self.record_dir / "timeline.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps({"t": round(time.time(), 3), "who": who, "event": event, **data}, ensure_ascii=False) + "\n")
        except OSError:
            pass  # A take log must never interrupt the conversation.

    def emit(self, kind, value=""):
        self.events.put((kind, value))

    def output(self, text):
        stripped = text.strip()
        action = stripped[1:-1] if stripped.startswith("[") and stripped.endswith("]") else ""
        if action in ACTIVITY:
            self.emit("status", ACTIVITY[action])
            now = time.monotonic()
            if (action != "local model thinking..." and self.speech and self.speech_intro
                    and self.turn_started and now - self.turn_started >= 12
                    and now - self.last_tool_spoken >= 15 and self.tool_announcements < 2
                    and not self.cancelled.is_set()):
                self.speech.feed(ACTIVITY[action] + ". ", final=True)
                self.last_tool_spoken = now
                self.tool_announcements += 1
        else:
            self.emit("text", text + "\n")

    def confirm(self, prompt):
        self.stop_speech()
        self.emit("confirm", prompt)
        while not self.closing.is_set() and not self.cancelled.is_set():
            try:
                allowed = self.answers.get(timeout=0.1)
                if self.closing.is_set() or self.cancelled.is_set():
                    return False
                if self.speech:
                    self.speech.begin()  # Resume only subsequent reply text, never replay the prompt.
                return allowed
            except queue.Empty:
                pass
        return False

    def stop_speech(self):
        if self.assistant:
            self.assistant.cancel_acks()
        if self.speech:
            self.speech.stop()

    def close(self):
        self.closing.set()
        self.cancelled.set()
        self.stop_speech()
        self.requests.put(None)

    def job_status(self):
        # Jobs has its own lock; this never reads SQLite or calls the model.
        return self.runtime.jobs.status() if self.runtime else {"active": 0, "jobs": []}

    def cancel_research(self, job_id):
        if not self.runtime or self.closing.is_set():
            raise ValueError("Dot is not ready for task controls.")
        return self.runtime.jobs.cancel_job(job_id)

    def workspace_snapshot(self, store, runtime, notice=""):
        self.workspace_state = {"notes": store.notes(), "briefs": runtime.briefs.list(),
                                "folder": str(runtime.briefs.folder), "notice": notice}
        self.emit("workspace", self.workspace_state)

    def local_control(self, request, store, runtime, assistant):
        # Only native UI buttons enqueue these dictionaries; models use the tool allowlist.
        action = request.get("control")
        notice = ""
        if action == "save_note":
            if request.get("kind") not in {"preference", "idea"}:
                raise ValueError("Choose preference or idea.")
            store.remember(request.get("name"), request.get("text"), request["kind"])
            notice = "Saved confirmed note. This is not model training."
        elif action == "forget_note":
            store.forget(request.get("name"))
            notice = "Note removed. Old conversations and briefs are unchanged."
        elif action == "read_brief":
            name = request.get("name")
            self.emit("workspace_content", str(runtime.briefs.path(name)) + "\n\n" + runtime.briefs.read(name))
        elif action == "research":
            task = request.get("task")
            if not isinstance(task, str) or not 1 <= len(task) <= 3500:
                raise ValueError("Use a public research question under 3,500 characters.")
            result = runtime.tool({"action": "start_research", "task": task}, assistant, "/research " + task)
            notice = f"Started task #{result['id']}. Work continues only while Dot is open."
            store.add_exchange(task, notice)
            self.output("\nDot> " + notice)
        elif action != "refresh":
            raise ValueError("Unknown local control; nothing changed.")
        self.workspace_snapshot(store, runtime, notice)

    def run(self):
        store = runtime = None
        try:
            store = dot.Store(self.db)
            if self.fresh_session and store.history(limit=1):
                # Old chat stays saved as an earlier session; confirmed notes and saved pages are unchanged.
                store.new_session("Demo take " + time.strftime("%Y-%m-%d %H:%M"), "")
            runtime = dot.workspace_runtime.Runtime(
                store, lambda sid: dot.Store(store.path, session_id=sid),
                dot.research_runner(self.config, store.path), self.output,
                limit=self.config.get("worker_limit", 2), live_notifications=False,
                interface=self.interface)
            self.runtime = runtime
            if self.voice:
                from speech import Speech
                self.emit("status", "Warming voice")
                self.speech = Speech(event_callback=lambda event: self.emit("voice", event), record_dir=self.record_dir)
                try:
                    result = self.speech.start()
                    self.output("Voice ready: " + str(result["output"]))
                except (OSError, ValueError) as exc:
                    self.output("Voice unavailable; text still works: " + str(exc))
                    self.speech.close()
                    self.speech = None
            assistant = dot.Assistant(self.config, store, model=self.model,
                confirm=self.confirm, output=self.output,
                stream_output=lambda text: self.emit("chunk", text),
                runtime=runtime, cancelled=self.cancelled, speech=self.speech,
                speech_intro=dot.ACK_SCHEDULE if self.speech_intro else None)
            self.assistant = assistant
            self.output("Local Dot · session " + str(store.session_id) + "\n"
                        "Typed text: Enter. Voice: Right Ctrl starts, again finishes and sends. Replies stream before final checks. "
                        "Microphone opens only on an explicit listening gesture, when enabled.")
            history = store.history(limit=12)
            if history:
                self.output("\nRecent messages from this saved session (not spoken):")
                for message in history:
                    self.output(("\nYou> " if message["role"] == "user" else "\nDot> ") + message["text"])
            self.workspace_snapshot(store, runtime)
            self.emit("ready")
            delivered = 0
            while not self.closing.is_set():
                runtime.drain()
                if len(runtime.delivered) != delivered:
                    delivered = len(runtime.delivered)
                    self.workspace_snapshot(store, runtime)
                try:
                    question = self.requests.get(timeout=0.1)
                except queue.Empty:
                    continue
                if question is None or self.closing.is_set():
                    break
                if isinstance(question, dict):
                    try:
                        self.local_control(question, store, runtime, assistant)
                    except Exception as exc:
                        self.emit("workspace_error", str(exc))
                    continue
                self.stop_speech()
                self.take("asked", text=question)
                self.turn_started = time.monotonic()
                self.last_tool_spoken = self.turn_started
                self.tool_announcements = 0
                try:
                    answer = assistant.chat(question)
                    self.take("answered", text=answer)
                    self.emit("accepted", answer)
                except Exception as exc:
                    self.take("failed", text=str(exc))
                    self.emit("failed", str(exc))
                    self.output("\nError: " + str(exc) + "\nCompleted actions are not rolled back.")
                finally:
                    self.turn_started = 0
                    self.workspace_snapshot(store, runtime)
                    self.emit("done")
        except Exception as exc:
            self.emit("fatal", str(exc))
        finally:
            if self.speech:
                self.speech.close()
            if runtime:
                runtime.close()
            self.runtime = None
            if store:
                store.db.close()
            self.emit("closed")


class Pet:
    WIDTH, HEIGHT = 240, 289
    CANVAS_H = 144

    def __init__(self, root, backend, listen=False, show_chat=False):
        import tkinter as tk
        from tkinter import scrolledtext
        self.tk, self.root, self.backend = tk, root, backend
        self.busy, self.closing, self.frame = True, False, 0
        self.listener = None
        self.workspace = None
        self.next_task_refresh = 0
        self.listen_closer = None
        self.listen_ready = False
        self.listen_error = None
        self.capture = "idle"
        self.draft_before_capture = ""
        self.segments = []
        self.pending_draft = False
        self.reply_preview = ""
        self.turn_failed = False
        self.last_active = time.monotonic()
        self.speaking = False
        self.mood, self.mood_hold = "thinking", 0
        self.rgb = [_rgb(c) for c in MOODS["thinking"][:3]]
        self.status = tk.StringVar(value="Starting")
        self.bubble = tk.StringVar(value="Starting")  # short text under the pet; status or live transcript
        self.voice_status = tk.StringVar(value="Voice warming" if backend.voice else "Text test · voice off")
        root.title("Local Dot")
        root.configure(bg=BG)
        x = max(0, root.winfo_screenwidth() - self.WIDTH - 30)
        y = max(0, root.winfo_screenheight() - self.HEIGHT - 80)
        root.geometry(f"{self.WIDTH}x{self.HEIGHT}+{x}+{y}")
        root.resizable(False, False)
        root.overrideredirect(True)
        try:
            root.wm_attributes("-transparentcolor", BG)
        except tk.TclError:
            pass
        root.attributes("-topmost", True)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.canvas = tk.Canvas(root, width=self.WIDTH, height=self.CANVAS_H, bg=BG, highlightthickness=0)
        self.canvas.pack()
        self.canvas.bind("<ButtonPress-1>", self.drag_start)
        self.canvas.bind("<B1-Motion>", self.drag_move)
        self.canvas.bind("<ButtonRelease-1>", self.drag_end)
        menu = tk.Menu(root, tearoff=False)
        menu.add_command(label="Open / hide conversation", command=self.toggle)
        menu.add_command(label="Tasks / briefs / memory", command=self.open_workspace)
        menu.add_command(label="Quit Dot", command=self.close)
        self.canvas.bind("<Button-3>", lambda event: menu.tk_popup(event.x_root, event.y_root))
        self.bubble_label = tk.Canvas(root, width=228, height=92, bg=BG, highlightthickness=0)
        self.bubble_label.pack(fill="x", padx=6)
        self.bubble_label.create_polygon(16, 8, 96, 8, 108, 1, 120, 8, 212, 8,
            226, 8, 226, 22, 226, 76, 226, 90, 212, 90, 16, 90, 2, 90, 2, 76,
            2, 22, 2, 8, fill="#263849", smooth=True, splinesteps=24)
        self.bubble_text = self.bubble_label.create_text(114, 48, width=202,
            text=self.bubble.get(), fill="#eaf6f2", font=("Segoe UI", 9), justify="center")
        self.bubble.trace_add("write", lambda *_: self.bubble_label.itemconfigure(
            self.bubble_text, text=self.bubble.get()))
        self.bubble_label.bind("<ButtonPress-1>", self.drag_start)
        self.bubble_label.bind("<B1-Motion>", self.drag_move)
        self.bubble_label.bind("<ButtonRelease-1>", self.drag_end)
        tk.Button(root, text="Chat / hide", command=self.toggle, relief="flat", bg="#2f4457", fg="#eaf6f2",
                  activebackground="#3b566d", activeforeground="#ffffff", font=("Segoe UI", 9)).pack(pady=4)
        self.quick_send = tk.Button(root, text="Send draft", command=self.submit, state="disabled")
        self.quick_send.pack()
        # Local Enter remains available for typed/fallback drafts; voice uses the global Right Ctrl gesture.
        root.bind("<Return>", self.submit)
        root.bind("<Escape>", lambda event: self.cancel_capture())
        self.panel = tk.Toplevel(root)
        self.panel.title("Dot · conversation")
        self.panel.geometry(f"520x600+{max(0, x - 535)}+{max(0, y - 400)}")
        self.panel.minsize(420, 520)
        self.panel.configure(bg=BG)
        self.panel.columnconfigure(0, weight=1)
        self.panel.rowconfigure(2, weight=1)
        self.panel.protocol("WM_DELETE_WINDOW", self.panel.withdraw)
        if not show_chat:
            self.panel.withdraw()
        tk.Label(self.panel, textvariable=self.status, bg="#17202b", fg="#99e7ce",
                 font=("Segoe UI", 12, "bold")).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 0))
        voicebar = tk.Frame(self.panel, bg=BG)
        voicebar.grid(row=1, column=0, sticky="ew", padx=12)
        tk.Label(voicebar, textvariable=self.voice_status, bg=BG, fg="#d4dce8", wraplength=230,
                 justify="left", anchor="w").pack(side="left", fill="x", expand=True)
        self.work_button = tk.Button(voicebar, text="Work / memory", command=self.open_workspace)
        self.work_button.pack(side="right")
        self.chat = scrolledtext.ScrolledText(self.panel, height=10, wrap="word", state="disabled",
            bg="#202c3a", fg="#eef3f9", font=("Segoe UI", 11), relief="flat", padx=12, pady=12)
        self.chat.grid(row=2, column=0, sticky="nsew", padx=10, pady=10)
        self.consent = tk.Frame(self.panel, bg="#17202b")
        self.consent_text = scrolledtext.ScrolledText(self.consent, height=4, width=20, wrap="word", state="disabled",
            bg="#263849", fg="#ffe4a3", font=("Segoe UI", 10))
        self.consent_text.pack(fill="x")
        tk.Button(self.consent, text="Allow", command=lambda: self.answer(True)).pack(side="left")
        tk.Button(self.consent, text="Decline", command=lambda: self.answer(False)).pack(side="left")
        self.entry = tk.Text(self.panel, height=3, wrap="word", font=("Segoe UI", 11))
        self.entry.grid(row=4, column=0, sticky="ew", padx=10)
        self.entry.bind("<Return>", self.submit)
        self.entry.bind("<Shift-Return>", lambda event: None)
        self.entry.bind("<Escape>", lambda event: self.cancel_capture())
        controls = tk.Frame(self.panel, bg="#17202b")
        controls.grid(row=5, column=0, sticky="ew", padx=10, pady=10)
        self.send = tk.Button(controls, text="Send · Enter", command=self.submit, state="disabled")
        self.send.pack(side="left")
        tk.Button(controls, text="Stop speech", command=backend.stop_speech).pack(side="left", padx=6)
        tk.Button(controls, text="Cancel reply", command=self.cancel).pack(side="left")
        tk.Button(controls, text="Quit", command=self.close).pack(side="right")
        self.listen_status = tk.StringVar(value="Right Ctrl listening off · typed test" if not listen else "Loading local dictation")
        self.listen_label = tk.Label(self.panel, textvariable=self.listen_status, bg="#17202b", fg="#d4dce8", wraplength=480)
        self.listen_label.grid(row=6, column=0, sticky="ew", padx=10)
        self.panel.bind("<Configure>", self.resize)
        self.listen_button = tk.Button(self.panel, text="Talk / send · Right Ctrl", command=self.toggle_listening, state="disabled")
        self.listen_button.grid(row=7, column=0, pady=(4, 8))
        if listen:
            from listening import Listening
            backend.interface["dictation"] = "loading"
            self.listener = Listening(lambda event: backend.emit("listen", event))
            try:
                self.listener.start()
            except (OSError, ValueError) as exc:
                backend.interface["dictation"] = "unavailable"
                self.listen_error = str(exc)
                self.listen_status.set("Listening unavailable: " + str(exc))
                self.listen_button.configure(text="Retry listening", state="normal")
        self.entry.focus_set()
        root.after(40, self.poll)
        self.animate()

    def resize(self, event):
        if event.widget == self.panel:
            width = max(200, event.width - 30)
            self.listen_label.configure(wraplength=width)

    def append(self, text):
        self.chat.configure(state="normal")
        self.chat.insert("end", text)
        self.chat.see("end")
        self.chat.configure(state="disabled")

    def panel_visible(self):
        return self.panel.state() != "withdrawn"

    def set_mood(self, mood, hold=0):
        if mood not in MOODS:
            return
        self.mood, self.mood_hold = mood, hold
        self.last_active = time.monotonic()

    def say(self, text):
        """Status for the panel header and the pet bubble (bubble keeps it short)."""
        self.status.set(text)
        self.bubble.set(text if len(text) <= 90 else text[:87].rstrip() + "…")

    def drag_start(self, event):
        self.drag_origin = (event.x_root, event.y_root, self.root.winfo_x(), self.root.winfo_y())
        self.dragged = False

    def drag_move(self, event):
        sx, sy, x, y = self.drag_origin
        dx, dy = event.x_root - sx, event.y_root - sy
        if abs(dx) + abs(dy) > 4:
            self.dragged = True
        x = max(0, min(self.root.winfo_screenwidth() - self.WIDTH, x + dx))
        y = max(0, min(self.root.winfo_screenheight() - self.HEIGHT, y + dy))
        self.root.geometry(f"+{x}+{y}")

    def drag_end(self, event):
        if not self.dragged:
            self.toggle()

    def toggle(self):
        if self.mood == "drowsy":
            self.set_mood("idle")
        self.last_active = time.monotonic()
        if not self.panel_visible():
            self.panel.deiconify()
            self.panel.lift()
            self.entry.focus_set()
        else:
            self.panel.withdraw()

    def open_workspace(self):
        if self.closing:
            return
        if self.workspace is None:
            from workspace_panel import WorkspacePanel
            self.workspace = WorkspacePanel(self)
        else:
            self.workspace.show()

    def add_followup(self, text):
        if self.closing or self.capture != "idle":
            return False
        old = self.entry.get("1.0", "end").strip()
        self.set_draft((old + "\n\n" if old else "") + text)
        self.panel.deiconify()
        self.entry.focus_set()
        return True

    def submit(self, event=None):
        if self.busy or self.closing or self.consent.winfo_manager():
            return "break"
        if self.capture != "idle":
            self.listen_status.set("Right Ctrl finishes and sends final speech; Escape cancels before sending.")
            self.say("Right Ctrl finishes and sends · Esc cancels")
            return "break"
        text = self.entry.get("1.0", "end").strip()
        if not text:
            return "break"
        if len(text) > 12000:
            self.append("\nMessage is too long; use fewer than 12,000 characters.\n")
            return "break"
        if text.startswith("/"):
            self.append("\nThis first pet version takes ordinary conversation, not CLI slash commands.\n")
            return "break"
        kept_draft = self.draft_before_capture if self.pending_draft else ""
        self.pending_draft = False
        self.quick_send.configure(state="disabled")
        self.reply_preview = ""
        self.turn_failed = False
        self.set_draft(kept_draft)
        self.append("\nYou> " + text + "\n")
        self.busy = True
        self.send.configure(state="disabled")
        self.say("Thinking")
        self.set_mood("thinking")
        self.backend.cancelled.clear()
        self.backend.requests.put(text)
        return "break"

    def answer(self, allowed):
        self.consent.grid_remove()
        self.backend.answers.put(allowed)
        self.say("Continuing" if allowed else "Declined")
        self.set_mood("thinking")

    def cancel(self):
        if self.busy:
            self.backend.cancelled.set()
            self.backend.stop_speech()
            self.say("Cancelling · waiting for current request")
            self.set_mood("working")
            self.consent.grid_remove()

    def toggle_listening(self):
        if self.closing:
            return
        if not self.listen_ready:
            if self.listen_error is None:
                return  # Still loading (or typed-only); do not restart on an early keypress.
            if self.listener and not self.busy:
                self.listen_error = None
                self.listen_status.set("Retrying selected microphone; no capture yet")
                try:
                    self.listener.close()
                    self.listener.start()
                except (OSError, ValueError) as exc:
                    self.on_listen({"event": "error", "detail": str(exc)})
            return
        if self.busy and self.capture == "idle":
            self.backend.stop_speech()
            self.listen_status.set("Reply still running · Cancel reply first, then Right Ctrl")
            return
        if self.capture == "idle":
            if self.consent.winfo_manager():
                self.listen_status.set("Choose Allow or Decline before starting another voice turn.")
                return
            if self.pending_draft:
                self.listen_status.set("Review/send the held voice draft, or Escape to discard it first.")
                return
            self.backend.stop_speech()
            self.draft_before_capture = self.entry.get("1.0", "end-1c")
            self.segments = []
            self.pending_draft = False
            self.quick_send.configure(state="disabled")
            self.capture = "starting"
            self.set_draft("", locked=True)
            # Keep the user's current app/focus. Final speech sends on the second gesture;
            # unrelated typed text is held aside, never silently added to a voice message.
            self.say("Starting microphone")
            self.set_mood("listening")
            self.listen_command("begin")
        elif self.capture in {"starting", "listening"}:
            self.capture = "finalizing"
            self.listen_status.set("Finishing transcription, then sending · Escape cancels before send")
            self.say("Finishing, then sending · Esc cancels")
            self.set_mood("working")
            self.listen_command("end")

    def listen_command(self, command):
        try:
            self.listener.send(command)
        except OSError as exc:
            self.on_listen({"event": "error", "detail": str(exc)})

    def cancel_capture(self):
        if self.pending_draft and self.capture == "idle":
            self.pending_draft = False
            self.quick_send.configure(state="disabled")
            self.set_draft(self.draft_before_capture)
            self.say("Dictation discarded")
            self.set_mood("idle")
        if self.capture in {"starting", "listening", "finalizing"}:
            self.capture = "cancelling"
            self.listen_command("cancel")
        return "break"

    def set_draft(self, text, locked=False):
        self.entry.configure(state="normal")
        self.entry.delete("1.0", "end")
        self.entry.insert("1.0", text)
        if locked:
            self.entry.configure(state="disabled")

    def on_listen(self, event):
        kind = event.get("event")
        if self.closing:
            return
        if kind == "ready":
            self.listen_ready = True
            self.listen_error = None
            self.backend.interface["dictation"] = "ready; microphone closed until explicit gesture"
            self.listen_button.configure(text="Talk / send · Right Ctrl", state="normal")
            self.listen_status.set("Right Ctrl: start / finish and send · " + str(event.get("microphone")) + " · " + str(event.get("device")))
        elif kind == "key":
            self.toggle_listening()
        elif kind == "escape":
            self.cancel_capture()
        elif kind == "listening" and self.capture == "starting":
            self.capture = "listening"
            self.backend.interface["dictation"] = "listening to owner-initiated capture"
            self.say("Listening · Right Ctrl sends · Esc discards")
            self.set_mood("listening")
            self.listen_status.set("Listening · Right Ctrl finishes and sends · Escape discards")
        elif kind == "transcript" and self.capture in {"listening", "finalizing"}:
            text = event.get("text", "").strip()
            if event.get("final"):
                self.segments.append(text)
                text = ""
            draft = " ".join([*self.segments, text]).strip()
            self.set_draft(draft, locked=True)
            if draft:
                self.bubble.set("“" + (draft if len(draft) <= 80 else "…" + draft[-78:]) + "”")
        elif kind == "capture_finalized" and self.capture in {"starting", "listening", "finalizing"}:
            finish_requested = self.capture == "finalizing"
            self.capture = "idle"  # Duplicate/late completion events cannot send another turn.
            self.backend.interface["dictation"] = "finalized; microphone released"
            draft = " ".join(self.segments).strip()  # Never submit an unfinalized partial preview.
            self.pending_draft = bool(draft)
            self.quick_send.configure(state="normal" if draft else "disabled")
            if not draft:
                self.set_draft(self.draft_before_capture)
                self.say("No final speech · nothing sent")
                self.set_mood("idle")
                self.listen_status.set("Nothing sent · Right Ctrl starts again; typed draft preserved")
                return
            self.set_draft(draft)
            if finish_requested and not self.busy and not self.consent.winfo_manager():
                self.submit()
                if self.busy:  # Accepted by the normal length/command/consent guards.
                    self.backend.interface["dictation"] = "submitted after owner finish gesture; microphone released"
                    self.listen_status.set("Voice message sent · Right Ctrl starts the next capture")
                    return
            # An unexpected completion, busy reply, permission prompt or invalid message
            # stays a visible editable draft; it is not queued later without another gesture.
            self.say("Voice not sent · review draft in chat")
            self.set_mood("needs-you")
            self.listen_status.set("Voice draft held · resolve any permission/reply, then review and Send")
        elif kind == "capture_cancelled" and self.capture == "cancelling":
            self.backend.interface["dictation"] = "cancelled; microphone released"
            self.capture = "idle"
            self.set_draft(self.draft_before_capture)
            self.say("Dictation discarded")
            self.set_mood("idle")
            self.listen_status.set("Dictation discarded · Right Ctrl starts again")
        elif kind in {"error", "closed"}:
            self.backend.interface["dictation"] = "unavailable"
            self.listen_ready = False
            self.listen_button.configure(text="Retry listening", state="normal")
            if kind == "error" or self.listen_error is None:
                self.listen_error = str(event.get("detail"))
            if self.capture != "idle":
                self.set_draft(self.draft_before_capture)
            self.capture = "idle"
            self.listen_status.set("Listening unavailable: " + (self.listen_error or str(event.get("detail"))) + ". Reconnect the selected mic, then Retry listening.")
            if not self.busy:
                self.say("Mic unavailable · typing still works")
                self.set_mood("idle")
        elif kind == "loading":
            self.listen_status.set(event.get("detail", "Loading dictation"))

    def close(self):
        if not self.closing:
            self.closing = True
            self.busy = True
            self.send.configure(state="disabled")
            self.consent.grid_remove()
            self.say("Closing · waiting for current request")
            self.set_mood("off")
            self.backend.close()
            if self.listener:
                self.listen_ready = False
                self.listen_closer = threading.Thread(target=self.listener.close, name="dot-listening-close")
                self.listen_closer.start()

    def poll(self):
        try:
            while True:
                kind, value = self.backend.events.get_nowait()
                if kind == "workspace":
                    if self.workspace:
                        self.workspace.update(value)
                elif kind == "workspace_content":
                    if self.workspace:
                        self.workspace.show_text(value)
                elif kind == "workspace_error":
                    if self.workspace:
                        self.workspace.status.set("Control failed: " + value)
                elif kind == "accepted":
                    self.reply_preview = " ".join(str(value).split())[:80]
                elif kind == "failed":
                    self.turn_failed = True
                    self.reply_preview = ""
                elif kind in {"text", "chunk"}:
                    self.append(value)
                    if kind == "chunk" and not self.closing:
                        if self.mood != "replying":
                            self.say("Replying" + (" · spoken aloud" if self.backend.voice else " · open chat to read"))
                        self.set_mood("replying")
                elif kind == "status" and not self.closing:
                    self.say(value)
                    if value == "Thinking":
                        self.set_mood("thinking")
                    elif value in ACTIVITY.values() or value == "Warming voice":
                        self.set_mood("working")
                elif kind in {"ready", "done"} and not self.closing:
                    self.consent.grid_remove()
                    self.busy = False
                    self.send.configure(state="normal")
                    if kind == "done":
                        if self.turn_failed or self.backend.cancelled.is_set():
                            self.say("Reply stopped · open chat for details")
                            self.set_mood("off")
                        else:
                            self.say(self.reply_preview + "…" if self.reply_preview and not self.panel_visible()
                                     else "Done · click me to read")
                            # Quiet completion cue: yellow sparkle flash, no focus theft or chime.
                            self.set_mood("done", hold=DONE_FLASH_FRAMES)
                    else:
                        self.say("Ready · click me to chat")
                        self.set_mood("idle")
                elif kind == "confirm" and not self.closing and not self.backend.cancelled.is_set():
                    self.say("Needs your permission")
                    self.set_mood("needs-you")
                    self.consent_text.configure(state="normal")
                    self.consent_text.delete("1.0", "end")
                    self.consent_text.insert("1.0", value)
                    self.consent_text.configure(state="disabled")
                    self.consent.grid(row=3, column=0, sticky="ew", padx=10, pady=5)
                    self.panel.deiconify()
                elif kind == "listen":
                    self.on_listen(value)
                elif kind == "voice":
                    if value.get("event") == "error":
                        self.speaking = False
                        if not self.busy and self.mood == "replying":
                            self.set_mood("idle")
                        self.voice_status.set("Voice error: " + str(value.get("error")))
                    elif value.get("event") == "ready":
                        self.voice_status.set("Voice ready · Ctrl+Alt+S stops speech")
                    elif value.get("event") == "play_started":
                        self.voice_status.set("Speaking · Ctrl+Alt+S stops speech")
                        self.speaking = True
                        if not self.closing and not self.busy and self.mood not in ("needs-you", "off"):
                            self.set_mood("replying")
                    elif value.get("event") == "stopped":
                        self.speaking = False
                        if not self.busy and self.mood == "replying":
                            self.set_mood("idle")
                        self.voice_status.set("Speech stopped · text continues")
                    elif value.get("event") == "played":
                        self.speaking = False
                        if not self.busy and self.mood == "replying":
                            self.set_mood("done", hold=DONE_FLASH_FRAMES)
                        self.voice_status.set("Voice output completed · Ctrl+Alt+S stops speech")
                elif kind == "fatal":
                    self.append("\nStartup failed: " + value + "\n")
                    self.say("Unavailable · click me for details")
                    self.set_mood("off")
                    self.panel.deiconify()
                elif kind == "closed":
                    if self.closing:
                        self.root.destroy()
                        return
                    self.say("Backend closed · quit and reopen")
                    self.set_mood("off")
                    self.send.configure(state="disabled")
                    self.busy = True
        except queue.Empty:
            pass
        if not self.closing and time.monotonic() >= self.next_task_refresh:
            self.next_task_refresh = time.monotonic() + 0.75
            snapshot = self.backend.job_status()
            if isinstance(snapshot, dict):
                active = snapshot.get("active", 0)
                self.work_button.configure(text=f"Work ({active} running)" if active else "Work / memory")
                if self.workspace:
                    self.workspace.refresh_jobs(snapshot)
        if self.closing and not self.backend.thread.is_alive():
            self.root.destroy()
            return
        self.root.after(40, self.poll)

    def animate(self):
        """Mood-driven frame: colours ease toward the current mood; props show what Dot is doing."""
        self.frame += 1
        f = self.frame
        if self.mood_hold:
            self.mood_hold -= 1
            if not self.mood_hold and self.mood == "done":
                self.mood = "idle"
        if (self.mood == "idle" and not self.busy and not self.speaking
                and self.capture == "idle" and not self.pending_draft
                and time.monotonic() - self.last_active >= 180):
            self.mood = "drowsy"
        mood = self.mood
        targets = [_rgb(c) for c in MOODS[mood][:3]]
        self.rgb = [_mix(cur, tgt, 0.18) for cur, tgt in zip(self.rgb, targets)]
        body, light, dark = (_hex(c) for c in self.rgb)
        c = self.canvas
        c.delete("all")
        cx, ground = self.WIDTH // 2, self.CANVAS_H - 10
        # Motion: slow breath when idle; quicker bob when busy; wobble when it needs you.
        if mood in ("thinking", "working", "replying"):
            bob = 4 * math.sin(f * 0.25)
        elif mood == "needs-you":
            bob = 2 * math.sin(f * 0.5)
        elif mood == "done":
            bob = 7 * abs(math.sin(f * 0.3))
        else:
            bob = 2 * math.sin(f * 0.08)
        wob = 3 * math.sin(f * 0.6) if mood == "needs-you" else 0
        squash = 1 + 0.03 * math.sin(f * (0.25 if mood != "idle" else 0.08))
        rx, ry = 44 * squash, 38 / squash
        bx, by = cx + wob, ground - 14 - ry - bob
        # Shadow shrinks as the body lifts.
        sw = 36 - bob * 0.8
        c.create_oval(bx - sw, ground - 6, bx + sw, ground + 4, fill="#101822", outline="")
        # Listening: expanding pulse rings.
        if mood == "listening":
            for k in range(2):
                p = ((f * 0.04) + k * 0.5) % 1.0
                r = rx + 6 + 22 * p
                c.create_oval(bx - r, by - r * 0.9, bx + r, by + r * 0.9, outline=light if p < 0.5 else body, width=2)
        # Working: a spinning arc around the body, like a quiet progress ring.
        if mood == "working":
            start = (-f * 9) % 360
            c.create_arc(bx - rx - 7, by - ry - 7, bx + rx + 7, by + ry + 7, start=start, extent=100,
                         style="arc", outline=light, width=3)
        # Body with a soft highlight.
        c.create_oval(bx - rx, by - ry, bx + rx, by + ry, fill=body, outline="")
        c.create_oval(bx - rx * 0.55, by - ry * 0.8, bx - rx * 0.05, by - ry * 0.35, fill=light, outline="")
        # Antenna with a glowing tip whose pulse speeds up when busy.
        pulse = 0.5 + 0.5 * math.sin(f * (0.35 if mood != "idle" else 0.1))
        c.create_line(bx + rx * 0.45, by - ry * 0.85, bx + rx * 0.6, by - ry - 12, fill=dark, width=3)
        g = 4 + 2.5 * pulse
        c.create_oval(bx + rx * 0.6 - g, by - ry - 12 - g, bx + rx * 0.6 + g, by - ry - 12 + g, fill=light, outline="")
        # Eyes: blink occasionally; pupils scan while thinking, look up while listening.
        blink = mood not in ("needs-you", "off") and (f % 90) < 3
        look_x = 4 * math.sin(f * 0.3) if mood == "thinking" else (2 * math.sin(f * 0.05) if mood == "idle" else 0)
        look_y = -3 if mood == "listening" else 0
        ey = by - ry * 0.15
        for ex in (bx - 15, bx + 15):
            if blink or mood == "drowsy":
                c.create_line(ex - 6, ey, ex + 6, ey, fill=dark, width=3)
            elif mood == "done":
                c.create_arc(ex - 7, ey - 6, ex + 7, ey + 8, start=20, extent=140, style="arc", outline=dark, width=3)
            elif mood == "needs-you":
                c.create_oval(ex - 8, ey - 9, ex + 8, ey + 9, fill="#ffffff", outline=dark, width=2)
                c.create_oval(ex - 3 + look_x, ey - 3 + look_y, ex + 3 + look_x, ey + 3 + look_y, fill=dark, outline="")
            elif mood == "off":
                c.create_line(ex - 5, ey - 5, ex + 5, ey + 5, fill=dark, width=3)
                c.create_line(ex - 5, ey + 5, ex + 5, ey - 5, fill=dark, width=3)
            else:
                c.create_oval(ex - 6, ey - 8, ex + 6, ey + 8, fill=dark, outline="")
                c.create_oval(ex - 2 + look_x, ey - 5 + look_y, ex + 2 + look_x, ey - 1 + look_y, fill=light, outline="")
        # Mouth.
        my = by + ry * 0.35
        if mood == "replying":
            h = 3 + 5 * abs(math.sin(f * 0.6))
            c.create_oval(bx - 7, my - h, bx + 7, my + h, fill=dark, outline="")
        elif mood == "thinking":
            c.create_line(bx - 8, my + 2, bx + 8, my, fill=dark, width=3)
        elif mood == "done":
            c.create_arc(bx - 16, my - 14, bx + 16, my + 8, start=200, extent=140, style="arc", outline=dark, width=4)
        elif mood == "needs-you":
            c.create_oval(bx - 5, my - 5, bx + 5, my + 5, fill=dark, outline="")
        elif mood == "off":
            c.create_line(bx - 8, my + 1, bx + 8, my + 1, fill=dark, width=3)
        else:
            c.create_arc(bx - 13, my - 10, bx + 13, my + 6, start=200, extent=140, style="arc", outline=dark, width=3)
        # Props above the head.
        top = by - ry
        if mood == "drowsy":
            c.create_text(bx + rx + 12, top + 8 + 3 * math.sin(f * 0.06),
                          text="z", fill=light, font=("Segoe UI", 13))
        if mood == "thinking":
            for k in range(3):
                on = ((f // 6) % 3) == k
                r = 5 if on else 3.5
                dx = bx - rx * 0.9 + k * 13
                c.create_oval(dx - r, top - 14 - r, dx + r, top - 14 + r, fill=light if on else body, outline="")
        elif mood == "replying":
            for k in range(3):
                a = 0.4 + 0.6 * abs(math.sin(f * 0.5 - k * 0.8))
                r = 12 + k * 8
                c.create_arc(bx + rx - r + 4, by - r, bx + rx + r + 4, by + r, start=-35, extent=70,
                             style="arc", outline=_hex(_mix(_rgb(body), _rgb(light), a)), width=2)
        elif mood == "needs-you":
            c.create_text(bx, top - 18 + 2 * math.sin(f * 0.5), text="!", fill=light, font=("Segoe UI", 20, "bold"))
        elif mood == "listening":
            for k in range(5):
                h = 3 + 9 * abs(math.sin(f * 0.45 + k * 1.1))
                xk = bx - 16 + k * 8
                c.create_line(xk, top - 14 - h, xk, top - 14 + h, fill=light, width=3, capstyle="round")
        elif mood == "done":
            for k in range(4):
                ang = f * 0.1 + k * math.pi / 2
                sx, sy = bx + math.cos(ang) * (rx + 12), by + math.sin(ang) * (ry + 10)
                c.create_text(sx, sy, text="✦", fill=light, font=("Segoe UI", 10))
        self.root.after(FRAME_MS, self.animate)


def main():
    import tkinter as tk
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=dot.ROOT / ".local" / "dot.sqlite3")
    parser.add_argument("--voice", action="store_true")
    parser.add_argument("--listen", action="store_true")
    parser.add_argument("--no-ack", action="store_true", help="Disable the brief spoken acknowledgement")
    parser.add_argument("--show-chat", action="store_true", help="Open the conversation window at launch")
    parser.add_argument("--fresh-session", action="store_true", help="Open on a new empty conversation (old chat kept as an earlier session; notes unchanged)")
    parser.add_argument("--record-take", type=Path, help="Demo filming: save Dot's spoken phrases and a timeline in a new timestamped folder here")
    args = parser.parse_args()
    import os
    from tkinter import messagebox
    root = tk.Tk()
    root.withdraw()
    mutex = kernel = None
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        mutex = kernel.CreateMutexW(None, False, "Local\\LocalDotPet")
        if not mutex or ctypes.get_last_error() == 183:
            messagebox.showinfo("Local Dot", "Dot Pet is already open, or its single-instance lock is unavailable. Close the existing pet first.")
            if mutex:
                kernel.CloseHandle(mutex)
            root.destroy()
            return
    backend = pet = None
    try:
        config = json.loads((dot.ROOT / "config.json").read_text(encoding="utf-8"))
        record_dir = None
        if args.record_take:
            record_dir = args.record_take / time.strftime("take-%Y%m%d-%H%M%S")
            record_dir.mkdir(parents=True, exist_ok=True)
        backend = Backend(config, args.db, voice=args.voice, speech_intro=not args.no_ack, record_dir=record_dir,
                          fresh_session=args.fresh_session)
        pet = Pet(root, backend, listen=args.listen, show_chat=args.show_chat)
        def ui_error(kind, value, tb):
            import traceback
            log = dot.ROOT / ".local" / "pet-errors.log"
            log.parent.mkdir(exist_ok=True)
            with log.open("a", encoding="utf-8") as handle:
                traceback.print_exception(kind, value, tb, file=handle)
            pet.append("\nInterface error: " + str(value) + ". See .local/pet-errors.log.\n")
        root.report_callback_exception = ui_error
        root.deiconify()
        backend.thread.start()
        root.mainloop()
    except Exception as exc:
        messagebox.showerror("Local Dot startup failed", str(exc))
    finally:
        if backend:
            backend.close()
            if backend.thread.ident:
                backend.thread.join()
        if pet and pet.listen_closer:
            pet.listen_closer.join()
        elif pet and pet.listener:
            pet.listener.close()
        if mutex:
            kernel.CloseHandle(mutex)


if __name__ == "__main__":
    main()
