from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import video_dedup

from .settings import GatewaySettings


def _check(name: str, ok: bool, detail: str, *, required: bool = True) -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "required": bool(required), "detail": detail}


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _binary(name: str) -> tuple[bool, str]:
    try:
        path = video_dedup.find_binary(name, None)
    except (OSError, ValueError) as exc:
        return False, str(exc)
    return True, str(path)


def run_preflight(settings: GatewaySettings, selected: dict[str, Any] | None = None) -> dict[str, Any]:
    """Perform a fast, non-mutating readiness check for the selected workflow."""
    selected = dict(selected or {})
    pipeline = dict(selected.get("pipeline") or selected)
    enable_subtitles = bool(pipeline.get("enable_subtitles"))
    enable_recap = bool(pipeline.get("enable_recap"))
    enable_dedup = bool(pipeline.get("enable_dedup"))
    subtitle_source = str(pipeline.get("subtitle_source") or "auto")
    hardware = str(pipeline.get("hardware_acceleration") or "auto")
    ocr_device = str(pipeline.get("ocr_device") or hardware)
    whisper_device = str(pipeline.get("whisper_device") or hardware)
    checks: list[dict[str, Any]] = []

    ffmpeg_ok, ffmpeg_detail = _binary("ffmpeg")
    ffprobe_ok, ffprobe_detail = _binary("ffprobe")
    checks.extend(
        (
            _check("ffmpeg", ffmpeg_ok, ffmpeg_detail, required=enable_subtitles or enable_recap or enable_dedup),
            _check("ffprobe", ffprobe_ok, ffprobe_detail, required=enable_subtitles or enable_recap or enable_dedup),
        )
    )
    preferred_python = settings.project_root / ".venv-ocr" / "Scripts" / "python.exe"
    python_path = preferred_python if preferred_python.is_file() else Path(sys.executable)
    checks.append(_check("python", python_path.is_file(), str(python_path)))

    try:
        usage = shutil.disk_usage(settings.storage_root)
        disk_ok = usage.free >= 5 * 1024**3
        checks.append(
            _check(
                "storage_free",
                disk_ok,
                f"{usage.free / 1024**3:.1f} GiB 可用（建议至少 5 GiB）",
            )
        )
    except OSError as exc:
        checks.append(_check("storage_free", False, str(exc)))

    writable_probe = settings.service_root / ".preflight-write-test"
    try:
        writable_probe.parent.mkdir(parents=True, exist_ok=True)
        writable_probe.write_text("ok", encoding="ascii")
        writable_probe.unlink(missing_ok=True)
        checks.append(_check("storage_writable", True, str(settings.storage_root)))
    except OSError as exc:
        checks.append(_check("storage_writable", False, str(exc)))

    needs_ocr = enable_subtitles and subtitle_source in {"auto", "auto-ocr", "ocr-asr", "hard-ocr"}
    needs_asr = enable_subtitles and subtitle_source in {"auto", "soft-asr", "ocr-asr", "asr"}
    if needs_ocr:
        easy = _module_available("easyocr")
        paddle = _module_available("paddleocr")
        checks.append(_check("ocr_runtime", easy or paddle, f"EasyOCR={easy}, PaddleOCR={paddle}"))
    if needs_asr:
        whisper = _module_available("faster_whisper")
        checks.append(_check("faster_whisper", whisper, "已安装" if whisper else "未安装 faster-whisper"))

    wants_cuda = (
        (hardware == "nvidia" and (enable_dedup or enable_recap))
        or (needs_ocr and ocr_device in {"cuda", "nvidia"})
        or (needs_asr and whisper_device in {"cuda", "nvidia"})
    )
    if wants_cuda:
        nvidia_smi = shutil.which("nvidia-smi")
        cuda_ok = False
        detail = "未找到 nvidia-smi"
        if nvidia_smi:
            try:
                result = subprocess.run(
                    [nvidia_smi, "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=8,
                    **video_dedup.hidden_subprocess_kwargs(),
                )
                cuda_ok = result.returncode == 0
                detail = (result.stdout or result.stderr).strip() or f"退出码 {result.returncode}"
            except (OSError, subprocess.TimeoutExpired) as exc:
                detail = str(exc)
        checks.append(_check("nvidia_cuda", cuda_ok, detail))

    required_failures = [item for item in checks if item["required"] and not item["ok"]]
    return {
        "ok": not required_failures,
        "checks": checks,
        "required_failures": [item["name"] for item in required_failures],
        "selected": {
            "subtitles": enable_subtitles,
            "recap": enable_recap,
            "dedup": enable_dedup,
            "subtitle_source": subtitle_source,
        },
    }
