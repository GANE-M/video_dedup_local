from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable

from .project_store import atomic_write_json


def dhash_frame(frame: bytes) -> int:
    if len(frame) != 72:
        raise ValueError("dHash frame must be 9x8 grayscale bytes")
    mean = sum(frame) / len(frame)
    deviation = (sum((value - mean) ** 2 for value in frame) / len(frame)) ** 0.5
    if deviation < 4:
        return -1
    value = 0
    for row in range(8):
        base = row * 9
        for column in range(8):
            value = (value << 1) | int(frame[base + column + 1] > frame[base + column])
    return value


def sample_video_hashes(video: Path, ffmpeg: str, fps: float = 2.0, crop_ratio: float = 0.78) -> list[int]:
    command = [
        ffmpeg, "-v", "error", "-i", str(Path(video).resolve()),
        "-vf", f"fps={fps},crop=iw:ih*{crop_ratio:.6f}:0:0,scale=9:8,format=gray",
        "-f", "rawvideo", "pipe:1",
    ]
    result = subprocess.run(command, capture_output=True, check=True)
    frame_size = 72
    return [dhash_frame(result.stdout[offset:offset + frame_size]) for offset in range(0, len(result.stdout) - frame_size + 1, frame_size)]


def duplicate_run(first: list[int], second: list[int], max_distance: int = 3, minimum_frames: int = 3) -> tuple[int, int, int] | None:
    for first_index in range(max(0, len(first) - minimum_frames + 1)):
        for second_index in range(max(0, len(second) - minimum_frames + 1)):
            length = 0
            while first_index + length < len(first) and second_index + length < len(second):
                left, right = first[first_index + length], second[second_index + length]
                if left < 0 or right < 0 or (left ^ right).bit_count() > max_distance:
                    break
                length += 1
            if length >= minimum_frames:
                return first_index, second_index, length
    return None


def detect_duplicates(
    manifest: list[dict[str, Any]],
    hash_provider: Callable[[dict[str, Any]], list[int]],
    fps: float = 2.0,
    max_distance: int = 3,
    minimum_frames: int = 3,
) -> list[dict[str, Any]]:
    hashes = [hash_provider(item) for item in manifest]
    duplicates: list[dict[str, Any]] = []
    output_starts: list[float] = []
    running = 0.0
    for item in manifest:
        output_starts.append(running)
        running += float(item["video_seconds"])
    for first_index in range(len(manifest)):
        for second_index in range(first_index + 1, len(manifest)):
            found = duplicate_run(hashes[first_index], hashes[second_index], max_distance, minimum_frames)
            if not found:
                continue
            first_frame, second_frame, frame_count = found
            first, second = manifest[first_index], manifest[second_index]
            duplicates.append({
                "first_segment_id": first["segment_id"],
                "first_episode": first.get("episode"),
                "first_source_seconds": round(float(first.get("source_start", 0)) + first_frame / fps, 3),
                "first_output_seconds": round(output_starts[first_index] + first_frame / fps, 3),
                "second_segment_id": second["segment_id"],
                "second_episode": second.get("episode"),
                "second_source_seconds": round(float(second.get("source_start", 0)) + second_frame / fps, 3),
                "second_output_seconds": round(output_starts[second_index] + second_frame / fps, 3),
                "continuous_repeat_seconds": round(frame_count / fps, 3),
            })
    return duplicates


def validate_rendered_visual_uniqueness(
    manifest: list[dict[str, Any]], ffmpeg: str, report_path: Path,
    hash_provider: Callable[[dict[str, Any]], list[int]] | None = None,
) -> dict[str, Any]:
    provider = hash_provider or (lambda item: sample_video_hashes(Path(item["rendered_path"]), ffmpeg))
    duplicates = detect_duplicates(manifest, provider)
    report = {
        "status": "blocked" if duplicates else "ok",
        "sample_fps": 2.0,
        "crop_ratio": 0.78,
        "hash": "64-bit-dhash-9x8-gray",
        "max_hamming_distance": 3,
        "minimum_consecutive_frames": 3,
        "duplicates": duplicates,
    }
    atomic_write_json(Path(report_path), report)
    return report
