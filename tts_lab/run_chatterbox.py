from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import soundfile
import torch
from chatterbox.mtl_tts import ChatterboxMultilingualTTS


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference-audio", type=Path)
    args = parser.parse_args()

    text = args.text_file.read_text(encoding="utf-8").strip()
    args.output.parent.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    model = ChatterboxMultilingualTTS.from_pretrained(
        device="cuda",
        t3_model="v3",
    )
    loaded = time.perf_counter()

    generate_args: dict[str, object] = {"language_id": "ar"}
    if args.reference_audio:
        generate_args["audio_prompt_path"] = str(args.reference_audio)
    with torch.inference_mode():
        wav = model.generate(text, **generate_args)
    generated = time.perf_counter()

    audio = wav.detach().cpu().squeeze().numpy()
    soundfile.write(str(args.output), audio, model.sr)
    duration = wav.shape[-1] / model.sr
    metrics = {
        "model": "Chatterbox Multilingual V3",
        "sample_rate": model.sr,
        "duration_seconds": round(duration, 3),
        "load_seconds": round(loaded - started, 3),
        "generation_seconds": round(generated - loaded, 3),
        "rtf": round((generated - loaded) / duration, 3),
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
