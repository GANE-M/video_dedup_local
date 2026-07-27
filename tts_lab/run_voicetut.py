from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import huggingface_hub
import soundfile
import torch
from voicetut_tts import VoiceTutTTS


_snapshot_download = huggingface_hub.snapshot_download


def inference_snapshot(repo_id: str, *args, **kwargs):
    """Do not download VoiceTut's multi-gigabyte training checkpoint files."""
    if repo_id == "mohammedaly22/VoiceTut-TTS" and not kwargs.get(
        "allow_patterns"
    ):
        kwargs["ignore_patterns"] = [
            "optimizer.bin",
            "scheduler.bin",
            "random_states_*.pkl",
        ]
    return _snapshot_download(repo_id, *args, **kwargs)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--speaker", default="Asmaa")
    args = parser.parse_args()

    text = args.text_file.read_text(encoding="utf-8").strip()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    huggingface_hub.snapshot_download = inference_snapshot

    started = time.perf_counter()
    model = VoiceTutTTS.from_pretrained(
        "mohammedaly22/VoiceTut-TTS",
        device="cuda",
        dtype="float16",
    )
    loaded = time.perf_counter()
    model.synthesize(
        text,
        speaker=args.speaker,
        output=str(args.output),
        num_step=32,
        guidance_scale=2.5,
        speed=0.95,
    )
    generated = time.perf_counter()

    info = soundfile.info(str(args.output))
    metrics = {
        "model": "VoiceTut-TTS",
        "speaker": args.speaker,
        "sample_rate": info.samplerate,
        "duration_seconds": round(info.duration, 3),
        "load_seconds": round(loaded - started, 3),
        "generation_seconds": round(generated - loaded, 3),
        "rtf": round((generated - loaded) / info.duration, 3),
        "cuda_peak_memory_mb": round(
            torch.cuda.max_memory_allocated() / 1024**2,
            1,
        ),
        "output": str(args.output.resolve()),
    }
    args.output.with_suffix(".json").write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
