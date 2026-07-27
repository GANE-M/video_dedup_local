from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable

import video_dedup

from .loudness import duration_weighted_loudness, measure_loudness, normalize_narration
from .models import RecapProject, RecapSegment, natural_path_key, stable_hash
from .narration_text import (
    canonical_language,
    normalize_narration_text,
    split_caption_sentences,
    wrap_caption,
)
from .pacing import fitted_narration_seconds, get_preset
from .tts_routing import resolve_tts_engine as resolve_language_tts_engine
from .project_store import atomic_write_json
from .timeline import validate_source_intervals
from .visual_dedup import validate_rendered_visual_uniqueness
from .voice_library import VoiceLibrary, voice_cache_key, voice_cache_path


RENDER_CACHE_VERSION = 1
VIDEO_SUFFIXES = video_dedup.VIDEO_SUFFIXES
_CANCELLED: ContextVar[Callable[[], bool] | None] = ContextVar("recap_cancelled", default=None)
_PROCESS_OBSERVER: ContextVar[Callable[[int | None, str, float | None], None] | None] = ContextVar(
    "recap_process_observer", default=None
)


class RenderCancelled(RuntimeError):
    pass


def _terminate_tree(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            **video_dedup.hidden_subprocess_kwargs(),
        )
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)


def _run(
    command: list[str],
    *,
    text: bool = False,
    env: dict[str, str] | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess:
    process_kwargs = video_dedup.hidden_subprocess_kwargs()
    if os.name != "nt":
        process_kwargs["start_new_session"] = True
    process = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=text,
        encoding="utf-8" if text else None, errors="replace" if text else None,
        env=env, cwd=cwd, **process_kwargs,
    )
    observer = _PROCESS_OBSERVER.get()
    if observer:
        observer(process.pid, str(Path(command[0]).resolve()), time.time())
    stdout = stderr = None
    try:
        while process.poll() is None:
            cancelled = _CANCELLED.get()
            if cancelled and cancelled():
                _terminate_tree(process)
                process.communicate()
                raise RenderCancelled("解说渲染已由用户停止")
            try:
                stdout, stderr = process.communicate(timeout=0.25)
            except subprocess.TimeoutExpired:
                continue
        if stdout is None and stderr is None:
            stdout, stderr = process.communicate()
    finally:
        if observer:
            observer(None, "", None)
    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
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
    files = sorted(
        (path for path in root.glob(pattern) if path.suffix.casefold() in VIDEO_SUFFIXES),
        key=natural_path_key,
    )
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


def resolved_target_loudness(project: RecapProject, ffmpeg: str, ffprobe: str) -> tuple[float | None, dict[str, Any]]:
    configured = project.narration_target_loudness
    if isinstance(configured, (int, float)):
        value = float(configured)
        return value, {"status": "configured", "integrated_lufs": value, "episodes": [], "warnings": []}
    text = str(configured).strip().casefold()
    if text in {"", "keep", "keep_original", "preserve", "none", "off"}:
        return None, {
            "status": "preserved",
            "mode": "keep_original",
            "integrated_lufs": None,
            "episodes": [],
            "warnings": [],
        }
    try:
        return float(text), {"status": "configured", "integrated_lufs": float(text), "episodes": [], "warnings": []}
    except ValueError:
        if text not in {"match_source", "match_source_program", "auto"}:
            raise ValueError("narration_target_loudness must be keep_original, a LUFS number, or match_source_program")
    report = measure_project_loudness(project, ffmpeg, ffprobe)
    return float(report["integrated_lufs"]), report


def _qwen_python(project: RecapProject) -> Path:
    configured = str(project.rendering.get("qwen_python", "")).strip()
    if configured:
        return Path(configured).expanduser().resolve()
    candidate = Path(__file__).resolve().parents[2] / ".qwen-tts-lab" / ".venv" / "Scripts" / "python.exe"
    return candidate.resolve() if candidate.is_file() else Path(sys.executable).resolve()


def resolve_tts_engine(project: RecapProject) -> str:
    return resolve_language_tts_engine(project.target_language, project.tts_engine)


def _tts_python(project: RecapProject, engine: str) -> Path:
    root = Path(__file__).resolve().parents[2]
    configured = str(project.rendering.get(f"{engine}_python", "")).strip()
    if configured:
        return Path(configured).expanduser().resolve()
    candidates = {
        "fish_s2": root / ".tts-envs" / "fish-s2" / "Scripts" / "python.exe",
        "chatterbox_v3": root / ".tts-envs" / "chatterbox" / "Scripts" / "python.exe",
        "qwen3_tts": root / ".qwen-tts-lab" / ".venv" / "Scripts" / "python.exe",
    }
    candidate = candidates[engine]
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
    engine = resolve_tts_engine(project)
    if profile.languages and canonical_language(project.target_language) not in {
        canonical_language(item) for item in profile.languages
    }:
        raise ValueError(f"声纹 {profile.display_name} 不支持目标语言 {project.target_language}")
    if profile.allowed_engines and engine not in profile.allowed_engines:
        raise ValueError(f"声纹 {profile.display_name} 不支持 {engine}")
    errors = library.validate_assets(profile.voice_id)
    if errors:
        raise FileNotFoundError("; ".join(errors))
    outputs: dict[str, Path] = {}
    hits: list[str] = []
    missing_items = []
    for segment in segments:
        if segment.mode != "narration":
            continue
        normalized_text = normalize_narration_text(segment.narration_text, project.target_language)
        key = voice_cache_key(
            profile,
            normalized_text,
            canonical_language(project.target_language),
            project.narration_speed,
            {"tts_engine": engine, **profile.generation_parameters},
            reference_audio_path=library.resolve_asset(profile.reference_audio),
        )
        target = voice_cache_path(cache_root, project.project_id, profile, key)
        outputs[segment.segment_id] = target
        if target.is_file():
            hits.append(segment.segment_id)
        else:
            missing_items.append({
                "segment_id": segment.segment_id, "text": normalized_text,
                "language": canonical_language(project.target_language), "output": str(target),
            })
    if missing_items:
        tts_python = _tts_python(project, engine)
        if not tts_python.is_file():
            raise FileNotFoundError(f"{engine} Python does not exist: {tts_python}")
        payload_dir.mkdir(parents=True, exist_ok=True)
        payload_path = payload_dir / f"{engine}_voice_request.json"
        payload = {
            "profile": {
                **profile.to_dict(),
                "reference_audio": str(library.resolve_asset(profile.reference_audio)),
            },
            "items": missing_items,
        }
        module = {
            "fish_s2": "recap.fish_s2_tts",
            "chatterbox_v3": "recap.chatterbox_tts",
            "qwen3_tts": "recap.qwen_tts",
        }[engine]
        if engine == "fish_s2":
            root = Path(__file__).resolve().parents[2]
            payload["checkpoint"] = str(
                Path(project.rendering.get("fish_s2_checkpoint") or root / ".model-cache" / "fish-s2-pro-nf4").resolve()
            )
            payload["max_seq_len"] = int(project.rendering.get("fish_s2_max_seq_len", 4096))
            for key in ("temperature", "top_p", "top_k"):
                if key in profile.generation_parameters:
                    payload[key] = profile.generation_parameters[key]
        atomic_write_json(payload_path, payload)
        env = os.environ.copy()
        project_root = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = project_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        if engine == "chatterbox_v3" and bool(project.rendering.get("chatterbox_offline", True)):
            # The model is installed locally with the application. Avoid a slow
            # Hugging Face connectivity check on every folder task.
            env["HF_HUB_OFFLINE"] = "1"
        result = _run([str(tts_python), "-m", module, str(payload_path)], text=True, env=env)
        response = json.loads(result.stdout.strip().splitlines()[-1])
        if response.get("status") != "ok":
            raise RuntimeError(f"{engine} voice generation failed: {response}")
    return outputs, hits, [item["segment_id"] for item in missing_items]


def _escape_filter_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def _caption_filters(text: str, caption_dir: Path, segment_id: str, voice_length: float, lead_in: float, project: RecapProject) -> list[str]:
    sentences = split_caption_sentences(text, project.target_language)
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
        text_file.write_text(wrap_caption(sentence, width=wrap_width), encoding="utf-8")
        # FFmpeg's filter parser cannot reliably represent an apostrophe inside
        # a single-quoted textfile path. Run FFmpeg with the caption directory
        # as its cwd and keep only the safe basename in the filter graph.
        textfile_filter_value = text_file.name
        filters.append(
            f"drawtext=fontfile='{_escape_filter_path(Path(font_file))}':textfile='{textfile_filter_value}':"
            f"text_shaping=1:fontcolor=white:fontsize={font_size}:line_spacing=8:borderw=4:bordercolor=black@0.85:"
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
    preference = str(project.rendering.get("hardware_acceleration", "nvidia"))
    try:
        mode = video_dedup.resolve_hardware_acceleration(ffmpeg, preference)
    except ValueError:
        mode = "cpu"
    encoder = video_dedup.video_encoder_name(mode)
    if encoder == "libx264":
        return ["-c:v", encoder, "-preset", str(project.rendering.get("encoder_preset", "medium")), "-crf", str(project.rendering.get("crf", 23))]
    return ["-c:v", encoder, "-preset", "p5", "-cq", str(project.rendering.get("crf", 23)), "-b:v", "0"]


def render_segment(
    project: RecapProject, segment: RecapSegment, index: int, previous_mode: str | None,
    next_mode: str | None, voice_path: Path | None, target_lufs: float | None,
    work_root: Path, cache_root: Path, ffmpeg: str, ffprobe: str,
) -> tuple[dict[str, Any], bool]:
    source = project.episode_path(segment.episode)
    info = probe(source, ffprobe)
    pacing_preset = get_preset(project.narration_preset, default="legacy")
    if project.narration_preset == "legacy":
        default_lead_in = 0.5 if previous_mode == "original" else 0.0
    else:
        default_lead_in = pacing_preset.lead_in_seconds
    lead_in = float(
        segment.rendering.get(
            "narration_lead_in",
            project.rendering.get("narration_lead_in", default_lead_in),
        )
    )
    audio_tail = float(segment.rendering.get("original_audio_tail", project.rendering.get("original_audio_tail", 0.5))) if next_mode == "narration" else 0.0
    voice_identity = ""
    voice_length = 0.0
    tail_seconds = 0.0
    fit_policy = ""
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
        "project_rendering": project.rendering,
        "narration_preset": project.narration_preset,
        "voice": voice_identity,
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
        if target_lufs is None:
            normalized_voice = voice_path
            loudness = {"status": "preserved", "mode": "keep_original"}
        else:
            normalized_voice = work_root / "voice" / f"{segment.segment_id}.wav"
            loudness = normalize_narration(voice_path, normalized_voice, ffmpeg, target_lufs)
        raw_voice_seconds = probe_audio_duration(normalized_voice, ffprobe)
        speed = max(0.5, min(2.0, float(project.narration_speed)))
        voice_length = raw_voice_seconds / speed
        fit_policy = str(
            segment.rendering.get(
                "narration_fit_policy",
                project.rendering.get(
                    "narration_fit_policy", pacing_preset.fit_policy
                ),
            )
        ).strip().casefold()
        tail_seconds = max(
            0.0,
            float(
                segment.rendering.get(
                    "narration_tail_seconds",
                    project.rendering.get(
                        "narration_tail_seconds", pacing_preset.tail_seconds
                    ),
                )
            ),
        )
        hold_visual = bool(segment.rendering.get("allow_visual_hold", False))
        # A narration interval is a pool of candidate visuals, not a mandatory
        # silent hold. Keep the full interval only for legacy projects or an
        # explicitly justified visual hold.
        video_seconds = fitted_narration_seconds(
            planned_seconds=base_duration,
            voice_seconds=voice_length,
            lead_in_seconds=lead_in,
            tail_seconds=tail_seconds,
            fit_policy=(
                "preserve_window"
                if project.narration_preset == "legacy"
                else fit_policy
            ),
            hold_visual=hold_visual,
        )
        caption_dir = work_root / "captions"
        captions = _caption_filters(
            segment.narration_text, caption_dir, segment.segment_id, voice_length, lead_in, project
        )
        vf = ",".join([common_vf, *captions, f"trim=duration={video_seconds:.3f}", "setpts=PTS-STARTPTS"])
        audio_chain = [f"atempo={speed:.6f}"] if abs(speed - 1.0) > 1e-6 else []
        if target_lufs is not None:
            audio_chain.append("alimiter=limit=0.85:level=false")
        audio_chain.extend([f"adelay={int(lead_in * 1000)}:all=1", f"apad=pad_dur={video_seconds:.3f}", f"atrim=duration={video_seconds:.3f}", "asetpts=PTS-STARTPTS"])
        command = [
            ffmpeg, "-y", "-hide_banner", "-stream_loop", "-1", "-ss", f"{segment.source_start:.3f}", "-t", f"{video_seconds:.3f}", "-i", str(source), "-i", str(normalized_voice),
            "-filter_complex", f"[0:v]{vf}[v];[1:a]{','.join(audio_chain)}[a]", "-map", "[v]", "-map", "[a]",
            *encoder_args, "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "44100", "-ac", "2", "-b:a", "192k", str(rendered),
        ]
        _run(command, cwd=caption_dir)
        segment.rendering["last_loudness"] = loudness

    cached.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(rendered, cached)
    segment.cache_key = cache_key
    return ({
        "segment_id": segment.segment_id, "episode": segment.episode, "mode": segment.mode,
        "source": str(source), "source_start": segment.source_start, "source_end": segment.source_end,
        "video_seconds": video_seconds, "voice_seconds": voice_length,
        "planned_video_seconds": base_duration,
        "narration_lead_in_seconds": lead_in if segment.mode == "narration" else 0.0,
        "narration_tail_seconds": tail_seconds if segment.mode == "narration" else 0.0,
        "narration_fit_policy": fit_policy if segment.mode == "narration" else "",
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
    cancelled: Callable[[], bool] | None = None,
    process_observer: Callable[[int | None, str, float | None], None] | None = None,
) -> dict[str, Any]:
    token = _CANCELLED.set(cancelled)
    process_token = _PROCESS_OBSERVER.set(process_observer)
    try:
        return _render_project(project, final, only_segment_id, ffmpeg, ffprobe, library_path)
    finally:
        _PROCESS_OBSERVER.reset(process_token)
        _CANCELLED.reset(token)


def _render_project(
    project: RecapProject,
    final: bool,
    only_segment_id: str | None,
    ffmpeg: str | None,
    ffprobe: str | None,
    library_path: Path | None,
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
    narration_loudness = (
        measure_loudness(narration_master, ffmpeg, target_lufs if target_lufs is not None else -18.0)
        if any(item.mode == "narration" for item in project.segments)
        else None
    )
    return {
        "status": "ok", "project_id": project.project_id, "version": project.current_version,
        "affected_segments": [item.segment_id for item in project.segments],
        "cache_hits": {"voice": voice_hits, "segments": segment_cache_hits}, "voice_generated": voice_generated,
        "validation_errors": [], "duplicate_report": duplicate_report, "output_paths": outputs,
        "duration": total_seconds, "master_durations": actual_durations,
        "loudness": {**loudness_report, "target_lufs": target_lufs, "narration_master": narration_loudness},
        "warnings": loudness_report.get("warnings", []),
    }
