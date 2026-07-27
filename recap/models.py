from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1


def natural_path_key(path: Path) -> tuple:
    return tuple(
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", path.name)
    )


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass
class RecapSegment:
    segment_id: str
    episode: int
    source_start: float
    source_end: float
    mode: str
    narration_text: str = ""
    purpose: str = ""
    revision: int = 1
    rendering: dict[str, Any] = field(default_factory=dict)
    cache_key: str = ""

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RecapSegment":
        if not isinstance(value, dict):
            raise ValueError("segment must be an object")
        return cls(
            segment_id=str(value.get("segment_id", "")).strip(),
            episode=int(value.get("episode", 0)),
            source_start=float(value.get("source_start", 0.0)),
            source_end=float(value.get("source_end", 0.0)),
            mode=str(value.get("mode", "")).strip().casefold(),
            narration_text=str(value.get("narration_text", value.get("text", ""))).strip(),
            purpose=str(value.get("purpose", "")).strip(),
            revision=max(1, int(value.get("revision", 1))),
            rendering=dict(value.get("rendering") or {}),
            cache_key=str(value.get("cache_key", "")).strip(),
        )

    def semantic_payload(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("cache_key", None)
        return value

    def computed_cache_key(self, extra: dict[str, Any] | None = None) -> str:
        return stable_hash({"segment": self.semantic_payload(), "extra": extra or {}})

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["cache_key"] = self.cache_key or self.computed_cache_key()
        return value


@dataclass
class RecapProject:
    schema_version: int
    project_id: str
    project_name: str
    source_root: str
    episode_pattern: str
    subtitle_root: str
    output_root: str
    target_language: str
    target_duration_seconds: float
    voice_id: str
    narration_speed: float
    narration_target_loudness: str | float
    segments: list[RecapSegment]
    current_version: int
    created_at: str
    updated_at: str
    rendering: dict[str, Any] = field(default_factory=dict)
    tts_engine: str = "auto"
    narration_preset: str = "legacy"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RecapProject":
        if not isinstance(value, dict):
            raise ValueError("project root must be an object")
        return cls(
            schema_version=int(value.get("schema_version", SCHEMA_VERSION)),
            project_id=str(value.get("project_id", "")).strip(),
            project_name=str(value.get("project_name", "")).strip(),
            source_root=str(value.get("source_root", "")).strip(),
            episode_pattern=str(value.get("episode_pattern", "*{episode}*.mp4")).strip(),
            subtitle_root=str(value.get("subtitle_root", "")).strip(),
            output_root=str(value.get("output_root", "")).strip(),
            target_language=str(value.get("target_language", "English")).strip(),
            target_duration_seconds=float(value.get("target_duration_seconds", 0.0)),
            voice_id=str(value.get("voice_id", "calm_female")).strip(),
            tts_engine=str(value.get("tts_engine", "auto")).strip().casefold(),
            narration_preset=str(value.get("narration_preset", "legacy")).strip().casefold(),
            narration_speed=float(value.get("narration_speed", 1.0)),
            narration_target_loudness=value.get("narration_target_loudness", "keep_original"),
            segments=[RecapSegment.from_dict(item) for item in value.get("segments", [])],
            current_version=max(1, int(value.get("current_version", 1))),
            created_at=str(value.get("created_at") or now_iso()),
            updated_at=str(value.get("updated_at") or now_iso()),
            rendering=dict(value.get("rendering") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["segments"] = [item.to_dict() for item in self.segments]
        return value

    def source_path(self) -> Path:
        return Path(self.source_root).expanduser().resolve()

    def output_path(self) -> Path:
        return Path(self.output_root).expanduser().resolve()

    def episode_path(self, episode: int) -> Path:
        pattern = self.episode_pattern
        if "{" in pattern:
            name = pattern.format(episode=episode, number=episode)
            candidate = self.source_path() / name
            if any(char in name for char in "*?["):
                matches = sorted(self.source_path().glob(name), key=natural_path_key)
                if len(matches) == 1:
                    return matches[0].resolve()
                if len(matches) > 1:
                    raise ValueError(f"episode {episode} matches multiple files: {[str(item) for item in matches]}")
            return candidate.resolve()
        matches = sorted(self.source_path().glob(pattern), key=natural_path_key)
        if 1 <= episode <= len(matches):
            return matches[episode - 1].resolve()
        raise FileNotFoundError(f"cannot resolve episode {episode} with pattern {pattern!r}")


@dataclass
class VoiceProfile:
    voice_id: str
    display_name: str
    gender: str
    languages: list[str]
    style: list[str]
    reference_audio: str
    reference_text: str
    default_speed: float
    target_loudness_mode: str
    preview_text: str
    preview_audio: str
    engine: str
    model_version: str
    generation_parameters: dict[str, Any]
    design_instruction: str = ""
    reference_language: str = ""
    allowed_engines: list[str] = field(default_factory=list)
    speech_rate: dict[str, Any] = field(default_factory=dict)
    age_group: str = ""
    role_archetype: str = ""
    source_kind: str = ""
    review_status: str = "approved"
    quality_score: float | None = None
    provenance: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "VoiceProfile":
        return cls(
            voice_id=str(value.get("voice_id", "")).strip(),
            display_name=str(value.get("display_name", "")).strip(),
            gender=str(value.get("gender", "")).strip(),
            languages=[str(item) for item in value.get("languages", [])],
            style=[str(item) for item in value.get("style", [])],
            reference_audio=str(value.get("reference_audio", "")).strip(),
            reference_text=str(value.get("reference_text", "")).strip(),
            default_speed=float(value.get("default_speed", 1.0)),
            target_loudness_mode=str(value.get("target_loudness_mode", "match_source_program")),
            preview_text=str(value.get("preview_text", "")).strip(),
            preview_audio=str(value.get("preview_audio", "")).strip(),
            engine=str(value.get("engine", "")).strip(),
            model_version=str(value.get("model_version", "")).strip(),
            generation_parameters=dict(value.get("generation_parameters") or {}),
            design_instruction=str(value.get("design_instruction", "")).strip(),
            reference_language=str(value.get("reference_language", "")).strip(),
            allowed_engines=[str(item).strip().casefold() for item in value.get("allowed_engines", [])],
            speech_rate=dict(value.get("speech_rate") or {}),
            age_group=str(value.get("age_group", "")).strip(),
            role_archetype=str(value.get("role_archetype", "")).strip(),
            source_kind=str(value.get("source_kind", "")).strip(),
            review_status=str(value.get("review_status", "approved")).strip() or "approved",
            quality_score=(float(value["quality_score"]) if value.get("quality_score") is not None else None),
            provenance=dict(value.get("provenance") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
