# Setting up Local Dot

Tested on Windows 11 with Python 3.11, Microsoft Edge and a Qwen model served by vLLM on a separate machine on the LAN. Other OpenAI-compatible servers should work but were not tested.

## 1. Model server

Any server with an OpenAI-compatible `/v1/chat/completions` endpoint and streaming, for example vLLM, llama.cpp `llama-server`, LM Studio or Ollama (`http://localhost:11434/v1`).

In `config.json`:

| Key | Meaning |
|---|---|
| `base_url` | Server address ending in `/v1`, e.g. `http://localhost:8000/v1`. Must be `localhost` or a private/loopback IP address (no hostnames, no cloud); Dot refuses anything else |
| `model` | Model name exactly as the server lists it (`/status` shows the list) |
| `chat_template_kwargs` | Passed to the server. The default turns on Qwen's thinking mode at low effort; remove it for other models |
| `timeout_seconds`, `max_tokens` | Per-request limits |
| `worker_limit`, `research_tool_budget`, `research_seconds` | Background research limits (2 jobs, 8 tool calls, 300 s) |
| `news_days`, `feeds` | News window and RSS sources; article reading is limited to each feed's `article_hosts` |
| `dictation_python`, `dictation_root` | Optional dictation engine (step 4); blank = typing only |

Check it: run `Start Local Dot.cmd`, then type `/status`.

## 2. Core install

```bat
pip install -r requirements.txt
python -m playwright install msedge
```

Run the tests if you like: `python -m unittest discover -p "test_*.py"` (one test is skipped without `prompt_toolkit`).

On first launch Dot creates `.local\dot.sqlite3` with four starter notes. Open the pet's memory panel (or use `/notes` and `/remember name = text`) and replace the `hardware` and `channel` notes with your own; they are shared with research jobs, so keep them free of secrets.

## 3. Optional voice (Kokoro, NVIDIA GPU)

Voice runs in its own environment so it can't break the core install. It reuses the CUDA PyTorch installed in your base Python.

```bat
python -m venv --system-site-packages .local\kokoro-venv
.local\kokoro-venv\Scripts\pip install -r tts-requirements.txt
.local\kokoro-venv\Scripts\python -m spacy download en_core_web_sm
set HF_HOME=%CD%\.local\kokoro-cache
.local\kokoro-venv\Scripts\python -c "from huggingface_hub import hf_hub_download as d; [d('hexgrad/Kokoro-82M', f) for f in ('config.json','kokoro-v1_0.pth','voices/af_heart.pt')]"
```

The worker then runs fully offline from `.local\kokoro-cache`. You'll also need espeak-ng support (installed by `espeakng-loader`). Start with `Start Local Dot Voice.cmd` or `DotPet.vbs`. `Ctrl+Alt+S` stops speech.

Optional speed check: `.local\kokoro-venv\Scripts\python tts_probe.py --device cuda --out .local\probe.json`.

## 4. Optional dictation

Push-to-talk (Right Ctrl) uses an **external** local speech-to-text engine that is **not included** in this repository: `listen_worker.py` expects a `realtime_stt` package (faster-whisper based) with `audio.MicrophoneCapture`, `pipeline.TranscriptionPipeline`, `stt.faster_whisper_local.FasterWhisperStreamingSTT` and `hotkey.GlobalHoldHotkey`, a `settings.json` and a `vocabulary.txt` in `dictation_root`. If you have a compatible engine, set `dictation_python` (its Python) and `dictation_root` in `config.json` and add `--listen` to `DotPet.vbs`. Otherwise, just type: everything else works without it.

## 5. Pet options

`pythonw pet.py [--voice] [--listen] [--show-chat] [--fresh-session] [--no-ack] [--db PATH]`

## Where your data lives

Everything is under `.local\`: memory database, saved pages, briefs, browser profile, logs and the voice cache. Delete that folder to reset. Nothing is sent anywhere except to your model server and the public web pages Dot reads.
