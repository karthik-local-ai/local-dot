"""Turn one demo take folder into an editor-friendly track and transcript.

Input (written by DotTake.vbs / pet.py --record-take): timeline.jsonl + dot-NNN.wav.
Output in the same folder:
  dot-voice-full.wav  Dot's speech placed at its real times, silence between (24 kHz mono).
                      Time 0 = the first line in the timeline (usually your first question).
  transcript.md       mm:ss timeline: what you asked, each phrase Dot spoke, final answers.
Standard library only. Usage: python take_export.py PATH\\TO\\take-YYYYMMDD-HHMMSS
"""
import json
from pathlib import Path
import sys
import wave

RATE = 24000


def stamp(seconds):
    return f"{int(seconds // 60):02d}:{seconds % 60:05.2f}"


def export(folder):
    folder = Path(folder)
    lines = [json.loads(line) for line in (folder / "timeline.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        raise SystemExit("Empty timeline: nothing was asked or spoken in this take.")
    start = min(line.get("started", line["t"]) for line in lines)
    spoken = [line for line in lines if line.get("event") == "spoke"]
    end = max([line["started"] - start + line["seconds"] for line in spoken] + [0])
    track = bytearray(int((end + 1) * RATE) * 2)
    for line in spoken:
        with wave.open(str(folder / line["file"]), "rb") as clip:
            frames = clip.readframes(clip.getnframes())
        at = int((line["started"] - start) * RATE) * 2
        track[at:at + len(frames)] = frames  # Phrases play one after another, so they never overlap.
    with wave.open(str(folder / "dot-voice-full.wav"), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(bytes(track))
    rows = ["# Demo take transcript", "", f"Folder: `{folder}`", "",
            "Time 0 = first timeline entry. Dot's audio: `dot-voice-full.wav` (same clock) or the separate `dot-NNN.wav` phrases.", ""]
    for line in sorted(lines, key=lambda item: item.get("started", item["t"])):
        at = stamp(line.get("started", line["t"]) - start)
        event = line.get("event")
        if event == "asked":
            rows.append(f"- **{at} You:** {line['text']}")
        elif event == "spoke":
            rows.append(f"- {at} Dot says ({line['file']}, {line['seconds']:.1f}s): {line['text']}")
        elif event == "answered":
            rows += [f"- **{at} Dot's full reply (on screen):**", "", "  > " + line["text"].replace("\n", "\n  > "), ""]
        elif event == "failed":
            rows.append(f"- **{at} Turn failed:** {line['text']}")
        elif event == "stopped_early":
            rows.append(f"- {at} (speech stopped early)")
    (folder / "transcript.md").write_text("\n".join(rows) + "\n", encoding="utf-8")
    return folder / "dot-voice-full.wav", folder / "transcript.md", len(spoken), end


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    audio, transcript, phrases, seconds = export(sys.argv[1])
    print(f"{phrases} phrases, {seconds:.1f}s of timeline\n{audio}\n{transcript}")
