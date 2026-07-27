#!/usr/bin/env python3
"""Standalone local video transformation tool powered by FFmpeg.

Use only with video that you own or are licensed to modify.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Sequence


VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
EFFECT_SUFFIXES = VIDEO_SUFFIXES | {".png", ".jpg", ".jpeg", ".webp"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}
_ORIGINAL_POPEN = subprocess.Popen
_HIDDEN_SUBPROCESS_POLICY_INSTALLED = False


def install_hidden_subprocess_policy() -> None:
    """Hide Windows consoles created by this process or imported ML libraries."""
    global _HIDDEN_SUBPROCESS_POLICY_INSTALLED
    if os.name != "nt" or _HIDDEN_SUBPROCESS_POLICY_INSTALLED:
        return

    class HiddenPopen(_ORIGINAL_POPEN):
        def __init__(self, *args, **kwargs):
            creationflags = int(kwargs.get("creationflags", 0) or 0)
            # Do not combine mutually exclusive caller-requested console modes.
            explicit_console = creationflags & (
                getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
            )
            if not explicit_console:
                startupinfo = kwargs.get("startupinfo")
                if startupinfo is None:
                    startupinfo = subprocess.STARTUPINFO()
                startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
                kwargs["startupinfo"] = startupinfo
                kwargs["creationflags"] = creationflags | subprocess.CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = HiddenPopen
    _HIDDEN_SUBPROCESS_POLICY_INSTALLED = True


def hidden_subprocess_kwargs() -> dict:
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
    return {"startupinfo": startup, "creationflags": subprocess.CREATE_NO_WINDOW}


@dataclass(frozen=True)
class TransformConfig:
    crop_percent: float = 2.0
    zoom_percent: float = 0.0
    mirror: bool = False
    speed: float = 1.0
    brightness: float = 0.01
    contrast: float = 1.01
    saturation: float = 1.01
    color: str | None = None
    color_opacity: float = 0.0
    fade_seconds: float = 0.0
    trim_start: float = 0.0
    trim_end: float = 0.0
    background_music: str | None = None
    background_music_dir: str | None = None
    music_volume: float = 0.08
    keep_audio: bool = True
    crf: int = 23
    preset: str = "medium"
    audio_bitrate: str = "192k"
    hardware_acceleration: str = "nvidia"
    blur_background: bool = False
    blur_sigma: float = 18.0
    foreground_scale: float = 0.96
    output_aspect: str = "source"
    target_fps: float = 0.0
    border_file: str | None = None
    enable_dynamic_effects: bool = False
    effect_file: str | None = None
    effect_dir: str | None = None
    effect_random_type: bool = True
    effect_timing: str = "random"
    effect_frequency: float = 3.0
    effect_duration: float = 2.5
    effect_opacity: float = 0.12
    effect_key_mode: str = "black"
    effect_position: str = "full"
    random_seed: int | None = None


_BUNDLED_SWEEP_EFFECT = str(Path(__file__).resolve().with_name("effects") / "14_lens_flare_cc0.mp4")


def production_strength_preset(
    *,
    crop: float,
    filter_opacity: float,
    music_volume: float,
    zoom: float,
    sweep_opacity: float,
    brightness: float,
    speed_percent: float,
    mirror: bool = False,
) -> TransformConfig:
    """Build one of the four production-strength transformation presets."""
    return TransformConfig(
        crop_percent=crop,
        zoom_percent=zoom,
        mirror=mirror,
        speed=1.0 + speed_percent / 100.0,
        brightness=brightness / 100.0,
        contrast=1.0,
        saturation=1.0,
        color="#00f9ff",
        color_opacity=filter_opacity / 100.0,
        music_volume=music_volume / 100.0,
        blur_background=True,
        blur_sigma=15.0,
        foreground_scale=1.0 - crop / 100.0,
        target_fps=25.0,
        enable_dynamic_effects=True,
        effect_file=_BUNDLED_SWEEP_EFFECT,
        effect_random_type=False,
        effect_timing="continuous",
        effect_opacity=sweep_opacity / 100.0,
        effect_key_mode="black",
        effect_position="full",
    )


PRESETS = {
    "light": production_strength_preset(
        crop=1, filter_opacity=5, music_volume=8, zoom=10,
        sweep_opacity=4, brightness=10, speed_percent=3,
    ),
    "medium": production_strength_preset(
        crop=2, filter_opacity=8, music_volume=15, zoom=15,
        sweep_opacity=6, brightness=14, speed_percent=6,
    ),
    "strong": production_strength_preset(
        crop=4, filter_opacity=10, music_volume=20, zoom=20,
        sweep_opacity=10, brightness=16, speed_percent=10,
    ),
    "deep": production_strength_preset(
        crop=6, filter_opacity=10, music_volume=20, zoom=30,
        sweep_opacity=12, brightness=16, speed_percent=12, mirror=True,
    ),
}


def find_binary(name: str, explicit: str | None = None) -> str:
    if explicit:
        path = Path(explicit)
        if path.is_file():
            return str(path)
        raise FileNotFoundError(f"找不到指定的 {name}: {path}")
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(f"找不到 {name}，请安装 FFmpeg 或使用 --ffmpeg 指定路径")


def available_hardware_encoders(ffmpeg: str) -> set[str]:
    result = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, encoding="utf-8", errors="replace", **hidden_subprocess_kwargs())
    text = result.stdout + result.stderr
    return {name for name in ("h264_nvenc", "h264_amf", "h264_qsv", "h264_videotoolbox") if name in text}


def resolve_hardware_acceleration(ffmpeg: str, preference: str) -> str:
    if preference not in {"auto", "nvidia", "amd", "intel", "apple", "cpu"}:
        raise ValueError("hardware_acceleration 必须是 auto、nvidia、amd、intel、apple 或 cpu")
    if preference == "cpu":
        return "cpu"
    available = available_hardware_encoders(ffmpeg)
    candidates = {
        "auto": (("nvidia", "h264_nvenc"), ("amd", "h264_amf"), ("intel", "h264_qsv"), ("apple", "h264_videotoolbox")),
        "nvidia": (("nvidia", "h264_nvenc"),),
        "amd": (("amd", "h264_amf"),),
        "intel": (("intel", "h264_qsv"),),
        "apple": (("apple", "h264_videotoolbox"),),
    }[preference]
    for mode, encoder in candidates:
        if encoder in available:
            return mode
    if preference == "auto":
        return "cpu"
    raise ValueError(f"当前 FFmpeg 不支持所选硬件编码模式: {preference}")


def video_encoder_name(mode: str) -> str:
    return {
        "nvidia": "h264_nvenc",
        "amd": "h264_amf",
        "intel": "h264_qsv",
        "apple": "h264_videotoolbox",
        "cpu": "libx264",
    }[mode]


def video_encoder_label(mode: str) -> str:
    return {
        "nvidia": "NVIDIA NVENC 显卡编码",
        "amd": "AMD AMF 显卡编码",
        "intel": "Intel Quick Sync 显卡编码",
        "apple": "Apple VideoToolbox 硬件编码",
        "cpu": "CPU libx264 编码",
    }[mode]


def probe_video(path: Path, ffprobe: str) -> dict:
    command = [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]
    result = subprocess.run(command, check=True, capture_output=True, text=True, encoding="utf-8", **hidden_subprocess_kwargs())
    data = json.loads(result.stdout)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    if not video:
        raise ValueError(f"文件不包含视频流: {path}")
    duration = float(data.get("format", {}).get("duration") or video.get("duration") or 0)
    return {
        "width": int(video["width"]),
        "height": int(video["height"]),
        "duration": duration,
        "has_audio": any(s.get("codec_type") == "audio" for s in data.get("streams", [])),
    }


def atempo_filters(speed: float) -> list[str]:
    values: list[float] = []
    remaining = speed
    while remaining > 2.0:
        values.append(2.0)
        remaining /= 2.0
    while remaining < 0.5:
        values.append(0.5)
        remaining /= 0.5
    values.append(remaining)
    return [f"atempo={value:.6f}" for value in values if abs(value - 1.0) > 1e-6]


def even(value: float) -> int:
    return max(2, int(value) // 2 * 2)


def output_dimensions(info: dict, mode: str) -> tuple[int, int]:
    if mode == "source":
        return even(info["width"]), even(info["height"])
    dimensions = {
        "portrait": (1080, 1920),
        "landscape": (1920, 1080),
        "square": (1080, 1080),
    }
    if mode not in dimensions:
        raise ValueError("output_aspect 必须是 source、portrait、landscape 或 square")
    return dimensions[mode]


def effect_candidates(config: TransformConfig) -> list[Path]:
    candidates: list[Path] = []
    if config.effect_file:
        path = Path(config.effect_file).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"找不到特效素材: {path}")
        if path.suffix.lower() not in EFFECT_SUFFIXES:
            raise ValueError(f"不支持的特效素材格式: {path.suffix or '(无扩展名)'}")
        candidates.append(path)
    if config.effect_dir:
        directory = Path(config.effect_dir).expanduser().resolve()
        if not directory.is_dir():
            raise FileNotFoundError(f"特效素材目录不存在: {directory}")
        candidates.extend(
            path for path in sorted(directory.rglob("*"))
            if path.is_file() and path.suffix.lower() in EFFECT_SUFFIXES
        )
    # Preserve order while removing a fixed file that is also inside the folder.
    return list(dict.fromkeys(candidates))


def plan_effect_events(config: TransformConfig, duration: float) -> list[tuple[Path, float, float]]:
    if not config.enable_dynamic_effects:
        return []
    candidates = effect_candidates(config)
    if not candidates:
        raise ValueError("已启用动态特效，但没有选择特效文件或包含素材的目录")
    if config.effect_timing not in {"random", "continuous"}:
        raise ValueError("effect_timing 必须是 random 或 continuous")
    if config.effect_frequency <= 0:
        raise ValueError("effect_frequency 必须大于 0")
    if config.effect_duration <= 0:
        raise ValueError("effect_duration 必须大于 0")
    rng = random.Random(config.random_seed)
    if config.effect_timing == "continuous":
        selected = rng.choice(candidates) if config.effect_random_type else candidates[0]
        return [(selected, 0.0, max(0.01, duration))]

    count = max(1, min(20, int(duration / 60.0 * config.effect_frequency + 0.999999)))
    event_duration = min(config.effect_duration, max(0.01, duration))
    slot = duration / count
    events: list[tuple[Path, float, float]] = []
    for index in range(count):
        earliest = index * slot
        latest = min(duration - event_duration, (index + 1) * slot - event_duration)
        start = earliest if latest <= earliest else rng.uniform(earliest, latest)
        selected = rng.choice(candidates) if config.effect_random_type else candidates[0]
        events.append((selected, max(0.0, start), event_duration))
    return events


def add_looping_input(command: list[str], path: Path) -> None:
    if path.suffix.lower() in IMAGE_SUFFIXES:
        command += ["-loop", "1", "-i", str(path)]
    else:
        command += ["-stream_loop", "-1", "-i", str(path)]


def base_video_filters(info: dict, config: TransformConfig) -> list[str]:
    filters: list[str] = []
    if config.zoom_percent:
        zoom = 1.0 + config.zoom_percent / 100.0
        filters.append(
            f"scale={even(info['width'] * zoom)}:{even(info['height'] * zoom)}:flags=lanczos"
        )
    if config.crop_percent and config.zoom_percent:
        # Match the observed preset structure: enlarge the source, then crop
        # a slightly smaller centered foreground before restoring canvas size.
        ratio = 1 - config.crop_percent / 100.0
        filters += [
            f"crop={even(info['width'] * ratio)}:{even(info['height'] * ratio)}:(iw-ow)/2:(ih-oh)/2",
            f"scale={info['width']}:{info['height']}:flags=lanczos",
            "setsar=1",
        ]
    elif config.crop_percent:
        ratio = 1 - config.crop_percent / 100.0 * 2
        filters += [
            f"crop=trunc(iw*{ratio:.6f}/2)*2:trunc(ih*{ratio:.6f}/2)*2",
            f"scale={info['width']}:{info['height']}:flags=lanczos",
            "setsar=1",
        ]
    if config.mirror:
        filters.append("hflip")
    filters.append(
        f"eq=brightness={config.brightness:.4f}:contrast={config.contrast:.4f}:"
        f"saturation={config.saturation:.4f}"
    )
    if config.color and config.color_opacity > 0:
        color = config.color.replace("#", "0x")
        filters.append(f"drawbox=x=0:y=0:w=iw:h=ih:color={color}@{config.color_opacity:.4f}:t=fill")
    if abs(config.speed - 1.0) > 1e-6:
        filters.append(f"setpts=PTS/{config.speed:.6f}")
    return filters


def effect_input_filter(
    input_index: int,
    label: str,
    width: int,
    height: int,
    start: float,
    duration: float,
    config: TransformConfig,
) -> str:
    if config.effect_position == "full":
        scale = (
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black@0"
        )
    elif config.effect_position in {"top", "bottom"}:
        scale = f"scale={width}:-2:force_original_aspect_ratio=decrease"
    else:
        raise ValueError("effect_position 必须是 full、top 或 bottom")
    key_filter = {
        "black": "colorkey=0x000000:0.18:0.10",
        "green": "chromakey=0x00FF00:0.18:0.08",
        "alpha": "null",
    }.get(config.effect_key_mode)
    if key_filter is None:
        raise ValueError("effect_key_mode 必须是 black、green 或 alpha")
    return (
        f"[{input_index}:v]trim=duration={duration:.3f},setpts=PTS-STARTPTS+{start:.3f}/TB,"
        f"{scale},format=rgba,{key_filter},colorchannelmixer=aa={config.effect_opacity:.4f}[{label}]"
    )


def build_command(input_path: Path, output_path: Path, info: dict, config: TransformConfig, ffmpeg: str) -> list[str]:
    if not 0 <= config.crop_percent < 45:
        raise ValueError("crop_percent 必须在 0 到 45 之间")
    if not 0 <= config.zoom_percent <= 100:
        raise ValueError("zoom_percent 必须在 0 到 100 之间")
    if not 0.25 <= config.speed <= 4.0:
        raise ValueError("speed 必须在 0.25 到 4.0 之间")
    if config.trim_start + config.trim_end >= info["duration"]:
        raise ValueError("首尾裁剪时长超过视频总时长")

    if not 0 <= config.effect_opacity <= 1:
        raise ValueError("effect_opacity 必须在 0 到 1 之间")
    if not 0 < config.foreground_scale <= 1:
        raise ValueError("foreground_scale 必须在 0 到 1 之间")
    if config.blur_sigma < 0:
        raise ValueError("blur_sigma 不能小于 0")
    if config.target_fps < 0:
        raise ValueError("target_fps 不能小于 0")

    command = [ffmpeg, "-hide_banner", "-y"]
    if config.trim_start:
        command += ["-ss", f"{config.trim_start:.3f}"]
    command += ["-i", str(input_path)]

    music = Path(config.background_music).resolve() if config.background_music else None
    input_indexes: dict[str, int] = {}
    next_input_index = 1
    if music:
        if not music.is_file():
            raise FileNotFoundError(f"找不到背景音乐: {music}")
        command += ["-stream_loop", "-1", "-i", str(music)]
        input_indexes["music"] = next_input_index
        next_input_index += 1

    output_duration = max(0.01, (info["duration"] - config.trim_start - config.trim_end) / config.speed)
    width, height = output_dimensions(info, config.output_aspect)
    effect_events = plan_effect_events(config, output_duration)
    effect_inputs: list[tuple[int, Path, float, float]] = []
    for path, start, duration in effect_events:
        add_looping_input(command, path)
        effect_inputs.append((next_input_index, path, start, duration))
        next_input_index += 1

    border = Path(config.border_file).expanduser().resolve() if config.border_file else None
    if border:
        if not border.is_file():
            raise FileNotFoundError(f"找不到边框素材: {border}")
        if border.suffix.lower() not in EFFECT_SUFFIXES:
            raise ValueError(f"不支持的边框素材格式: {border.suffix or '(无扩展名)'}")
        add_looping_input(command, border)
        input_indexes["border"] = next_input_index
        next_input_index += 1

    advanced_video = bool(
        config.blur_background
        or config.output_aspect != "source"
        or effect_inputs
        or border
    )
    linear_video_filters = base_video_filters(info, config)

    has_source_audio = info["has_audio"] and config.keep_audio
    audio_filters = atempo_filters(config.speed)
    if config.fade_seconds:
        fade = min(config.fade_seconds, output_duration / 3)
        audio_filters += [f"afade=t=in:st=0:d={fade:.3f}", f"afade=t=out:st={max(0, output_duration-fade):.3f}:d={fade:.3f}"]

    if advanced_video:
        graph: list[str] = []
        if config.blur_background:
            foreground_width = even(width * config.foreground_scale)
            foreground_height = even(height * config.foreground_scale)
            graph += [
                "[0:v]split=2[bgsrc][fgsrc]",
                f"[bgsrc]scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},gblur=sigma={config.blur_sigma:.3f}[bg]",
                f"[fgsrc]{','.join(linear_video_filters)},"
                f"scale={foreground_width}:{foreground_height}:force_original_aspect_ratio=decrease[fg]",
                "[bg][fg]overlay=(W-w)/2:(H-h)/2:format=auto[vbase]",
            ]
        else:
            graph.append(
                f"[0:v]{','.join(linear_video_filters)},"
                f"scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height}[vbase]"
            )

        current_video = "vbase"
        for event_index, (input_index, _path, start, duration) in enumerate(effect_inputs):
            effect_label = f"effect{event_index}"
            next_video = f"veffect{event_index}"
            graph.append(
                effect_input_filter(input_index, effect_label, width, height, start, duration, config)
            )
            y = "0" if config.effect_position != "bottom" else "H-h"
            graph.append(
                f"[{current_video}][{effect_label}]overlay=x=(W-w)/2:y={y}:eof_action=pass:shortest=0:"
                f"format=auto:enable='between(t,{start:.3f},{start + duration:.3f})'[{next_video}]"
            )
            current_video = next_video

        if border:
            border_index = input_indexes["border"]
            graph += [
                f"[{border_index}:v]scale={width}:{height},format=rgba[border]",
                f"[{current_video}][border]overlay=0:0:eof_action=repeat:shortest=0:format=auto[vborder]",
            ]
            current_video = "vborder"

        final_filters: list[str] = []
        if config.fade_seconds:
            fade = min(config.fade_seconds, output_duration / 3)
            final_filters += [
                f"fade=t=in:st=0:d={fade:.3f}",
                f"fade=t=out:st={max(0, output_duration-fade):.3f}:d={fade:.3f}",
            ]
        if config.target_fps > 0:
            final_filters.append(f"fps={config.target_fps:.3f}:round=up")
        final_filters += ["setsar=1", "format=yuv420p"]
        graph.append(f"[{current_video}]{','.join(final_filters)}[vout]")

        if music and has_source_audio:
            source_chain = ",".join(audio_filters) if audio_filters else "anull"
            graph += [
                f"[0:a]{source_chain}[a0]",
                f"[{input_indexes['music']}:a]volume={config.music_volume:.4f}[bgmusic]",
                "[a0][bgmusic]amix=inputs=2:duration=first:dropout_transition=2[aout]",
            ]
            audio_map = "[aout]"
        elif music:
            graph.append(f"[{input_indexes['music']}:a]volume={config.music_volume:.4f}[aout]")
            audio_map = "[aout]"
        elif has_source_audio and audio_filters:
            graph.append(f"[0:a]{','.join(audio_filters)}[aout]")
            audio_map = "[aout]"
        elif has_source_audio:
            audio_map = "0:a:0"
        else:
            audio_map = None
        command += ["-filter_complex", ";".join(graph), "-map", "[vout]"]
        if audio_map:
            command += ["-map", audio_map]
        else:
            command += ["-an"]
    else:
        if config.fade_seconds:
            fade = min(config.fade_seconds, output_duration / 3)
            linear_video_filters += [
                f"fade=t=in:st=0:d={fade:.3f}",
                f"fade=t=out:st={max(0, output_duration-fade):.3f}:d={fade:.3f}",
            ]
        if config.target_fps > 0:
            linear_video_filters.append(f"fps={config.target_fps:.3f}:round=up")
        command += ["-vf", ",".join(linear_video_filters)]
        if music and has_source_audio:
            source_chain = ",".join(audio_filters) if audio_filters else "anull"
            command += ["-filter_complex", f"[0:a]{source_chain}[a0];[1:a]volume={config.music_volume:.4f}[bg];[a0][bg]amix=inputs=2:duration=first:dropout_transition=2[a]", "-map", "0:v:0", "-map", "[a]"]
        elif music:
            command += ["-filter_complex", f"[1:a]volume={config.music_volume:.4f}[a]", "-map", "0:v:0", "-map", "[a]"]
        elif has_source_audio and audio_filters:
            command += ["-af", ",".join(audio_filters)]
        elif not has_source_audio:
            command += ["-an"]

    command += ["-t", f"{output_duration:.3f}"]
    if config.hardware_acceleration == "nvidia":
        command += ["-c:v", "h264_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr", "-cq", str(config.crf), "-b:v", "0"]
    elif config.hardware_acceleration == "amd":
        command += ["-c:v", "h264_amf", "-quality", "balanced", "-rc", "cqp", "-qp_i", str(config.crf), "-qp_p", str(config.crf)]
    elif config.hardware_acceleration == "intel":
        command += ["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", str(config.crf)]
    elif config.hardware_acceleration == "apple":
        command += ["-c:v", "h264_videotoolbox", "-b:v", "8M"]
    else:
        command += ["-c:v", "libx264", "-preset", config.preset, "-crf", str(config.crf)]
    command += [
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        "-metadata", f"comment=local-transform-{uuid.uuid4()}",
    ]
    if has_source_audio or music:
        command += ["-c:a", "aac", "-b:a", config.audio_bitrate]
    command += [str(output_path)]
    return command


def load_config(preset: str, config_file: str | None, seed: int | None) -> TransformConfig:
    config = PRESETS[preset]
    if config_file:
        values = json.loads(Path(config_file).read_text(encoding="utf-8-sig"))
        unknown = set(values) - set(asdict(config))
        if unknown:
            raise ValueError(f"未知配置项: {', '.join(sorted(unknown))}")
        config = replace(config, **values)
    if seed is not None:
        rng = random.Random(seed)
        config = replace(
            config,
            crop_percent=max(0, config.crop_percent + rng.uniform(-0.35, 0.35)),
            speed=max(0.25, config.speed + rng.uniform(-0.004, 0.004)),
            brightness=config.brightness + rng.uniform(-0.003, 0.003),
            random_seed=seed,
        )
    return config


def collect_inputs(source: Path) -> list[Path]:
    if source.is_file():
        if source.suffix.lower() not in VIDEO_SUFFIXES:
            raise ValueError(f"不支持的视频格式: {source.suffix or '(无扩展名)'}")
        return [source]
    if source.is_dir():
        return sorted(p for p in source.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES)
    raise FileNotFoundError(f"输入路径不存在: {source}")


def choose_background_music(config: TransformConfig, seed: int | None) -> TransformConfig:
    if config.background_music:
        return config
    if not config.background_music_dir:
        return config
    directory = Path(config.background_music_dir).expanduser().resolve()
    if not directory.is_dir():
        raise FileNotFoundError(f"背景音乐目录不存在: {directory}")
    candidates = sorted(p for p in directory.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_SUFFIXES)
    if not candidates:
        raise ValueError(f"背景音乐目录中没有支持的音频文件: {directory}")
    selected = random.Random(seed).choice(candidates)
    return replace(config, background_music=str(selected))


def process(args: argparse.Namespace) -> int:
    ffmpeg = find_binary("ffmpeg", args.ffmpeg)
    ffprobe = find_binary("ffprobe", args.ffprobe)
    config = load_config(args.preset, args.config, args.seed)
    if args.hardware_acceleration:
        config = replace(config, hardware_acceleration=args.hardware_acceleration)
    requested_acceleration = config.hardware_acceleration
    resolved_acceleration = resolve_hardware_acceleration(ffmpeg, requested_acceleration)
    config = replace(config, hardware_acceleration=resolved_acceleration)
    print(f"FFmpeg: {ffmpeg}")
    print(f"实际视频编码器: {video_encoder_name(resolved_acceleration)} ({video_encoder_label(resolved_acceleration)})")
    if requested_acceleration != resolved_acceleration:
        print(f"请求模式 {requested_acceleration} 已自动解析为 {resolved_acceleration}")
    if args.input_list:
        raw_inputs = json.loads(Path(args.input_list).read_text(encoding="utf-8-sig"))
        if not isinstance(raw_inputs, list):
            raise ValueError("input-list 必须是 JSON 路径数组")
        inputs = [Path(item).resolve() for item in raw_inputs]
        invalid = [str(item) for item in inputs if not item.is_file() or item.suffix.lower() not in VIDEO_SUFFIXES]
        if invalid:
            raise ValueError(f"无效的视频文件: {invalid[0]}")
    else:
        inputs = collect_inputs(Path(args.input).resolve())
    if not inputs:
        raise ValueError("输入目录中没有可处理的视频")
    output = Path(args.output).resolve()
    output_is_file = len(inputs) == 1 and output.suffix.lower() in VIDEO_SUFFIXES
    if not output_is_file:
        output.mkdir(parents=True, exist_ok=True)
    else:
        output.parent.mkdir(parents=True, exist_ok=True)

    for index, source in enumerate(inputs, 1):
        target = output if output_is_file else output / f"{source.stem}_local{source.suffix}"
        if source == target:
            raise ValueError("输出文件不能覆盖输入文件")
        info = probe_video(source, ffprobe)
        per_file = replace(config)
        if args.seed is not None and len(inputs) > 1:
            per_file = load_config(args.preset, args.config, args.seed + index - 1)
            if args.hardware_acceleration:
                per_file = replace(per_file, hardware_acceleration=args.hardware_acceleration)
            per_file = replace(per_file, hardware_acceleration=resolved_acceleration)
        music_seed = (args.seed + index - 1) if args.seed is not None else None
        per_file = choose_background_music(per_file, music_seed)
        if per_file.random_seed is None:
            per_file = replace(per_file, random_seed=random.SystemRandom().randrange(0, 2**31))
        command = build_command(source, target, info, per_file, ffmpeg)
        print(f"[{index}/{len(inputs)}] {source.name} -> {target.name}")
        if per_file.background_music:
            print(f"  背景音乐: {Path(per_file.background_music).name}")
        if per_file.blur_background:
            print(
                f"  模糊画中画: sigma={per_file.blur_sigma:g}, "
                f"主体比例={per_file.foreground_scale:.0%}, 画幅={per_file.output_aspect}"
            )
        if per_file.enable_dynamic_effects:
            events = plan_effect_events(per_file, max(0.01, (info["duration"] - per_file.trim_start - per_file.trim_end) / per_file.speed))
            names = ", ".join(dict.fromkeys(path.name for path, _start, _duration in events))
            print(f"  动态特效: {len(events)} 次，素材={names}")
        if per_file.border_file:
            print(f"  视频边框: {Path(per_file.border_file).name}")
        if args.dry_run:
            print(subprocess.list2cmdline(command))
        else:
            try:
                subprocess.run(command, check=True, **hidden_subprocess_kwargs())
            except subprocess.CalledProcessError:
                if resolved_acceleration == "cpu":
                    raise
                print("GPU 编码失败，自动回退到 CPU 重试…", file=sys.stderr)
                fallback = build_command(source, target, info, replace(per_file, hardware_acceleration="cpu"), ffmpeg)
                subprocess.run(fallback, check=True, **hidden_subprocess_kwargs())
    return 0


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="完全本地的批量视频变换工具")
    parser.add_argument("input", help="输入视频或视频目录")
    parser.add_argument("output", help="输出视频或输出目录")
    parser.add_argument("--preset", choices=PRESETS, default="medium", help="变换强度")
    parser.add_argument("--config", help="JSON 自定义配置；覆盖预设参数")
    parser.add_argument("--seed", type=int, help="加入可复现的轻微随机变化")
    parser.add_argument("--ffmpeg", help="ffmpeg 可执行文件路径")
    parser.add_argument("--ffprobe", help="ffprobe 可执行文件路径")
    parser.add_argument("--hardware-acceleration", choices=("auto", "nvidia", "amd", "intel", "apple", "cpu"), help="覆盖配置文件中的硬件编码模式")
    parser.add_argument("--dry-run", action="store_true", help="仅打印命令，不执行")
    parser.add_argument("--input-list", help="JSON 文件路径数组；用于界面任意多选")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    install_hidden_subprocess_policy()
    try:
        return process(make_parser().parse_args(argv))
    except (FileNotFoundError, ValueError, json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
