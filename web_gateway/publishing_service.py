from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .database import now_iso
from .publishing_materials import (
    PUBLISHING_AGENT_DIRECTORY,
    first_file,
    series_information,
    validate_publishing_plan,
)
from .storage import JobPaths, JobStorage


PUBLISHING_RULES = """# Agent通用发布物料任务

你正在处理 PUBLISHING_JOB。它与字幕翻译、解说编排相互独立，不能返回字幕或 recap schema。

1. 读取 request.required_artifacts 中的全部素材。PNG 是原始封面，TXT 是剧名与简介；缺失可选素材
   不得阻塞任务，也不得虚构不存在的平台。
2. 根据剧名、简介和封面中可见的可靠品牌证据判断平台。只有明确看见 ReelShort、DramaBox、
   GoodShort、ShortMax、NetShort、FlexTV 等平台名或标识时才填写 platform，并在
   platform_evidence 中说明证据；不能仅凭剧情类型猜测平台。
3. 输出一个可直接粘贴到 TikTok 的标题、Bio 和 5-7 个 hashtag。不得添加“标题：”“Bio：”等标签、
   解释、备注或多余段落。
4. hashtag 顺序：确认平台时为“#平台、#fyp、其余剧情相关标签”；无法确认平台时必须以 #fyp 开头。
   不要使用与剧情无关的泛滥标签。
5. 封面由服务器确定性渲染。你只能在四个安全位置中选择金色集数数字的位置：top_left、
   top_right、bottom_left、bottom_right。不要返回图片二进制、代码、主机路径或外部下载地址。
6. 提交前自检准确性、可复制性、平台证据和 hashtag 顺序。quality_score 必须达到 8.5/10；
   不足时先自行修订再提交。
7. 返回严格 JSON：
   {
     "schema_version": 1,
     "task_type": "publishing_materials",
     "language": "English or Arabic",
     "platform": "reelshort or null",
     "platform_evidence": "evidence or null",
     "title": "single line",
     "bio": "single line",
     "hashtags": ["#fyp", "..."],
     "cover": {"episode_number_position": "bottom_right"},
     "quality_score": 8.5,
     "quality_notes": "brief self-check"
   }
"""


def _parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class PublishingService:
    def __init__(self, storage: JobStorage, *, claim_seconds: int = 20 * 60):
        self.storage = storage
        self.claim_seconds = max(120, int(claim_seconds))
        self._lock = threading.RLock()

    @staticmethod
    def root(paths: JobPaths) -> Path:
        return paths.result / PUBLISHING_AGENT_DIRECTORY

    def status_path(self, paths: JobPaths) -> Path:
        return self.root(paths) / "status.json"

    def request_path(self, paths: JobPaths) -> Path:
        return self.root(paths) / "request.json"

    def plan_path(self, paths: JobPaths) -> Path:
        return self.root(paths) / "plan.json"

    def status(self, paths: JobPaths) -> dict[str, Any]:
        try:
            return json.loads(self.status_path(paths).read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, TypeError):
            return {"status": "not_queued"}

    def plan(self, paths: JobPaths) -> dict[str, Any]:
        try:
            payload = json.loads(self.plan_path(paths).read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            raise RuntimeError("发布物料 Agent 尚未提交有效方案") from exc
        return validate_publishing_plan(payload)

    def queue(self, job: dict[str, Any], paths: JobPaths, language: str) -> dict[str, Any]:
        with self._lock:
            existing = self.status(paths)
            if existing.get("status") in {"pending", "claimed", "completed"}:
                return existing
            cover = first_file(paths.assets / "cover", {".png"})
            information = first_file(paths.assets / "series_info", {".txt"})
            title, synopsis = series_information(information, str(job.get("series_name") or ""))
            artifacts: list[str] = []
            if cover:
                artifacts.append(f"assets/cover/{cover.name}")
            if information:
                artifacts.append(f"assets/series_info/{information.name}")
            request = {
                "schema_version": 1,
                "task_type": "publishing_materials",
                "external_job_id": str(job["id"]),
                "series_name": str(job.get("series_name") or ""),
                "target_language": str(language or "English"),
                "source_information_preview": {"title": title, "synopsis": synopsis},
                "required_artifacts": artifacts,
                "episode_count": sum(
                    1
                    for item in paths.input.iterdir()
                    if item.is_file() and item.suffix.casefold() in {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
                ),
                "required_response": {
                    "task_type": "publishing_materials",
                    "title": "single line",
                    "bio": "single line",
                    "hashtags": "5-7 ordered strings",
                    "cover": {"episode_number_position": "top_left|top_right|bottom_left|bottom_right"},
                    "quality_score": ">=8.5/10",
                },
            }
            self.storage.write_json(self.request_path(paths), request)
            state = {
                "status": "pending",
                "queued_at": now_iso(),
                "required_artifacts": artifacts,
                "fetched_artifacts": [],
            }
            self.storage.write_json(self.status_path(paths), state)
            return state

    def claim(self, job: dict[str, Any], paths: JobPaths, base_url: str) -> dict[str, Any] | None:
        with self._lock:
            if str(job.get("status") or "") != "waiting_publishing_agent":
                return None
            state = self.status(paths)
            status = str(state.get("status") or "")
            if status == "claimed":
                heartbeat = _parse_time(state.get("last_heartbeat_at") or state.get("claimed_at"))
                if heartbeat and datetime.now(timezone.utc) - heartbeat <= timedelta(seconds=self.claim_seconds):
                    return None
                status = "pending"
            if status != "pending":
                return None
            claim_id = secrets.token_urlsafe(18)
            state.update(
                {
                    "status": "claimed",
                    "claim_id": claim_id,
                    "claimed_at": now_iso(),
                    "last_heartbeat_at": now_iso(),
                }
            )
            self.storage.write_json(self.status_path(paths), state)
            request = json.loads(self.request_path(paths).read_text(encoding="utf-8-sig"))
            prefix = f"{base_url.rstrip('/')}/api/v1/agent/jobs/{job['id']}/publishing"
            return {
                "event": "PUBLISHING_JOB",
                "external_job_id": str(job["id"]),
                "claim_id": claim_id,
                "request": request,
                "rules_endpoint": f"{prefix}/rules",
                "heartbeat_endpoint": f"{prefix}/heartbeat",
                "submit_endpoint": f"{prefix}/submit",
                "artifacts_endpoint": f"{base_url.rstrip('/')}/api/v1/agent/jobs/{job['id']}/artifacts",
                "instruction": "读取规则和全部 required_artifacts，生成并自检发布方案，提交成功后继续监听。",
            }

    def cancel(self, paths: JobPaths) -> None:
        with self._lock:
            state = self.status(paths)
            if state.get("status") in {"pending", "claimed"}:
                state.update(
                    {
                        "status": "cancelled",
                        "cancelled_at": now_iso(),
                        "claim_id": None,
                    }
                )
                self.storage.write_json(self.status_path(paths), state)

    def heartbeat(self, paths: JobPaths, claim_id: str) -> dict[str, Any]:
        with self._lock:
            state = self.status(paths)
            if state.get("status") != "claimed" or not secrets.compare_digest(
                str(state.get("claim_id") or ""), str(claim_id or "")
            ):
                raise ValueError("发布物料 Agent 租约无效或已过期")
            state["last_heartbeat_at"] = now_iso()
            self.storage.write_json(self.status_path(paths), state)
            return {"event": "HEARTBEAT_OK", "status": "claimed"}

    def record_artifact_fetch(self, paths: JobPaths, artifact_path: str) -> None:
        with self._lock:
            state = self.status(paths)
            if state.get("status") != "claimed":
                return
            required = {str(item) for item in state.get("required_artifacts") or []}
            if artifact_path not in required:
                return
            fetched = {str(item) for item in state.get("fetched_artifacts") or []}
            fetched.add(artifact_path)
            state["fetched_artifacts"] = sorted(fetched)
            self.storage.write_json(self.status_path(paths), state)

    def submit(self, paths: JobPaths, claim_id: str, response: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            state = self.status(paths)
            if state.get("status") == "completed":
                accepted = self.plan(paths)
                if validate_publishing_plan(response) != accepted:
                    raise ValueError("发布物料任务已经完成，不能用不同响应覆盖")
                return {"event": "SUBMITTED", "idempotent": True, "plan": accepted}
            if state.get("status") != "claimed" or not secrets.compare_digest(
                str(state.get("claim_id") or ""), str(claim_id or "")
            ):
                raise ValueError("发布物料 Agent 租约无效或已过期")
            missing = sorted(
                set(str(item) for item in state.get("required_artifacts") or [])
                - set(str(item) for item in state.get("fetched_artifacts") or [])
            )
            if missing:
                raise ValueError(f"尚未读取全部发布素材: {missing}")
            accepted = validate_publishing_plan(response)
            self.storage.write_json(self.plan_path(paths), accepted)
            state.update({"status": "completed", "completed_at": now_iso(), "claim_id": None})
            self.storage.write_json(self.status_path(paths), state)
            return {"event": "SUBMITTED", "idempotent": False, "plan": accepted}
