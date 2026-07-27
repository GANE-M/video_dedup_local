from __future__ import annotations

import hashlib
import dataclasses
import json
import math
import mimetypes
import re
import secrets
import shutil
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.requests import ClientDisconnect

import video_dedup

from .agent_http import RemoteAgentBridge, recap_remote_rules, remote_rules
from .agent_orchestration import RECAP_STAGES, stage_rule_path
from .database import GatewayDatabase, hash_secret, now_iso
from .preflight import run_preflight
from .recap_service import RecapService
from .security import SlidingWindowRateLimiter, normalize_public_recap_rendering
from .settings import GatewaySettings
from .storage import JobStorage, safe_component, safe_upload_name
from .worker import GatewayWorker, normalize_settings
from .workflows import RECAP_PLANNING_STAGE, RECAP_RENDER_STAGE, WorkflowCheckpointStore, WorkflowPlan


WEB_BUILD_VERSION = "20260727-30"


def _redact_public_value(value: Any, roots: tuple[Path, ...] = ()) -> Any:
    if isinstance(value, dict):
        return {key: _redact_public_value(item, roots) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_public_value(item, roots) for item in value]
    if not isinstance(value, str):
        return value
    text = value
    for root in roots:
        root_text = str(root)
        if root_text:
            text = text.replace(root_text, "<server-path>")
    text = re.sub(r"(?i)\b[a-z]:\\[^\s\"']+", "<server-path>", text)
    text = re.sub(r"(?<!\w)/(?:tmp|home|var|usr)(?:/[^\s\"']+)+", "<server-path>", text)
    return text


class UploadDeclaration(BaseModel):
    name: str
    size: int = Field(gt=0)
    sha256: str | None = None
    role: str = "video"

    @field_validator("role")
    @classmethod
    def validate_role(cls, value: str) -> str:
        normalized = str(value or "video").strip().casefold()
        if normalized not in {"video", "subtitle_final", "music", "music_pool", "border", "effect", "effect_pool"}:
            raise ValueError("不支持的上传用途")
        return normalized

    @field_validator("sha256")
    @classmethod
    def validate_sha256(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip().casefold()
        if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
            raise ValueError("sha256 必须是64位十六进制")
        return normalized


class CreateJobRequest(BaseModel):
    series_name: str = Field(min_length=1, max_length=120)
    project_name: str | None = Field(default=None, max_length=120)
    files: list[UploadDeclaration] = Field(min_length=1, max_length=200)
    settings: dict[str, Any] = Field(default_factory=dict)
    llm_api_key: str | None = Field(default=None, max_length=1000)


class AgentJobBody(BaseModel):
    internal_job_id: str


class AgentCheckpointBody(AgentJobBody):
    progress: dict[str, Any]


class AgentSubmitBody(AgentJobBody):
    response: dict[str, Any]


class RecapProjectBody(BaseModel):
    project_name: str = Field(min_length=1, max_length=120)
    episode_pattern: str = "*.mp4"
    target_language: str = "English"
    target_duration_seconds: float | None = Field(default=None, gt=0, le=7200)
    narration_preset: str = "standard"
    voice_id: str = "calm_female"
    tts_engine: str = "auto"
    narration_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    narration_target_loudness: str | float = "keep_original"
    rendering: dict[str, Any] = Field(default_factory=dict)


class RecapUpdateBody(BaseModel):
    changes: dict[str, Any]


class RecapSegmentBody(BaseModel):
    segment_id: str = ""
    episode: int = Field(default=1, ge=1)
    source_start: float = Field(default=0.0, ge=0)
    source_end: float = Field(default=10.0, gt=0)
    mode: str = "narration"
    narration_text: str = ""
    purpose: str = ""
    rendering: dict[str, Any] = Field(default_factory=dict)


class RecapActionBody(BaseModel):
    action: str
    segment_id: str = ""


class RecapAgentSubmitBody(BaseModel):
    response: dict[str, Any]


class RecapAgentStageBody(BaseModel):
    payload: dict[str, Any]


class AgentCapabilityVerifyBody(BaseModel):
    probe_nonce: str = Field(min_length=8, max_length=256)
    capabilities: dict[str, Any]


class PreflightBody(BaseModel):
    settings: dict[str, Any] = Field(default_factory=dict)


class StorageCleanupBody(BaseModel):
    categories: list[str] = Field(default_factory=lambda: ["chunks"])
    older_than_days: int = Field(default=7, ge=0, le=3650)
    dry_run: bool = True


SUBTITLE_LANGUAGE_CODES = {
    "chinese": "zh",
    "english": "en",
    "arabic": "ar",
    "spanish": "es",
    "french": "fr",
    "german": "de",
    "portuguese": "pt",
    "japanese": "ja",
    "korean": "ko",
    "russian": "ru",
    "turkish": "tr",
    "indonesian": "id",
    "vietnamese": "vi",
    "thai": "th",
}


def _subtitle_language_code(value: object) -> str:
    text = str(value or "").strip().casefold()
    return SUBTITLE_LANGUAGE_CODES.get(text, re.split(r"[-_]", text, maxsplit=1)[0])


def validate_subtitle_uploads(
    declarations: list[UploadDeclaration],
    normalized_settings: dict[str, Any],
) -> None:
    """Enforce one repaired source and one active target subtitle per video."""
    subtitles = [item for item in declarations if item.role == "subtitle_final"]
    if not subtitles:
        return
    videos = [
        safe_upload_name(item.name, "video")
        for item in declarations
        if item.role == "video"
    ]
    if not videos:
        raise ValueError("字幕文件必须与同一任务中的一级视频对应")
    pipeline = normalized_settings["pipeline"]
    target_language = (
        pipeline.get("target_language")
        if pipeline.get("enable_subtitles")
        else normalized_settings.get("recap", {}).get("target_language")
    )
    target_code = _subtitle_language_code(target_language)
    selected_roles: set[tuple[str, str]] = set()
    for declaration in subtitles:
        name = safe_upload_name(declaration.name, declaration.role)
        if not name.casefold().endswith(".srt"):
            raise ValueError("字幕上传只允许 SRT，不上传清单、实体表或其他 JSON")
        if re.search(r"__[^.]*\.srt$", name, flags=re.IGNORECASE):
            raise ValueError(f"不能上传历史字幕副本：{name}")
        matched_video = ""
        matched_role = ""
        matched_language = ""
        for video in videos:
            prefixes = (video, Path(video).stem)
            for prefix in prefixes:
                match = re.fullmatch(
                    rf"{re.escape(prefix)}\.(source|final)\.([^.]+)\.srt",
                    name,
                    flags=re.IGNORECASE,
                )
                if match:
                    matched_video = video
                    matched_role = match.group(1).casefold()
                    matched_language = _subtitle_language_code(match.group(2))
                    break
            if matched_video:
                break
        if not matched_video:
            raise ValueError(f"字幕没有对应所选一级视频：{name}")
        if matched_role == "final" and matched_language != target_code:
            raise ValueError(
                f"字幕 {name} 不是当前目标语言 {target_language}；"
                "每集只上传当前目标译文"
            )
        role_key = (matched_video.casefold(), matched_role)
        if role_key in selected_roles:
            raise ValueError(
                f"{matched_video} 存在多份 {matched_role} 字幕；"
                "每集最多上传一份修复原文和一份目标译文"
            )
        selected_roles.add(role_key)


def account_agent_command(prefix: str, token: str) -> str:
    return (
        "请将本对话初始化为此账号的远程短剧字幕翻译与解说编排 Agent。禁止使用第三方翻译 API。"
        "先读取规则并完成子 Agent 能力探针：领取 probe_nonce 后，必须用 fork_turns=none 或等价的"
        "无继承上下文方式创建一个真实子 Agent，让它原样返回 nonce，再把能力提交给验证接口。"
        "能力验证请求体必须严格使用："
        "{\"probe_nonce\":\"<探针返回的最新nonce>\",\"capabilities\":{"
        "\"native_subagents\":true,\"context_isolation\":true,"
        "\"max_child_agents\":3,\"probe_role\":\"<实际探针子Agent角色名>\","
        "\"child_agent_run_id\":\"<子Agent运行ID>\","
        "\"isolated_context_id\":\"<隔离上下文ID>\","
        "\"probe_result\":\"<nonce> <角色名>\"}}。"
        "不得猜测、改名或把这些字段移到顶层；每次重新领取probe_nonce都会使旧nonce立即失效。"
        "然后启动监听租约，约每 60 秒调用监听接口。连续空闲监听最多20分钟，该期限由服务器执行。达到上限后简短报告"
        "“监听已因空闲超时结束”，不要自动续跑；需要时我会用同一注册重新启动监听。领取任务后必须"
        "在同一回合持续处理、"
        "发送心跳、保存检查点并完成最终提交，不得只报告仍在进行；提交成功后继续监听下一任务。"
        "读取规则、读取材料、完成单集、生成草稿、自检和报告进度都只是同一任务的中间步骤，"
        "绝对不得发送最终回复、结束回合或等待我说“继续”；进度只能作为非最终更新，更新后必须立即继续调用工具。"
        "只有提交接口确认成功、服务器确认取消/STOP_ALL、会话失效，或不可重试错误已按规则处理完毕，"
        "才允许结束当前任务回合。"
        "如果事件是 RECAP_JOB 或 RECAP_JOB_RESUME，必须读取事件中的 rules_endpoint，"
        "按照事件 completion_contract 的动态清单完整读取规则和全部字幕材料；不得写死集数或文件数。"
        "按照 orchestration_contract 依次提交 story_analysis、recap_draft、independent_review、"
        "final_revision、final_verification；两个审核阶段必须使用隔离子 Agent。全部阶段通过后再提交 recap_plan，"
        "不得套用字幕翻译响应格式。监听若返回 JOB_STATUS_NOTIFICATION，必须立即在本对话中"
        "清楚报告任务名、任务ID、状态和下一步操作；报告后POST事件中的ack_endpoint确认，随后继续监听。\n"
        f"规则：GET {prefix}/rules\n"
        f"能力探针：POST {prefix}/capabilities/probe\n"
        f"能力验证：POST {prefix}/capabilities/verify\n"
        f"启动监听租约：POST {prefix}/listener/start\n"
        f"监听：POST {prefix}/listen\n"
        f"Agent会话令牌：{token}\n"
        "所有请求使用 HTTP Header：Authorization: Bearer <Agent会话令牌>。"
    )


def _present_job(
    job: dict[str, Any], database: GatewayDatabase, storage: JobStorage, chunk_size: int | None = None
) -> dict[str, Any]:
    paths = storage.paths_from_job(job)
    workflow_plan = WorkflowPlan.from_settings(job["settings"])
    published = storage.public_layout(job, paths)
    uploads = database.list_uploads(job["id"])
    public_roots = (
        storage.settings.project_root,
        storage.settings.storage_root,
        storage.settings.service_root,
    )
    return {
        "id": job["id"],
        "series_name": job["series_name"],
        "version": job["version"],
        "status": job["status"],
        "queue_position": job.get("queue_position"),
        "queue": database.queue_summary(),
        "error": _redact_public_value(job.get("error"), public_roots),
        "created_at": job["created_at"],
        "started_at": job.get("started_at"),
        "completed_at": job.get("completed_at"),
        "settings": job["settings"],
        "workflow": WorkflowCheckpointStore(paths).summary(workflow_plan),
        "chunk_size": chunk_size,
        "uploads": [
            {
                "id": item["id"],
                "name": item["original_name"],
                "stored_name": item["stored_name"],
                "size": item["expected_size"],
                "total_chunks": item["total_chunks"],
                "uploaded_chunks": database.chunk_indexes(item["id"]),
                "status": item["status"],
                "role": item.get("role", "video"),
            }
            for item in uploads
        ],
        "result": {
            "project_root": "project",
            "videos": "processed",
            "subtitles": "字幕终稿",
            "recap": "解说",
            "records": "任务记录",
            "record": f"任务记录/{published['record_name']}",
            "record_name": published["record_name"],
            "runtime": {
                "videos": "videos",
                "subtitles": "subtitles",
                "logs": "logs",
                "agent_records": "agent",
            },
        },
    }


def create_app(settings: GatewaySettings | None = None, *, start_worker: bool = True) -> FastAPI:
    settings = settings or GatewaySettings.from_environment()
    settings.ensure_directories()
    database = GatewayDatabase(settings.database_path)
    storage = JobStorage(settings)
    for record in database.project_ownership_records():
        storage.register_existing_project_owner(
            record["work_directory"],
            record["access_key_id"],
        )
    worker = GatewayWorker(settings, database, storage)
    remote_agent = RemoteAgentBridge(database, storage, settings.project_root)
    recap = RecapService(storage)
    session_lock = threading.Lock()
    upload_limiter = SlidingWindowRateLimiter(
        settings.maximum_upload_chunks_per_minute, 60.0
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if start_worker:
            worker.start()
        yield
        if start_worker:
            worker.stop()

    app = FastAPI(
        title="Video Dedup Local Web Gateway",
        version="1.1.0",
        description="Batch upload, queue, local processing, and remote Codex Agent bridge.",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.database = database
    app.state.storage = storage
    app.state.worker = worker
    app.state.recap = recap
    app.mount("/static", StaticFiles(directory=Path(__file__).with_name("static")), name="static")
    if settings.allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(settings.allowed_origins),
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Agent-Token", "X-Chunk-SHA256"],
        )

    def client_key(request: Request) -> dict[str, Any]:
        value = request.headers.get("x-api-key", "")
        authorization = request.headers.get("authorization", "")
        if not value and authorization.casefold().startswith("bearer "):
            value = authorization[7:].strip()
        key = database.authenticate_access_key(value) if value else None
        if not key:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="无效或已过期的访问密钥")
        return key

    def owned_job(request: Request, job_id: str) -> dict[str, Any]:
        key = client_key(request)
        job = database.owned_job(job_id, key["id"])
        if not job:
            raise HTTPException(status_code=404, detail="任务不存在")
        return job

    def agent_job(request: Request, job_id: str) -> dict[str, Any]:
        authorization = request.headers.get("authorization", "")
        value = request.headers.get("x-agent-token", "")
        if not value and authorization.casefold().startswith("bearer "):
            value = authorization[7:].strip()
        job = database.authenticate_agent(job_id, value) if value else None
        if job is None and value:
            try:
                candidate = database.get_job(job_id)
            except KeyError:
                candidate = None
            if candidate and database.authenticate_agent_session(candidate["access_key_id"], value):
                job = candidate
        if not job:
            raise HTTPException(status_code=401, detail="无效的单任务 Agent 令牌")
        return job

    def secret_path(job: dict[str, Any]) -> Path:
        paths = storage.paths_from_job(job)
        path = paths.root / ".secrets" / "agent-token.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def session_secret_path(access_key_id: str) -> Path:
        root = settings.service_root / ".secrets" / "agent-sessions"
        root.mkdir(parents=True, exist_ok=True)
        return root / f"{safe_component(access_key_id)}.txt"

    def ensure_agent_session(access_key_id: str, *, rotate: bool = False) -> tuple[str, dict[str, Any]]:
        with session_lock:
            secret_file = session_secret_path(access_key_id)
            if not rotate:
                try:
                    record = database.get_agent_session(access_key_id)
                    token = secret_file.read_text(encoding="utf-8").strip()
                    if token and database.authenticate_agent_session(access_key_id, token):
                        return token, record
                except (KeyError, OSError):
                    pass
            token = "agent_session_" + secrets.token_urlsafe(36)
            record = database.upsert_agent_session(
                access_key_id, token, maximum_parallel=settings.maximum_subtitle_workers
            )
            secret_file.write_text(token, encoding="utf-8")
            return token, record

    def session_bootstrap(access_key_id: str, token: str, record: dict[str, Any]) -> dict[str, Any]:
        base = settings.public_base_url.rstrip("/")
        prefix = f"{base}/api/v1/agent/sessions/{access_key_id}"
        command = (
            "请将本对话初始化为此账号的远程短剧字幕翻译 Agent，并持续监听后续任务，"
            "直到接口返回会话失效或我明确要求停止。禁止使用第三方翻译API。"
            "如果当前环境支持持续目标或自动续跑机制，请立即建立持续监听目标。"
            "先读取规则；之后约每60秒调用监听接口。领取任务后必须在同一回合持续处理、"
            "心跳、分集检查点和最终提交，不得只报告仍在进行；提交成功后继续监听下一任务。\n"
            "读取规则、读取材料、完成单集、生成草稿和自检都不是停止点；"
            "进度只能作为非最终更新，随后必须立即继续调用工具，绝对不得等待用户说“继续”。\n"
            f"规则：GET {prefix}/rules\n"
            f"监听：POST {prefix}/listen\n"
            f"Agent会话令牌：{token}\n"
            "所有请求使用 HTTP Header：Authorization: Bearer <Agent会话令牌>。"
        )
        return {
            "scope": "account",
            "access_key_id": access_key_id,
            "generation": record["generation"],
            "maximum_parallel": record["maximum_parallel"],
            "command": account_agent_command(prefix, token),
            "rules_url": f"{prefix}/rules",
            "listen_url": f"{prefix}/listen",
            "capability_probe_url": f"{prefix}/capabilities/probe",
            "capability_verify_url": f"{prefix}/capabilities/verify",
            "listener_start_url": f"{prefix}/listener/start",
            "agent_token": token,
            "note": "一次初始化可持续领取该账号后续任务；重新生成会立即使旧对话令牌失效。",
        }

    def agent_session(request: Request, access_key_id: str) -> dict[str, Any]:
        authorization = request.headers.get("authorization", "")
        value = request.headers.get("x-agent-token", "")
        if not value and authorization.casefold().startswith("bearer "):
            value = authorization[7:].strip()
        session = database.authenticate_agent_session(access_key_id, value) if value else None
        if not session:
            raise HTTPException(status_code=401, detail="Agent账号会话无效或已被替换")
        return session

    def bootstrap(job: dict[str, Any], token: str) -> dict[str, Any]:
        base = settings.public_base_url.rstrip("/")
        prefix = f"{base}/api/v1/agent/jobs/{job['id']}"
        command = (
            "请将本对话初始化为远程短剧字幕翻译 Agent，并持续监听直到我明确停止。"
            "禁止使用第三方翻译API。先读取规则接口，然后约每60秒调用监听接口；"
            "领取任务后必须在同一回合持续处理、心跳、分集检查点和最终提交，"
            "不得只报告仍在进行。读取规则、读取材料、完成单集、生成草稿和自检都不是停止点；"
            "进度只能作为非最终更新，随后必须立即继续调用工具，绝对不得等待用户说“继续”。\n"
            f"规则：GET {prefix}/rules\n"
            f"监听：POST {prefix}/listen\n"
            f"任务令牌：{token}\n"
            "所有请求使用 HTTP Header：Authorization: Bearer <任务令牌>。"
        )
        return {
            "command": command,
            "rules_url": f"{prefix}/rules",
            "listen_url": f"{prefix}/listen",
            "agent_token": token,
            "note": "令牌仅限当前任务；不要把它写入字幕、报告或公开日志。",
        }

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        # The HTML names the exact frontend module revision. Never let a
        # browser or reverse proxy combine an old HTML shell with newly split
        # JavaScript files; that leaves visible controls without event handlers.
        return HTMLResponse(
            (Path(__file__).with_name("static") / "index.html").read_text(encoding="utf-8"),
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    @app.get("/health")
    async def health(response: Response) -> dict[str, Any]:
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return {
            "status": "ok",
            "version": app.version,
            "build_version": WEB_BUILD_VERSION,
            "limits": {
                "chunk_size": settings.chunk_size,
                "video_workers": settings.maximum_video_workers,
                "subtitle_workers": settings.maximum_subtitle_workers,
                "processing_jobs": settings.maximum_processing_jobs,
                "maximum_file_size": settings.maximum_file_size,
                "maximum_job_upload_size": settings.maximum_job_upload_size,
                "maximum_account_storage": settings.maximum_account_storage,
            },
        }

    @app.get("/api/v1/settings-schema")
    async def settings_schema(request: Request) -> dict[str, Any]:
        client_key(request)
        default_settings = normalize_settings({})
        for field in ("background_music", "background_music_dir", "border_file", "effect_file", "effect_dir"):
            default_settings["video_config"][field] = None
        preset_payload = {}
        for key, value in video_dedup.PRESETS.items():
            preset = dataclasses.asdict(value)
            for field in ("background_music", "background_music_dir", "border_file", "effect_file", "effect_dir"):
                preset[field] = None
            preset_payload[key] = preset
        glossaries = []
        for path in sorted((settings.project_root / "glossaries").glob("*.json")):
            if path.name.casefold().startswith("template_"):
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
                label = str(payload.get("name") or path.stem)
            except (OSError, json.JSONDecodeError, AttributeError):
                label = path.stem
            glossaries.append({"value": path.name, "label": label})
        return {
            "defaults": default_settings,
            "presets": preset_payload,
            "glossaries": glossaries,
            "choices": {
                "preset": ["light", "medium", "strong", "deep"],
                "hardware_acceleration": ["auto", "nvidia", "amd", "intel", "apple", "cpu"],
                "encoder_preset": ["ultrafast", "veryfast", "fast", "medium", "slow"],
                "output_aspect": ["source", "portrait", "landscape", "square"],
                "target_fps": [0, 24, 25, 30, 50, 60],
                "effect_timing": ["random", "continuous"],
                "effect_key_mode": ["black", "green", "alpha"],
                "effect_position": ["full", "top", "bottom"],
                "translation_backend": ["agent", "api"],
                "translation_quality": ["fast", "advanced"],
                "localization_strategy": ["cinematic_standard", "conversational", "formal_faithful", "gulf_neutral"],
                "subtitle_source": ["auto", "auto-ocr", "soft-asr", "ocr-asr", "soft", "hard-ocr", "asr"],
                "languages": ["auto", "Chinese", "English", "Arabic", "Spanish", "French", "German", "Portuguese", "Japanese", "Korean", "Russian", "Turkish", "Indonesian", "Vietnamese", "Thai"],
                "subtitle_mode": ["burn", "soft"],
                "subtitle_layout": ["replace", "bilingual"],
                "subtitle_position": ["auto", "bottom", "top"],
                "cover_mode": ["blur", "color"],
                "font_name": ["Arial", "Microsoft YaHei", "Microsoft YaHei UI", "SimHei", "SimSun", "Noto Sans", "Noto Sans Arabic", "Segoe UI", "Tahoma"],
            },
            "upload_roles": ["video", "subtitle_final", "music", "music_pool", "border", "effect", "effect_pool"],
            "limits": {
                "chunk_size": settings.chunk_size,
                "maximum_files_per_job": 200,
                "maximum_video_workers": settings.maximum_video_workers,
                "maximum_subtitle_workers": settings.maximum_subtitle_workers,
                "maximum_processing_jobs": 1,
                "maximum_file_size": settings.maximum_file_size,
                "maximum_job_upload_size": settings.maximum_job_upload_size,
                "maximum_account_storage": settings.maximum_account_storage,
            },
        }

    @app.post("/api/v1/preflight")
    async def preflight_endpoint(body: PreflightBody, request: Request) -> dict[str, Any]:
        client_key(request)
        try:
            normalized = normalize_settings(body.settings)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return run_preflight(settings, normalized)

    @app.get("/api/v1/storage")
    async def account_storage_endpoint(request: Request) -> dict[str, Any]:
        key = client_key(request)
        jobs = [
            job for job in database.list_all_jobs(limit=20000)
            if str(job.get("access_key_id")) == str(key["id"])
        ]
        return storage.account_usage(jobs, key["id"])

    @app.post("/api/v1/storage/cleanup")
    async def account_storage_cleanup_endpoint(
        body: StorageCleanupBody, request: Request
    ) -> dict[str, Any]:
        key = client_key(request)
        jobs = [
            job for job in database.list_all_jobs(limit=20000)
            if str(job.get("access_key_id")) == str(key["id"])
        ]
        try:
            return storage.cleanup_account_jobs(
                jobs,
                key["id"],
                categories=body.categories,
                older_than_days=body.older_than_days,
                dry_run=body.dry_run,
            )
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/jobs", status_code=201)
    async def create_job_endpoint(body: CreateJobRequest, request: Request) -> dict[str, Any]:
        key = client_key(request)
        series_name = safe_component(body.series_name, fallback="未命名短剧")
        project_name = safe_component(body.project_name or body.series_name, fallback=series_name)
        project_name = storage.claim_project_name(project_name, key["id"], key.get("label") or "")
        normalized_settings = normalize_settings(body.settings)
        declared_total = sum(int(item.size) for item in body.files)
        oversized = [item.name for item in body.files if int(item.size) > settings.maximum_file_size]
        if oversized:
            raise HTTPException(
                status_code=413,
                detail={"message": "存在超过单文件容量上限的文件", "files": oversized[:20]},
            )
        if declared_total > settings.maximum_job_upload_size:
            raise HTTPException(status_code=413, detail="任务声明的上传总容量超过服务器上限")
        account_jobs = [
            job for job in database.list_all_jobs(limit=20000)
            if str(job.get("access_key_id")) == str(key["id"])
        ]
        account_bytes = int(storage.account_usage(account_jobs, key["id"])["total_bytes"])
        reserved_bytes = database.pending_upload_bytes(key["id"])
        if account_bytes + reserved_bytes + declared_total > settings.maximum_account_storage:
            raise HTTPException(status_code=413, detail="账户存储配额不足，请先清理服务器任务文件")
        try:
            validate_subtitle_uploads(body.files, normalized_settings)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        llm_api_key = str(body.llm_api_key or "").strip()
        if normalized_settings["pipeline"]["enable_subtitles"] and normalized_settings["pipeline"]["translation_backend"] == "api" and len(llm_api_key) < 8:
            raise HTTPException(status_code=422, detail="API 模式需要填写有效的 LLM API Key")
        job_id = "job-" + uuid.uuid4().hex[:20]
        agent_token = "agent_" + secrets.token_urlsafe(32)
        # The editable task name belongs in records, while the selected source
        # folder remains the stable project root.
        paths = storage.paths(
            project_name,
            0,
            job_id,
            owner_id=key["id"],
            owner_label=key.get("label") or "",
        ).ensure()
        used_names: set[str] = set()
        uploads = []
        for declaration in body.files:
            original = safe_upload_name(declaration.name, declaration.role)
            stored = original
            counter = 2
            while stored.casefold() in used_names:
                path = Path(original)
                stored = f"{path.stem}_{counter}{path.suffix}"
                counter += 1
            used_names.add(stored.casefold())
            uploads.append(
                {
                    "id": "upload-" + uuid.uuid4().hex[:20],
                    "original_name": original,
                    "stored_name": stored,
                    "expected_size": declaration.size,
                    "expected_sha256": declaration.sha256,
                    "total_chunks": math.ceil(declaration.size / settings.chunk_size),
                    "role": declaration.role,
                }
            )
        try:
            job = database.create_job(
                {
                    "id": job_id,
                    "access_key_id": key["id"],
                    "series_name": series_name,
                    "settings": normalized_settings,
                    "agent_token_hash": hash_secret(agent_token),
                    "work_directory": str(paths.root),
                },
                uploads,
            )
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        secret_path(job).write_text(agent_token, encoding="utf-8")
        if normalized_settings["pipeline"]["enable_subtitles"] and normalized_settings["pipeline"]["translation_backend"] == "api":
            llm_secret = paths.root / ".secrets" / "llm-api-key.txt"
            llm_secret.parent.mkdir(parents=True, exist_ok=True)
            llm_secret.write_text(llm_api_key, encoding="utf-8")
        video_count = sum(1 for item in uploads if item.get("role") == "video")
        database.add_event(job_id, f"已创建批量任务，共 {video_count} 个视频")
        response = _present_job(job, database, storage, settings.chunk_size)
        session_token, session_record = ensure_agent_session(key["id"])
        response["agent_bootstrap"] = session_bootstrap(key["id"], session_token, session_record)
        response["job_agent_bootstrap"] = bootstrap(job, agent_token)
        return response

    @app.get("/api/v1/jobs/{job_id}")
    async def get_job_endpoint(job_id: str, request: Request) -> dict[str, Any]:
        return _present_job(owned_job(request, job_id), database, storage, settings.chunk_size)

    @app.get("/api/v1/jobs")
    async def list_jobs_endpoint(request: Request, limit: int = 100) -> dict[str, Any]:
        key = client_key(request)
        jobs = database.list_jobs(key["id"], limit=limit)
        return {
            "queue": database.queue_summary(),
            "jobs": [_present_job(job, database, storage, settings.chunk_size) for job in jobs],
        }

    @app.get("/api/v1/jobs/{job_id}/agent-bootstrap")
    async def agent_bootstrap_endpoint(job_id: str, request: Request) -> dict[str, Any]:
        job = owned_job(request, job_id)
        token, record = ensure_agent_session(job["access_key_id"])
        return session_bootstrap(job["access_key_id"], token, record)

    @app.get("/api/v1/agent-session")
    async def get_agent_session_endpoint(request: Request) -> dict[str, Any]:
        key = client_key(request)
        token, record = ensure_agent_session(key["id"])
        return session_bootstrap(key["id"], token, record)

    @app.post("/api/v1/agent-session/rotate")
    async def rotate_agent_session_endpoint(request: Request) -> dict[str, Any]:
        key = client_key(request)
        token, record = ensure_agent_session(key["id"], rotate=True)
        return session_bootstrap(key["id"], token, record)

    @app.get("/api/v1/agent-session/status")
    async def agent_session_status_endpoint(request: Request) -> dict[str, Any]:
        key = client_key(request)
        try:
            record = database.get_agent_session(key["id"])
        except KeyError:
            return {"initialized": False, "connected": False, "state": "not_initialized"}
        heartbeat = record.get("last_heartbeat_at")
        age = None
        if heartbeat:
            try:
                moment = datetime.fromisoformat(str(heartbeat).replace("Z", "+00:00"))
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                age = max(0.0, (datetime.now(timezone.utc) - moment.astimezone(timezone.utc)).total_seconds())
            except ValueError:
                age = None
        enabled = bool(record.get("enabled"))
        return {
            "initialized": True,
            "enabled": enabled,
            "connected": enabled and age is not None and age <= 180,
            "heartbeat_age_seconds": age,
            "generation": record.get("generation"),
            "maximum_parallel": record.get("maximum_parallel"),
            "capabilities_verified": bool(record.get("capabilities_verified_at")),
            "capabilities": (
                json.loads(record["capabilities_json"])
                if record.get("capabilities_json")
                else None
            ),
            "listener_lease_id": record.get("listener_lease_id"),
            "idle_deadline_at": record.get("idle_deadline_at"),
        }

    @app.post("/api/v1/agent-session/stop")
    async def stop_agent_session_endpoint(request: Request) -> dict[str, Any]:
        key = client_key(request)
        database.disable_agent_session(key["id"])
        return {"event": "AGENT_SESSION_STOPPED", "note": "旧对话令牌已失效；再次使用请重新生成初始化命令。"}

    @app.put("/api/v1/jobs/{job_id}/uploads/{upload_id}/chunks/{chunk_index}")
    async def upload_chunk(job_id: str, upload_id: str, chunk_index: int, request: Request) -> dict[str, Any]:
        job = owned_job(request, job_id)
        if not upload_limiter.allow(str(job["access_key_id"])):
            raise HTTPException(status_code=429, detail="上传分片过快，请稍后自动重试")
        if job["status"] != "uploading":
            raise HTTPException(status_code=409, detail="任务已结束上传阶段")
        try:
            upload = database.get_upload(upload_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="上传文件不存在") from exc
        if upload["job_id"] != job_id:
            raise HTTPException(status_code=404, detail="上传文件不存在")
        if upload["status"] == "completed":
            return {
                "event": "UPLOAD_COMPLETED",
                "name": upload["stored_name"],
                "size": upload["assembled_size"],
                "sha256": upload["assembled_sha256"],
                "idempotent": True,
            }
        if chunk_index < 0 or chunk_index >= int(upload["total_chunks"]):
            raise HTTPException(status_code=400, detail="分片序号超出范围")
        try:
            declared_length = int(request.headers.get("content-length", "0") or 0)
        except ValueError:
            declared_length = 0
        if declared_length > settings.maximum_chunk_size:
            raise HTTPException(status_code=413, detail="分片超过允许大小")
        collected = bytearray()
        try:
            async for block in request.stream():
                if len(collected) + len(block) > settings.maximum_chunk_size:
                    raise HTTPException(status_code=413, detail="分片超过允许大小")
                collected.extend(block)
        except ClientDisconnect as exc:
            raise HTTPException(status_code=499, detail="上传连接中断，请安全重试当前分片") from exc
        data = bytes(collected)
        if not data or len(data) > settings.maximum_chunk_size:
            raise HTTPException(status_code=413, detail="分片为空或超过40MB")
        expected_chunk_hash = request.headers.get("x-chunk-sha256", "").strip().casefold()
        actual_hash = hashlib.sha256(data).hexdigest()
        if expected_chunk_hash and not secrets.compare_digest(expected_chunk_hash, actual_hash):
            raise HTTPException(status_code=422, detail="分片SHA256校验失败")
        try:
            free_bytes = shutil.disk_usage(settings.storage_root).free
        except OSError as exc:
            raise HTTPException(status_code=503, detail=f"无法检查服务器磁盘空间: {exc}") from exc
        if free_bytes - len(data) < settings.minimum_free_space:
            raise HTTPException(status_code=507, detail="服务器磁盘空间不足，已拒绝继续上传")
        paths = storage.paths_from_job(job)
        storage.write_chunk(paths, upload_id, chunk_index, data)
        database.record_chunk(upload_id, chunk_index, len(data), actual_hash)
        return {"event": "CHUNK_STORED", "index": chunk_index, "size": len(data), "sha256": actual_hash}

    @app.post("/api/v1/jobs/{job_id}/uploads/{upload_id}/complete")
    async def complete_upload(job_id: str, upload_id: str, request: Request) -> dict[str, Any]:
        job = owned_job(request, job_id)
        try:
            upload = database.get_upload(upload_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="上传文件不存在") from exc
        if upload["job_id"] != job_id:
            raise HTTPException(status_code=404, detail="上传文件不存在")
        if upload["status"] == "completed":
            return {
                "event": "UPLOAD_COMPLETED",
                "name": upload["stored_name"],
                "size": upload["assembled_size"],
                "sha256": upload["assembled_sha256"],
                "idempotent": True,
            }
        indexes = database.chunk_indexes(upload_id)
        expected_indexes = list(range(int(upload["total_chunks"])))
        if indexes != expected_indexes:
            missing = sorted(set(expected_indexes) - set(indexes))
            raise HTTPException(status_code=409, detail={"message": "上传分片不完整", "missing": missing[:100]})
        paths = storage.paths_from_job(job)
        try:
            target, byte_size, digest = storage.assemble_upload(
                paths, upload_id, upload["stored_name"], upload["total_chunks"], upload.get("role", "video")
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if byte_size != int(upload["expected_size"]):
            target.unlink(missing_ok=True)
            raise HTTPException(status_code=422, detail=f"文件大小不一致: {byte_size} != {upload['expected_size']}")
        if upload.get("expected_sha256") and not secrets.compare_digest(upload["expected_sha256"], digest):
            target.unlink(missing_ok=True)
            raise HTTPException(status_code=422, detail="完整文件SHA256校验失败")
        database.complete_upload(upload_id, byte_size, digest)
        storage.cleanup_chunks(paths, upload_id)
        database.add_event(job_id, f"上传完成: {upload['stored_name']}", data={"size": byte_size, "sha256": digest})
        return {"event": "UPLOAD_COMPLETED", "name": upload["stored_name"], "size": byte_size, "sha256": digest}

    @app.post("/api/v1/jobs/{job_id}/start")
    async def start_job(job_id: str, request: Request) -> dict[str, Any]:
        job = owned_job(request, job_id)
        readiness = run_preflight(settings, job["settings"])
        if not readiness["ok"]:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "服务器环境预检未通过",
                    "required_failures": readiness["required_failures"],
                    "checks": readiness["checks"],
                },
            )
        database.add_event(job_id, "服务器环境预检通过")
        try:
            position = database.queue_job(job_id)
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        database.add_event(job_id, f"任务已加入队列，当前位置 {position}")
        return {"event": "QUEUED", "job_id": job_id, "queue_position": position}

    @app.post("/api/v1/jobs/{job_id}/resume")
    async def resume_job(job_id: str, request: Request) -> dict[str, Any]:
        job = owned_job(request, job_id)
        if job["status"] not in {"failed", "cancelled", "paused"}:
            raise HTTPException(status_code=409, detail=f"任务状态 {job['status']} 不能断点续做")
        paths = storage.paths_from_job(job, ensure=False)
        missing_uploads = storage.missing_completed_uploads(paths, database.list_uploads(job_id))
        if missing_uploads:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": "任务源文件已被清理或不完整，无法断点续做；请新建任务并重新上传",
                    "missing_uploads": missing_uploads,
                },
            )
        paths.ensure()
        planning = recap.planning_status(paths)
        if job["settings"]["pipeline"].get("enable_recap") and planning.get("status") == "completed":
            WorkflowCheckpointStore(paths).complete(
                RECAP_PLANNING_STAGE,
                {"planning_response": str(recap.planning_response_path(paths))},
            )
            database.set_job_status(
                job_id,
                "recap_ready",
                error=None,
                completed_at=None,
                cancelled_at=None,
                process_pid=None,
            )
            database.add_event(
                job_id,
                "断点续做：字幕与解说编排已完成，返回成片预览/渲染阶段",
                data={"workflow_stage": RECAP_RENDER_STAGE, "resume": True},
            )
            return {"event": "RESUMED_RECAP_READY", "job_id": job_id, "status": "recap_ready"}
        try:
            position = database.resume_job(job_id)
        except (KeyError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        database.add_event(
            job_id,
            f"任务从阶段检查点恢复，已加入队列，当前位置 {position}",
            data={
                "resume": True,
                "workflow": WorkflowCheckpointStore(paths).summary(
                    WorkflowPlan.from_settings(job["settings"])
                ),
            },
        )
        return {"event": "RESUMED", "job_id": job_id, "queue_position": position}

    @app.post("/api/v1/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str, request: Request) -> dict[str, Any]:
        job = owned_job(request, job_id)
        if job["status"] in {"completed", "failed", "cancelled"}:
            return {"event": "ALREADY_FINISHED", "status": job["status"]}
        if job["status"] in {"uploading", "queued", "waiting_recap_agent", "recap_ready"}:
            paths = storage.paths_from_job(job)
            recap.cancel_planning(paths)
            database.set_job_status(job_id, "cancelled", cancelled_at=now_iso())
            database.add_event(job_id, "任务在执行前已取消", level="warning")
            return {"event": "CANCELLED", "job_id": job_id}
        database.set_job_status(job_id, "cancellation_requested")
        database.add_event(job_id, "用户请求停止任务", level="warning")
        return {"event": "CANCELLATION_REQUESTED", "job_id": job_id}

    @app.get("/api/v1/jobs/{job_id}/events")
    async def job_events(job_id: str, request: Request, after: int = 0, limit: int = 500) -> dict[str, Any]:
        owned_job(request, job_id)
        roots = (settings.project_root, settings.storage_root, settings.service_root)
        return {"events": _redact_public_value(database.events(job_id, after=after, limit=limit), roots)}

    @app.get("/api/v1/jobs/{job_id}/artifacts")
    async def client_artifacts(job_id: str, request: Request) -> dict[str, Any]:
        job = owned_job(request, job_id)
        paths = storage.paths_from_job(job)
        return {"artifacts": storage.list_artifacts(paths, include_runtime=False)}

    @app.get("/api/v1/jobs/{job_id}/artifacts/{artifact_path:path}")
    async def client_artifact(job_id: str, artifact_path: str, request: Request):
        job = owned_job(request, job_id)
        try:
            path = storage.resolve_artifact(storage.paths_from_job(job), artifact_path, include_runtime=False)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="文件不存在") from exc
        return FileResponse(path, filename=path.name, media_type=mimetypes.guess_type(path.name)[0])

    def present_recap(project) -> dict[str, Any]:
        payload = project.to_dict()
        # Host-local filesystem locations are implementation details. Remote
        # users edit logical project data and download artifacts through APIs.
        payload.pop("source_root", None)
        payload.pop("subtitle_root", None)
        payload.pop("output_root", None)
        return payload

    @app.get("/api/v1/recap/voices")
    async def recap_voices(request: Request) -> dict[str, Any]:
        client_key(request)
        return {"voices": recap.voices(), "engines_by_language": recap.engines()}

    @app.get("/api/v1/recap/voices/{voice_id}/preview")
    async def recap_voice_preview(voice_id: str, request: Request):
        client_key(request)
        try:
            path = recap.preview_path(voice_id)
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(status_code=404, detail="声纹试听不存在") from exc
        return FileResponse(path, filename=path.name, media_type="audio/wav")

    @app.post("/api/v1/jobs/{job_id}/recap", status_code=201)
    async def create_recap_project(job_id: str, body: RecapProjectBody, request: Request) -> dict[str, Any]:
        job = owned_job(request, job_id)
        uploads = database.list_uploads(job_id)
        videos = [item for item in uploads if item.get("role", "video") == "video"]
        if not videos or any(item["status"] != "completed" for item in videos):
            raise HTTPException(status_code=409, detail="请先完成全部解说源视频上传")
        paths = storage.paths_from_job(job)
        if recap.project_path(paths).is_file():
            raise HTTPException(status_code=409, detail="该素材任务已经建立解说项目")
        try:
            payload = body.model_dump()
            payload["rendering"] = normalize_public_recap_rendering(payload.get("rendering"))
            project = recap.create(paths, payload)
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        try:
            planning = recap.queue_planning(job, paths)
            database.add_event(job_id, "解说素材和字幕已就绪，等待解说 Agent 生成结构化时间轴")
        except ValueError as exc:
            planning = {"status": "blocked", "error": str(exc)}
            database.add_event(job_id, f"解说项目已建立，但 Agent 规划尚未排队：{exc}", level="warning")
        if planning.get("status") in {"pending", "claimed"}:
            database.set_job_status(job_id, "waiting_recap_agent", completed_at=None)
        elif planning.get("status") == "completed":
            database.set_job_status(job_id, "recap_ready", completed_at=None)
        else:
            # The project can still be edited manually, but it must not remain
            # forever in the upload state after all declared files completed.
            database.set_job_status(job_id, "recap_ready", completed_at=None)
        return {"job_id": job_id, "project": present_recap(project), "planning": planning}

    @app.get("/api/v1/jobs/{job_id}/recap")
    async def get_recap_project(job_id: str, request: Request) -> dict[str, Any]:
        job = owned_job(request, job_id)
        paths = storage.paths_from_job(job)
        try:
            project = recap.load(paths)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="解说项目尚未建立") from exc
        return {
            "job_id": job_id,
            "project": present_recap(project),
            "operation": recap.status(paths),
            "planning": recap.planning_status(paths),
        }

    @app.post("/api/v1/jobs/{job_id}/recap/agent/queue", status_code=202)
    async def queue_recap_agent(job_id: str, request: Request) -> dict[str, Any]:
        job = owned_job(request, job_id)
        paths = storage.paths_from_job(job)
        try:
            planning = recap.queue_planning(job, paths)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="解说项目尚未建立") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        WorkflowCheckpointStore(paths).invalidate(
            (RECAP_PLANNING_STAGE, RECAP_RENDER_STAGE),
            "用户重新提交解说 Agent 规划",
        )
        recap.status_path(paths).unlink(missing_ok=True)
        database.set_job_status(job_id, "waiting_recap_agent", completed_at=None, error=None)
        database.add_event(
            job_id,
            f"已提交解说 Agent 规划任务（第 {planning['planning_attempt']} 次）",
            data={
                "workflow_stage": RECAP_PLANNING_STAGE,
                "planning_attempt": planning["planning_attempt"],
                "planning_attempt_id": planning["planning_attempt_id"],
            },
        )
        return {"job_id": job_id, "planning": planning}

    @app.get("/api/v1/jobs/{job_id}/recap/agent/status")
    async def recap_agent_status(job_id: str, request: Request) -> dict[str, Any]:
        job = owned_job(request, job_id)
        return {"job_id": job_id, "planning": recap.planning_status(storage.paths_from_job(job))}

    @app.put("/api/v1/jobs/{job_id}/recap")
    async def update_recap_project(job_id: str, body: RecapUpdateBody, request: Request) -> dict[str, Any]:
        job = owned_job(request, job_id)
        try:
            changes = dict(body.changes)
            if "rendering" in changes:
                changes["rendering"] = normalize_public_recap_rendering(changes.get("rendering"))
            project = recap.update(storage.paths_from_job(job), changes)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="解说项目尚未建立") from exc
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"job_id": job_id, "project": present_recap(project)}

    @app.post("/api/v1/jobs/{job_id}/recap/segments", status_code=201)
    async def add_recap_segment(job_id: str, body: RecapSegmentBody, request: Request) -> dict[str, Any]:
        job = owned_job(request, job_id)
        try:
            project = recap.add_segment(storage.paths_from_job(job), body.model_dump())
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="解说项目尚未建立") from exc
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"job_id": job_id, "project": present_recap(project)}

    @app.put("/api/v1/jobs/{job_id}/recap/segments/{segment_id}")
    async def update_recap_segment(
        job_id: str, segment_id: str, body: RecapUpdateBody, request: Request
    ) -> dict[str, Any]:
        job = owned_job(request, job_id)
        try:
            project = recap.update_segment(storage.paths_from_job(job), segment_id, body.changes)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="解说项目尚未建立") from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"job_id": job_id, "project": present_recap(project)}

    @app.delete("/api/v1/jobs/{job_id}/recap/segments/{segment_id}")
    async def delete_recap_segment(job_id: str, segment_id: str, request: Request) -> dict[str, Any]:
        job = owned_job(request, job_id)
        try:
            project = recap.delete_segment(storage.paths_from_job(job), segment_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="解说项目尚未建立") from exc
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"job_id": job_id, "project": present_recap(project)}

    @app.post("/api/v1/jobs/{job_id}/recap/validate")
    async def validate_recap_project(job_id: str, request: Request) -> dict[str, Any]:
        job = owned_job(request, job_id)
        try:
            return recap.validate(storage.paths_from_job(job))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="解说项目或源视频不存在") from exc

    @app.post("/api/v1/jobs/{job_id}/recap/actions", status_code=202)
    async def recap_action(job_id: str, body: RecapActionBody, request: Request) -> dict[str, Any]:
        job = owned_job(request, job_id)
        paths = storage.paths_from_job(job)
        try:
            recap.load(paths)
            def observe_recap_process(pid: int | None, executable: str, started_at: float | None) -> None:
                if body.action != "final":
                    return
                database.set_job_status(
                    job_id,
                    "recap_rendering",
                    process_pid=pid,
                    process_started_at_epoch=started_at,
                    process_executable=executable or None,
                )
            state = recap.start_action(
                job,
                paths,
                body.action,
                body.segment_id,
                cancelled=lambda: database.get_job(job_id)["status"] in {
                    "cancellation_requested", "cancelled",
                },
                process_observer=observe_recap_process,
            )
            if body.action == "final":
                database.set_job_status(job_id, "recap_rendering", completed_at=None, error=None)
                database.add_event(job_id, "开始生成最终解说成片", data={"workflow_stage": "recap_rendering"})

                def monitor_final_render() -> None:
                    while True:
                        current = recap.status(paths)
                        if current.get("status") not in {"queued", "running"}:
                            break
                        time.sleep(1)
                    latest = database.get_job(job_id)
                    if latest["status"] in {"cancellation_requested", "cancelled"} or current.get("status") == "cancelled":
                        database.set_job_status(job_id, "cancelled", cancelled_at=now_iso(), process_pid=None)
                        database.add_event(job_id, "解说渲染已取消，未发布最终成片", level="warning")
                    elif current.get("status") == "completed" and current.get("result", {}).get("status") == "ok":
                        WorkflowCheckpointStore(paths).complete(
                            RECAP_RENDER_STAGE,
                            {"output_paths": current.get("result", {}).get("output_paths", {})},
                        )
                        database.set_job_status(job_id, "completed", completed_at=now_iso(), process_pid=None)
                        database.add_event(job_id, "组合任务全部完成：字幕与解说成片均已发布")
                    else:
                        error = str(current.get("error") or current.get("result", {}).get("status") or "解说渲染失败")
                        database.set_job_status(
                            job_id, "failed", error=error, completed_at=now_iso(), process_pid=None,
                        )
                        database.add_event(job_id, f"解说最终渲染失败：{error}", level="error")

                threading.Thread(
                    target=monitor_final_render,
                    name=f"recap-final-monitor-{job_id[:8]}",
                    daemon=True,
                ).start()
            return state
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail="解说项目尚未建立") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/v1/agent/jobs/{job_id}/rules", response_class=PlainTextResponse)
    async def agent_rules(job_id: str, request: Request) -> str:
        agent_job(request, job_id)
        return remote_rules(settings.project_root)

    @app.get("/api/v1/agent/sessions/{access_key_id}/rules", response_class=PlainTextResponse)
    async def agent_session_rules(access_key_id: str, request: Request) -> str:
        agent_session(request, access_key_id)
        return remote_rules(settings.project_root)

    @app.post("/api/v1/agent/sessions/{access_key_id}/capabilities/probe")
    async def agent_capability_probe(
        access_key_id: str, request: Request,
    ) -> dict[str, Any]:
        agent_session(request, access_key_id)
        return database.create_agent_capability_probe(access_key_id)

    @app.post("/api/v1/agent/sessions/{access_key_id}/capabilities/verify")
    async def agent_capability_verify(
        access_key_id: str,
        body: AgentCapabilityVerifyBody,
        request: Request,
    ) -> dict[str, Any]:
        agent_session(request, access_key_id)
        try:
            record = database.verify_agent_capabilities(
                access_key_id, body.probe_nonce, body.capabilities
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "event": "CAPABILITIES_VERIFIED",
            "verified_at": record["capabilities_verified_at"],
            "capabilities": json.loads(record["capabilities_json"]),
        }

    @app.post("/api/v1/agent/sessions/{access_key_id}/listener/start")
    async def agent_listener_start(
        access_key_id: str, request: Request,
    ) -> dict[str, Any]:
        agent_session(request, access_key_id)
        record = database.start_agent_listener_lease(
            access_key_id, settings.agent_idle_timeout_seconds
        )
        return {
            "event": "LISTENER_STARTED",
            "lease_id": record["listener_lease_id"],
            "idle_deadline_at": record["idle_deadline_at"],
            "idle_timeout_seconds": settings.agent_idle_timeout_seconds,
        }

    @app.post("/api/v1/agent/sessions/{access_key_id}/listen")
    async def agent_session_listen(access_key_id: str, request: Request) -> dict[str, Any]:
        session = agent_session(request, access_key_id)
        database.touch_agent_session(access_key_id)
        lease = database.ensure_agent_listener_lease(
            access_key_id, settings.agent_idle_timeout_seconds
        )
        deadline_value = lease.get("idle_deadline_at")
        if deadline_value:
            deadline = datetime.fromisoformat(
                str(deadline_value).replace("Z", "+00:00")
            )
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) >= deadline.astimezone(timezone.utc):
                return {
                    "event": "LISTEN_EXPIRED",
                    "lease_id": lease["listener_lease_id"],
                    "expired_at": deadline_value,
                    "restart_endpoint": (
                        f"{settings.public_base_url.rstrip('/')}/api/v1/agent/"
                        f"sessions/{access_key_id}/listener/start"
                    ),
                }
        notification = database.next_agent_notification(access_key_id)
        if notification:
            base = settings.public_base_url.rstrip("/")
            return {
                "event": "JOB_STATUS_NOTIFICATION",
                "notification_id": notification["id"],
                "kind": notification["kind"],
                "job_id": notification["job_id"],
                "status": notification["status"],
                "title": notification["title"],
                "message": notification["message"],
                "data": notification.get("data"),
                "created_at": notification["created_at"],
                "ack_endpoint": (
                    f"{base}/api/v1/agent/sessions/{access_key_id}/notifications/"
                    f"{notification['id']}/ack"
                ),
                "instruction": (
                    "请立即在Agent对话中向用户明确报告此状态；报告后POST确认接口，"
                    "然后继续调用监听接口。"
                ),
            }
        jobs = list(reversed(database.list_jobs(access_key_id, limit=500)))
        if not session.get("capabilities_verified_at"):
            recap_waiting = any(
                recap.planning_status(storage.paths_from_job(job)).get("status")
                in {"pending", "claimed"}
                for job in jobs
            )
            if recap_waiting:
                prefix = (
                    f"{settings.public_base_url.rstrip('/')}/api/v1/agent/"
                    f"sessions/{access_key_id}"
                )
                return {
                    "event": "CAPABILITY_REQUIRED",
                    "reason": "RECAP 任务要求原生子 Agent 与隔离审核上下文",
                    "probe_endpoint": f"{prefix}/capabilities/probe",
                    "verify_endpoint": f"{prefix}/capabilities/verify",
                }
        for job in jobs:
            recap_event = recap.claim_planning(job, storage.paths_from_job(job), settings.public_base_url)
            if recap_event:
                lease = database.mark_agent_session_work(access_key_id)
                recap_event["account_session_generation"] = session["generation"]
                recap_event["listener_lease_id"] = lease["listener_lease_id"]
                return recap_event
            if job["status"] not in {"starting", "running", "waiting_agent"}:
                continue
            event = remote_agent.listen(job, storage.paths_from_job(job), settings.public_base_url)
            if event.get("event") in {"JOB", "JOB_RESUME", "STOP_ALL", "REGISTRATION_INVALID"}:
                lease = database.mark_agent_session_work(access_key_id)
                event["account_session_generation"] = session["generation"]
                event["external_job_id"] = job["id"]
                event["listener_lease_id"] = lease["listener_lease_id"]
                return event
        lease = database.start_agent_idle_window(
            access_key_id, settings.agent_idle_timeout_seconds
        )
        return {
            "event": "IDLE",
            "retry_after_seconds": 60,
            "queue": database.queue_summary(),
            "lease_id": lease["listener_lease_id"],
            "idle_deadline_at": lease["idle_deadline_at"],
        }

    @app.post("/api/v1/agent/sessions/{access_key_id}/notifications/{notification_id}/ack")
    async def agent_notification_ack(
        access_key_id: str, notification_id: int, request: Request,
    ) -> dict[str, Any]:
        agent_session(request, access_key_id)
        try:
            notification = database.acknowledge_agent_notification(
                access_key_id, notification_id
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Agent通知不存在") from exc
        database.touch_agent_session(access_key_id)
        return {
            "event": "NOTIFICATION_ACKNOWLEDGED",
            "notification_id": notification["id"],
            "job_id": notification["job_id"],
            "acknowledged_at": notification["acknowledged_at"],
        }

    @app.post("/api/v1/agent/jobs/{job_id}/listen")
    async def agent_listen(job_id: str, request: Request) -> dict[str, Any]:
        job = agent_job(request, job_id)
        return remote_agent.listen(job, storage.paths_from_job(job), settings.public_base_url)

    @app.post("/api/v1/agent/jobs/{job_id}/heartbeat")
    async def agent_heartbeat(job_id: str, body: AgentJobBody, request: Request) -> dict[str, Any]:
        job = agent_job(request, job_id)
        return remote_agent.heartbeat(storage.paths_from_job(job), body.internal_job_id)

    @app.post("/api/v1/agent/jobs/{job_id}/checkpoint")
    async def agent_checkpoint(job_id: str, body: AgentCheckpointBody, request: Request) -> dict[str, Any]:
        job = agent_job(request, job_id)
        try:
            return remote_agent.checkpoint(storage.paths_from_job(job), body.internal_job_id, body.progress)
        except RuntimeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/agent/jobs/{job_id}/submit")
    async def agent_submit(job_id: str, body: AgentSubmitBody, request: Request) -> dict[str, Any]:
        job = agent_job(request, job_id)
        try:
            return remote_agent.submit(storage.paths_from_job(job), body.internal_job_id, body.response)
        except RuntimeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/v1/agent/jobs/{job_id}/artifacts")
    async def agent_artifacts(job_id: str, request: Request) -> dict[str, Any]:
        job = agent_job(request, job_id)
        paths = storage.paths_from_job(job)
        return {"artifacts": storage.list_artifacts(paths, include_runtime=True)}

    @app.get("/api/v1/agent/jobs/{job_id}/artifacts/{artifact_path:path}")
    async def agent_artifact(job_id: str, artifact_path: str, request: Request):
        job = agent_job(request, job_id)
        paths = storage.paths_from_job(job)
        try:
            path = storage.resolve_artifact(paths, artifact_path, include_runtime=True)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=404, detail="文件不存在") from exc
        recap.record_artifact_fetch(
            paths,
            artifact_path,
            agent_run_id=request.headers.get("x-agent-run-id", ""),
        )
        media_type = mimetypes.guess_type(path.name)[0]
        if path.suffix.casefold() in {".md", ".txt", ".log", ".srt", ".csv"}:
            media_type = "text/plain; charset=utf-8"
        elif path.suffix.casefold() == ".json":
            media_type = "application/json"
        return FileResponse(path, media_type=media_type, content_disposition_type="inline")

    @app.get("/api/v1/agent/jobs/{job_id}/recap/rules", response_class=PlainTextResponse)
    async def agent_recap_rules(job_id: str, request: Request) -> str:
        job = agent_job(request, job_id)
        try:
            content = recap_remote_rules(settings.project_root)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=500, detail=f"缺少解说规则文件: {exc}") from exc
        recap.record_rules_fetch(storage.paths_from_job(job))
        return content

    @app.get(
        "/api/v1/agent/jobs/{job_id}/recap/roles/{stage}",
        response_class=PlainTextResponse,
    )
    async def agent_recap_role_rules(
        job_id: str, stage: str, request: Request,
    ) -> str:
        job = agent_job(request, job_id)
        if stage not in RECAP_STAGES:
            raise HTTPException(status_code=404, detail="未知 RECAP 阶段")
        path = stage_rule_path(settings.project_root, stage)
        if not path.is_file():
            raise HTTPException(status_code=500, detail=f"缺少角色规则文件: {path}")
        paths = storage.paths_from_job(job)
        content = path.read_text(encoding="utf-8-sig")
        if stage == "final_verification":
            try:
                contract = recap.stage_contract(paths, stage)
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            content += (
                "\n\n## Server-supplied binding contract\n\n"
                "Use this exact server-generated object; do not recompute or rename its fields.\n\n"
                "```json\n"
                + json.dumps(contract, ensure_ascii=False, indent=2)
                + "\n```\n"
                "下载 review_target.artifact_path 时必须附带 HTTP Header "
                "X-Agent-Run-ID: <本次 final_verification 的 execution.agent_run_id>。"
                "未绑定到该审核运行的下载不会被视为审核证据。\n"
            )
        recap.record_role_rules_fetch(paths, stage)
        return content

    @app.post("/api/v1/agent/jobs/{job_id}/recap/stages/{stage}")
    async def agent_recap_stage_submit(
        job_id: str,
        stage: str,
        body: RecapAgentStageBody,
        request: Request,
    ) -> dict[str, Any]:
        job = agent_job(request, job_id)
        try:
            return recap.submit_stage(
                storage.paths_from_job(job), stage, body.payload
            )
        except (ValueError, RuntimeError, KeyError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/v1/agent/jobs/{job_id}/recap/heartbeat")
    async def agent_recap_heartbeat(
        job_id: str,
        request: Request,
        planning_attempt_id: str = "",
    ) -> dict[str, Any]:
        job = agent_job(request, job_id)
        database.touch_agent_session(job["access_key_id"])
        try:
            return recap.planning_heartbeat(
                storage.paths_from_job(job), planning_attempt_id,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/v1/agent/jobs/{job_id}/recap/submit")
    async def agent_recap_submit(
        job_id: str, body: RecapAgentSubmitBody, request: Request,
    ) -> dict[str, Any]:
        job = agent_job(request, job_id)
        try:
            paths = storage.paths_from_job(job)
            result = recap.submit_planning(paths, body.response)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        WorkflowCheckpointStore(paths).complete(
            RECAP_PLANNING_STAGE,
            {
                "segment_count": result["planning"]["segment_count"],
                "quality_score": result["planning"]["quality_score"],
            },
        )
        database.add_event(
            job_id,
            f"解说 Agent 时间轴已通过校验：{result['planning']['segment_count']} 个片段，"
            f"质量 {result['planning']['quality_score']:.1f}/100",
        )
        database.set_job_status(job_id, "recap_ready", completed_at=None, process_pid=None)
        return result

    return app
