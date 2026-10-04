# Local Dot

A small personal AI research companion that runs on **your own local model**. Inspired by OpenAI's Dots and Meta's Muse, built as a hobby project for the Local AI Lab YouTube channel ("Meta Has Muse. OpenAI Has Dots. So I Built My Own.").

It is **not** an official or complete replica of either product. It is one person's working prototype, shared as-is.

## What it does

- **Chat** with any OpenAI-compatible local model server (built and tested with Qwen on vLLM).
- **Desktop pet** (`pet.py`): a small animated dot whose colour shows its state (listening, thinking, working, replying), with a chat window.
- **Its own browser**: a separate, visible Microsoft Edge window it can search (Bing), read, scroll, follow links and switch tabs in. Never your normal profile, no logins, no form filling. Private-network and non-HTTPS addresses are blocked.
- **Background research**: up to two bounded research jobs while you keep chatting; results come back as saved briefs with sources.
- **Memory you can see**: conversations and read pages are saved automatically in a local SQLite file. Notes (preferences, goals) are saved only after you approve them, and you can read, edit or delete every one.
- **Optional voice**: replies are spoken with Kokoro TTS on an NVIDIA GPU while text is still streaming.
- **Optional dictation**: push-to-talk with an external local speech-to-text engine (not included; see SETUP.md).
- **News**: a short configured list of RSS feeds, with dated headlines and source-backed briefings.

Answers cite their sources by ID; a citation check rejects IDs that were never retrieved.

## What it can't do (by design)

No logins, no purchases, no form filling, no file editing or execution, no idle listening, no cloud model calls. Microphone capture starts only when you press the key.

## Requirements

- Windows 10/11 with Python 3.11+ and Microsoft Edge.
- An OpenAI-compatible model server reachable from the PC (local machine or LAN). Tool use works best with a model that follows JSON instructions well; it was built on Qwen with the chat template's thinking mode.
- Optional voice: an NVIDIA GPU with CUDA PyTorch.

## Quick start

```bat
pip install -r requirements.txt
python -m playwright install msedge
```

Edit `config.json`: set `base_url` and `model` to your server. Then:

- `Start Local Dot.cmd`: text version in a terminal (`/help` lists commands, `/status` checks the model).
- `DotPetText.vbs`: desktop pet without voice.
- `DotPet.vbs`: desktop pet with voice (after the voice setup).

Full setup, voice and dictation: **[SETUP.md](SETUP.md)**.

## Files

| File | Purpose |
|---|---|
| `dot.py` | Core assistant, memory store, tools, CLI |
| `pet.py`, `workspace_panel.py` | Desktop pet and its memory/permissions panel |
| `shared_browser.py`, `browser_search.py`, `web_tools.py`, `workspace_tools.py`, `runtime.py` | Dot's own Edge browser and its guards |
| `jobs.py`, `briefs.py` | Background research jobs and saved briefs |
| `streaming.py`, `speech.py`, `voice_worker.py` | Streaming replies and optional spoken output |
| `listening.py`, `listen_worker.py` | Optional push-to-talk dictation bridge |
| `take_export.py`, `tts_probe.py`, `window_title.py` | Small helpers (demo take export, voice speed probe, window titles) |
| `test_*.py` | 242 automated tests: `python -m unittest discover -p "test_*.py"` |

Your data stays in `.local/` (ignored by git).

## License

MIT. See [LICENSE](LICENSE).
