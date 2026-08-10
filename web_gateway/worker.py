from __future__ import annotations

import dataclasses
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import video_dedup
from recap.pacing import normalize_preset

from .agent_http import RemoteAgentBridge
from .database import GatewayDatabase, now_iso
from .publishing_materials import build_publishing_materials
from .publishing_service import PublishingService
from .recap_service import RecapService
from .settings import GatewaySettings
from .storage import JobPaths, JobStorage
from .security import normalize_public_recap_rendering, validate_public_http_url
from .workflows import (
    DEDUP_STAGE,
    RECAP_PLANNING_STAGE,
    PUBLISHING_PLANNING_STAGE,
    PUBLISHING_RENDER_STAGE,
    SUBTITLE_STAGE,
    WorkflowCheckpointStore,
    WorkflowPlan,
)


PIPELINE_CHOICES = {
    "preset": set(video_dedup.PRESETS),
    "hardware_acceleration": {"auto", "nvidia", "amd", "intel", "apple", "cpu"},
    "translation_backend": {"agent", "api"},
    "translation_quality": {"fast", "advanced"},
    "localization_strategy": {"cinematic_standard", "conversational", "formal_faithful", "gulf_neutral"},
    "subtitle_source": {"auto", "auto-ocr", "soft-asr", "ocr-asr", "soft", "hard-ocr", "asr"},
    "ocr_device": {"auto", "cuda", "cpu"},
    "whisper_device": {"auto", "cuda", "cpu"},
    "subtitle_mode": {"burn", "soft"},
    "subtitle_layout": {"replace", "bilingual"},
    "subtitle_position": {"auto", "bottom", "above-original", "top"},
    "cover_mode": {"blur", "color"},
    "publishing_language": {"auto", "English", "Arabic"},
}

PIPELINE_DEFAULTS: dict[str, Any] = {
    "preset": "custom",
    "seed": 2026,
    "hardware_acceleration": "nvidia",
    "enable_dedup": True,
    "enable_subtitles": True,
    "enable_recap": False,
    "enable_publishing": False,
    "publishing_language": "auto",
    "translation_backend": "agent",
    "translation_quality": "fast",
    "localization_strategy": "cinematic_standard",
    "subtitle_source": "ocr-asr",
    "target_language": "Arabic",
    "source_language": "auto",
    "ocr_language": "auto",
    "ocr_device": "cuda",
    "whisper_model": "medium",
    "whisper_device": "cuda",
    "subtitle_mode": "burn",
    "subtitle_layout": "bilingual",
    "subtitle_position": "auto",
    "subtitle_cover": False,
    "cover_auto_detect": False,
    "cover_x_percent": 0.0,
    "cover_y_percent": 74.0,
    "cover_width_percent": 100.0,
    "cover_height_percent": 11.0,
    "cover_opacity": 0.82,
    "cover_color": "white",
    "cover_mode": "blur",
    "cover_blur_sigma": 22.0,
    "font_name": "Arial",
    "font_size": 28,
    "force_subtitle_translation": False,
    "glossary_name": None,
    "requested_workers": 5,
    "llm_base_url": "https://theruta.ai/api/v1/chat/completions",
    "llm_model": "deepseek-v4-flash",
    "enable_llm_review": True,
    "llm_review_model": "deepseek-v4-flash",
    "review_confidence_threshold": 0.82,
}

LOCAL_PATH_VIDEO_FIELDS = {"background_music", "background_music_dir", "border_file", "effect_file", "effect_dir"}
RECAP_DEFAULTS: dict[str, Any] = {
    "project_name": "",
    "episode_pattern": "*.mp4",
    "target_language": "",
    "target_duration_seconds": None,
    "narration_preset": "standard",
    "voice_id": "",
    "tts_engine": "auto",
    "narration_speed": 1.0,
    "narration_target_loudness": "keep_original",
    "rendering": {
        "hardware_acceleration": "nvidia",
        "crf": 23,
        "caption_y_percent": 12.0,
        "caption_font_size": 38,
    },
}


def normalize_recap_settings(value: dict[str, Any] | None, pipeline: dict[str, Any]) -> dict[str, Any]:
    submitted = dict(value or {})
    unknown = set(submitted) - set(RECAP_DEFAULTS)
    if unknown:
        raise ValueError(f"未知解说配置项: {sorted(unknown)}")
    recap = {**RECAP_DEFAULTS, **submitted}
    recap["rendering"] = normalize_public_recap_rendering(
        submitted.get("rendering"), defaults=RECAP_DEFAULTS["rendering"]
    )
    target_language = str(recap.get("target_language") or pipeline["target_language"]).strip()
    recap["target_language"] = target_language
    recap["narration_preset"] = normalize_preset(recap.get("narration_preset"))
    recap["project_name"] = str(recap.get("project_name") or "").strip()
    recap["episode_pattern"] = str(recap.get("episode_pattern") or "*.mp4").strip()
    if not recap["episode_pattern"] or any(char in recap["episode_pattern"] for char in "\r\n\0"):
        raise ValueError("无效的解说视频匹配规则")
    default_voice = "ar_fish_female" if target_language.casefold() in {"arabic", "阿拉伯语", "ar"} else "calm_female"
    recap["voice_id"] = str(recap.get("voice_id") or default_voice).strip()
    recap["tts_engine"] = str(recap.get("tts_engine") or "auto").strip().casefold()
    recap["narration_speed"] = float(recap.get("narration_speed", 1.0))
    if not 0.5 <= recap["narration_speed"] <= 2.0:
        raise ValueError("解说语速必须在 0.5 到 2.0 之间")
    requested_duration = recap.get("target_duration_seconds")
    recap["target_duration_seconds"] = None if requested_duration in (None, "") else float(requested_duration)
    if recap["target_duration_seconds"] is not None and not 0 < recap["target_duration_seconds"] <= 7200:
        raise ValueError("解说目标时长必须在 0 到 7200 秒之间")
    return recap


def normalize_settings(value: dict[str, Any] | None, *, trusted_normalized: bool = False) -> dict[str, Any]:
    submitted = dict(value or {})
    pipeline = {**PIPELINE_DEFAULTS, **dict(submitted.get("pipeline") or {})}
    for key in (
        "enable_dedup", "enable_subtitles", "enable_recap", "enable_publishing",
        "subtitle_cover", "cover_auto_detect", "force_subtitle_translation",
        "enable_llm_review",
    ):
        if not isinstance(pipeline.get(key), bool):
            raise ValueError(f"{key} 必须是布尔值")
    for key, choices in PIPELINE_CHOICES.items():
        if pipeline.get(key) not in choices:
            raise ValueError(f"无效设置 {key}={pipeline.get(key)!r}")
    pipeline["seed"] = int(pipeline["seed"])
    pipeline["requested_workers"] = max(1, min(10, int(pipeline["requested_workers"])))
    pipeline["font_size"] = max(8, min(160, int(pipeline["font_size"])))
    for key in (
        "target_language", "source_language", "ocr_language", "font_name", "llm_model", "llm_review_model",
    ):
        text = str(pipeline.get(key) or "").strip()
        if not text or len(text) > 80 or any(ord(char) < 32 for char in text):
            raise ValueError(f"无效文本设置: {key}")
        pipeline[key] = text
    base_url = str(pipeline.get("llm_base_url") or "").strip()
    parsed_url = urlparse(base_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc or len(base_url) > 500:
        raise ValueError("无效的 LLM 接口地址")
    pipeline["llm_base_url"] = validate_public_http_url(base_url, resolve_dns=False)
    cover_color = str(pipeline.get("cover_color") or "").strip()
    if not re.fullmatch(r"(?:#[0-9a-fA-F]{6}|[A-Za-z]{3,24})", cover_color):
        raise ValueError("cover_color 只允许英文颜色名或 #RRGGBB")
    pipeline["cover_color"] = cover_color
    glossary_name = pipeline.get("glossary_name")
    if glossary_name not in (None, ""):
        glossary_name = str(glossary_name).strip()
        if Path(glossary_name).name != glossary_name or not glossary_name.casefold().endswith(".json"):
            raise ValueError("glossary_name 只允许 glossaries 目录内的 JSON 文件名")
        pipeline["glossary_name"] = glossary_name
    for key in (
        "cover_x_percent", "cover_y_percent", "cover_width_percent", "cover_height_percent",
        "cover_opacity", "cover_blur_sigma",
    ):
        pipeline[key] = float(pipeline[key])
    for key in ("cover_x_percent", "cover_y_percent", "cover_width_percent", "cover_height_percent"):
        if not 0.0 <= pipeline[key] <= 100.0:
            raise ValueError(f"{key} 必须在0到100之间")
    if not 0.0 <= pipeline["cover_opacity"] <= 1.0:
        raise ValueError("cover_opacity 必须在0到1之间")
    if not 0.0 <= pipeline["cover_blur_sigma"] <= 100.0:
        raise ValueError("cover_blur_sigma 必须在0到100之间")
    pipeline["review_confidence_threshold"] = float(pipeline["review_confidence_threshold"])
    if not 0.0 <= pipeline["review_confidence_threshold"] <= 1.0:
        raise ValueError("review_confidence_threshold 必须在0到1之间")
    if pipeline["translation_quality"] == "advanced" and pipeline["translation_backend"] != "agent":
        raise ValueError("高级翻译只能使用 Agent 模式")
    if not any((
        pipeline["enable_subtitles"], pipeline["enable_recap"],
        pipeline["enable_dedup"], pipeline["enable_publishing"],
    )):
        raise ValueError("请至少启用字幕、解说、发布物料或去重中的一个阶段")
    preset = pipeline["preset"]
    video_config = dataclasses.asdict(video_dedup.PRESETS[preset])
    allowed_video_fields = {field.name for field in dataclasses.fields(video_dedup.TransformConfig)}
    for key, item in dict(submitted.get("video_config") or {}).items():
        if key not in allowed_video_fields:
            raise ValueError(f"未知视频配置项: {key}")
        if key in LOCAL_PATH_VIDEO_FIELDS and item not in (None, "") and not (
            trusted_normalized and submitted.get("_gateway_normalized") is True
        ):
            raise ValueError(f"远程任务不能提交本机路径配置: {key}")
        video_config[key] = item
    recap = normalize_recap_settings(submitted.get("recap"), pipeline)
    return {"_gateway_normalized": True, "pipeline": pipeline, "video_config": video_config, "recap": recap}


def hidden_subprocess_kwargs() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {"startupinfo": startup, "creationflags": subprocess.CREATE_NO_WINDOW}


class GatewayWorker:
    def __init__(
        self,
        settings: GatewaySettings,
        database: GatewayDatabase,
        storage: JobStorage,
        *,
        publishing: PublishingService | None = None,
    ):
        self.settings = settings
        self.database = database
        self.storage = storage
        self.remote_agent = RemoteAgentBridge(database, storage, settings.project_root)
        self.recap = RecapService(storage)
        self.publishing = publishing or PublishingService(storage)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._current_process: subprocess.Popen[str] | None = None
        self._current_job_id: str | None = None
        self._lease_handle = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._acquire_worker_lease()
        try:
            for orphan in self.database.interrupted_processes():
                self._terminate_recorded_process(orphan)
            recovered = self.database.recover_interrupted_jobs()
            if recovered:
                print(f"[Web Gateway] 已将 {recovered} 个异常中断任务标记为失败，未自动重复编码。")
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="video-gateway-worker", daemon=True)
            self._thread.start()
        except Exception:
            self._release_worker_lease()
            raise

    def stop(self) -> None:
        self._stop.set()
        if self._current_process and self._current_process.poll() is None:
            self._terminate_process(self._current_process)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        self._release_worker_lease()

    def _acquire_worker_lease(self) -> None:
        path = self.settings.service_root / "gateway-worker.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+b", buffering=0)
        if path.stat().st_size == 0:
            handle.write(b"0")
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise RuntimeError("已有另一个网页处理Worker正在运行，拒绝重复启动") from exc
        self._lease_handle = handle

    def _release_worker_lease(self) -> None:
        handle, self._lease_handle = self._lease_handle, None
        if handle is None:
            return
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                job = self.database.claim_next_job()
                if job:
                    try:
                        self._run_job(job)
                    except Exception as exc:
                        error = f"启动或执行任务失败: {type(exc).__name__}: {exc}"
                        self.database.set_job_status(
                            job["id"], "failed", error=error, completed_at=now_iso(), process_pid=None
                        )
                        self.database.add_event(job["id"], error, level="error")
                        traceback.print_exc()
                    continue
            except Exception:
                traceback.print_exc()
            self._stop.wait(1.0)

    def _python_executable(self) -> str:
        preferred = self.settings.project_root / ".venv-ocr" / "Scripts" / "python.exe"
        return str(preferred if preferred.is_file() else Path(sys.executable))

    def _command(
        self,
        job: dict[str, Any],
        paths: JobPaths,
        bridge_root: Path | None,
        normalized_settings: dict[str, Any] | None = None,
        checkpoint_stage: str = "batch",
    ) -> list[str]:
        settings = normalized_settings or normalize_settings(job["settings"], trusted_normalized=True)
        pipeline = settings["pipeline"]
        video_config = dict(settings["video_config"])
        uploads = self.database.list_uploads(job["id"])
        role_files: dict[str, list[Path]] = {}
        for upload in uploads:
            role = str(upload.get("role") or "video")
            if role == "video":
                continue
            if role == "subtitle_final":
                continue
            candidate = paths.assets / role / upload["stored_name"]
            if candidate.is_file():
                role_files.setdefault(role, []).append(candidate)
        if role_files.get("music"):
            video_config["background_music"] = str(role_files["music"][0])
        elif role_files.get("music_pool"):
            video_config["background_music_dir"] = str(paths.assets / "music_pool")
        if role_files.get("border"):
            video_config["border_file"] = str(role_files["border"][0])
        if role_files.get("effect"):
            video_config["effect_file"] = str(role_files["effect"][0])
        elif role_files.get("effect_pool"):
            video_config["effect_dir"] = str(paths.assets / "effect_pool")
        config_path = self.storage.write_json(paths.config / "video-config.json", video_config)
        self.storage.write_json(paths.config / "job-settings.json", settings)
        subtitles = bool(pipeline["enable_subtitles"])
        if pipeline["enable_recap"] and pipeline["enable_dedup"]:
            raise ValueError("组合任务暂不允许在解说成片前执行去重；请先完成字幕与解说，再对最终解说成片单独去重")
        if not subtitles and not pipeline["enable_dedup"]:
            raise ValueError("普通批处理至少需要启用字幕或去重阶段")
        server_limit = self.settings.maximum_subtitle_workers if subtitles else self.settings.maximum_video_workers
        workers = min(server_limit, int(pipeline["requested_workers"]))
        command = [
            self._python_executable(), "-u", str(self.settings.project_root / "batch_pipeline.py"),
            str(paths.input), str(paths.videos),
            "--preset", str(pipeline["preset"]),
            "--config", str(config_path),
            "--seed", str(pipeline["seed"]),
            "--hardware-acceleration", str(pipeline["hardware_acceleration"]),
            "--video-workers", str(workers),
            "--translation-log-dir", str(paths.logs / "translation-records"),
            "--checkpoint-dir", str(paths.root / "checkpoints" / "batch" / checkpoint_stage),
        ]
        if subtitles:
            command.extend([
                "--enable-subtitles",
                "--translation-backend", str(pipeline["translation_backend"]),
                "--translation-quality", str(pipeline["translation_quality"]),
                "--localization-strategy", str(pipeline["localization_strategy"]),
                "--subtitle-source", str(pipeline["subtitle_source"]),
                "--target-language", str(pipeline["target_language"]),
                "--source-language", str(pipeline["source_language"]),
                "--ocr-language", str(pipeline["ocr_language"]),
                "--ocr-device", str(pipeline["ocr_device"]),
                "--whisper-model", str(pipeline["whisper_model"]),
                "--whisper-device", str(pipeline["whisper_device"]),
                "--subtitle-mode", str(pipeline["subtitle_mode"]),
                "--subtitle-layout", str(pipeline["subtitle_layout"]),
                "--subtitle-position", str(pipeline["subtitle_position"]),
                "--cover-x-percent", str(pipeline["cover_x_percent"]),
                "--cover-y-percent", str(pipeline["cover_y_percent"]),
                "--cover-width-percent", str(pipeline["cover_width_percent"]),
                "--cover-height-percent", str(pipeline["cover_height_percent"]),
                "--cover-opacity", str(pipeline["cover_opacity"]),
                "--cover-color", str(pipeline["cover_color"]),
                "--cover-mode", str(pipeline["cover_mode"]),
                "--cover-blur-sigma", str(pipeline["cover_blur_sigma"]),
                "--font-name", str(pipeline["font_name"]),
                "--font-size", str(pipeline["font_size"]),
                "--global-asr-workers", str(self.settings.maximum_subtitle_workers),
                "--llm-model", str(pipeline["llm_model"]),
            ])
            if bridge_root:
                command.extend(["--agent-bridge-root", str(bridge_root), "--agent-task-title", job["series_name"]])
            if pipeline["subtitle_cover"]:
                command.append("--subtitle-cover")
            if pipeline["cover_auto_detect"]:
                command.append("--cover-auto-detect")
            if pipeline["force_subtitle_translation"]:
                command.append("--force-subtitle-translation")
            if pipeline.get("glossary_name"):
                glossary = (self.settings.project_root / "glossaries" / pipeline["glossary_name"]).resolve()
                try:
                    glossary.relative_to((self.settings.project_root / "glossaries").resolve())
                except ValueError as exc:
                    raise ValueError("术语表路径越界") from exc
                if not glossary.is_file():
                    raise ValueError(f"术语表不存在: {pipeline['glossary_name']}")
                command.extend(["--glossary-file", str(glossary)])
            if pipeline["translation_backend"] == "api" and pipeline["enable_llm_review"]:
                command.extend([
                    "--enable-llm-review",
                    "--llm-review-model", str(pipeline["llm_review_model"]),
                    "--review-confidence-threshold", str(pipeline["review_confidence_threshold"]),
                ])
            if not pipeline["enable_dedup"] or pipeline["enable_recap"]:
                command.append("--subtitle-only")
        return command

    def _begin_recap_stage(
        self,
        job: dict[str, Any],
        paths: JobPaths,
        settings: dict[str, Any],
    ) -> None:
        """Persistently hand a completed subtitle/upload stage to the recap Agent."""
        recap_payload = dict(settings.get("recap") or {})
        recap_payload["project_name"] = recap_payload.get("project_name") or job["series_name"]
        project_path = self.recap.project_path(paths)
        if project_path.is_file():
            self.recap.load(paths)
        else:
            self.recap.create(paths, recap_payload)
        planning = self.recap.planning_status(paths)
        if planning.get("status") not in {"pending", "claimed", "completed"}:
            planning = self.recap.queue_planning(job, paths)
        if planning.get("status") == "completed":
            WorkflowCheckpointStore(paths).complete(
                RECAP_PLANNING_STAGE,
                {"planning_response": str(self.recap.planning_response_path(paths))},
            )
            self.database.set_job_status(job["id"], "recap_ready", process_pid=None)
            self.database.add_event(job["id"], "解说时间轴已经就绪，等待预览或生成最终成片")
            return
        self.database.set_job_status(job["id"], "waiting_recap_agent", process_pid=None)
        self.database.add_event(
            job["id"],
            "字幕和解说素材已就绪，组合任务进入解说 Agent 编排阶段",
            data={
                "workflow_stage": "recap_planning",
                "planning_status": planning.get("status"),
            },
        )

    @staticmethod
    def _publishing_language(normalized: dict[str, Any]) -> str:
        pipeline = normalized["pipeline"]
        selected = str(pipeline.get("publishing_language") or "auto")
        if selected != "auto":
            return selected
        if pipeline.get("enable_recap"):
            return str(normalized.get("recap", {}).get("target_language") or "English")
        if pipeline.get("enable_subtitles"):
            return str(pipeline.get("target_language") or "English")
        return "English"

    def _begin_publishing_stage(
        self,
        job: dict[str, Any],
        paths: JobPaths,
        normalized: dict[str, Any],
        checkpoints: WorkflowCheckpointStore,
    ) -> bool:
        if checkpoints.completed(PUBLISHING_PLANNING_STAGE):
            return True
        state = self.publishing.queue(
            job, paths, self._publishing_language(normalized)
        )
        if state.get("status") == "completed":
            plan = self.publishing.plan(paths)
            checkpoints.complete(
                PUBLISHING_PLANNING_STAGE,
                {
                    "quality_score": plan["quality_score"],
                    "platform": plan.get("platform"),
                },
            )
            return True
        self.database.set_job_status(
            job["id"], "waiting_publishing_agent", process_pid=None
        )
        self.database.add_event(
            job["id"],
            "发布素材已就绪，等待通用 Agent 生成封面方案与 TikTok 发布文案",
            data={
                "workflow_stage": PUBLISHING_PLANNING_STAGE,
                "publishing_status": state.get("status"),
            },
        )
        return False

    def _render_publishing_stage(
        self,
        paths: JobPaths,
        checkpoints: WorkflowCheckpointStore,
        *,
        recap: bool = False,
    ) -> dict[str, Any]:
        if checkpoints.completed(PUBLISHING_RENDER_STAGE):
            return {"resume": True}
        result = build_publishing_materials(
            paths, self.publishing.plan(paths), recap=recap
        )
        checkpoints.complete(
            PUBLISHING_RENDER_STAGE,
            {
                "copy_file": Path(result["copy_file"]).name,
                "cover_files": [Path(item).name for item in result["cover_files"]],
            },
        )
        return result

    def _run_job(self, job: dict[str, Any]) -> None:
        paths = self.storage.paths_from_job(job)
        normalized = normalize_settings(job["settings"], trusted_normalized=True)
        pipeline = normalized["pipeline"]
        plan = WorkflowPlan.from_settings(normalized)
        checkpoints = WorkflowCheckpointStore(paths)
        checkpoints.initialize(plan)
        if pipeline["enable_recap"] and pipeline["enable_dedup"]:
            raise ValueError("解说与去重组合必须等解说成片生成后再执行去重")

        if pipeline["enable_recap"] and not pipeline["enable_subtitles"]:
            copied = self.storage.collect_subtitle_finals(paths)
            if not copied:
                raise ValueError("仅解说任务必须上传现有字幕终稿，或同时启用字幕获取与翻译")
            self.database.add_event(
                job["id"],
                "已复用上传的字幕终稿，跳过字幕识别与翻译",
                data={"workflow_stage": RECAP_PLANNING_STAGE, "resume": True},
            )
            if pipeline["enable_publishing"] and not self._begin_publishing_stage(
                job, paths, normalized, checkpoints
            ):
                return
            self._begin_recap_stage(job, paths, normalized)
            return

        if pipeline["enable_subtitles"] and not checkpoints.completed(SUBTITLE_STAGE):
            subtitle_settings = {
                **normalized,
                "pipeline": {
                    **pipeline,
                    "enable_dedup": False,
                    "enable_recap": False,
                },
            }
            if not self._run_batch_process(
                job, paths, subtitle_settings, SUBTITLE_STAGE, initialize_agent=True
            ):
                return
            copied = self.storage.collect_subtitle_finals(paths)
            if not copied:
                raise RuntimeError("字幕阶段退出成功，但没有生成可用字幕终稿")
            checkpoints.complete(
                SUBTITLE_STAGE,
                {"subtitle_files": [path.name for path in copied]},
            )
            self.storage.publish_results(
                {**job, "completed_at": now_iso()}, paths, categories=("subtitles",)
            )
            self.database.add_event(
                job["id"],
                "字幕阶段检查点已保存",
                data={"workflow_stage": SUBTITLE_STAGE, "checkpoint": "completed"},
            )
        elif pipeline["enable_subtitles"]:
            copied = self.storage.collect_subtitle_finals(paths)
            self.database.add_event(
                job["id"],
                "断点续做：字幕阶段已经完成，本次跳过 OCR/ASR/翻译",
                data={"workflow_stage": SUBTITLE_STAGE, "resume": True},
            )

        if pipeline["enable_publishing"] and not self._begin_publishing_stage(
            job, paths, normalized, checkpoints
        ):
            return

        if pipeline["enable_recap"]:
            self._begin_recap_stage(job, paths, normalized)
            return

        if pipeline["enable_dedup"] and not checkpoints.completed(DEDUP_STAGE):
            dedup_settings = {
                **normalized,
                "pipeline": {**pipeline, "enable_recap": False},
            }
            if not self._run_batch_process(
                job, paths, dedup_settings, DEDUP_STAGE, initialize_agent=False
            ):
                return
            outputs = [
                item.name for item in paths.videos.iterdir()
                if item.is_file() and item.suffix.casefold() in video_dedup.VIDEO_SUFFIXES
            ]
            if not outputs:
                raise RuntimeError("去重阶段退出成功，但没有生成视频")
            checkpoints.complete(DEDUP_STAGE, {"video_files": outputs})
            self.database.add_event(
                job["id"],
                "去重/成片阶段检查点已保存",
                data={"workflow_stage": DEDUP_STAGE, "checkpoint": "completed"},
            )
        elif pipeline["enable_dedup"]:
            self.database.add_event(
                job["id"],
                "断点续做：去重/成片阶段已经完成，本次跳过编码",
                data={"workflow_stage": DEDUP_STAGE, "resume": True},
            )

        if pipeline["enable_publishing"]:
            result = self._render_publishing_stage(paths, checkpoints)
            self.database.add_event(
                job["id"],
                "发布物料已生成：processed 内包含成片、对应封面和可直接粘贴的 TikTok 文案",
                data={
                    "workflow_stage": PUBLISHING_RENDER_STAGE,
                    "cover_count": len(result.get("cover_files") or []),
                },
            )

        self._complete_job(job, paths, normalized, checkpoints)

    def _run_batch_process(
        self,
        job: dict[str, Any],
        paths: JobPaths,
        normalized: dict[str, Any],
        stage: str,
        *,
        initialize_agent: bool,
    ) -> bool:
        pipeline = normalized["pipeline"]
        bridge_root = None
        if initialize_agent and pipeline["enable_subtitles"] and pipeline["translation_backend"] == "agent":
            session = self.remote_agent.initialize(job, paths, self.settings.maximum_subtitle_workers)
            bridge_root = Path(session["bridge_root"])
        command = self._command(job, paths, bridge_root, normalized, checkpoint_stage=stage)
        self.storage.write_json(
            paths.config / f"execution-{stage}.json",
            {
                "schema_version": 1,
                "job_id": job["id"],
                "series_name": job["series_name"],
                "version": job["version"],
                "created_at": now_iso(),
                "limits": {
                    "processing_jobs": 1,
                    "video_workers_without_subtitles": self.settings.maximum_video_workers,
                    "video_workers_with_subtitles": self.settings.maximum_subtitle_workers,
                },
                "workflow_stage": stage,
                "command_without_secrets": command,
            },
        )
        log_path = paths.logs / "process.log"
        self.database.set_job_status(job["id"], "running")
        self.database.add_event(
            job["id"],
            f"开始执行模块: {stage}",
            data={
                "workflow_stage": stage,
                "video_workers": self.settings.maximum_subtitle_workers if pipeline["enable_subtitles"] else self.settings.maximum_video_workers,
            },
        )
        output_queue: queue.Queue[str | None] = queue.Queue()
        with log_path.open("a", encoding="utf-8", newline="") as log:
            process_env = os.environ.copy()
            process_env["VIDEO_GATEWAY_BLOCK_PRIVATE_NETWORK"] = "1"
            llm_secret_path = paths.root / ".secrets" / "llm-api-key.txt"
            if initialize_agent and pipeline["enable_subtitles"] and pipeline["translation_backend"] == "api":
                if not llm_secret_path.is_file():
                    raise RuntimeError("API 模式缺少用户提供的 LLM API Key")
                process_env["OPENAI_API_KEY"] = llm_secret_path.read_text(encoding="utf-8").strip()
                process_env["OPENAI_BASE_URL"] = normalized["pipeline"]["llm_base_url"]
                process_env["OPENAI_MODEL"] = normalized["pipeline"]["llm_model"]
                process_env["OPENAI_REVIEW_MODEL"] = normalized["pipeline"]["llm_review_model"]
            process_kwargs = hidden_subprocess_kwargs()
            if os.name != "nt":
                process_kwargs["start_new_session"] = True
            process = subprocess.Popen(
                command,
                cwd=self.settings.project_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=process_env,
                **process_kwargs,
            )
            self._current_process, self._current_job_id = process, job["id"]
            self.database.set_job_status(
                job["id"],
                "running",
                process_pid=process.pid,
                process_started_at_epoch=time.time(),
                process_executable=str(Path(command[0]).resolve()),
            )

            def reader() -> None:
                assert process.stdout is not None
                for line in process.stdout:
                    output_queue.put(line)
                output_queue.put(None)

            threading.Thread(target=reader, name=f"gateway-log-{job['id']}", daemon=True).start()
            reader_done = False
            while process.poll() is None or not reader_done:
                try:
                    line = output_queue.get(timeout=0.5)
                    if line is None:
                        reader_done = True
                    else:
                        log.write(line)
                        log.flush()
                        clean = line.rstrip()
                        if clean:
                            self.database.add_event(job["id"], clean)
                            if "Agent" in clean and ("2/3" in clean or "等待" in clean):
                                self.database.set_job_status(job["id"], "waiting_agent", process_pid=process.pid)
                except queue.Empty:
                    pass
                current = self.database.get_job(job["id"])
                if current["status"] == "cancellation_requested":
                    if bridge_root:
                        try:
                            import agent_bridge
                            agent_bridge.request_stop_all(bridge_root)
                        except Exception:
                            pass
                    self._terminate_process(process)
                    break
                if self._stop.is_set() and process.poll() is None:
                    self._terminate_process(process)
                    break
            exit_code = process.wait()
        self._current_process, self._current_job_id = None, None
        current = self.database.get_job(job["id"])
        if current["status"] == "cancellation_requested":
            self.database.set_job_status(job["id"], "cancelled", cancelled_at=now_iso(), process_pid=None)
            self.database.add_event(job["id"], "任务已取消", level="warning")
            return False
        if exit_code != 0:
            raise RuntimeError(f"模块 {stage} 退出码 {exit_code}")
        return True

    def _complete_job(
        self,
        job: dict[str, Any],
        paths: JobPaths,
        normalized: dict[str, Any],
        checkpoints: WorkflowCheckpointStore,
    ) -> None:
        pipeline = normalized["pipeline"]
        copied = self.storage.collect_subtitle_finals(paths)
        completed_at = now_iso()
        manifest = {
            "schema_version": 1,
            "job_id": job["id"],
            "series_name": job["series_name"],
            "version": job["version"],
            "completed_at": completed_at,
            "directories": {
                "videos": str(paths.videos),
                "subtitles": str(paths.subtitles),
                "logs": str(paths.logs),
                "agent": str(paths.agent),
            },
            "subtitle_files": [path.name for path in copied],
            "workflow": checkpoints.summary(WorkflowPlan.from_settings(normalized)),
        }
        self.storage.write_json(paths.result / "manifest.json", manifest)
        categories = []
        if pipeline["enable_subtitles"] and any(paths.subtitles.iterdir()):
            categories.append("subtitles")
        if any(item.is_file() for item in paths.videos.iterdir()):
            categories.append("videos")
        publication = self.storage.publish_results(
            {**job, "completed_at": completed_at},
            paths,
            categories=categories,
        )
        manifest["published"] = publication
        self.storage.write_json(paths.result / "manifest.json", manifest)
        # Retain the API secret only while the immutable job remains resumable.
        (paths.root / ".secrets" / "llm-api-key.txt").unlink(missing_ok=True)
        self.database.set_job_status(job["id"], "completed", completed_at=completed_at, process_pid=None)
        self.database.add_event(
            job["id"],
            "任务完成，成品已发布到工程根目录，日志已写入任务记录",
            data=publication["directories"],
        )

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, **hidden_subprocess_kwargs())
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                return
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)

    @staticmethod
    def _terminate_pid_tree(pid: int) -> None:
        if pid <= 0:
            return
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                **hidden_subprocess_kwargs(),
            )
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return

    @classmethod
    def _terminate_recorded_process(cls, record: dict[str, Any]) -> bool:
        pid = int(record.get("process_pid") or 0)
        started = float(record.get("process_started_at_epoch") or 0.0)
        expected_executable = str(record.get("process_executable") or "").strip()
        if pid <= 0 or started <= 0 or not expected_executable:
            return False
        try:
            import psutil

            process = psutil.Process(pid)
            actual_started = float(process.create_time())
            actual_executable = str(Path(process.exe()).resolve())
        except (ImportError, OSError, ValueError):
            return False
        except Exception as exc:
            if exc.__class__.__module__.startswith("psutil"):
                return False
            raise
        if abs(actual_started - started) > 10.0:
            return False
        if os.path.normcase(actual_executable) != os.path.normcase(str(Path(expected_executable).resolve())):
            return False
        cls._terminate_pid_tree(pid)
        return True
