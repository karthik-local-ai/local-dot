"""Small offline Kokoro CPU/CUDA comparison. No microphone, Qwen, service or playback.

Use the approved .local/kokoro-venv interpreter after installing the dependencies
and downloading config.json, kokoro-v1_0.pth and voices/af_heart.pt to the private
HF cache. Each invocation measures one device in a fresh process. Audio becomes
playable at a Kokoro yield boundary (a whole short phrase here), NOT per token.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import platform
from time import perf_counter

PROCESS_START = perf_counter()
ROOT = Path(__file__).resolve().parent
# Per-process settings only. No default/global cache, account token or network.
os.environ['HF_HOME'] = str(ROOT / '.local' / 'kokoro-cache')
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['HF_HUB_DISABLE_IMPLICIT_TOKEN'] = '1'
os.environ['HF_HUB_DISABLE_TELEMETRY'] = '1'
os.environ['TOKENIZERS_PARALLELISM'] = 'false'

PHRASES = [
    'Yes, I can help with that.',
    "Let's look at this page together and check what it actually says.",
    'I can research that in the background while we keep talking.',
]
VOICE = 'af_heart'
RATE = 24000


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--device', choices=('cpu', 'cuda'), required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--threads', type=int, default=4)
    args = parser.parse_args()
    if not 1 <= args.threads <= 16:
        parser.error('--threads must be between 1 and 16')
    args.out.mkdir(parents=True, exist_ok=True)

    import numpy as np
    import soundfile as sf
    import torch
    from kokoro import KPipeline
    import spacy.util

    imports_seconds = perf_counter() - PROCESS_START
    if not spacy.util.is_package('en_core_web_sm'):
        raise RuntimeError('Install the approved en_core_web_sm dependency first; no timed downloads.')
    if args.device == 'cuda' and not torch.cuda.is_available():
        raise RuntimeError('CUDA requested but unavailable. No silent CPU fallback.')
    torch.set_num_threads(args.threads)
    torch.set_num_interop_threads(1)
    torch.manual_seed(1234)

    def sync():
        if args.device == 'cuda':
            torch.cuda.synchronize()

    sync()
    load_start = perf_counter()
    pipe = KPipeline(lang_code='a', repo_id='hexgrad/Kokoro-82M', device=args.device)
    pipe.load_voice(VOICE)
    sync()
    load_seconds = perf_counter() - load_start
    assert str(pipe.model.device).startswith(args.device)
    if pipe.g2p.fallback is None:
        raise RuntimeError('The pronunciation fallback failed to load; refusing a silently degraded test.')

    def generate(text):
        sync()
        started = perf_counter()
        chunks, arrival_times, chunk_text = [], [], []
        with torch.inference_mode():
            for graphemes, _, audio in pipe(text, voice=VOICE, speed=1, split_pattern=r'\n+'):
                data = audio.detach().cpu().numpy().copy()
                if not data.size or not np.isfinite(data).all():
                    raise RuntimeError('Empty/non-finite audio output')
                sync()
                arrival_times.append(perf_counter() - started)
                chunks.append(data)
                chunk_text.append(graphemes)
        sync()
        elapsed = perf_counter() - started
        if not chunks:
            raise RuntimeError('No playable audio returned')
        wav = np.concatenate(chunks)
        duration = len(wav) / RATE
        return wav, {
            'first_audio_seconds': arrival_times[0],
            'total_seconds': elapsed,
            'audio_seconds': duration,
            'real_time_factor': elapsed / duration,
            'chunk_arrival_seconds': arrival_times,
            'chunk_audio_seconds': [len(chunk) / RATE for chunk in chunks],
            'chunk_text': chunk_text,
            'peak': float(np.abs(wav).max()),
            'clipped_sample_fraction': float(np.mean(np.abs(wav) >= 1)),
        }

    _, first_call = generate('Hello. This is a short voice test.')
    if args.device == 'cuda':
        torch.cuda.reset_peak_memory_stats()
    rows = []
    # Three phrases, three repetitions; same settings on both devices.
    for repeat in range(1, 4):
        for number, text in enumerate(PHRASES, 1):
            audio, row = generate(text)
            row.update(phrase=number, repeat=repeat, text=text)
            if repeat == 1:
                filename = f'{args.device}-{number}.wav'
                sf.write(args.out / filename, audio, RATE, subtype='PCM_16')
                row['file'] = filename
            rows.append(row)
            print(args.device, number, repeat, f"first={row['first_audio_seconds']:.3f}s", flush=True)
    # Prewritten phrases are available together, explicitly separated by newlines.
    # This checks sequential yield timing, not live LLM text arrival or sound output.
    audio, phrase_flow = generate('\n'.join(PHRASES))
    sf.write(args.out / f'{args.device}-phrases.wav', audio, RATE, subtype='PCM_16')
    result = {
        'checked_utc': datetime.now(timezone.utc).isoformat(),
        'device': args.device,
        'actual_device': str(pipe.model.device),
        'gpu': torch.cuda.get_device_name(0) if args.device == 'cuda' else None,
        'cpu': platform.processor(),
        'cpu_threads': torch.get_num_threads(),
        'voice': VOICE,
        'sample_rate': RATE,
        'dtype': str(next(pipe.model.parameters()).dtype),
        'versions': {p: importlib.metadata.version(p) for p in
                     ('kokoro', 'misaki', 'torch', 'transformers', 'numpy', 'soundfile', 'en-core-web-sm')},
        'imports_seconds': imports_seconds,
        'cached_model_voice_load_seconds': load_seconds,
        'first_inference': first_call,
        'warm_trials': rows,
        'phrase_flow': phrase_flow,
        'peak_gpu_allocated_mib': torch.cuda.max_memory_allocated() / 2**20 if args.device == 'cuda' else None,
        'peak_gpu_reserved_mib': torch.cuda.max_memory_reserved() / 2**20 if args.device == 'cuda' else None,
        'limits': 'Audio-array availability, not speaker/Bluetooth onset. No live Qwen, microphone, interruption or listening-quality test. No downloads during timing. Model freed on exit.',
    }
    (args.out / f'{args.device}.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print('Saved', args.out / f'{args.device}.json', flush=True)


if __name__ == '__main__':
    main()
