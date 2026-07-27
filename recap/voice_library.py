from __future__ import annotations

import json
import hashlib
from functools import lru_cache
from pathlib import Path
from typing import Any

from .models import VoiceProfile, stable_hash
from .narration_text import canonical_language
from .tts_routing import engines_for_language


PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_LIBRARY_PATH = PACKAGE_ROOT / "voices" / "library.json"
ROLE_PACK_NAME = "fish_s2_role_pack.json"

class VoiceLibrary:
    def __init__(self, path: Path = DEFAULT_LIBRARY_PATH) -> None:
        self.path = Path(path).resolve()
        payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
        self.schema_version = int(payload.get("schema_version", 0))
        if self.schema_version != 1:
            raise ValueError(f"unsupported voice library schema: {self.schema_version}")
        raw_profiles = list(payload.get("voices", []))
        role_pack = self.path.parent / ROLE_PACK_NAME
        if role_pack.is_file():
            role_payload = json.loads(role_pack.read_text(encoding="utf-8-sig"))
            if int(role_payload.get("schema_version", 0)) != 1:
                raise ValueError(f"unsupported role voice pack schema: {role_payload.get('schema_version')}")
            raw_profiles.extend(role_payload.get("profiles", []))
        self._profiles = {item.voice_id: item for item in map(VoiceProfile.from_dict, raw_profiles)}
        if len(self._profiles) != len(raw_profiles):
            raise ValueError("voice_id values must be unique")
        for profile in self._profiles.values():
            if not profile.voice_id or not profile.reference_audio or not profile.reference_text:
                raise ValueError(f"voice profile is incomplete: {profile.voice_id or '<missing>'}")

    def resolve_asset(self, value: str) -> Path:
        path = Path(value)
        return path.resolve() if path.is_absolute() else (self.path.parent / path).resolve()

    def list(self) -> list[VoiceProfile]:
        return list(self._profiles.values())

    def approved(self) -> list[VoiceProfile]:
        return [profile for profile in self.list() if profile.review_status == "approved"]

    def reload(self) -> "VoiceLibrary":
        return type(self)(self.path)

    def get(self, voice_id: str) -> VoiceProfile:
        try:
            return self._profiles[voice_id]
        except KeyError as exc:
            raise KeyError(f"unknown voice_id {voice_id!r}; available={sorted(self._profiles)}") from exc

    def compatible(self, language: str, engine: str | None = None) -> list[VoiceProfile]:
        target = canonical_language(language).casefold()
        requested_engine = str(engine or "").strip().casefold()
        return [
            profile
            for profile in self.approved()
            if (
                not profile.languages
                or target in {canonical_language(item).casefold() for item in profile.languages}
            )
            and (
                not requested_engine
                or requested_engine == "auto"
                or not profile.allowed_engines
                or requested_engine in profile.allowed_engines
            )
        ]

    def validate_assets(self, voice_id: str, *, require_preview: bool = False) -> list[str]:
        profile = self.get(voice_id)
        errors = []
        if not self.resolve_asset(profile.reference_audio).is_file():
            errors.append(f"missing reference_audio: {self.resolve_asset(profile.reference_audio)}")
        preview = self.resolve_asset(profile.preview_audio)
        if require_preview and profile.preview_audio and not preview.is_file():
            errors.append(f"missing preview_audio: {preview}")
        return errors


@lru_cache(maxsize=128)
def _cached_file_sha256(path_text: str, size: int, modified_ns: int) -> str:
    digest = hashlib.sha256()
    with Path(path_text).open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _file_sha256(path: Path) -> str:
    resolved = Path(path).resolve()
    stat = resolved.stat()
    return _cached_file_sha256(str(resolved), stat.st_size, stat.st_mtime_ns)


def voice_cache_key(
    profile: VoiceProfile,
    text: str,
    language: str,
    speed: float,
    parameters: dict[str, Any] | None = None,
    *,
    reference_audio_path: Path | None = None,
) -> str:
    return stable_hash({
        "voice_id": profile.voice_id,
        "text": text,
        "language": language,
        "speed": round(float(speed), 6),
        "model_version": profile.model_version,
        "engine": profile.engine,
        "reference_text": profile.reference_text,
        "reference_audio_sha256": (
            _file_sha256(reference_audio_path)
            if reference_audio_path is not None and Path(reference_audio_path).is_file()
            else ""
        ),
        "generation_parameters": parameters if parameters is not None else profile.generation_parameters,
    })


def voice_cache_path(cache_root: Path, project_id: str, profile: VoiceProfile, cache_key: str) -> Path:
    return Path(cache_root).resolve() / project_id / profile.voice_id / f"{cache_key}.wav"
