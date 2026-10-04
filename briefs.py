"""Small source-checked Markdown briefs. No arbitrary paths, execution or overwrites."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re


def text(value, limit=2000):
    if not isinstance(value, str):
        return ""
    return "".join(c for c in value if c in "\n\t" or (ord(c) >= 32 and ord(c) != 127)).strip()[:limit]


def normalized(value):
    return " ".join(value.split())


def checked_brief(decision, records, question, context=None):
    """Quote membership is checked mechanically; entailment/source kind are NOT certified."""
    raw = decision.get("brief")
    issues, findings = [], []
    if not isinstance(raw, dict):
        raw = {}
        issues.append("The model did not provide a structured brief. No factual findings were accepted.")
    candidates = raw.get("findings", [])
    if not isinstance(candidates, list):
        candidates = []
        issues.append("Malformed findings; no findings accepted.")
    if len(candidates) > 8:
        issues.append("Only the first eight findings were checked.")
    for row in candidates[:8]:
        if not isinstance(row, dict):
            issues.append("A malformed finding was omitted.")
            continue
        source_id = row.get("source_id")
        source = records.get(source_id) if type(source_id) is int else None
        claim, quote = text(row.get("claim"), 1200), text(row.get("quote"), 1200)
        if not source or not source.get("evidence", "").startswith("saved page text") or "SEARCH RESULTS" in source.get("evidence", ""):
            issues.append("A finding cited a source not read as article text in this job; omitted.")
            continue
        if not claim or len(quote) < 12 or normalized(quote) not in normalized(source["body"]):
            issues.append(f"A finding for [N{source_id}] had a missing/non-matching quote; omitted.")
            continue
        kind = row.get("source_kind")
        if not isinstance(kind, str) or kind not in {"primary", "secondary", "unknown"}:
            kind = "unknown"
        findings.append({"claim": claim, "quote": quote, "source_id": source_id,
                         "source_kind": kind, "caveat": text(row.get("caveat"), 1200),
                         "title": source["title"], "url": source["url"],
                         "published": source.get("published") or "unknown",
                         "accessed": source["fetched"]})
    def lines(key):
        values = raw.get(key, [])
        return [text(v, 1200) for v in values[:8] if text(v, 1200)] if isinstance(values, list) else []
    quality = ("No verified quotes" if not findings else
               "Partial evidence; review needed" if issues else "Quotes checked; review claim meaning")
    return {"question": text(question, 5000), "context": dict(context or {}),
            "title": text(raw.get("title"), 160) or "Research brief",
            "recommendation": text(raw.get("recommendation"), 2400),
            "findings": findings, "unknowns": lines("unknowns"), "next_steps": lines("next_steps"),
            "issues": list(dict.fromkeys(issues)), "quality": quality}


class Briefs:
    def __init__(self, database):
        database = Path(database).resolve()
        self.parent = database.parent
        self.folder = self.parent / ("briefs" if database.stem == "dot" else database.stem + "-briefs")

    def _folder(self, create=False):
        if create:
            self.folder.mkdir(exist_ok=True)
        if self.folder.is_symlink() or self.folder.resolve().parent != self.parent:
            raise ValueError("Briefs folder must remain beside this memory database; links outside are refused.")
        return self.folder

    def path(self, name):
        if not isinstance(name, str) or not re.fullmatch(r"\d{8}T\d{12}Z-s\d+\.md", name):
            raise ValueError("Select a generated brief name from the list, not an arbitrary file path.")
        path = self._folder() / name
        if path.is_symlink() or path.resolve().parent != self.folder.resolve():
            raise ValueError("Linked briefs are not supported.")
        return path

    def save(self, brief, session_id):
        if type(session_id) is not int or session_id < 1:
            raise ValueError("Invalid brief session.")
        self._folder(create=True)
        stamp = datetime.now(timezone.utc)
        name = stamp.strftime("%Y%m%dT%H%M%S%fZ") + f"-s{session_id}.md"
        path = self.path(name)
        parts = ["# " + brief["title"].replace("\n", " "),
                 f"\nCreated: {stamp.isoformat(timespec='seconds')} | Session: {session_id}",
                 "Status: " + brief["quality"],
                 "\nLocal model draft. Quote matching is not fact checking. Source type is model-labelled; inspect the original. No experiment was run. Check recommendations against the constraints and actual installed software.",
                 "\n## Question / constraints\n" + brief["question"],
                 "\n## Confirmed context at job start\n" + ("\n\n".join(
                     f"{key}: {value}" for key, value in brief.get("context", {}).items()) or "No current goal/decision/next-action notes supplied."),
                 "\n## Recommendation (draft)\n" + (brief["recommendation"] or "Not established."),
                 "\n## Evidence"]
        for row in brief["findings"]:
            parts += [f"\n### [N{row['source_id']}] {row['claim']}",
                      "> " + row["quote"].replace("\n", "\n> "),
                      f"Source: {row['title']}\n{row['url']}",
                      f"Published: {row['published']} | Accessed: {row['accessed']}",
                      f"Type (model label): {row['source_kind']} | Quote: found in saved page text",
                      "Caveat: " + (row["caveat"] or "No specific caveat supplied; still requires review.")]
        if not brief["findings"]:
            parts.append("No source-backed findings passed the quote checks. Do not treat this brief as verified research.")
        for heading, values in [("Missing evidence / limits", brief["unknowns"] + brief["issues"]),
                                ("Proposed next steps (not executed)", brief["next_steps"])]:
            parts.append("\n## " + heading)
            parts.extend("- " + value for value in values)
            if not values:
                parts.append("None supplied; this is not a completeness guarantee.")
        value = "\n\n".join(parts) + "\n"
        if len(value.encode("utf-8")) > 60000:
            raise ValueError("Brief exceeds the small output limit; nothing written.")
        with path.open("x", encoding="utf-8") as handle:
            handle.write(value)
        return {"name": name, "path": str(path), "quality": brief["quality"],
                "findings": len(brief["findings"]), "issues": brief["issues"]}

    def read(self, name):
        path = self.path(name)
        with path.open("rb") as handle:
            raw = handle.read(60001)
        if len(raw) > 60000:
            raise ValueError("Brief exceeds the reader limit.")
        return raw.decode("utf-8")

    def list(self):
        folder = self._folder()
        if not folder.exists():
            return []
        result = []
        for path in sorted(folder.glob("*.md"), reverse=True)[:20]:
            try:
                body = self.read(path.name)
                lines = body.splitlines()
                if not lines:
                    continue
                status = next((line[8:] for line in lines if line.startswith("Status: ")), "Review needed")
                result.append({"name": path.name, "title": lines[0].removeprefix("# ")[:160], "quality": status})
            except (ValueError, OSError, UnicodeError):
                continue
        return result
