"""A small task/brief/memory window for the existing pet. No model status polling."""
from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, scrolledtext, ttk


class WorkspacePanel:
    def __init__(self, pet):
        self.pet, self.backend = pet, pet.backend
        self.window = tk.Toplevel(pet.root)
        self.window.title("Dot · tasks, briefs and memory")
        self.window.geometry("780x650")
        self.window.minsize(620, 520)
        self.window.protocol("WM_DELETE_WINDOW", self.window.withdraw)
        self.status = tk.StringVar(value="Local controls. No model calls to check status.")
        ttk.Label(self.window, textvariable=self.status, wraplength=730).pack(fill="x", padx=12, pady=8)
        self.folder = tk.StringVar(value="Brief folder: waiting for Dot")
        ttk.Label(self.window, textvariable=self.folder, wraplength=730).pack(fill="x", padx=12, pady=(0, 5))
        tabs = ttk.Notebook(self.window)
        tabs.pack(fill="both", expand=True, padx=10)
        work, library, memory = (ttk.Frame(tabs) for _ in range(3))
        tabs.add(work, text="Tasks")
        tabs.add(library, text="Saved briefs")
        tabs.add(memory, text="Memory / current goal")
        self.tasks = ttk.Treeview(work, columns=("state", "elapsed", "sources"), height=5)
        self.tasks.heading("#0", text="Task / session")
        self.tasks.column("#0", width=350)
        for col, label, width in [("state", "State / action", 165), ("elapsed", "Seconds", 65), ("sources", "Read", 45)]:
            self.tasks.heading(col, text=label)
            self.tasks.column(col, width=width, stretch=False)
        self.tasks.pack(fill="x", padx=8, pady=8)
        self.tasks.bind("<<TreeviewSelect>>", lambda _: self.task_details())
        bar = ttk.Frame(work)
        bar.pack(fill="x", padx=8)
        ttk.Button(bar, text="Cancel selected task", command=self.cancel_task).pack(side="left")
        ttk.Button(bar, text="Open result", command=self.open_task).pack(side="left", padx=6)
        ttk.Label(work, text="New research (public topic only; two jobs maximum)").pack(anchor="w", padx=8, pady=(12, 0))
        self.request = tk.Text(work, height=3, wrap="word")
        self.request.pack(fill="x", padx=8)
        ttk.Button(work, text="Start background research", command=self.start_task).pack(anchor="w", padx=8, pady=6)
        ttk.Label(work, text="Closing Dot cancels running jobs. Saved briefs remain; they are drafts, not verified conclusions.", wraplength=700).pack(fill="x", padx=8)
        self.briefs = ttk.Treeview(library, columns=("quality",), height=8)
        self.briefs.heading("#0", text="Saved result (latest 20)")
        self.briefs.heading("quality", text="Evidence status")
        self.briefs.column("#0", width=420)
        self.briefs.column("quality", width=260)
        self.briefs.pack(fill="both", expand=True, padx=8, pady=8)
        controls = ttk.Frame(library)
        controls.pack(fill="x", padx=8, pady=8)
        ttk.Button(controls, text="Open selected brief", command=self.open_saved).pack(side="left")
        ttk.Button(controls, text="Use in next question", command=self.resume).pack(side="left", padx=6)
        ttk.Button(controls, text="Refresh", command=lambda: self.send("refresh")).pack(side="left")
        self.notes = ttk.Treeview(memory, columns=("kind",), height=5)
        self.notes.heading("#0", text="Confirmed note / last update")
        self.notes.heading("kind", text="Kind")
        self.notes.column("#0", width=470)
        self.notes.column("kind", width=100)
        self.notes.pack(fill="x", padx=8, pady=6)
        self.notes.bind("<<TreeviewSelect>>", lambda _: self.select_note())
        row = ttk.Frame(memory)
        row.pack(fill="x", padx=8)
        self.name = tk.StringVar()
        self.kind = tk.StringVar(value="preference")
        ttk.Label(row, text="Name").pack(side="left")
        ttk.Entry(row, textvariable=self.name, width=27).pack(side="left", padx=6)
        ttk.Combobox(row, textvariable=self.kind, values=("preference", "idea"), state="readonly", width=12).pack(side="left")
        ttk.Button(row, text="Current goal", command=self.goal).pack(side="left", padx=6)
        self.note = tk.Text(memory, height=4, wrap="word")
        self.note.pack(fill="x", padx=8, pady=6)
        row = ttk.Frame(memory)
        row.pack(fill="x", padx=8)
        ttk.Button(row, text="Save note…", command=self.save_note).pack(side="left")
        ttk.Button(row, text="Remove note…", command=self.delete_note).pack(side="left", padx=6)
        ttk.Label(memory, text="current_goal, current_decision, next_action and the four default topic notes are shared with local research workers. Keep them free of secrets. Other notes are foreground context only. Removing a note does NOT erase conversation history.", wraplength=710).pack(fill="x", padx=8, pady=6)
        self.viewer = scrolledtext.ScrolledText(self.window, height=10, wrap="word", state="disabled", font=("Segoe UI", 10))
        self.viewer.pack(fill="both", expand=True, padx=10, pady=10)
        self.current_jobs, self.current_notes = {}, {}
        self.view_mode = "brief"
        self.update(getattr(self.backend, "workspace_state", {}) or {})
        self.send("refresh")

    def send(self, action, **values):
        if self.pet.closing:
            return
        self.status.set("Waiting for current reply to finish…" if self.pet.busy else "Applying local control…")
        self.backend.requests.put({"control": action, **values})

    def show(self):
        self.window.deiconify()
        self.window.lift()
        self.send("refresh")

    def show_text(self, value, mode="brief"):
        self.view_mode = mode
        self.viewer.configure(state="normal")
        self.viewer.delete("1.0", "end")
        self.viewer.insert("1.0", value)
        self.viewer.configure(state="disabled")

    @staticmethod
    def selected(tree):
        selection = tree.selection()
        return selection[0] if selection else None

    def refresh_jobs(self, snapshot):
        if not isinstance(snapshot, dict):
            return
        self.current_jobs = {str(j["id"]): j for j in snapshot.get("jobs", [])}
        for key in self.tasks.get_children():
            if key not in self.current_jobs:
                self.tasks.delete(key)
        for key, job in self.current_jobs.items():
            values = (job["state"] if job["state"] != "running" else job["phase"], job["elapsed_seconds"], job["sources_read"])
            label = f"#{key} / S{job['session_id']} · {job['task'].splitlines()[0][:85]}"
            if self.tasks.exists(key):
                self.tasks.item(key, text=label, values=values)
            else:
                self.tasks.insert("", "end", iid=key, text=label, values=values)
        if self.view_mode == "task":
            self.task_details()

    def update(self, state):
        if not isinstance(state, dict):
            return
        self.current_notes = {n["name"]: n for n in state.get("notes", [])}
        if state.get("folder"):
            self.folder.set("Brief folder: " + state["folder"])
        for tree, rows in [(self.notes, state.get("notes", [])), (self.briefs, state.get("briefs", []))]:
            names = {item["name"] for item in rows}
            for key in tree.get_children():
                if key not in names:
                    tree.delete(key)
            for item in rows:
                name = item["name"]
                if tree is self.notes:
                    label, value = name + " · " + item["updated"], item["kind"]
                else:
                    label, value = item["title"], item["quality"]
                if tree.exists(name):
                    tree.item(name, text=label, values=(value,))
                else:
                    tree.insert("", "end", iid=name, text=label, values=(value,))
        self.status.set(state.get("notice") or "Saved locally. Model-written briefs still need review.")

    def task_details(self):
        job = self.current_jobs.get(self.selected(self.tasks))
        if job:
            self.show_text(f"Task #{job['id']} · session {job['session_id']}\n{job['task']}\n\n"
                           f"State: {job['state']} | Tool calls: {job['tool_calls']} | Sources read: {job['sources_read']}\n"
                           f"{job.get('error') or job.get('last_error') or ''}\n"
                           f"Evidence: {job.get('brief', {}).get('quality', 'No saved brief yet')}", mode="task")

    def cancel_task(self):
        key = self.selected(self.tasks)
        if key:
            try:
                self.backend.cancel_research(int(key))
                self.status.set("Cancellation requested; an in-flight request may need to finish or time out.")
            except (ValueError, OSError) as exc:
                self.status.set(str(exc))

    def open_task(self):
        # Completion may happen between periodic UI polls and this click.
        self.refresh_jobs(self.backend.job_status())
        job = self.current_jobs.get(self.selected(self.tasks), {})
        name = job.get("brief", {}).get("name")
        if name:
            self.send("read_brief", name=name)
        else:
            self.task_details()

    def start_task(self):
        task = self.request.get("1.0", "end").strip()
        if not task or len(task) > 3500:
            self.status.set("Enter a self-contained public research question under 3,500 characters.")
            return
        self.send("research", task=task)

    def open_saved(self):
        name = self.selected(self.briefs)
        if name:
            self.send("read_brief", name=name)

    def resume(self):
        name = self.selected(self.briefs)
        if name:
            added = self.pet.add_followup(f"Read the saved brief {name}. Help me continue from its recommendation and missing checks; don't start any new work yet.")
            self.status.set("Question added to chat. Edit it, then press Enter. No automatic resumption." if added
                            else "Finish or discard dictation first; no draft was changed.")

    def select_note(self):
        note = self.current_notes.get(self.selected(self.notes))
        if note:
            self.name.set(note["name"])
            self.kind.set(note["kind"])
            self.note.delete("1.0", "end")
            self.note.insert("1.0", note["text"])

    def goal(self):
        self.name.set("current_goal")
        self.kind.set("idea")
        self.note.delete("1.0", "end")
        self.note.insert("1.0", self.current_notes.get("current_goal", {}).get("text", ""))

    def save_note(self):
        name, body = self.name.get().strip(), self.note.get("1.0", "end").strip()
        if messagebox.askyesno("Save confirmed memory", f"Save/replace note '{name}'?\n\n{body[:600]}\n\nThis is a local note, not model training.", parent=self.window):
            self.send("save_note", name=name, text=body, kind=self.kind.get())

    def delete_note(self):
        name = self.name.get().strip()
        if messagebox.askyesno("Remove note only", f"Remove '{name}'?\nOld conversations and saved briefs are NOT erased and may still mention it.", parent=self.window):
            self.send("forget_note", name=name)
