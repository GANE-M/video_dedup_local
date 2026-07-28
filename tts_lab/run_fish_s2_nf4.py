from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path


ROOT = Path(r"E:\wangyang\Documents\Codexfile\climind")
REPO = ROOT / ".tts-packages" / "fish-speech-int4-patch"
PYTHON = ROOT / ".tts-envs" / "fish-s2" / "Scripts" / "python.exe"
CHECKPOINT = ROOT / ".model-cache" / "fish-s2-pro-nf4"
LAB = ROOT / "video-dedup-local" / "tts_lab"
OUTPUT = LAB / "outputs" / "fish_s2_pro_nf4_ar.wav"
LOG = LAB / "outputs" / "fish_s2_pro_nf4_ar.log"
METRICS = LAB / "outputs" / "fish_s2_pro_nf4_metrics.json"


def total_gpu_memory_mb() -> int:
    query = subprocess.run(
        [
            "nvidia-smi",
            "--query-gpu=memory.used",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        encoding="mbcs",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    values = [
        int(line.strip())
        for line in query.stdout.splitlines()
        if line.strip().isdigit()
    ]
    return max(values, default=0)


def main() -> int:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    text = (LAB / "sample_arabic.txt").read_text(encoding="utf-8").strip()
    command = [
        str(PYTHON),
        str(REPO / "fish_speech/models/text2semantic/inference.py"),
        "--text",
        text,
        "--checkpoint-path",
        str(CHECKPOINT),
        "--device",
        "cuda",
        "--half",
        "--bnb4",
        "--max-seq-len",
        "4096",
        "--seed",
        "2026",
        "--output",
        str(OUTPUT),
        "--output-dir",
        str(LAB / "outputs" / "fish_s2_codes"),
    ]
    started = time.perf_counter()
    peak_total_gpu_memory_mb = 0
    with LOG.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command,
            cwd=REPO,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        while process.poll() is None:
            peak_total_gpu_memory_mb = max(
                peak_total_gpu_memory_mb,
                total_gpu_memory_mb(),
            )
            time.sleep(0.5)
    elapsed = time.perf_counter() - started
    result = {
        "model": "groxaxo/s2-pro-BnB-4Bits",
        "base_model": "Fish Speech S2 Pro",
        "quantization": "bitsandbytes NF4 4-bit",
        "return_code": process.returncode,
        "generation_seconds_including_model_load": round(elapsed, 3),
        "peak_total_gpu_memory_mb": peak_total_gpu_memory_mb,
        "gpu_memory_note": "Windows WDDM exposes total GPU memory, not reliable per-process memory.",
        "output": str(OUTPUT),
    }
    METRICS.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False))
    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
