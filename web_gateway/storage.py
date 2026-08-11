from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from .settings import GatewaySettings


VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
UPLOAD_ROLES = {
    "video", "subtitle_final", "music", "music_pool", "border", "effect", "effect_pool",
    "cover", "series_info",
}
TEXT_SUFFIXES = {".json", ".md", ".txt", ".log", ".srt", ".csv"}
WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


def safe_component(value: str, *, fallback: str = "unnamed", maximum: int = 120) -> str:
    value = unicodedata.normalize("NFKC", str(value)).strip()
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    if not value:
        value = fallback
    if value.casefold() in WINDOWS_RESERVED:
        value = f"_{value}"
    return value[:maximum].rstrip(" .") or fallback


def safe_video_name(value: str) -> str:
    name = safe_component(Path(str(value)).name, fallback="video.mp4", maximum=180)
    suffix = Path(name).suffix.casefold()
    if suffix not in VIDEO_SUFFIXES:
        raise ValueError(f"不支持的视频格式: {suffix or '无扩展名'}")
    return name


def safe_upload_name(value: str, role: str = "video") -> str:
    role = str(role or "video").strip().casefold()
    if role not in UPLOAD_ROLES:
        raise ValueError(f"不支持的上传用途: {role}")
    name = safe_component(Path(str(value)).name, fallback="upload.bin", maximum=180)
    suffix = Path(name).suffix.casefold()
    allowed = {
        "video": VIDEO_SUFFIXES,
        "subtitle_final": {".srt", ".json"},
        "music": AUDIO_SUFFIXES,
        "music_pool": AUDIO_SUFFIXES,
        "border": VIDEO_SUFFIXES | IMAGE_SUFFIXES,
        "effect": VIDEO_SUFFIXES | IMAGE_SUFFIXES,
        "effect_pool": VIDEO_SUFFIXES | IMAGE_SUFFIXES,
        "cover": IMAGE_SUFFIXES,
        "series_info": {".txt", ".md"},
    }[role]
    if suffix not in allowed:
        raise ValueError(f"{role} 不支持的文件格式: {suffix or '无扩展名'}")
    return name


def ensure_within(path: Path, root: Path) -> Path:
    resolved, resolved_root = Path(path).resolve(), Path(root).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError("路径超出任务目录") from exc
    return resolved


def sha256_path(path: Path, block_size: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while block := source.read(block_size):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class JobPaths:
    root: Path
    input: Path
    result: Path
    videos: Path
    subtitles: Path
    logs: Path
    agent: Path
    chunks: Path
    config: Path
    assets: Path

    def ensure(self) -> "JobPaths":
        for path in (
            self.root, self.input, self.result, self.videos, self.subtitles,
            self.logs, self.agent, self.chunks, self.config,
            self.assets,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return self


@dataclass(frozen=True)
class ProjectPaths:
    root: Path
    subtitles: Path
    processed: Path
    recap: Path
    records: Path
    marker: Path

    def ensure(self) -> "ProjectPaths":
        for path in (self.root, self.subtitles, self.processed, self.recap, self.records, self.marker.parent):
            path.mkdir(parents=True, exist_ok=True)
        return self


class JobStorage:
    def __init__(self, settings: GatewaySettings):
        self.settings = settings
        self._publish_lock = threading.RLock()

    def account_root(self, owner_id: str, owner_label: str = "") -> Path:
        """Return an immutable per-account server storage namespace.

        The readable label is useful to the server operator while the key id
        prevents two users with the same display name from sharing a folder.
        """
        owner = safe_component(owner_id, fallback="owner", maximum=80)
        label = safe_component(owner_label, fallback="user", maximum=60)
        root = self.settings.storage_root / "用户" / f"{label}__{owner[:8]}"
        ensure_within(root, self.settings.storage_root)
        marker = root / ".account.json"
        with self._publish_lock:
            root.mkdir(parents=True, exist_ok=True)
            if not marker.is_file():
                self.write_json(
                    marker,
                    {
                        "schema_version": 1,
                        "owner_id": owner,
                        "label": owner_label or label,
                        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    },
                )
            else:
                payload = self._load_json(marker, {})
                if not isinstance(payload, dict) or str(payload.get("owner_id") or "") != owner:
                    raise RuntimeError("用户存储命名空间归属校验失败")
        return root

    def claim_project_name(self, requested_name: str, owner_id: str, owner_label: str = "") -> str:
        """Return a stable project folder name inside the account namespace."""
        base = safe_component(requested_name, fallback="未命名短剧")
        owner = safe_component(owner_id, fallback="owner", maximum=80)
        account_root = self.account_root(owner, owner_label)
        with self._publish_lock:
            for attempt in range(1000):
                if attempt == 0:
                    candidate = base
                else:
                    candidate = safe_component(f"{base}_{attempt}")
                root = ensure_within(account_root / candidate, account_root)
                marker = root / ".video-service" / "project-owner.json"
                if marker.is_file():
                    try:
                        payload = json.loads(marker.read_text(encoding="utf-8-sig"))
                    except (OSError, json.JSONDecodeError):
                        continue
                    if str(payload.get("owner_id") or "") == owner:
                        return candidate
                    continue
                marker.parent.mkdir(parents=True, exist_ok=True)
                temporary = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
                temporary.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "owner_id": owner,
                            "claimed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                os.replace(temporary, marker)
                return candidate
        raise RuntimeError("无法为同名工程分配隔离目录")

    def register_existing_project_owner(self, work_directory: str | Path, owner_id: str) -> bool:
        """Backfill ownership for projects created before ownership markers existed."""
        root = ensure_within(Path(work_directory), self.settings.storage_root)
        jobs_root = root.parent
        service_root = jobs_root.parent
        if jobs_root.name != "jobs" or service_root.name != ".video-service":
            return False
        project_root = ensure_within(service_root.parent, self.settings.storage_root)
        marker = project_root / ".video-service" / "project-owner.json"
        owner = safe_component(owner_id, fallback="owner", maximum=80)
        with self._publish_lock:
            if marker.is_file():
                try:
                    payload = json.loads(marker.read_text(encoding="utf-8-sig"))
                except (OSError, json.JSONDecodeError):
                    return False
                return str(payload.get("owner_id") or "") == owner
            marker.parent.mkdir(parents=True, exist_ok=True)
            temporary = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "owner_id": owner,
                        "claimed_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "backfilled": True,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            os.replace(temporary, marker)
            return True

    def paths(
        self,
        series_name: str,
        version: int,
        job_id: str,
        *,
        owner_id: str | None = None,
        owner_label: str = "",
    ) -> JobPaths:
        series = safe_component(series_name, fallback="未命名短剧")
        # The immutable job id is the filesystem identity. The human-readable
        # series version is stored in SQLite and result/manifest.json. Keeping
        # the directory independent of a preallocated version avoids races
        # when two users create jobs for the same series simultaneously.
        parent = self.account_root(owner_id, owner_label) if owner_id else self.settings.storage_root
        root = parent / series / ".video-service" / "jobs" / job_id
        ensure_within(root, self.settings.storage_root)
        return JobPaths(
            root=root,
            input=root / "input",
            result=root / "result",
            videos=root / "result" / "videos",
            subtitles=root / "result" / "subtitles",
            logs=root / "result" / "logs",
            agent=root / "result" / "agent",
            chunks=root / ".chunks",
            config=root / "config",
            assets=root / "assets",
        )

    @staticmethod
    def _directory_size(path: Path) -> int:
        total = 0
        if not path.exists():
            return total
        for item in path.rglob("*"):
            try:
                if item.is_file():
                    total += item.stat().st_size
            except OSError:
                continue
        return total

    def account_usage(self, jobs: Iterable[dict], owner_id: str) -> dict[str, object]:
        owned = [job for job in jobs if str(job.get("access_key_id")) == str(owner_id)]
        job_roots: dict[str, Path] = {}
        project_roots: dict[str, Path] = {}
        status_counts: dict[str, int] = {}
        for job in owned:
            try:
                paths = self.paths_from_job(job, ensure=False)
                project = self.project_paths(paths, ensure=False)
            except (OSError, ValueError):
                continue
            job_roots[job["id"]] = paths.root
            project_roots[str(project.root)] = project.root
            status = str(job.get("status") or "unknown")
            status_counts[status] = status_counts.get(status, 0) + 1
        runtime_bytes = sum(self._directory_size(root) for root in job_roots.values())
        project_bytes = sum(self._directory_size(root) for root in project_roots.values())
        published_bytes = max(0, project_bytes - runtime_bytes)
        return {
            "scope": "server_account",
            "owner_id": owner_id,
            "jobs": len(owned),
            "projects": len(project_roots),
            "runtime_bytes": runtime_bytes,
            "published_bytes": published_bytes,
            "total_bytes": project_bytes,
            "status_counts": status_counts,
        }

    def cleanup_account_jobs(
        self,
        jobs: Iterable[dict],
        owner_id: str,
        *,
        categories: Iterable[str],
        older_than_days: int = 0,
        dry_run: bool = True,
    ) -> dict[str, object]:
        """Clean only server-side resources belonging to one authenticated account."""
        allowed = {"chunks", "inputs", "failed_cancelled", "completed_runtime"}
        requested = tuple(dict.fromkeys(str(item) for item in categories))
        unknown = set(requested) - allowed
        if unknown:
            raise ValueError(f"未知清理分类: {sorted(unknown)}")
        cutoff = datetime.now().astimezone().timestamp() - max(0, int(older_than_days)) * 86400
        candidates: list[Path] = []
        for job in jobs:
            if str(job.get("access_key_id")) != str(owner_id):
                continue
            try:
                paths = self.paths_from_job(job, ensure=False)
            except (OSError, ValueError):
                continue
            try:
                timestamp = datetime.fromisoformat(str(job.get("updated_at") or job.get("created_at"))).timestamp()
            except (TypeError, ValueError):
                timestamp = 0
            if timestamp > cutoff:
                continue
            status = str(job.get("status") or "")
            if "chunks" in requested:
                candidates.append(paths.chunks)
            # Generic input cleanup must not silently destroy resumable work.
            # Failed/cancelled jobs require the explicit failed_cancelled choice.
            if "inputs" in requested and status == "completed":
                candidates.extend((paths.input, paths.assets, paths.root / ".secrets"))
            if "completed_runtime" in requested and status == "completed":
                candidates.append(paths.videos)
            if "failed_cancelled" in requested and status in {"failed", "cancelled"}:
                candidates.append(paths.root)
        unique: list[Path] = []
        for path in candidates:
            resolved = ensure_within(path, self.settings.storage_root)
            if resolved not in unique and resolved.exists():
                unique.append(resolved)
        reclaimable = sum(self._directory_size(path) for path in unique)
        deleted: list[str] = []
        if not dry_run:
            # Child paths before parents keeps the result deterministic.
            for path in sorted(unique, key=lambda value: len(value.parts), reverse=True):
                if not path.exists():
                    continue
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=False)
                else:
                    path.unlink(missing_ok=True)
                deleted.append(str(path))
        return {
            "scope": "server_account",
            "dry_run": bool(dry_run),
            "categories": list(requested),
            "older_than_days": max(0, int(older_than_days)),
            "candidate_count": len(unique),
            "reclaimable_bytes": reclaimable,
            "deleted": deleted,
        }

    def paths_from_job(self, job: dict, *, ensure: bool = True) -> JobPaths:
        root = ensure_within(Path(job["work_directory"]), self.settings.storage_root)
        paths = JobPaths(
            root=root,
            input=root / "input",
            result=root / "result",
            videos=root / "result" / "videos",
            subtitles=root / "result" / "subtitles",
            logs=root / "result" / "logs",
            agent=root / "result" / "agent",
            chunks=root / ".chunks",
            config=root / "config",
            assets=root / "assets",
        )
        return paths.ensure() if ensure else paths

    def project_paths(self, paths: JobPaths, *, ensure: bool = True) -> ProjectPaths:
        # <project>/.video-service/jobs/<job-id>
        jobs_root = paths.root.parent
        service_root = jobs_root.parent
        if jobs_root.name != "jobs" or service_root.name != ".video-service":
            raise ValueError("任务目录结构无效，无法确定工程根目录")
        project_root = ensure_within(service_root.parent, self.settings.storage_root)
        project = ProjectPaths(
            root=project_root,
            subtitles=project_root / "字幕终稿",
            processed=project_root / "processed",
            recap=project_root / "解说",
            records=project_root / "任务记录",
            marker=service_root / "published-results.json",
        )
        return project.ensure() if ensure else project

    @staticmethod
    def record_name(job: dict) -> str:
        raw_created = str(job.get("created_at") or "")
        try:
            created = datetime.fromisoformat(raw_created).strftime("%Y%m%d-%H%M%S")
        except ValueError:
            created = re.sub(r"\D+", "", raw_created)[:14] or "unknown-time"
        short_id = safe_component(str(job.get("id") or "job"))[-8:]
        title = safe_component(str(job.get("series_name") or "未命名任务"), maximum=70)
        return safe_component(f"{title}_{created}_{short_id}", maximum=110)

    def public_layout(self, job: dict, paths: JobPaths) -> dict[str, str]:
        project = self.project_paths(paths)
        record = project.records / self.record_name(job)
        return {
            "project_root": str(project.root),
            "subtitles": str(project.subtitles),
            "videos": str(project.processed),
            "recap": str(project.recap),
            "records": str(project.records),
            "record": str(record),
            "record_name": record.name,
        }

    @staticmethod
    def _load_json(path: Path, fallback: object) -> object:
        try:
            return json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return fallback

    @staticmethod
    def _iter_publish_files(source: Path, *, recursive: bool) -> Iterable[tuple[Path, Path]]:
        if not source.is_dir():
            return ()
        files = source.rglob("*") if recursive else source.iterdir()
        return tuple(
            (item, item.relative_to(source))
            for item in sorted(files)
            if item.is_file() and ".recap_cache" not in item.parts
        )

    @staticmethod
    def _unique_destination(path: Path, suffix: str) -> Path:
        if not path.exists():
            return path
        candidate = path.with_name(f"{path.stem}__{suffix}{path.suffix}")
        number = 2
        while candidate.exists():
            candidate = path.with_name(f"{path.stem}__{suffix}_{number}{path.suffix}")
            number += 1
        return candidate

    @staticmethod
    def _copy_tree(source: Path, destination: Path) -> None:
        if source.is_dir():
            shutil.copytree(source, destination, dirs_exist_ok=True)

    def _sync_record_materials(self, paths: JobPaths, record: Path) -> None:
        record.mkdir(parents=True, exist_ok=True)
        for source, name in (
            (paths.logs, "logs"),
            (paths.config, "config"),
            (paths.agent, "agent"),
        ):
            self._copy_tree(source, record / name)

    def publish_results(
        self,
        job: dict,
        paths: JobPaths,
        categories: Iterable[str] = ("subtitles", "videos"),
    ) -> dict[str, object]:
        """Publish immutable job results into the user-visible project folders.

        Runtime files remain under .video-service/jobs. Files previously
        published by this service are moved into their original task record
        before the new category is published. Untracked files are never moved
        or overwritten; a job-id suffix is used on collision.
        """
        requested = tuple(dict.fromkeys(str(item) for item in categories))
        unknown = set(requested) - {"subtitles", "videos", "recap"}
        if unknown:
            raise ValueError(f"未知发布分类: {sorted(unknown)}")
        project = self.project_paths(paths)
        record = project.records / self.record_name(job)
        sources = {
            "subtitles": (paths.subtitles, project.subtitles, False),
            "videos": (paths.videos, project.processed, False),
            "recap": (paths.videos / "recap", project.recap, True),
        }
        record_category_names = {"subtitles": "字幕终稿", "videos": "processed", "recap": "解说"}
        short_id = safe_component(str(job["id"]))[-8:]
        with self._publish_lock:
            marker = self._load_json(project.marker, {"schema_version": 1, "categories": {}})
            if not isinstance(marker, dict):
                marker = {"schema_version": 1, "categories": {}}
            owners = marker.setdefault("categories", {})
            if not isinstance(owners, dict):
                owners = {}
                marker["categories"] = owners
            self._sync_record_materials(paths, record)
            published: dict[str, list[str]] = {}
            archived: dict[str, list[str]] = {}
            for category in requested:
                source, destination_root, recursive = sources[category]
                previous = owners.get(category) if isinstance(owners.get(category), dict) else None
                previous_files = list(previous.get("files") or []) if previous else []
                if previous and previous.get("job_id") != job["id"]:
                    previous_record_name = safe_component(
                        str(previous.get("record_name") or f"历史任务_{previous.get('job_id', 'unknown')}")
                    )
                    previous_record = project.records / previous_record_name
                    archive_root = previous_record / record_category_names[category]
                    for relative_text in previous_files:
                        relative = Path(str(relative_text))
                        old_file = ensure_within(destination_root / relative, destination_root)
                        if not old_file.is_file():
                            continue
                        archived_file = self._unique_destination(
                            ensure_within(archive_root / relative, archive_root), "archived"
                        )
                        archived_file.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(old_file), str(archived_file))
                        archived.setdefault(category, []).append(archived_file.relative_to(project.root).as_posix())
                    previous_manifest_path = previous_record / "manifest.json"
                    previous_manifest = self._load_json(previous_manifest_path, {})
                    if isinstance(previous_manifest, dict):
                        archived_outputs = previous_manifest.setdefault("archived_outputs", {})
                        if isinstance(archived_outputs, dict):
                            archived_outputs[category] = archived.get(category, [])
                        previous_manifest["archived_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
                        self.write_json(previous_manifest_path, previous_manifest)
                elif previous and previous.get("job_id") == job["id"]:
                    for relative_text in previous_files:
                        owned_file = ensure_within(destination_root / Path(str(relative_text)), destination_root)
                        owned_file.unlink(missing_ok=True)

                category_files: list[str] = []
                for item, relative in self._iter_publish_files(source, recursive=recursive):
                    target = ensure_within(destination_root / relative, destination_root)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        target = self._unique_destination(target, short_id)
                    shutil.copy2(item, target)
                    category_files.append(target.relative_to(destination_root).as_posix())
                owners[category] = {
                    "job_id": job["id"],
                    "job_name": job["series_name"],
                    "record_name": record.name,
                    "published_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "files": category_files,
                }
                published[category] = category_files

            public = self.public_layout(job, paths)
            record_manifest = {
                "schema_version": 1,
                "job_name": job["series_name"],
                "job_id": job["id"],
                "version": job.get("version"),
                "created_at": job.get("created_at"),
                "completed_at": job.get("completed_at"),
                "project_root": public["project_root"],
                "published_directories": {
                    "subtitles": public["subtitles"],
                    "processed": public["videos"],
                    "recap": public["recap"],
                },
                "published_files": published,
                "archived_previous_files": archived,
            }
            self.write_json(record / "manifest.json", record_manifest)
            marker.update(
                {
                    "schema_version": 1,
                    "project_root": str(project.root),
                    "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                }
            )
            self.write_json(project.marker, marker)
            return {
                "directories": public,
                "published_files": published,
                "archived_previous_files": archived,
            }

    def write_chunk(self, paths: JobPaths, upload_id: str, index: int, data: bytes) -> tuple[Path, str]:
        if index < 0:
            raise ValueError("分片序号不能为负数")
        upload_root = ensure_within(paths.chunks / safe_component(upload_id), paths.chunks)
        upload_root.mkdir(parents=True, exist_ok=True)
        target = upload_root / f"{index:08d}.part"
        temporary = target.with_suffix(".tmp")
        temporary.write_bytes(data)
        os.replace(temporary, target)
        return target, hashlib.sha256(data).hexdigest()

    def assemble_upload(
        self,
        paths: JobPaths,
        upload_id: str,
        stored_name: str,
        total_chunks: int,
        role: str = "video",
    ) -> tuple[Path, int, str]:
        upload_root = ensure_within(paths.chunks / safe_component(upload_id), paths.chunks)
        role = str(role or "video").strip().casefold()
        target = self.upload_target(paths, stored_name, role)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + ".assembling")
        digest = hashlib.sha256()
        byte_size = 0
        with temporary.open("wb") as output:
            for index in range(int(total_chunks)):
                chunk_path = upload_root / f"{index:08d}.part"
                if not chunk_path.is_file():
                    raise ValueError(f"缺少上传分片 {index}")
                with chunk_path.open("rb") as source:
                    while block := source.read(4 * 1024 * 1024):
                        output.write(block)
                        digest.update(block)
                        byte_size += len(block)
        os.replace(temporary, target)
        return target, byte_size, digest.hexdigest()

    @staticmethod
    def upload_target(paths: JobPaths, stored_name: str, role: str = "video") -> Path:
        """Resolve an assembled upload without creating any directory.

        Resume validation uses the same mapping as upload assembly so a
        database row cannot make a cleaned or missing source look resumable.
        """
        role = str(role or "video").strip().casefold()
        name = safe_upload_name(stored_name, role)
        if role == "video":
            destination = paths.input
        elif role == "subtitle_final":
            destination = paths.input / "字幕终稿"
        else:
            destination = paths.assets / role
        return ensure_within(destination / name, destination)

    def missing_completed_uploads(self, paths: JobPaths, uploads: Iterable[dict]) -> list[str]:
        missing: list[str] = []
        for upload in uploads:
            if str(upload.get("status") or "") != "completed":
                missing.append(str(upload.get("stored_name") or upload.get("original_name") or "unknown"))
                continue
            target = self.upload_target(
                paths,
                str(upload.get("stored_name") or ""),
                str(upload.get("role") or "video"),
            )
            if not target.is_file() or target.stat().st_size != int(upload.get("expected_size") or -1):
                missing.append(str(upload.get("stored_name") or upload.get("original_name") or target.name))
        return missing

    def cleanup_chunks(self, paths: JobPaths, upload_id: str) -> None:
        upload_root = ensure_within(paths.chunks / safe_component(upload_id), paths.chunks)
        shutil.rmtree(upload_root, ignore_errors=True)

    def write_json(self, path: Path, payload: object) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, path)
        return path

    def collect_subtitle_finals(self, paths: JobPaths) -> list[Path]:
        source = paths.input / "字幕终稿"
        copied: list[Path] = []
        if not source.is_dir():
            return copied
        for item in source.iterdir():
            if not item.is_file() or item.suffix.casefold() not in {".srt", ".json"}:
                continue
            target = paths.subtitles / item.name
            shutil.copy2(item, target)
            copied.append(target)
        return copied

    def artifact_roots(self, paths: JobPaths, include_runtime: bool = False) -> list[tuple[str, Path]]:
        roots = [
            ("videos", paths.videos),
            ("subtitles", paths.subtitles),
            ("assets", paths.assets),
            ("logs", paths.logs),
            ("agent", paths.agent),
            ("config", paths.config),
        ]
        if include_runtime:
            # Expose request/progress/response material only. Registration and
            # control files contain host-local paths and are never remote data.
            roots.append(("agent-runtime", paths.root / "agent-runtime" / "runtime"))
        return roots

    def list_artifacts(self, paths: JobPaths, include_runtime: bool = False) -> list[dict]:
        artifacts = []
        for namespace, root in self.artifact_roots(paths, include_runtime):
            if not root.is_dir():
                continue
            for file in sorted(root.rglob("*")):
                if not file.is_file():
                    continue
                relative = file.relative_to(root).as_posix()
                artifacts.append(
                    {
                        "path": f"{namespace}/{relative}",
                        "size": file.stat().st_size,
                        "type": "text" if file.suffix.casefold() in TEXT_SUFFIXES else "binary",
                    }
                )
        return artifacts

    def resolve_artifact(self, paths: JobPaths, logical_path: str, include_runtime: bool = False) -> Path:
        logical = str(logical_path).replace("\\", "/").strip("/")
        if not logical or "/" not in logical:
            raise FileNotFoundError(logical_path)
        namespace, relative = logical.split("/", 1)
        roots = dict(self.artifact_roots(paths, include_runtime))
        if namespace not in roots:
            raise FileNotFoundError(logical_path)
        path = ensure_within(roots[namespace] / relative, roots[namespace])
        if not path.is_file():
            raise FileNotFoundError(logical_path)
        return path
