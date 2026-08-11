from __future__ import annotations

import argparse
import json
import time
from importlib.resources import files
from pathlib import Path

import soundfile
import torch
import torchaudio
from silma_tts.api import SilmaTTS


REFERENCE_TEXT = (
    "ويدقق النظر في القرآن الكريم وسائر الكتب السماوية "
    "ويتبع مسالك الرسل العظام عليهم الصلاة والسلام."
)


def load_audio_with_soundfile(path: str):
    """Avoid TorchCodec's shared-FFmpeg requirement on Windows."""
    audio, sample_rate = soundfile.read(path, always_2d=True, dtype="float32")
    return torch.from_numpy(audio.T.copy()), sample_rate


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    text = args.text_file.read_text(encoding="utf-8").strip()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    reference = files("silma_tts").joinpath("infer/ref_audio_samples/ar.ref.24k.wav")
    torchaudio.load = load_audio_with_soundfile

    started = time.perf_counter()
    model = SilmaTTS(
        device="cuda",
        enable_normalizer=False,
        force_tashkeel=True,
    )
    loaded = time.perf_counter()
    wav, sr, _ = model.infer(
        ref_file=str(reference),
        ref_text=REFERENCE_TEXT,
        gen_text=text,
        file_wave=str(args.output),
        seed=2026,
        speed=1.3,
        normalize_numbers=False,
        force_tashkeel=True,
    )
    generated = time.perf_counter()

    duration = len(wav) / sr
    metrics = {
        "model": "SILMA TTS v1",
        "sample_rate": sr,
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
