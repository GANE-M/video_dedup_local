from __future__ import annotations

import json
import math
import re
import subprocess
from pathlib import Path
from typing import Any


LOUDNESS_JSON_RE = re.compile(r"\{\s*\"input_i\".*?\}", re.DOTALL)


def measure_loudness(path: Path, ffmpeg: str, target_i: float = -18.0, target_tp: float = -0.5, target_lra: float = 5.0) -> dict[str, float]:
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostats", "-i", str(Path(path).resolve()), "-af",
         f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}:print_format=json", "-f", "null", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True,
    )
    match = LOUDNESS_JSON_RE.search(result.stderr)
    if not match:
        raise RuntimeError(f"unable to parse loudness for {path}")
    values = json.loads(match.group())
    return {
        "integrated_lufs": float(values["input_i"]),
        "true_peak_db": float(values["input_tp"]),
        "lra": float(values["input_lra"]),
        "threshold": float(values["input_thresh"]),
        "target_offset": float(values["target_offset"]),
    }


def duration_weighted_loudness(items: list[tuple[float, float]]) -> float:
    valid = [(duration, lufs) for duration, lufs in items if duration > 0 and math.isfinite(lufs)]
    if not valid:
        raise ValueError("no valid loudness measurements")
    total_duration = sum(duration for duration, _lufs in valid)
    energy = sum(duration * 10 ** (lufs / 10.0) for duration, lufs in valid) / total_duration
    return 10.0 * math.log10(energy)


def normalize_narration(path: Path, output: Path, ffmpeg: str, target_i: float, target_tp: float = -0.5, target_lra: float = 5.0) -> dict[str, Any]:
    measured = measure_loudness(path, ffmpeg, target_i, target_tp, target_lra)
    params = (
        f"measured_I={measured['integrated_lufs']}:measured_TP={measured['true_peak_db']}:"
        f"measured_LRA={measured['lra']}:measured_thresh={measured['threshold']}:"
        f"offset={measured['target_offset']}"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        ffmpeg, "-y", "-hide_banner", "-i", str(path), "-af",
        f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}:{params}:linear=true,alimiter=limit=0.85:level=false",
        "-ar", "44100", "-ac", "1", str(output),
    ], capture_output=True, check=True)
    corrections = 0
    actual = measure_loudness(output, ffmpeg, target_i, target_tp, target_lra)
    while abs(target_i - actual["integrated_lufs"]) > 0.15 and corrections < 4:
        gain = target_i - actual["integrated_lufs"]
        temporary = output.with_name(f".{output.stem}.gain{corrections}.wav")
        subprocess.run([
            ffmpeg, "-y", "-hide_banner", "-i", str(output), "-af",
            f"volume={gain:.3f}dB,alimiter=limit=0.85:level=false", "-ar", "44100", "-ac", "1", str(temporary),
        ], capture_output=True, check=True)
        temporary.replace(output)
        corrections += 1
        actual = measure_loudness(output, ffmpeg, target_i, target_tp, target_lra)
    return {"target_lufs": target_i, "measured_lufs": actual["integrated_lufs"], "correction_passes": corrections}
