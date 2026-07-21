from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import video_dedup

from .loudness import duration_weighted_loudness, measure_loudness, normalize_narration
from .models import RecapProject, RecapSegment, stable_hash
from .project_store import atomic_write_json
from .timeline import validate_source_intervals
from .visual_dedup import validate_rendered_visual_uniqueness
from .voice_library import VoiceLibrary, voice_cache_key, voice_cache_path


RENDER_CACHE_VERSION = 1
VIDEO_SUFFIXES = video_dedup.VIDEO_SUFFIXES


def _run(command: list[str], *, text: bool = False, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    result = subprocess.run(
        command, capture_output=True, text=text,
        encoding="utf-8" if text else None, errors="replace" if text else None,
        env=env, **video_dedup.hidden_subprocess_kwargs(),
    )
    if result.returncode:
        stderr = result.stderr if text else result.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(command[:8])}\n{stderr[-4000:]}")
    return result


def probe(path: Path, ffprobe: str) -> dict[str, Any]:
    return video_dedup.probe_video(Path(path), ffprobe)


def inspect_sources(source_root: Path, ffprobe: str, pattern: str = "*.mp4") -> dict[str, Any]:
    root = Path(source_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError(f"source directory does not exist: {root}")
    files = sorted(path for path in root.glob(pattern) if path.suffix.casefold() in VIDEO_SUFFIXES)
    episodes = []
    for index, path in enumerate(files, 1):
        info = probe(path, ffprobe)
        episodes.append({"episode": index, "path": str(path.resolve()), **info})
    return {
        "status": "ok", "source_root": str(root), "pattern": pattern,
        "episode_count": len(episodes), "total_duration": sum(item["duration"] for item in episodes),
        "episodes": episodes,
    }


def measure_project_loudness(project: RecapProject, ffmpeg: str, ffprobe: str) -> dict[str, Any]:
    measurements = []
    warnings = []
    for episode in sorted({segment.episode for segment in project.segments}):
        path = project.episode_path(episode)
        info = probe(path, ffprobe)
        if not info["has_audio"]:
            warnings.append(f"episode {episode} has no audio and was omitted from loudness measurement")
            continue
        measured = measure_loudness(path, ffmpeg)
        measurements.append({"episode": episode, "path": str(path), "duration": info["duration"], **measured})
    weighted = duration_weighted_loudness(
        [(item["duration"], item["integrated_lufs"]) for item in measurements]
    ) if measurements else -18.0
    return {"status": "ok", "project_id": project.project_id, "integrated_lufs": round(weighted, 3), "episodes": measurements, "warnings": warnings}


def resolved_target_loudness(project: RecapProject, ffmpeg: str, ffprobe: str) -> tuple[float, dict[str, Any]]:
    configured = project.narration_target_loudness
    if isinstance(configured, (int, float)):
        value = float(configured)
        return value, {"status": "configured", "integrated_lufs": value, "episodes": [], "warnings": []}
    text = str(configured).strip().casefold()
    try:
        return float(text), {"status": "configured", "integrated_lufs": float(text), "episodes": [], "warnings": []}
    except ValueError:
        if text not in {"match_source", "match_source_program", "auto"}:
            raise ValueError("narration_target_loudness must be a LUFS number or match_source_program")
    report = measure_project_loudness(project, ffmpeg, ffprobe)
    return float(report["integrated_lufs"]), report


def _qwen_python(project: RecapProject) -> Path:
    configured = str(project.rendering.get("qwen_python", "")).strip()
    if configured:
        return Path(configured).expanduser().resolve()
    candidate = Path(__file__).resolve().parents[2] / ".qwen-tts-lab" / ".venv" / "Scripts" / "python.exe"
    return candidate.resolve() if candidate.is_file() else Path(sys.executable).resolve()


def generate_voice_preview(library: VoiceLibrary, voice_id: str, qwen_python: Path | None = None) -> dict[str, Any]:
    profile = library.get(voice_id)
    output = library.resolve_asset(profile.preview_audio)
    if output.is_file():
        return {"status": "ok", "voice_id": voice_id, "preview_audio": str(output), "cache_hit": True}
    reference = library.resolve_asset(profile.reference_audio)
    if not reference.is_file():
        raise FileNotFoundError(f"voice reference is missing: {reference}")
    if qwen_python is None:
        candidate = Path(__file__).resolve().parents[2] / ".qwen-tts-lab" / ".venv" / "Scripts" / "python.exe"
        qwen_python = candidate if candidate.is_file() else Path(sys.executable)
    payload_path = output.parent / ".preview_request.json"
    atomic_write_json(payload_path, {
        "profile": {**profile.to_dict(), "reference_audio": str(reference)},
        "items": [{"segment_id": f"preview-{voice_id}", "text": profile.preview_text, "language": profile.languages[0], "output": str(output)}],
    })
    env = os.environ.copy()
    project_root = str(Path(__file__).resolve().parents[1])
    env["PYTHONPATH"] = project_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        result = _run([str(qwen_python), "-m", "recap.qwen_tts", str(payload_path)], text=True, env=env)
        response = json.loads(result.stdout.strip().splitlines()[-1])
        if response.get("status") != "ok" or not output.is_file():
            raise RuntimeError(f"voice preview generation failed: {response}")
    finally:
        payload_path.unlink(missing_ok=True)
    return {"status": "ok", "voice_id": voice_id, "preview_audio": str(output), "cache_hit": False}


def generate_voice_files(
    project: RecapProject, segments: list[RecapSegment], library: VoiceLibrary,
    cache_root: Path, payload_dir: Path,
) -> tuple[dict[str, Path], list[str], list[str]]:
    profile = library.get(project.voice_id)
    errors = library.validate_assets(profile.voice_id)
    if errors:
        raise FileNotFoundError("; ".join(errors))
    outputs: dict[str, Path] = {}
    hits: list[str] = []
    missing_items = []
    for segment in segments:
        if segment.mode != "narration":
            continue
        key = voice_cache_key(profile, segment.narration_text, project.target_language, project.narration_speed)
        target = voice_cache_path(cache_root, project.project_id, profile, key)
        outputs[segment.segment_id] = target
        if target.is_file():
            hits.append(segment.segment_id)
        else:
            missing_items.append({
                "segment_id": segment.segment_id, "text": segment.narration_text,
                "language": project.target_language, "output": str(target),
            })
    if missing_items:
        qwen_python = _qwen_python(project)
        if not qwen_python.is_file():
            raise FileNotFoundError(f"Qwen Python does not exist: {qwen_python}")
        payload_dir.mkdir(parents=True, exist_ok=True)
        payload_path = payload_dir / "qwen_voice_request.json"
        payload = {
            "profile": {
                **profile.to_dict(),
                "reference_audio": str(library.resolve_asset(profile.reference_audio)),
            },
            "items": missing_items,
        }
        atomic_write_json(payload_path, payload)
        env = os.environ.copy()
        project_root = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = project_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        result = _run([str(qwen_python), "-m", "recap.qwen_tts", str(payload_path)], text=True, env=env)
        response = json.loads(result.stdout.strip().splitlines()[-1])
        if response.get("status") != "ok":
            raise RuntimeError(f"Qwen voice generation failed: {response}")
    return outputs, hits, [item["segment_id"] for item in missing_items]


def _escape_filter_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def _caption_filters(text: str, caption_dir: Path, segment_id: str, voice_length: float, lead_in: float, project: RecapProject) -> list[str]:
    sentences = [item.strip() for item in re.split(r"(?<=[.!?。！？])\s+", text) if item.strip()] or [text]
    weights = [max(1, len(re.findall(r"\w+", sentence, flags=re.UNICODE))) for sentence in sentences]
    total = sum(weights)
    running = lead_in
    filters = []
    font_file = str(project.rendering.get("caption_font_file") or (r"C:\Windows\Fonts\arialbd.ttf" if os.name == "nt" else "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"))
    font_size = int(project.rendering.get("caption_font_size", 38))
    caption_y = float(project.rendering.get("caption_y_percent", 12.0)) / 100.0
    wrap_width = int(project.rendering.get("caption_wrap_chars", 31))
    for number, (sentence, weight) in enumerate(zip(sentences, weights), 1):
        end = lead_in + voice_length if number == len(sentences) else running + voice_length * weight / total
        text_file = caption_dir / f"{segment_id}_{number}.txt"
        text_file.write_text(textwrap.fill(sentence, width=wrap_width), encoding="utf-8")
        filters.append(
            f"drawtext=fontfile='{_escape_filter_path(Path(font_file))}':textfile='{_escape_filter_path(text_file)}':"
            f"fontcolor=white:fontsize={font_size}:line_spacing=8:borderw=4:bordercolor=black@0.85:"
            f"x=(w-text_w)/2:y=h*{caption_y:.4f}:box=1:boxcolor=black@0.25:boxborderw=18:"
            f"enable='between(t\\,{running:.3f}\\,{end:.3f})'"
        )
        running = end
    return filters


def _video_filter(project: RecapProject, info: dict[str, Any]) -> str:
    width = int(project.rendering.get("width", info["width"]))
    height = int(project.rendering.get("height", info["height"]))
    width -= width % 2
    height -= height % 2
    return f"scale={width}:{height}:force_original_aspect_ratio=decrease,pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1"


def _encoder_args(ffmpeg: str, project: RecapProject) -> list[str]:
    preference = str(project.rendering.get("hardware_acceleration", "auto"))
    try:
        mode = video_dedup.resolve_hardware_acceleration(ffmpeg, preference)
    except ValueError:
        mode = "cpu"
    encoder = video_dedup.video_encoder_name(mode)
    if encoder == "libx264":
        return ["-c:v", encoder, "-preset", str(project.rendering.get("encoder_preset", "medium")), "-crf", str(project.rendering.get("crf", 21))]
    return ["-c:v", encoder, "-preset", "p5", "-cq", str(project.rendering.get("crf", 21)), "-b:v", "0"]


def render_segment(
    project: RecapProject, segment: RecapSegment, index: int, previous_mode: str | None,
    next_mode: str | None, voice_path: Path | None, target_lufs: float,
    work_root: Path, cache_root: Path, ffmpeg: str, ffprobe: str,
) -> tuple[dict[str, Any], bool]:
    source = project.episode_path(segment.episode)
    info = probe(source, ffprobe)
    lead_in = float(segment.rendering.get("narration_lead_in", project.rendering.get("narration_lead_in", 0.5))) if previous_mode == "original" else 0.0
    audio_tail = float(segment.rendering.get("original_audio_tail", project.rendering.get("original_audio_tail", 0.5))) if next_mode == "narration" else 0.0
    voice_identity = ""
    voice_length = 0.0
    normalized_voice = None
    if segment.mode == "narration":
        if voice_path is None or not voice_path.is_file():
            raise FileNotFoundError(f"missing narration voice for {segment.segment_id}: {voice_path}")
        voice_identity = stable_hash({"path": str(voice_path), "size": voice_path.stat().st_size, "mtime_ns": voice_path.stat().st_mtime_ns, "target_lufs": target_lufs})
    source_stat = source.stat()
    cache_key = segment.computed_cache_key({
        "render_cache_version": RENDER_CACHE_VERSION,
        "source_size": source_stat.st_size, "source_mtime_ns": source_stat.st_mtime_ns,
        "previous_mode": previous_mode, "next_mode": next_mode,
        "project_rendering": project.rendering, "voice": voice_identity,
    })
    cached = cache_root / "segments" / f"{segment.segment_id}-{cache_key}.mp4"
    rendered = work_root / "segments" / f"{index:03d}-{segment.segment_id}-{segment.mode}.mp4"
    rendered.parent.mkdir(parents=True, exist_ok=True)
    if cached.is_file():
        shutil.copy2(cached, rendered)
        rendered_info = probe(rendered, ffprobe)
        return ({
            "segment_id": segment.segment_id, "episode": segment.episode, "mode": segment.mode,
            "source": str(source), "source_start": segment.source_start, "source_end": segment.source_end,
            "video_seconds": rendered_info["duration"], "rendered_path": str(rendered),
            "cache_key": cache_key, "cache_hit": True, "purpose": segment.purpose,
        }, True)

    base_duration = segment.source_end - segment.source_start
    common_vf = _video_filter(project, info)
    encoder_args = _encoder_args(ffmpeg, project)
    if segment.mode == "original":
        if not info["has_audio"]:
            raise RuntimeError(f"original segment {segment.segment_id} uses episode {segment.episode}, but the video has no audio stream")
        fades = []
        if previous_mode == "narration":
            fades.append("afade=t=in:st=0:d=0.3")
        if audio_tail:
            fades.append(f"afade=t=out:st={base_duration:.3f}:d={audio_tail:.3f}")
        audio_filter = ",".join([f"atrim=duration={base_duration + audio_tail:.3f}", *fades, "asetpts=PTS-STARTPTS"])
        command = [
            ffmpeg, "-y", "-hide_banner", "-ss", f"{segment.source_start:.3f}", "-t", f"{base_duration + audio_tail:.3f}", "-i", str(source),
            "-filter_complex", f"[0:v]{common_vf},trim=duration={base_duration:.3f},setpts=PTS-STARTPTS[v];[0:a]{audio_filter}[a]",
            "-map", "[v]", "-map", "[a]", *encoder_args, "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "192k", str(rendered),
        ]
        _run(command)
        video_seconds = base_duration
    else:
        normalized_voice = work_root / "voice" / f"{segment.segment_id}.wav"
        loudness = normalize_narration(voice_path, normalized_voice, ffmpeg, target_lufs)
        raw_voice_seconds = probe_audio_duration(normalized_voice, ffprobe)
        speed = max(0.5, min(2.0, float(project.narration_speed)))
        voice_length = raw_voice_seconds / speed
        video_seconds = max(base_duration, lead_in + voice_length)
        captions = _caption_filters(segment.narration_text, work_root / "captions", segment.segment_id, voice_length, lead_in, project)
        vf = ",".join([common_vf, *captions, f"trim=duration={video_seconds:.3f}", "setpts=PTS-STARTPTS"])
        audio_chain = [f"atempo={speed:.6f}"] if abs(speed - 1.0) > 1e-6 else []
        audio_chain.extend(["alimiter=limit=0.85:level=false", f"adelay={int(lead_in * 1000)}:all=1", f"apad=pad_dur={video_seconds:.3f}", f"atrim=duration={video_seconds:.3f}", "asetpts=PTS-STARTPTS"])
        command = [
            ffmpeg, "-y", "-hide_banner", "-stream_loop", "-1", "-ss", f"{segment.source_start:.3f}", "-t", f"{video_seconds:.3f}", "-i", str(source), "-i", str(normalized_voice),
            "-filter_complex", f"[0:v]{vf}[v];[1:a]{','.join(audio_chain)}[a]", "-map", "[v]", "-map", "[a]",
            *encoder_args, "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "192k", str(rendered),
        ]
        _run(command)
        segment.rendering["last_loudness"] = loudness

    cached.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(rendered, cached)
    segment.cache_key = cache_key
    return ({
        "segment_id": segment.segment_id, "episode": segment.episode, "mode": segment.mode,
        "source": str(source), "source_start": segment.source_start, "source_end": segment.source_end,
        "video_seconds": video_seconds, "voice_seconds": voice_length,
        "audio_tail_seconds": audio_tail, "rendered_path": str(rendered),
        "cache_key": cache_key, "cache_hit": False, "purpose": segment.purpose,
    }, False)


def probe_audio_duration(path: Path, ffprobe: str) -> float:
    result = _run([ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)], text=True)
    return float(result.stdout.strip())


def join_video_segments(manifest: list[dict[str, Any]], output: Path, ffmpeg: str, project: RecapProject) -> None:
    inputs = [part for item in manifest for part in ("-i", item["rendered_path"])]
    labels = "".join(f"[{index}:v]" for index in range(len(manifest)))
    _run([ffmpeg, "-y", "-hide_banner", *inputs, "-filter_complex", f"{labels}concat=n={len(manifest)}:v=1:a=0[v]", "-map", "[v]", "-an", *_encoder_args(ffmpeg, project), "-pix_fmt", "yuv420p", str(output)])


def render_audio_stem(manifest: list[dict[str, Any]], mode: str, output: Path, total_seconds: float, ffmpeg: str) -> None:
    selected = []
    running = 0.0
    for item in manifest:
        if item["mode"] == mode:
            selected.append((item["rendered_path"], running))
        running += float(item["video_seconds"])
    if not selected:
        _run([ffmpeg, "-y", "-hide_banner", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-t", f"{total_seconds:.3f}", "-c:a", "pcm_s24le", str(output)])
        return
    inputs = [part for path, _offset in selected for part in ("-i", path)]
    filters, labels = [], []
    for index, (_path, offset) in enumerate(selected):
        label = f"a{index}"
        filters.append(f"[{index}:a]adelay={int(offset * 1000)}:all=1[{label}]")
        labels.append(f"[{label}]")
    mix = f"{labels[0]}anull" if len(labels) == 1 else f"{''.join(labels)}amix=inputs={len(labels)}:duration=longest:dropout_transition=0:normalize=0"
    filters.append(f"{mix},apad=pad_dur={total_seconds:.3f},atrim=duration={total_seconds:.3f}[a]")
    _run([ffmpeg, "-y", "-hide_banner", *inputs, "-filter_complex", ";".join(filters), "-map", "[a]", "-c:a", "pcm_s24le", "-ar", "44100", "-ac", "2", str(output)])


def mix_audio_command(ffmpeg: str, narration: Path, original: Path, output: Path, total_seconds: float) -> list[str]:
    return [
        ffmpeg, "-y", "-hide_banner", "-i", str(narration), "-i", str(original), "-filter_complex",
        f"[0:a][1:a]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,alimiter=limit=0.94:level=false,apad=pad_dur={total_seconds:.3f},atrim=duration={total_seconds:.3f}[a]",
        "-map", "[a]", "-c:a", "pcm_s24le", "-ar", "44100", "-ac", "2", str(output),
    ]


def mix_audio_masters(narration: Path, original: Path, output: Path, total_seconds: float, ffmpeg: str) -> None:
    _run(mix_audio_command(ffmpeg, narration, original, output, total_seconds))


def mux_master(video: Path, audio: Path, output: Path, ffmpeg: str) -> None:
    _run([ffmpeg, "-y", "-hide_banner", "-i", str(video), "-i", str(audio), "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2", "-movflags", "+faststart", "-shortest", str(output)])


def decode_check(path: Path, ffmpeg: str) -> None:
    _run([ffmpeg, "-v", "error", "-i", str(path), "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"])


def render_project(
    project: RecapProject, *, final: bool = True, only_segment_id: str | None = None,
    ffmpeg: str | None = None, ffprobe: str | None = None, library_path: Path | None = None,
) -> dict[str, Any]:
    ffmpeg = video_dedup.find_binary("ffmpeg", ffmpeg)
    ffprobe = video_dedup.find_binary("ffprobe", ffprobe)
    errors = validate_source_intervals(project, lambda path: probe(path, ffprobe))
    if errors:
        return {"status": "validation_failed", "project_id": project.project_id, "version": project.current_version, "validation_errors": errors}
    library = VoiceLibrary(library_path) if library_path else VoiceLibrary()
    output_root = project.output_path()
    version_root = output_root / f"v{project.current_version:04d}"
    for name in ("segments", "voice", "captions", "audio"):
        (version_root / name).mkdir(parents=True, exist_ok=True)
    cache_root = output_root / ".recap_cache" / project.project_id
    selected = project.segments
    if only_segment_id:
        selected = [item for item in project.segments if item.segment_id == only_segment_id]
        if not selected:
            raise KeyError(f"unknown segment_id: {only_segment_id}")
    # A local render must not generate unrelated missing narration takes.
    voice_paths, voice_hits, voice_generated = generate_voice_files(
        project, selected, library, cache_root / "voice", version_root
    )
    target_lufs, loudness_report = resolved_target_loudness(project, ffmpeg, ffprobe)
    manifest = []
    segment_cache_hits = []
    for segment in selected:
        full_index = project.segments.index(segment)
        previous_mode = project.segments[full_index - 1].mode if full_index > 0 else None
        next_mode = project.segments[full_index + 1].mode if full_index + 1 < len(project.segments) else None
        entry, cache_hit = render_segment(
            project, segment, full_index + 1, previous_mode, next_mode, voice_paths.get(segment.segment_id),
            target_lufs, version_root, cache_root, ffmpeg, ffprobe,
        )
        manifest.append(entry)
        if cache_hit:
            segment_cache_hits.append(segment.segment_id)
    manifest_path = version_root / "timeline_manifest.json"
    atomic_write_json(manifest_path, {"project_id": project.project_id, "version": project.current_version, "segments": manifest})
    if only_segment_id:
        return {
            "status": "ok", "project_id": project.project_id, "version": project.current_version,
            "affected_segments": [only_segment_id], "cache_hits": {"voice": voice_hits, "segments": segment_cache_hits},
            "output_paths": {"segment": manifest[0]["rendered_path"], "manifest": str(manifest_path)},
            "duration": manifest[0]["video_seconds"], "loudness": loudness_report, "warnings": [],
        }
    duplicate_path = version_root / "duplicate_report.json"
    duplicate_report = validate_rendered_visual_uniqueness(manifest, ffmpeg, duplicate_path)
    if duplicate_report["duplicates"]:
        return {
            "status": "duplicate_blocked", "project_id": project.project_id, "version": project.current_version,
            "affected_segments": [item.segment_id for item in project.segments],
            "cache_hits": {"voice": voice_hits, "segments": segment_cache_hits},
            "validation_errors": [], "duplicate_report": duplicate_report,
            "output_paths": {"manifest": str(manifest_path), "duplicate_report": str(duplicate_path)},
            "duration": sum(item["video_seconds"] for item in manifest), "loudness": loudness_report, "warnings": [],
        }
    total_seconds = sum(float(item["video_seconds"]) for item in manifest)
    video_master = version_root / "video_master_silent.mp4"
    narration_master = version_root / "audio" / "narration_master.wav"
    original_master = version_root / "audio" / "original_master.wav"
    complete_master = version_root / "audio" / "complete_audio_master.wav"
    output = version_root / ("final.mp4" if final else "preview.mp4")
    join_video_segments(manifest, video_master, ffmpeg, project)
    render_audio_stem(manifest, "narration", narration_master, total_seconds, ffmpeg)
    render_audio_stem(manifest, "original", original_master, total_seconds, ffmpeg)
    mix_audio_masters(narration_master, original_master, complete_master, total_seconds, ffmpeg)
    mux_master(video_master, complete_master, output, ffmpeg)
    decode_check(output, ffmpeg)
    outputs = {
        "video_master_silent": str(video_master), "narration_master": str(narration_master),
        "original_master": str(original_master), "complete_audio_master": str(complete_master),
        "final" if final else "preview": str(output), "manifest": str(manifest_path), "duplicate_report": str(duplicate_path),
    }
    actual_durations = {key: probe_audio_duration(Path(path), ffprobe) for key, path in outputs.items() if key in {"narration_master", "original_master", "complete_audio_master"}}
    actual_durations["video_master_silent"] = probe(video_master, ffprobe)["duration"]
    narration_loudness = measure_loudness(narration_master, ffmpeg, target_lufs) if any(item.mode == "narration" for item in project.segments) else None
    return {
        "status": "ok", "project_id": project.project_id, "version": project.current_version,
        "affected_segments": [item.segment_id for item in project.segments],
        "cache_hits": {"voice": voice_hits, "segments": segment_cache_hits}, "voice_generated": voice_generated,
        "validation_errors": [], "duplicate_report": duplicate_report, "output_paths": outputs,
        "duration": total_seconds, "master_durations": actual_durations,
        "loudness": {**loudness_report, "target_lufs": target_lufs, "narration_master": narration_loudness},
        "warnings": loudness_report.get("warnings", []),
    }
