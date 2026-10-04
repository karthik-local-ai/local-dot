"""SSE transport and reply-text preview for Local Dot's small JSON actions."""
from __future__ import annotations

import json
import re

# Only this known prefix is eligible for display. Other key orders stay buffered.
# In particular, a remember action's text and nested tool arguments never appear here.
REPLY_PREFIX = re.compile(r'^\s*\{\s*"action"\s*:\s*"reply"\s*,\s*"text"\s*:\s*"')


def string_prefix(value: str) -> str:
    """Decode the complete portion of a JSON string, holding split escapes/pairs."""
    end = 1  # opening quote
    while end < len(value):
        char = value[end]
        if char == '"':
            return json.loads(value[:end + 1])
        if char == "\\":
            if end + 1 >= len(value):
                break
            escape = value[end + 1]
            if escape == "u":
                if end + 6 > len(value):
                    break
                unit = int(value[end + 2:end + 6], 16)
                width = 6
                if 0xD800 <= unit <= 0xDBFF:
                    if end + 12 > len(value):
                        break
                    if value[end + 6:end + 8] != "\\u":
                        raise ValueError("Unpaired Unicode surrogate in streamed text.")
                    low = int(value[end + 8:end + 12], 16)
                    if not 0xDC00 <= low <= 0xDFFF:
                        raise ValueError("Unpaired Unicode surrogate in streamed text.")
                    width = 12
                elif 0xDC00 <= unit <= 0xDFFF:
                    raise ValueError("Unpaired Unicode surrogate in streamed text.")
                end += width
            else:
                if escape not in '"\\/bfnrt':
                    raise ValueError("Invalid JSON escape in streamed text.")
                end += 2
        else:
            if ord(char) < 32:
                raise ValueError("Unescaped control character in streamed text.")
            end += 1
    return json.loads(value[:end] + '"')


class ReplyText:
    """Expose only incremental reply.text; full action validation still comes later."""
    def __init__(self, emit):
        self.emit = emit
        self.raw = ""
        self.emitted = ""

    def feed(self, content: str) -> None:
        self.raw += content
        match = REPLY_PREFIX.match(self.raw)
        if match is None:
            return
        text = string_prefix(self.raw[match.end() - 1:])
        if not text.startswith(self.emitted):
            raise ValueError("Streamed reply changed its already displayed prefix.")
        new = text[len(self.emitted):]
        if new:
            new.encode("utf-8")  # do not pass broken Unicode to terminal/future speech
            self.emit(new)
            self.emitted = text

    def check(self, decision: dict) -> None:
        if self.emitted and (decision.get("action") != "reply"
                             or not isinstance(decision.get("text"), str)
                             or not decision["text"].startswith(self.emitted)):
            raise ValueError("Completed action disagrees with its streamed reply.")


def events(response, max_bytes=8_000_000, cancelled=None):
    """Read bounded UTF-8 SSE events, including comments and multi-line data fields."""
    data = []
    used = 0
    while True:
        if cancelled is not None and cancelled.is_set():
            raise ValueError("Research job cancelled.")
        line = response.readline(max_bytes - used + 1)
        if not line:
            if data:
                yield "\n".join(data)
            return
        used += len(line)
        if used > max_bytes:
            raise ValueError("Model stream exceeded the response-size limit.")
        line = line.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                yield "\n".join(data)
                data = []
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)
        # Ignore SSE comments, ids and event names.


def read_completion(response, on_content=None, cancelled=None) -> dict:
    """Collect one streamed completion. A cut connection is never a final answer."""
    pieces = []
    finish = None
    done = False
    for data in events(response, cancelled=cancelled):
        if data == "[DONE]":
            done = True
            break
        event = json.loads(data)
        if not isinstance(event, dict):
            raise ValueError("Invalid model stream event.")
        if event.get("error"):
            raise ValueError("Model reported an error during streaming.")
        for choice in event.get("choices", []):
            if choice.get("index", 0) != 0:
                continue
            # Reasoning, role and native tool-call deltas are never displayed.
            content = choice.get("delta", {}).get("content")
            if content is not None:
                if not isinstance(content, str):
                    raise ValueError("Model content delta was not text.")
                if finish is not None and content:
                    raise ValueError("Model sent content after the finish event.")
                if content:
                    pieces.append(content)
                    if on_content is not None:
                        on_content(content)
            reason = choice.get("finish_reason")
            if reason is not None:
                finish = reason
    if not done or finish is None:
        raise ValueError("Model stream ended without a complete finish/[DONE] sequence.")
    return {"choices": [{"message": {"content": "".join(pieces)}, "finish_reason": finish}]}
