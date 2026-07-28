from __future__ import annotations

import json
import math
import re
import secrets
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import video_dedup
from recap.models import RecapProject, RecapSegment, natural_path_key, now_iso
from recap.narration_text import canonical_language
from recap.pacing import (
    build_duration_budget,
    get_preset,
    normalize_preset,
    segment_pacing,
)
from recap.project_store import create_project, delete_segment, load_project, save_new_version, update_segment
from recap.renderer import RenderCancelled, inspect_sources, measure_project_loudness, render_project
from recap.timeline import validate_source_intervals
from recap.tts_routing import resolve_tts_engine
from recap.voice_library import VoiceLibrary, engines_for_language

from .agent_orchestration import (
    RECAP_STAGES,
    canonical_digest,
    require_text,
    stage_token,
    validate_episode_indexes,
    validate_execution,
    validate_review_payload,
)
from .storage import JobPaths, JobStorage
from .security import normalize_public_recap_rendering


def safe_project_id(value: str) -> str:
    text = re.sub(r"[^0-9A-Za-z_-]+", "-", str(value).strip()).strip("-").casefold()
    return text[:80] or "recap-project"


def subtitle_language_code(value: str) -> str:
    normalized = str(value or "").strip().casefold()
    aliases = {
        "arabic": "ar", "阿拉伯语": "ar",
        "english": "en", "英语": "en",
        "chinese": "zh", "中文": "zh", "mandarin": "zh",
    }
    if normalized in aliases:
        return aliases[normalized]
    cleaned = "".join(char for char in normalized if char.isascii() and (char.isalnum() or char == "-"))
    return cleaned[:16] or "und"


class RecapService:
    """HTTP-facing adapter around the existing recap project modules."""

    def __init__(self, storage: JobStorage):
        self.storage = storage
        self.library = VoiceLibrary()
        self._render_lock = threading.Lock()
        self._running: set[str] = set()
        self._state_lock = threading.Lock()

    @staticmethod
    def project_path(paths: JobPaths) -> Path:
        return paths.config / "recap-project.json"

    @staticmethod
    def status_path(paths: JobPaths) -> Path:
        return paths.logs / "recap-status.json"

    @staticmethod
    def planning_status_path(paths: JobPaths) -> Path:
        return paths.agent / "recap-planning-status.json"

    @staticmethod
    def planning_request_path(paths: JobPaths) -> Path:
        return paths.agent / "recap-planning-request.json"

    @staticmethod
    def planning_response_path(paths: JobPaths) -> Path:
        return paths.agent / "recap-planning-response.json"

    @staticmethod
    def planning_stages_path(paths: JobPaths) -> Path:
        return paths.agent / "recap-stages"

    def _stage_records(self, paths: JobPaths) -> list[dict[str, Any]]:
        root = self.planning_stages_path(paths)
        records: list[dict[str, Any]] = []
        for index, stage in enumerate(RECAP_STAGES, 1):
            path = root / f"{index:02d}-{stage}.json"
            if not path.is_file():
                break
            records.append(json.loads(path.read_text(encoding="utf-8-sig")))
        return records

    @staticmethod
    def _final_revision_review_target(record: dict[str, Any]) -> dict[str, str]:
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        segments = payload.get("segments") if isinstance(payload.get("segments"), list) else []
        return {
            "stage": "final_revision",
            "artifact_path": "agent/recap-stages/04-final_revision.json",
            "stage_token": str(record.get("stage_token") or ""),
            "payload_sha256": canonical_digest(payload),
            "segments_sha256": canonical_digest(segments),
        }

    def stage_contract(self, paths: JobPaths, stage: str) -> dict[str, Any]:
        if stage != "final_verification":
            return {"stage": stage}
        records = self._stage_records(paths)
        if not records or records[-1].get("stage") != "final_revision":
            raise ValueError("final_verification 尚无可审核的 final_revision")
        return {
            "stage": stage,
            "review_target": self._final_revision_review_target(records[-1]),
            "accepted_binding_fields": [
                "reviewed_stage_token",
                "reviewed_payload_sha256",
                "checked_revision_digest",
            ],
            "binding_rule": "至少提交一个字段；如果提交多个，所有字段都必须匹配 review_target。",
        }

    @staticmethod
    def _subtitle_assets(paths: JobPaths, videos: list[Path], target_language: str) -> list[dict[str, Any]]:
        manifest_path = paths.subtitles / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError, TypeError):
            manifest = {"entries": {}}
        manifest_entries = manifest.get("entries") if isinstance(manifest, dict) else {}
        if not isinstance(manifest_entries, dict):
            manifest_entries = {}

        by_video: dict[str, list[dict[str, Any]]] = {video.name: [] for video in videos}
        seen: set[tuple[str, str]] = set()

        def append_asset(video_name: str, asset: dict[str, Any]) -> None:
            if video_name not in by_video:
                return
            key = (video_name, str(asset["artifact"]))
            if key in seen:
                return
            seen.add(key)
            by_video[video_name].append(asset)

        for entry in manifest_entries.values():
            if not isinstance(entry, dict):
                continue
            video_name = str(entry.get("source_name") or "")
            subtitle_name = str(entry.get("subtitle_file") or "")
            subtitle_path = paths.subtitles / subtitle_name
            if video_name not in by_video or not subtitle_path.is_file() or subtitle_path.suffix.casefold() != ".srt":
                continue
            kind = str(entry.get("asset_kind") or "translation_final")
            language = str(
                entry.get("source_language")
                if kind == "source_repaired"
                else entry.get("target_language")
                or entry.get("source_language")
                or entry.get("language_code")
                or ""
            )
            append_asset(
                video_name,
                {
                    "artifact": f"subtitles/{subtitle_name}",
                    "asset_kind": kind,
                    "language": language,
                    "language_code": str(entry.get("language_code") or subtitle_language_code(language)),
                    "repair_method": entry.get("repair_method"),
                    "paired_source_file": entry.get("paired_source_file"),
                },
            )

        # Compatibility with version-1 manifests and manually copied subtitle
        # folders. The filename remains a trustworthy fallback for role and
        # language, but never for episode ordering.
        for video in videos:
            prefixes = (video.name, video.stem)
            patterns = [
                re.compile(rf"^{re.escape(prefix)}\.(source|final)\.([^.]+)\.srt$", re.IGNORECASE)
                for prefix in prefixes
            ]
            candidates = {
                path
                for prefix in prefixes
                for path in paths.subtitles.glob(f"{prefix}.*.srt")
            }
            for path in sorted(candidates, key=natural_path_key):
                match = next((pattern.match(path.name) for pattern in patterns if pattern.match(path.name)), None)
                if not match:
                    continue
                role, language_code = match.groups()
                append_asset(
                    video.name,
                    {
                        "artifact": f"subtitles/{path.name}",
                        "asset_kind": "source_repaired" if role.casefold() == "source" else "translation_final",
                        "language": language_code,
                        "language_code": language_code.casefold(),
                        "repair_method": None,
                        "paired_source_file": None,
                    },
                )

        target_code = subtitle_language_code(target_language)
        episode_assets: list[dict[str, Any]] = []
        for index, video in enumerate(videos, 1):
            assets = sorted(
                by_video[video.name],
                key=lambda asset: (
                    asset["language_code"] != target_code,
                    asset["asset_kind"] != "translation_final",
                    asset["artifact"],
                ),
            )
            translations = [asset for asset in assets if asset["asset_kind"] == "translation_final"]
            sources = [asset for asset in assets if asset["asset_kind"] == "source_repaired"]
            matching_translations = [asset for asset in translations if asset["language_code"] == target_code]
            matching_sources = [asset for asset in sources if asset["language_code"] == target_code]
            primary = next(
                iter(matching_translations or matching_sources or sources or translations),
                None,
            )
            paired_source = None
            if primary and primary.get("paired_source_file"):
                expected_artifact = f"subtitles/{primary['paired_source_file']}"
                paired_source = next(
                    (asset for asset in sources if asset["artifact"] == expected_artifact),
                    None,
                )
            selected_source = paired_source or next(iter(sources), None)
            selected_translations = matching_translations[:1]
            selected_assets = []
            for asset in (selected_source, primary):
                if asset and asset["artifact"] not in {
                    selected["artifact"] for selected in selected_assets
                }:
                    selected_assets.append(asset)
            episode_assets.append(
                {
                    "episode": index,
                    "name": video.name,
                    "subtitle_artifact": primary["artifact"] if primary else None,
                    "subtitle_assets": {
                        "target_language": target_language,
                        "target_language_code": target_code,
                        "primary": primary,
                        "sources": [selected_source] if selected_source else [],
                        "translations": selected_translations,
                        "all": selected_assets,
                    },
                }
            )
        return episode_assets

    def voices(self) -> list[dict[str, Any]]:
        self.library = self.library.reload()
        return [
            {
                "voice_id": profile.voice_id,
                "display_name": profile.display_name,
                "gender": profile.gender,
                "languages": profile.languages,
                "reference_language": profile.reference_language,
                "allowed_engines": profile.allowed_engines,
                "style": profile.style,
                "age_group": profile.age_group,
                "role_archetype": profile.role_archetype,
                "source_kind": profile.source_kind,
                "review_status": profile.review_status,
                "quality_score": profile.quality_score,
                "default_speed": profile.default_speed,
                "preview_available": bool(
                    profile.preview_audio and self.library.resolve_asset(profile.preview_audio).is_file()
                ),
            }
            for profile in self.library.list()
        ]

    def preview_path(self, voice_id: str) -> Path:
        self.library = self.library.reload()
        profile = self.library.get(voice_id)
        path = self.library.resolve_asset(profile.preview_audio)
        if not path.is_file():
            raise FileNotFoundError(path)
        return path

    @staticmethod
    def engines() -> dict[str, list[dict[str, str]]]:
        return {
            language: engines_for_language(language)
            for language in ("English", "Arabic", "Chinese")
        }

    def _validate_voice_selection(self, language: str, engine: str, voice_id: str) -> None:
        resolved = resolve_tts_engine(language, engine)
        compatible = {item.voice_id for item in self.library.compatible(language, resolved)}
        if voice_id not in compatible:
            raise ValueError(f"声纹 {voice_id} 不支持 {canonical_language(language)} / {resolved}")

    def _voice_speech_rate(self, project: RecapProject) -> float | None:
        profile = self.library.get(project.voice_id)
        configured = dict(profile.speech_rate or {})
        engines = configured.get("engines")
        if isinstance(engines, dict):
            resolved = resolve_tts_engine(project.target_language, project.tts_engine)
            value = engines.get(resolved)
            if value is not None:
                return float(value)
        value = configured.get("value", configured.get("default"))
        return None if value is None else float(value)

    def create(self, paths: JobPaths, payload: dict[str, Any]) -> RecapProject:
        self.library = self.library.reload()
        name = str(payload.get("project_name") or "远程解说项目").strip()
        # Uploaded project subtitle finals live beside the source videos first.
        # Mirror them into the immutable result area used by recap projects.
        self.storage.collect_subtitle_finals(paths)
        episode_pattern = str(payload.get("episode_pattern") or "*.mp4")
        requested_duration = payload.get("target_duration_seconds")
        narration_preset = normalize_preset(payload.get("narration_preset"))
        pacing_preset = get_preset(narration_preset)
        rendering = normalize_public_recap_rendering(payload.get("rendering"))
        if requested_duration is None:
            ffprobe = video_dedup.find_binary("ffprobe", rendering.get("ffprobe"))
            source_info = inspect_sources(paths.input, ffprobe, episode_pattern)
            if source_info["episode_count"] < 1 or source_info["total_duration"] <= 0:
                raise ValueError("无法按解说预设自动计算成片时长：没有可读取的源视频")
            target_duration = round(
                float(source_info["total_duration"]) * pacing_preset.target_ratio, 3
            )
            rendering["target_duration_ratio"] = pacing_preset.target_ratio
        else:
            target_duration = float(requested_duration)
        project_payload = {
            "schema_version": 1,
            "project_id": safe_project_id(str(payload.get("project_id") or name)),
            "project_name": name,
            "source_root": str(paths.input),
            "episode_pattern": episode_pattern,
            "subtitle_root": str(paths.subtitles),
            "output_root": str(paths.videos / "recap"),
            "target_language": str(payload.get("target_language") or "English"),
            "target_duration_seconds": target_duration,
            "narration_preset": narration_preset,
            "voice_id": str(payload.get("voice_id") or "calm_female"),
            "tts_engine": str(payload.get("tts_engine") or "auto").casefold(),
            "narration_speed": float(payload.get("narration_speed", 1.0)),
            "narration_target_loudness": payload.get("narration_target_loudness", "keep_original"),
            "segments": list(payload.get("segments") or []),
            "current_version": 1,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "rendering": rendering,
        }
        self.library.get(project_payload["voice_id"])
        self._validate_voice_selection(
            project_payload["target_language"],
            project_payload["tts_engine"],
            project_payload["voice_id"],
        )
        return create_project(self.project_path(paths), project_payload)

    def load(self, paths: JobPaths) -> RecapProject:
        return load_project(self.project_path(paths))

    def planning_status(self, paths: JobPaths) -> dict[str, Any]:
        path = self.planning_status_path(paths)
        if not path.is_file():
            return {"status": "not_queued"}
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {"status": "unknown", "error": "解说 Agent 状态文件损坏"}

    def queue_planning(self, job: dict[str, Any], paths: JobPaths) -> dict[str, Any]:
        project = self.load(paths)
        subtitle_files = sorted(paths.subtitles.glob("*.srt"), key=natural_path_key)
        if not subtitle_files:
            raise ValueError("没有可用字幕终稿；请先完成字幕阶段，或把 SRT 放入字幕终稿目录")
        videos = sorted(
            (path for path in paths.input.iterdir() if path.is_file() and path.suffix.casefold() in video_dedup.VIDEO_SUFFIXES),
            key=natural_path_key,
        )
        episode_assets = self._subtitle_assets(paths, videos, project.target_language)
        missing_subtitle_episodes = [
            int(episode["episode"])
            for episode in episode_assets
            if not episode["subtitle_assets"]["all"]
        ]
        if missing_subtitle_episodes:
            raise ValueError(
                "以下视频没有匹配到任何可用字幕，无法建立解说证据清单："
                f"{missing_subtitle_episodes}"
            )
        selected_subtitle_artifacts = []
        for episode in episode_assets:
            for asset in episode["subtitle_assets"]["all"]:
                artifact = asset["artifact"]
                if artifact not in selected_subtitle_artifacts:
                    selected_subtitle_artifacts.append(artifact)
        minimum_segments = max(1, math.ceil(project.target_duration_seconds / 30.0))
        if len(videos) <= 1:
            minimum_episode_coverage = 1
        elif project.target_duration_seconds >= 60:
            minimum_episode_coverage = math.ceil(len(videos) * 0.8)
        else:
            minimum_episode_coverage = min(
                len(videos),
                max(1, math.ceil(project.target_duration_seconds / 30.0)),
            )
        request = {
            "schema_version": 1,
            "task_type": "recap_plan",
            "external_job_id": str(job["id"]),
            "project_id": project.project_id,
            "project_name": project.project_name,
            "target_language": project.target_language,
            "target_duration_seconds": project.target_duration_seconds,
            "narration_preset": project.narration_preset,
            "duration_budget": build_duration_budget(
                preset_name=project.narration_preset,
                target_duration_seconds=project.target_duration_seconds,
                target_language=project.target_language,
                narration_speed=project.narration_speed,
                speech_rate_override=self._voice_speech_rate(project),
            ),
            "voice_id": project.voice_id,
            "tts_engine": project.tts_engine,
            "episode_pattern": project.episode_pattern,
            "subtitle_selection_policy": {
                "match_by": "source_video_name",
                "primary_language": project.target_language,
                "primary_preference": [
                    "translation_final_in_target_language",
                    "source_repaired_in_target_language",
                    "source_repaired_in_source_language",
                    "other_translation_as_last_resort",
                ],
                "semantic_rule": (
                    "Use the selected primary subtitle for target-language narration and cutting. "
                    "Use repaired source subtitles as authoritative semantic evidence. "
                    "Never translate through an intermediate translation when a repaired source is available."
                ),
            },
            "episodes": episode_assets,
            "subtitle_artifacts": selected_subtitle_artifacts,
            "completion_contract": {
                "verification": "server_observed_fetch_manifest",
                "rules_bundle_required": True,
                "expected_episode_indexes": [
                    int(episode["episode"]) for episode in episode_assets
                ],
                "required_artifacts": selected_subtitle_artifacts,
                "hardcoded_counts_forbidden": True,
                "instruction": (
                    "Fetch every path in required_artifacts and process every "
                    "expected_episode_indexes entry. These arrays are the only "
                    "completion totals; never assume a fixed episode or file count. "
                    "The server records successful GET requests and rejects a submit "
                    "whose dynamic manifest has not been completely fetched."
                ),
            },
            "orchestration_contract": {
                "native_subagents_required": True,
                "context_isolation_required": True,
                "stages": list(RECAP_STAGES),
                "stage_order_enforced_by_server": True,
                "reviewer_must_not_receive": [
                    "creator_reasoning",
                    "creator_self_score",
                    "user_satisfaction",
                ],
                "instruction": (
                    "Use the stage_endpoint and role_rules endpoints supplied by the event. "
                    "Each accepted stage returns the token required by the next stage."
                ),
            },
            "validation_policy": {
                "minimum_segment_count": minimum_segments,
                "minimum_episode_coverage": minimum_episode_coverage,
                "minimum_selected_source_seconds": round(project.target_duration_seconds * 0.5, 3),
                "unicode_text_required": True,
                "placeholder_or_question_mark_garbage_rejected": True,
                "all_declared_creative_materials_required": True,
                "narration_pacing_required": project.narration_preset != "legacy",
                "instruction": (
                    "Return a complete timeline for the requested target duration and the whole drama, "
                    "not a sample or one-episode draft. Preserve real UTF-8 target-language text. "
                    "Do not submit question-mark placeholders, mojibake, watermark text, UI text, or logos. "
                    "Before drafting prose, use duration_budget to allocate approximate narration units. "
                    "Keep each narration segment inside the preset occupancy range. If the story does not "
                    "need enough words, shorten that narration interval and use valuable original footage "
                    "elsewhere; never pad narration with repetition."
                ),
            },
            "required_response": {
                "schema_version": 1,
                "task_type": "recap_plan",
                "project_id": project.project_id,
                "segments": [{
                    "segment_id": "stable unique string",
                    "episode": "integer >= 1",
                    "source_start": "seconds",
                    "source_end": "seconds",
                    "mode": "narration or original",
                    "narration_text": "required for narration",
                    "purpose": "hook/identity/payoff/transition/cliffhanger/etc.",
                    "rendering": {},
                }],
                "creative_materials": {
                    "story_bible.md": "full UTF-8 markdown",
                    "subtitle_evidence.md": "full UTF-8 markdown",
                    "beat_sheet.md": "full UTF-8 markdown",
                    "hook_candidates.md": "full UTF-8 markdown",
                    "timeline_notes.md": "full UTF-8 markdown",
                    "revision_log.md": "full UTF-8 markdown",
                },
                "quality": {
                    "score": "0-100, must be >= 85",
                    "summary": "brief self-review",
                    "known_risks": [],
                },
            },
        }
        with self._state_lock:
            previous = self.planning_status(paths)
            planning_attempt = max(0, int(previous.get("planning_attempt") or 0)) + 1
            planning_attempt_id = secrets.token_urlsafe(18)
            request["planning_attempt"] = planning_attempt
            request["planning_attempt_id"] = planning_attempt_id
            request["required_response"]["planning_attempt_id"] = planning_attempt_id
            self.storage.write_json(self.planning_request_path(paths), request)
            self.planning_response_path(paths).unlink(missing_ok=True)
            stage_root = self.planning_stages_path(paths)
            stage_root.mkdir(parents=True, exist_ok=True)
            for path in stage_root.glob("*.json"):
                path.unlink()
            state = {
                "status": "pending",
                "queued_at": now_iso(),
                "project_id": project.project_id,
                "episode_count": len(videos),
                "subtitle_count": len(selected_subtitle_artifacts),
                "planning_attempt": planning_attempt,
                "planning_attempt_id": planning_attempt_id,
                "rules_fetched_at": None,
                "fetched_artifacts": [],
                "fetched_episode_indexes": [],
                "completed_stages": [],
                "current_stage": RECAP_STAGES[0],
                "fetched_role_rules": [],
            }
            self.storage.write_json(self.planning_status_path(paths), state)
            return state

    def cancel_planning(self, paths: JobPaths) -> dict[str, Any]:
        with self._state_lock:
            state = self.planning_status(paths)
            if state.get("status") in {"pending", "claimed", "blocked"}:
                state = {
                    **state,
                    "status": "cancelled",
                    "cancelled_at": now_iso(),
                }
                self.storage.write_json(self.planning_status_path(paths), state)
            return state

    @staticmethod
    def _seconds_since(value: str | None) -> float:
        if not value:
            return float("inf")
        try:
            return max(0.0, (datetime.now().astimezone() - datetime.fromisoformat(value)).total_seconds())
        except ValueError:
            return float("inf")

    def claim_planning(
        self, job: dict[str, Any], paths: JobPaths, base_url: str,
    ) -> dict[str, Any] | None:
        with self._state_lock:
            state = self.planning_status(paths)
            status = state.get("status")
            if status == "claimed" and self._seconds_since(state.get("last_heartbeat_at")) < 180:
                return None
            if status not in {"pending", "claimed"}:
                return None
            resume = status == "claimed"
            state = {
                **state,
                "status": "claimed",
                "claimed_at": state.get("claimed_at") or now_iso(),
                "last_heartbeat_at": now_iso(),
            }
            self.storage.write_json(self.planning_status_path(paths), state)
        request = json.loads(self.planning_request_path(paths).read_text(encoding="utf-8-sig"))
        prefix = f"{base_url.rstrip('/')}/api/v1/agent/jobs/{job['id']}/recap"
        return {
            "event": "RECAP_JOB_RESUME" if resume else "RECAP_JOB",
            "external_job_id": str(job["id"]),
            "planning_attempt": state.get("planning_attempt"),
            "planning_attempt_id": state.get("planning_attempt_id"),
            "execution_contract": {
                "atomic": True,
                "intermediate_steps_are_not_terminal": True,
                "progress_updates_must_be_non_final": True,
                "continue_without_user_prompt": True,
                "terminal_outcomes": [
                    "submit_confirmed",
                    "server_cancelled",
                    "stop_all",
                    "registration_invalid",
                    "non_retryable_error_after_required_repairs",
                ],
            },
            "request": request,
            "completion_contract": request.get("completion_contract", {}),
            "orchestration_contract": request.get("orchestration_contract", {}),
            "rules_endpoint": f"{prefix}/rules",
            "role_rules_endpoint_template": f"{prefix}/roles/{{stage}}",
            "stage_endpoint_template": f"{prefix}/stages/{{stage}}",
            "heartbeat_endpoint": (
                f"{prefix}/heartbeat?planning_attempt_id="
                f"{state.get('planning_attempt_id') or ''}"
            ),
            "submit_endpoint": f"{prefix}/submit",
            "artifacts_endpoint": f"{base_url.rstrip('/')}/api/v1/agent/jobs/{job['id']}/artifacts",
            "retry_after_seconds": 60,
        }

    def submit_stage(
        self,
        paths: JobPaths,
        stage: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        with self._state_lock:
            state = self.planning_status(paths)
            if state.get("status") not in {"pending", "claimed"}:
                raise RuntimeError(
                    f"解说任务当前不能提交阶段：{state.get('status', 'unknown')}"
                )
            expected_attempt = str(state.get("planning_attempt_id") or "")
            received_attempt = str(payload.get("planning_attempt_id") or "")
            if expected_attempt and received_attempt != expected_attempt:
                raise ValueError("planning_attempt_id 已过期；请停止旧执行并重新领取最新解说任务")
            records = self._stage_records(paths)
            expected_index = len(records)
            if expected_index >= len(RECAP_STAGES):
                raise RuntimeError("全部 Agent 阶段均已完成，请提交最终方案")
            expected_stage = RECAP_STAGES[expected_index]
            if stage != expected_stage or str(payload.get("stage") or "") != stage:
                raise ValueError(
                    f"阶段顺序错误：当前必须提交 {expected_stage}，不能提交 {stage}"
                )
            if stage not in {
                str(value) for value in state.get("fetched_role_rules", [])
            }:
                raise ValueError(f"Agent 尚未读取当前阶段角色规则：{stage}")
            if int(payload.get("schema_version", 0)) != 1:
                raise ValueError("阶段 schema_version 必须是 1")
            previous_token = records[-1]["stage_token"] if records else ""
            if str(payload.get("previous_stage_token") or "") != previous_token:
                raise ValueError("previous_stage_token 与服务器最新阶段不一致")
            execution = validate_execution(stage, payload, records)
            request = json.loads(
                self.planning_request_path(paths).read_text(encoding="utf-8-sig")
            )
            self._validate_completion_manifest(request, state)
            project = self.load(paths)

            if stage == "story_analysis":
                validate_episode_indexes(
                    payload,
                    [
                        int(value)
                        for value in request["completion_contract"][
                            "expected_episode_indexes"
                        ]
                    ],
                )
                require_text(payload, "story_bible", minimum=120)
                require_text(payload, "subtitle_evidence", minimum=120)
            elif stage == "recap_draft":
                require_text(payload, "beat_sheet", minimum=80)
                require_text(payload, "hook_candidates", minimum=80)
                require_text(payload, "timeline_notes", minimum=80)
                raw_segments = payload.get("segments")
                if not isinstance(raw_segments, list) or not raw_segments:
                    raise ValueError("recap_draft 必须返回完整 segments")
                segments = [RecapSegment.from_dict(item) for item in raw_segments]
                self._validate_agent_timeline(project, segments, state)
                self._validate_repeated_narration(segments)
            elif stage == "independent_review":
                validate_review_payload(payload, final=False)
                require_text(payload, "review_summary", minimum=40)
            elif stage == "final_revision":
                raw_segments = payload.get("segments")
                if not isinstance(raw_segments, list) or not raw_segments:
                    raise ValueError("final_revision 必须返回完整 segments")
                segments = [RecapSegment.from_dict(item) for item in raw_segments]
                self._validate_agent_timeline(project, segments, state)
                self._validate_repeated_narration(segments)
                require_text(payload, "revision_log", minimum=40)
                review = records[-1]["payload"]
                blocking_ids = {
                    str(item["issue_id"])
                    for item in review.get("issues", [])
                    if str(item.get("severity") or "").casefold()
                    in {"critical", "major"}
                }
                resolved_ids = {
                    str(value)
                    for value in payload.get("resolved_issue_ids", [])
                    if str(value).strip()
                }
                if not blocking_ids.issubset(resolved_ids):
                    raise ValueError(
                        "final_revision 未处理全部 critical/major 问题："
                        f"{sorted(blocking_ids - resolved_ids)}"
                    )
            elif stage == "final_verification":
                score, verdict, _issues = validate_review_payload(
                    payload, final=True
                )
                require_text(payload, "review_summary", minimum=40)
                review_target = self._final_revision_review_target(records[-1])
                review_fetch = (
                    state.get("final_revision_fetch")
                    if isinstance(state.get("final_revision_fetch"), dict)
                    else {}
                )
                if (
                    str(review_fetch.get("stage_token") or "") != review_target["stage_token"]
                    or str(review_fetch.get("agent_run_id") or "") != execution["agent_run_id"]
                ):
                    raise ValueError(
                        "The final verifier has not fetched the latest revision with its own agent_run_id"
                    )
                nested_target = (
                    payload.get("review_target")
                    if isinstance(payload.get("review_target"), dict)
                    else {}
                )
                provided = {
                    "reviewed_stage_token": str(
                        payload.get("reviewed_stage_token")
                        or nested_target.get("stage_token")
                        or ""
                    ),
                    "reviewed_payload_sha256": str(
                        payload.get("reviewed_payload_sha256")
                        or nested_target.get("payload_sha256")
                        or ""
                    ),
                    "checked_revision_digest": str(
                        payload.get("checked_revision_digest")
                        or nested_target.get("segments_sha256")
                        or ""
                    ),
                }
                expected = {
                    "reviewed_stage_token": review_target["stage_token"],
                    "reviewed_payload_sha256": review_target["payload_sha256"],
                    "checked_revision_digest": review_target["segments_sha256"],
                }
                supplied = {key: value for key, value in provided.items() if value}
                if not supplied:
                    raise ValueError(
                        "final_verification 缺少审核对象绑定；请使用角色规则返回的 review_target，"
                        "提交 reviewed_stage_token、reviewed_payload_sha256 或 checked_revision_digest"
                    )
                mismatched = [key for key, value in supplied.items() if value != expected[key]]
                if mismatched:
                    raise ValueError(
                        "最终审核检查的不是服务器保存的最新修订版本；不匹配字段："
                        f"{', '.join(mismatched)}。请重新读取 final_verification 角色规则中的 review_target"
                    )
                payload = {
                    **payload,
                    "score": score,
                    "verdict": verdict,
                    "reviewed_stage_token": review_target["stage_token"],
                    "reviewed_payload_sha256": review_target["payload_sha256"],
                    "checked_revision_digest": review_target["segments_sha256"],
                    "reviewed_artifact_path": review_target["artifact_path"],
                }

            token = stage_token(stage, payload)
            record = {
                "schema_version": 1,
                "stage": stage,
                "role": execution["role"],
                "agent_run_id": execution["agent_run_id"],
                "context_isolated": execution["context_isolated"],
                "accepted_at": now_iso(),
                "stage_token": token,
                "payload": payload,
            }
            root = self.planning_stages_path(paths)
            root.mkdir(parents=True, exist_ok=True)
            self.storage.write_json(
                root / f"{expected_index + 1:02d}-{stage}.json", record
            )
            completed = [
                *state.get("completed_stages", []),
                {
                    "stage": stage,
                    "role": execution["role"],
                    "agent_run_id": execution["agent_run_id"],
                    "accepted_at": record["accepted_at"],
                    "stage_token": token,
                },
            ]
            next_stage = (
                RECAP_STAGES[expected_index + 1]
                if expected_index + 1 < len(RECAP_STAGES)
                else "final_submit"
            )
            state = {
                **state,
                "completed_stages": completed,
                "current_stage": next_stage,
                "last_heartbeat_at": now_iso(),
            }
            next_stage_contract: dict[str, Any] | None = None
            if stage == "final_revision":
                next_stage_contract = {
                    "stage": "final_verification",
                    "review_target": self._final_revision_review_target(record),
                    "accepted_binding_fields": [
                        "reviewed_stage_token",
                        "reviewed_payload_sha256",
                        "checked_revision_digest",
                    ],
                }
                state["next_stage_contract"] = next_stage_contract
            elif stage == "final_verification":
                state.pop("next_stage_contract", None)
            self.storage.write_json(self.planning_status_path(paths), state)
            response = {
                "status": "accepted",
                "stage": stage,
                "stage_token": token,
                "next_stage": next_stage,
            }
            if next_stage_contract is not None:
                response["next_stage_contract"] = next_stage_contract
            return response

    def record_role_rules_fetch(
        self,
        paths: JobPaths,
        stage: str,
    ) -> dict[str, Any]:
        with self._state_lock:
            state = self.planning_status(paths)
            if state.get("status") not in {"pending", "claimed"}:
                return state
            fetched = {
                str(value) for value in state.get("fetched_role_rules", [])
            }
            fetched.add(stage)
            state = {
                **state,
                "fetched_role_rules": sorted(fetched),
                "last_heartbeat_at": now_iso(),
            }
            self.storage.write_json(self.planning_status_path(paths), state)
            return state

    def planning_heartbeat(
        self, paths: JobPaths, planning_attempt_id: str = "",
    ) -> dict[str, Any]:
        with self._state_lock:
            state = self.planning_status(paths)
            if state.get("status") not in {"claimed", "pending"}:
                return state
            expected_attempt = str(state.get("planning_attempt_id") or "")
            if expected_attempt and str(planning_attempt_id or "") != expected_attempt:
                raise ValueError("planning_attempt_id 已过期；旧解说执行不能续写新任务心跳")
            state = {**state, "status": "claimed", "last_heartbeat_at": now_iso()}
            self.storage.write_json(self.planning_status_path(paths), state)
            return state

    def record_rules_fetch(self, paths: JobPaths) -> dict[str, Any]:
        """Record a successful read of the task-specific bundled rule document."""
        with self._state_lock:
            state = self.planning_status(paths)
            if state.get("status") not in {"pending", "claimed"}:
                return state
            state = {
                **state,
                "rules_fetched_at": now_iso(),
                "last_heartbeat_at": now_iso(),
            }
            self.storage.write_json(self.planning_status_path(paths), state)
            return state

    def record_artifact_fetch(
        self,
        paths: JobPaths,
        artifact_path: str,
        *,
        agent_run_id: str = "",
    ) -> dict[str, Any]:
        """Record a successful artifact GET against the current dynamic manifest."""
        with self._state_lock:
            state = self.planning_status(paths)
            if state.get("status") not in {"pending", "claimed"}:
                return state
            request_path = self.planning_request_path(paths)
            if not request_path.is_file():
                return state
            request = json.loads(request_path.read_text(encoding="utf-8-sig"))
            required = {
                str(value)
                for value in request.get("subtitle_artifacts", [])
                if str(value).strip()
            }
            normalized = str(artifact_path).replace("\\", "/").lstrip("/")
            fetched = {
                str(value)
                for value in state.get("fetched_artifacts", [])
                if str(value).strip()
            }
            if normalized in required:
                fetched.add(normalized)

            fetched_episodes: list[int] = []
            for episode in request.get("episodes", []):
                if not isinstance(episode, dict):
                    continue
                assets = episode.get("subtitle_assets")
                selected = assets.get("all", []) if isinstance(assets, dict) else []
                episode_required = {
                    str(asset.get("artifact"))
                    for asset in selected
                    if isinstance(asset, dict) and str(asset.get("artifact") or "").strip()
                }
                if episode_required and episode_required.issubset(fetched):
                    fetched_episodes.append(int(episode["episode"]))

            state = {
                **state,
                "fetched_artifacts": sorted(fetched),
                "fetched_episode_indexes": sorted(set(fetched_episodes)),
                "last_artifact_fetch_at": now_iso(),
                "last_heartbeat_at": now_iso(),
            }
            if normalized == "agent/recap-stages/04-final_revision.json":
                records = self._stage_records(paths)
                if (
                    records
                    and records[-1].get("stage") == "final_revision"
                    and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{5,127}", str(agent_run_id or ""))
                ):
                    state["final_revision_fetch"] = {
                        "agent_run_id": str(agent_run_id),
                        "stage_token": str(records[-1].get("stage_token") or ""),
                        "artifact_path": normalized,
                        "fetched_at": now_iso(),
                    }
            self.storage.write_json(self.planning_status_path(paths), state)
            return state

    @staticmethod
    def _validate_completion_manifest(
        request: dict[str, Any], state: dict[str, Any],
    ) -> None:
        contract = (
            request.get("completion_contract")
            if isinstance(request.get("completion_contract"), dict)
            else {}
        )
        required_artifacts = {
            str(value)
            for value in (
                contract.get("required_artifacts")
                if isinstance(contract.get("required_artifacts"), list)
                else request.get("subtitle_artifacts", [])
            )
            if str(value).strip()
        }
        expected_episodes = {
            int(value)
            for value in (
                contract.get("expected_episode_indexes")
                if isinstance(contract.get("expected_episode_indexes"), list)
                else [
                    episode.get("episode")
                    for episode in request.get("episodes", [])
                    if isinstance(episode, dict)
                ]
            )
        }
        fetched_artifacts = {
            str(value)
            for value in state.get("fetched_artifacts", [])
            if str(value).strip()
        }
        fetched_episodes = {
            int(value) for value in state.get("fetched_episode_indexes", [])
        }
        if not state.get("rules_fetched_at"):
            raise ValueError("Agent 尚未读取当前任务的解说规则包")
        missing_artifacts = sorted(required_artifacts - fetched_artifacts)
        missing_episodes = sorted(expected_episodes - fetched_episodes)
        if missing_artifacts or missing_episodes:
            details = []
            if missing_artifacts:
                details.append(f"未读取字幕材料 {missing_artifacts}")
            if missing_episodes:
                details.append(f"未完成材料读取的集 {missing_episodes}")
            raise ValueError(
                "Agent 动态输入清单未完成：" + "；".join(details)
                + "。请按 completion_contract 读取后重提，不要使用固定数量判断。"
            )

    def submit_planning(self, paths: JobPaths, response: dict[str, Any]) -> dict[str, Any]:
        with self._state_lock:
            state = self.planning_status(paths)
            response_digest = canonical_digest(response)
            if state.get("status") == "completed":
                if state.get("final_response_digest") == response_digest:
                    return {
                        "status": "submitted",
                        "idempotent": True,
                        "project": self.load(paths).to_dict(),
                        "planning": state,
                    }
                raise RuntimeError("解说任务已经完成，不能提交不同的最终结果")
            if state.get("status") not in {"pending", "claimed"}:
                raise RuntimeError(f"解说任务当前不能提交：{state.get('status', 'unknown')}")
            expected_attempt = str(state.get("planning_attempt_id") or "")
            received_attempt = str(response.get("planning_attempt_id") or "")
            if expected_attempt and received_attempt != expected_attempt:
                raise ValueError("planning_attempt_id 已过期；请停止旧执行并重新领取最新解说任务")
            project = self.load(paths)
            if int(response.get("schema_version", 0)) != 1:
                raise ValueError("Agent 返回 schema_version 必须是 1")
            if str(response.get("task_type") or "") != "recap_plan":
                raise ValueError("Agent 返回 task_type 必须是 recap_plan")
            if str(response.get("project_id") or "") != project.project_id:
                raise ValueError("Agent 返回 project_id 与当前项目不一致")
            request = json.loads(
                self.planning_request_path(paths).read_text(encoding="utf-8-sig")
            )
            self._validate_completion_manifest(request, state)
            records = self._stage_records(paths)
            if [record["stage"] for record in records] != list(RECAP_STAGES):
                raise ValueError(
                    "Agent 阶段未完成，当前仅完成："
                    f"{[record['stage'] for record in records]}"
                )
            revision = records[3]["payload"]
            verifier = records[4]["payload"]
            score = float(verifier["score"])
            raw_segments = response.get("segments")
            if not isinstance(raw_segments, list) or not raw_segments:
                raise ValueError("Agent 未返回任何结构化片段")
            if canonical_digest(raw_segments) != canonical_digest(revision["segments"]):
                raise ValueError("最终提交的 segments 与服务器已验收修订版不一致")
            segment_ids: set[str] = set()
            for index, item in enumerate(raw_segments, 1):
                if not isinstance(item, dict):
                    raise ValueError(f"Agent 片段 {index} 必须是对象")
                segment_id = str(item.get("segment_id") or "").strip()
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", segment_id):
                    raise ValueError(f"Agent 片段 {index} 的 segment_id 格式无效")
                if segment_id in segment_ids:
                    raise ValueError(f"Agent segment_id 重复: {segment_id}")
                segment_ids.add(segment_id)
                if not isinstance(item.get("purpose"), str) or not item["purpose"].strip():
                    raise ValueError(f"Agent 片段 {index} 的 purpose 必须是非空字符串")
                if "rendering" in item and not isinstance(item.get("rendering"), dict):
                    raise ValueError(f"Agent 片段 {index} 的 rendering 必须是对象")
            segments = [RecapSegment.from_dict(item) for item in raw_segments]
            self._validate_agent_timeline(project, segments, state)
            project.segments = segments
            errors = validate_source_intervals(
                project,
                lambda path: video_dedup.probe_video(path, video_dedup.find_binary("ffprobe")),
            )
            if errors:
                raise ValueError("Agent 时间轴校验失败: " + json.dumps(errors, ensure_ascii=False))
            materials = {
                "story_bible.md": records[0]["payload"]["story_bible"],
                "subtitle_evidence.md": records[0]["payload"]["subtitle_evidence"],
                "beat_sheet.md": records[1]["payload"]["beat_sheet"],
                "hook_candidates.md": records[1]["payload"]["hook_candidates"],
                "timeline_notes.md": records[1]["payload"]["timeline_notes"],
                "revision_log.md": revision["revision_log"],
            }
            allowed_materials = set(materials)
            updated = save_new_version(self.project_path(paths), project)
            material_root = paths.agent / "recap-creative"
            material_root.mkdir(parents=True, exist_ok=True)
            for name in allowed_materials:
                (material_root / name).write_text(str(materials[name]), encoding="utf-8")
            self.storage.write_json(self.planning_response_path(paths), response)
            final_state = {
                **state,
                "status": "completed",
                "completed_at": now_iso(),
                "project_version": updated.current_version,
                "segment_count": len(updated.segments),
                "quality_score": score,
                "quality_summary": str(verifier.get("review_summary") or ""),
                "independent_review": {
                    "verdict": verifier.get("verdict"),
                    "issues": verifier.get("issues", []),
                    "agent_run_id": records[4]["agent_run_id"],
                    "context_isolated": records[4]["context_isolated"],
                },
                "final_response_digest": response_digest,
                "verified_input_manifest": {
                    "rules_fetched": True,
                    "fetched_artifacts": state.get("fetched_artifacts", []),
                    "fetched_episode_indexes": state.get("fetched_episode_indexes", []),
                },
            }
            self.storage.write_json(self.planning_status_path(paths), final_state)
            return {"status": "submitted", "project": updated.to_dict(), "planning": final_state}

    @staticmethod
    def _validate_repeated_narration(segments: list[RecapSegment]) -> None:
        seen: dict[str, list[int]] = {}
        previous = ""
        for index, segment in enumerate(segments, 1):
            if segment.mode != "narration":
                continue
            normalized = re.sub(
                r"[\W_]+", "", segment.narration_text, flags=re.UNICODE
            ).casefold()
            if not normalized:
                continue
            if normalized == previous:
                raise ValueError(f"相邻解说片段重复：{index - 1} 与 {index}")
            seen.setdefault(normalized, []).append(index)
            previous = normalized
        repeated = {
            text: indexes for text, indexes in seen.items() if len(indexes) >= 3
        }
        if repeated:
            first = next(iter(repeated.values()))
            raise ValueError(f"解说文案存在三次以上完全重复，片段索引：{first}")

    @staticmethod
    def _looks_corrupted_text(text: str) -> bool:
        value = str(text or "").strip()
        if not value:
            return True
        if "\ufffd" in value:
            return True
        if any(ord(char) < 32 and char not in "\n\r\t" for char in value):
            return True
        meaningful = [char for char in value if not char.isspace()]
        if meaningful and sum(char in "?？" for char in meaningful) / len(meaningful) >= 0.35:
            return True
        return False

    def _validate_agent_timeline(
        self,
        project: RecapProject,
        segments: list[RecapSegment],
        state: dict[str, Any],
    ) -> None:
        target = max(0.0, float(project.target_duration_seconds))
        episode_count = max(1, int(state.get("episode_count") or 1))
        minimum_segments = max(1, math.ceil(target / 30.0))
        if len(segments) < minimum_segments:
            raise ValueError(
                f"Agent 时间轴明显不完整：目标 {target:.1f} 秒至少需要 {minimum_segments} 个片段，"
                f"实际仅 {len(segments)} 个"
            )
        covered_episodes = {segment.episode for segment in segments}
        if episode_count <= 1:
            minimum_episode_coverage = 1
        elif target >= 60:
            minimum_episode_coverage = math.ceil(episode_count * 0.8)
        else:
            minimum_episode_coverage = min(episode_count, max(1, math.ceil(target / 30.0)))
        if len(covered_episodes) < minimum_episode_coverage:
            raise ValueError(
                f"Agent 全集覆盖不足：共 {episode_count} 集，至少应覆盖 {minimum_episode_coverage} 集，"
                f"实际仅覆盖 {len(covered_episodes)} 集"
            )
        selected_seconds = sum(max(0.0, segment.source_end - segment.source_start) for segment in segments)
        minimum_selected_seconds = target * 0.5
        if target > 0 and selected_seconds + 0.01 < minimum_selected_seconds:
            raise ValueError(
                f"Agent 时间轴素材过短：目标 {target:.1f} 秒，选取素材仅 {selected_seconds:.1f} 秒"
            )
        language = canonical_language(project.target_language)
        pacing_failures: list[str] = []
        for index, segment in enumerate(segments, 1):
            if segment.mode != "narration":
                continue
            text = segment.narration_text
            if self._looks_corrupted_text(text):
                raise ValueError(f"Agent 片段 {index} 的解说词为空、乱码或问号占位")
            if language == "Arabic" and not re.search(r"[\u0600-\u06ff]", text):
                raise ValueError(f"Agent 片段 {index} 未返回有效阿拉伯语解说词")
            if language == "English" and not re.search(r"[A-Za-z]", text):
                raise ValueError(f"Agent 片段 {index} 未返回有效英语解说词")
            if project.narration_preset != "legacy" and not bool(
                segment.rendering.get("allow_visual_hold", False)
            ):
                pacing = segment_pacing(
                    text=text,
                    duration_seconds=segment.source_end - segment.source_start,
                    target_language=project.target_language,
                    preset_name=project.narration_preset,
                    narration_speed=project.narration_speed,
                    speech_rate_override=self._voice_speech_rate(project),
                )
                if pacing["status"] != "ok":
                    pacing_failures.append(
                        f"{segment.segment_id}: {pacing['units']}{pacing['unit']}，"
                        f"预计占用 {pacing['estimated_occupancy'] * 100:.1f}% ，"
                        f"允许字数/词数 {pacing['allowed_units']}"
                    )
        if pacing_failures:
            raise ValueError(
                "Agent 解说文案与画面时长不匹配："
                + "；".join(pacing_failures)
                + "。请优先重分配画面区间或改写文案，不要用无关内容填充。"
            )

    def update(self, paths: JobPaths, changes: dict[str, Any]) -> RecapProject:
        self.library = self.library.reload()
        project = self.load(paths)
        allowed = {
            "project_name", "episode_pattern", "target_language", "target_duration_seconds",
            "narration_preset", "voice_id", "tts_engine", "narration_speed",
            "narration_target_loudness", "rendering",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"不支持的项目字段: {sorted(unknown)}")
        for key, value in changes.items():
            if key == "voice_id":
                self.library.get(str(value))
            if key in {"target_duration_seconds", "narration_speed"}:
                value = float(value)
            elif key == "narration_preset":
                value = normalize_preset(str(value))
            elif key == "rendering":
                value = normalize_public_recap_rendering(value)
            elif key != "narration_target_loudness":
                value = str(value)
            setattr(project, key, value)
        self._validate_voice_selection(project.target_language, project.tts_engine, project.voice_id)
        return save_new_version(self.project_path(paths), project)

    def add_segment(self, paths: JobPaths, payload: dict[str, Any]) -> RecapProject:
        project = self.load(paths)
        existing = {item.segment_id for item in project.segments}
        segment_id = str(payload.get("segment_id") or "").strip()
        if not segment_id:
            number = 1
            while f"seg-{number:03d}" in existing:
                number += 1
            segment_id = f"seg-{number:03d}"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", segment_id):
            raise ValueError("片段编号只允许字母、数字、下划线和连字符，且最长80字符")
        if segment_id in existing:
            raise ValueError(f"片段编号已存在: {segment_id}")
        data = dict(payload)
        data["segment_id"] = segment_id
        data.setdefault("episode", 1)
        data.setdefault("source_start", 0.0)
        data.setdefault("source_end", 10.0)
        data.setdefault("mode", "narration")
        project.segments.append(RecapSegment.from_dict(data))
        return save_new_version(self.project_path(paths), project)

    def update_segment(self, paths: JobPaths, segment_id: str, changes: dict[str, Any]) -> RecapProject:
        project, _ = update_segment(self.project_path(paths), segment_id, changes)
        return project

    def delete_segment(self, paths: JobPaths, segment_id: str) -> RecapProject:
        project, _ = delete_segment(self.project_path(paths), segment_id)
        return project

    def validate(self, paths: JobPaths) -> dict[str, Any]:
        project = self.load(paths)
        ffprobe = video_dedup.find_binary("ffprobe")
        errors = validate_source_intervals(project, lambda video: video_dedup.probe_video(video, ffprobe))
        planning = self.planning_status(paths)
        if planning.get("status") == "completed":
            try:
                self._validate_agent_timeline(project, project.segments, planning)
            except ValueError as exc:
                errors.append(
                    {
                        "code": "agent_timeline_incomplete",
                        "message": str(exc),
                    }
                )
        return {"status": "ok" if not errors else "validation_failed", "validation_errors": errors}

    def status(self, paths: JobPaths) -> dict[str, Any]:
        path = self.status_path(paths)
        if not path.is_file():
            return {"status": "idle"}
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return {"status": "unknown"}

    def start_action(
        self,
        job: dict[str, Any],
        paths: JobPaths,
        action: str,
        segment_id: str = "",
        cancelled: Callable[[], bool] | None = None,
        process_observer: Callable[[int | None, str, float | None], None] | None = None,
    ) -> dict[str, Any]:
        if action not in {"loudness", "preview", "final", "segment"}:
            raise ValueError("未知解说操作")
        validation = self.validate(paths)
        if validation["status"] != "ok":
            raise ValueError(
                "解说项目校验未通过: "
                + json.dumps(validation["validation_errors"], ensure_ascii=False)
            )
        job_id = str(job["id"])
        with self._state_lock:
            if job_id in self._running:
                raise RuntimeError("该解说项目已有操作正在运行")
            self._running.add(job_id)
        state = {"status": "queued", "action": action, "started_at": now_iso()}
        self.storage.write_json(self.status_path(paths), state)

        def work() -> None:
            try:
                with self._render_lock:
                    if cancelled and cancelled():
                        self.storage.write_json(
                            self.status_path(paths),
                            {**state, "status": "cancelled", "cancelled_at": now_iso()},
                        )
                        return
                    self.storage.write_json(self.status_path(paths), {**state, "status": "running"})
                    project = self.load(paths)
                    if action == "loudness":
                        result = measure_project_loudness(
                            project, video_dedup.find_binary("ffmpeg"), video_dedup.find_binary("ffprobe")
                        )
                    else:
                        result = render_project(
                            project,
                            final=action == "final",
                            only_segment_id=segment_id if action == "segment" else None,
                            cancelled=cancelled,
                            process_observer=process_observer,
                        )
                    publication = None
                    if cancelled and cancelled():
                        self.storage.write_json(
                            self.status_path(paths),
                            {
                                **state,
                                "status": "cancelled",
                                "cancelled_at": now_iso(),
                                "result": result,
                            },
                        )
                        return
                    if action == "final" and result.get("status") == "ok":
                        publication = self.storage.publish_results(
                            {**job, "completed_at": now_iso()},
                            paths,
                            categories=("recap",),
                        )
                    self.storage.write_json(
                        self.status_path(paths),
                        {
                            **state,
                            "status": "completed",
                            "completed_at": now_iso(),
                            "result": result,
                            "publication": publication,
                        },
                    )
            except RenderCancelled as exc:
                self.storage.write_json(
                    self.status_path(paths),
                    {**state, "status": "cancelled", "cancelled_at": now_iso(), "error": str(exc)},
                )
            except Exception as exc:
                self.storage.write_json(
                    self.status_path(paths),
                    {**state, "status": "failed", "completed_at": now_iso(), "error": str(exc)},
                )
            finally:
                with self._state_lock:
                    self._running.discard(job_id)

        threading.Thread(target=work, name=f"recap-{job_id[:8]}", daemon=True).start()
        return state
