from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_VIDEO_ROOT = Path(r"E:\wangyang\Videos\短剧输出")


@dataclass(frozen=True)
class GatewaySettings:
    project_root: Path
    storage_root: Path
    service_root: Path
    database_path: Path
    public_base_url: str
    chunk_size: int = 32 * 1024 * 1024
    maximum_chunk_size: int = 40 * 1024 * 1024
    maximum_video_workers: int = 10
    maximum_subtitle_workers: int = 3
    maximum_processing_jobs: int = 1
    maximum_file_size: int = 20 * 1024**3
    maximum_job_upload_size: int = 100 * 1024**3
    maximum_account_storage: int = 500 * 1024**3
    minimum_free_space: int = 5 * 1024**3
    maximum_upload_chunks_per_minute: int = 240
    agent_idle_timeout_seconds: int = 20 * 60
    upload_retention_days: int = 7
    result_retention_days: int = 14
    allowed_origins: tuple[str, ...] = ()

    @classmethod
    def from_environment(cls) -> "GatewaySettings":
        project_root = Path(__file__).resolve().parent.parent
        storage_root = Path(os.environ.get("VIDEO_GATEWAY_STORAGE_ROOT", DEFAULT_VIDEO_ROOT)).expanduser().resolve()
        service_root = Path(
            os.environ.get("VIDEO_GATEWAY_SERVICE_ROOT", storage_root / ".video-service")
        ).expanduser().resolve()
        database_path = Path(
            os.environ.get("VIDEO_GATEWAY_DATABASE", service_root / "gateway.sqlite3")
        ).expanduser().resolve()
        public_base_url = os.environ.get("VIDEO_GATEWAY_PUBLIC_URL", "http://127.0.0.1:8765").rstrip("/")
        chunk_size = max(1024 * 1024, int(os.environ.get("VIDEO_GATEWAY_CHUNK_SIZE", 32 * 1024 * 1024)))
        allowed_origins = tuple(
            item.strip().rstrip("/")
            for item in os.environ.get("VIDEO_GATEWAY_ALLOWED_ORIGINS", "").split(",")
            if item.strip()
        )
        return cls(
            project_root=project_root,
            storage_root=storage_root,
            service_root=service_root,
            database_path=database_path,
            public_base_url=public_base_url,
            chunk_size=chunk_size,
            maximum_chunk_size=max(40 * 1024 * 1024, chunk_size + 1024 * 1024),
            maximum_video_workers=max(1, min(10, int(os.environ.get("VIDEO_GATEWAY_VIDEO_WORKERS", 10)))),
            maximum_subtitle_workers=max(1, min(3, int(os.environ.get("VIDEO_GATEWAY_SUBTITLE_WORKERS", 3)))),
            maximum_processing_jobs=1,
            maximum_file_size=max(1024**3, int(os.environ.get("VIDEO_GATEWAY_MAX_FILE_SIZE", 20 * 1024**3))),
            maximum_job_upload_size=max(1024**3, int(os.environ.get("VIDEO_GATEWAY_MAX_JOB_UPLOAD_SIZE", 100 * 1024**3))),
            maximum_account_storage=max(1024**3, int(os.environ.get("VIDEO_GATEWAY_MAX_ACCOUNT_STORAGE", 500 * 1024**3))),
            minimum_free_space=max(1024**3, int(os.environ.get("VIDEO_GATEWAY_MIN_FREE_SPACE", 5 * 1024**3))),
            maximum_upload_chunks_per_minute=max(30, int(os.environ.get("VIDEO_GATEWAY_UPLOAD_CHUNKS_PER_MINUTE", 240))),
            agent_idle_timeout_seconds=max(
                60,
                int(os.environ.get("VIDEO_GATEWAY_AGENT_IDLE_TIMEOUT", 20 * 60)),
            ),
            allowed_origins=allowed_origins,
        )

    def ensure_directories(self) -> None:
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.service_root.mkdir(parents=True, exist_ok=True)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
