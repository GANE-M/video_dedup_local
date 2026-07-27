from __future__ import annotations

import argparse
import ctypes
import difflib
import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

_DLL_HANDLES = []
_DLL_LIBRARIES = []


def configure_nvidia_runtime() -> None:
    if os.name != "nt" or not hasattr(os, "add_dll_directory"):
        return
    root = Path(sys.prefix) / "Lib" / "site-packages" / "nvidia"
    directories = sorted(path for path in root.glob("*/bin") if path.is_dir())
    for directory in directories:
        _DLL_HANDLES.append(os.add_dll_directory(str(directory)))
    for package in ("nvjitlink", "cuda_runtime", "cublas", "cudnn"):
        directory = root / package / "bin"
        for dll in sorted(directory.glob("*.dll")):
            try:
                _DLL_LIBRARIES.append(ctypes.WinDLL(str(dll)))
            except OSError:
                pass


configure_nvidia_runtime()

from faster_whisper import WhisperModel


def normalize_arabic(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.translate(
        str.maketrans(
            {
                "أ": "ا",
                "إ": "ا",
                "آ": "ا",
                "ى": "ي",
                "ؤ": "و",
                "ئ": "ي",
                "ة": "ه",
            }
        )
    )
    return " ".join(re.findall(r"[\u0600-\u06ff]+", text))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--text-file", type=Path, required=True)
    parser.add_argument("--audio-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    source = args.text_file.read_text(encoding="utf-8").strip()
    normalized_source = normalize_arabic(source)
    model = WhisperModel("medium", device="cuda", compute_type="float16")
    results = []
    for audio_path in sorted(args.audio_dir.glob("*.wav")):
        started = time.perf_counter()
        segments, info = model.transcribe(
            str(audio_path),
            language="ar",
            beam_size=5,
            vad_filter=True,
        )
        transcript = " ".join(segment.text.strip() for segment in segments).strip()
        normalized_transcript = normalize_arabic(transcript)
        results.append(
            {
                "file": audio_path.name,
                "transcript": transcript,
                "similarity": round(
                    difflib.SequenceMatcher(
                        None,
                        normalized_source,
                        normalized_transcript,
                        autojunk=False,
                    ).ratio(),
                    3,
                ),
                "language_probability": round(info.language_probability, 3),
                "asr_seconds": round(time.perf_counter() - started, 3),
            }
        )

    payload = {
        "source": source,
        "note": "Similarity is an automatic intelligibility signal, not a naturalness score.",
        "results": results,
    }
    args.output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
