"""Small in-process pool for owner-requested research. No scheduler or nested workers."""
from __future__ import annotations

from dataclasses import dataclass, field
import threading
import time


@dataclass
class Job:
    id: int
    task: str
    session_id: int
    state: str = "running"
    answer: str = ""
    news_ids: list[int] = field(default_factory=list)
    error: str = ""
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    phase: str = "Starting"
    tool_calls: int = 0
    sources_read: int = 0
    last_error: str = ""
    brief: dict = field(default_factory=dict)
    cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    thread: threading.Thread | None = field(default=None, repr=False)

    def status(self):
        return {"id": self.id, "task": self.task, "session_id": self.session_id,
                "state": self.state, "error": self.error, "phase": self.phase,
                "elapsed_seconds": round((self.finished or time.monotonic()) - self.started, 1),
                "tool_calls": self.tool_calls, "sources_read": self.sources_read,
                "last_error": self.last_error, "brief": dict(self.brief)}


class Jobs:
    def __init__(self, run, on_done, limit=2):
        if type(limit) is not int or limit not in (1, 2):
            raise ValueError("Worker limit must be 1 or 2; the hard maximum is two.")
        self.run, self.on_done, self.limit = run, on_done, limit
        self.lock = threading.RLock()
        self.items = {}
        self.next_id = 1
        self.closed = False

    def status(self):
        with self.lock:
            active = sum(j.state in {"running", "cancelling"} for j in self.items.values())
            return {"limit": self.limit, "active": active, "free": self.limit - active,
                    "jobs": [j.status() for j in list(self.items.values())[-12:]]}

    def start(self, task, session_id):
        if not isinstance(task, str) or not task.strip() or len(task) > 4000:
            raise ValueError("Give a self-contained research task under 4000 characters.")
        with self.lock:
            if self.closed:
                raise ValueError("Dot is closing; no new jobs can start.")
            if self.status()["free"] == 0:
                raise ValueError("Both available worker slots are occupied (or the configured limit is one). Wait or cancel a job; no extra worker started.")
            job = Job(self.next_id, task.strip(), session_id)
            self.next_id += 1
            self.items[job.id] = job
            job.thread = threading.Thread(target=self._work, args=(job,), daemon=True,
                                          name=f"dot-research-{job.id}")
            job.thread.start()
            return {"started": True, **job.status(), "workers": self.status()}

    def _work(self, job):
        def progress(update):
            with self.lock:
                for key in ("phase", "tool_calls", "sources_read", "last_error"):
                    if key in update:
                        setattr(job, key, update[key])
        try:
            result = self.run(job.task, job.cancel, job.session_id, progress)
            with self.lock:
                if job.cancel.is_set():
                    job.state = "cancelled"
                else:
                    job.answer = result["answer"]
                    job.news_ids = result.get("news_ids", [])
                    job.brief = result.get("brief", {})
                    job.state = "done"
        except Exception as exc:
            with self.lock:
                job.state = "cancelled" if job.cancel.is_set() else "failed"
                job.error = "" if job.cancel.is_set() else str(exc)[:1000]
        with self.lock:
            job.finished = time.monotonic()
            job.phase = job.state
        try:
            self.on_done(job)
        except Exception as exc:
            # Keep a result accessible through /task even if display/storage fails.
            with self.lock:
                job.error = "Result delivery failed: " + str(exc)[:800]

    def get(self, job_id):
        if type(job_id) is not int:
            raise ValueError("Use an integer task ID from /tasks.")
        with self.lock:
            if job_id not in self.items:
                raise ValueError("Unknown task in this app run. Older results remain in saved chat, not a restarted task queue.")
            job = self.items[job_id]
            return {**job.status(), "answer": job.answer, "news_ids": list(job.news_ids)}

    def cancel_job(self, job_id):
        with self.lock:
            self.get(job_id)
            job = self.items[job_id]
            if job.state == "running":
                job.cancel.set()
                job.state = "cancelling"
            return {**job.status(), "note": "Stops at the next stream/tool boundary. Already completed page/brief writes are not rolled back."}

    def close(self):
        with self.lock:
            self.closed = True
            jobs = list(self.items.values())
            for job in jobs:
                if job.state in {"running", "cancelling"}:
                    job.cancel.set()
                    job.state = "cancelling"
        for job in jobs:
            if job.thread:
                # Ordinary model/tool timeouts are <=90s. No follow-up action after cancel.
                job.thread.join(timeout=95)
