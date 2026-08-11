from __future__ import annotations

import json
import hashlib
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx
from fastapi import FastAPI, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
STATIC_DIR = APP_DIR / "static"
AUTH_FILE = DATA_DIR / "auth.json"
DB_FILE = DATA_DIR / "downloads.db"
DEFAULT_OUTPUT = Path(r"E:\wangyang\Videos\北斗视频库")
DEFAULT_LIBRARY_SCAN_ROOT = DEFAULT_OUTPUT
GATEWAY_DATABASE_FILE: Path | None = None
GATEWAY_DATABASE: Any = None
PORTAL_PREFIX = "/beidou"
ALLOWED_LIBRARY_ROOTS: tuple[Path, ...] = (DEFAULT_OUTPUT,)
API_BASE = "https://api-scenter.inbeidou.cn"
SITE_BASE = "https://inbeidou.cn"
PLATFORM_NAMES = {1: "TikTok", 2: "Facebook", 3: "YouTube", 4: "Instagram"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".ts"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
TEXT_EXTENSIONS = {".txt", ".md", ".json"}
LIBRARY_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS | TEXT_EXTENSIONS

DATA_DIR.mkdir(parents=True, exist_ok=True)


def configure(
    *,
    data_root: Path,
    database_path: Path,
    library_root: Path,
    gateway_database_path: Path,
    additional_library_roots: tuple[Path, ...] = (),
    default_library_scan_root: Path | None = None,
) -> None:
    """Bind the embedded portal to the main service without copying live data."""
    global DATA_DIR, AUTH_FILE, DB_FILE, DEFAULT_OUTPUT, DEFAULT_LIBRARY_SCAN_ROOT, GATEWAY_DATABASE_FILE, GATEWAY_DATABASE, ALLOWED_LIBRARY_ROOTS
    DATA_DIR = Path(data_root).expanduser().resolve()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    AUTH_FILE = DATA_DIR / "auth.json"
    DB_FILE = Path(database_path).expanduser().resolve()
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEFAULT_OUTPUT = Path(library_root).expanduser().resolve()
    DEFAULT_OUTPUT.mkdir(parents=True, exist_ok=True)
    DEFAULT_LIBRARY_SCAN_ROOT = Path(default_library_scan_root or DEFAULT_OUTPUT).expanduser().resolve()
    GATEWAY_DATABASE_FILE = Path(gateway_database_path).expanduser().resolve()
    from ..database import GatewayDatabase
    GATEWAY_DATABASE = GatewayDatabase(GATEWAY_DATABASE_FILE)
    ALLOWED_LIBRARY_ROOTS = tuple(
        dict.fromkeys(
            [DEFAULT_OUTPUT, *(Path(item).expanduser().resolve() for item in additional_library_roots)]
        )
    )
    init_db()


def allowed_library_root(value: Path) -> Path:
    resolved = value.expanduser().resolve()
    for root in ALLOWED_LIBRARY_ROOTS:
        try:
            resolved.relative_to(root)
            return resolved
        except ValueError:
            continue
    raise HTTPException(403, "只能扫描服务器配置的视频库目录")


def allowed_download_root(value: Path) -> Path:
    resolved = value.expanduser().resolve()
    try:
        resolved.relative_to(DEFAULT_OUTPUT)
    except ValueError as exc:
        raise HTTPException(403, "下载目录必须位于服务器配置的北斗视频库内") from exc
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


class SiteError(RuntimeError):
    pass


class CatalogQuery(BaseModel):
    language: int = 10
    date_from: str = ""
    date_to: str = ""
    app_id: str = ""
    search: str = ""
    page: int = Field(1, ge=1)
    page_size: int = Field(20, ge=1, le=100)


class JobRequest(BaseModel):
    mode: str = "selected"
    task_ids: list[int] = []
    language: int = 10
    date_from: str = ""
    date_to: str = ""
    app_id: str = ""
    search: str = ""
    output_dir: str = str(DEFAULT_OUTPUT)
    sleep_seconds: int = Field(5, ge=5, le=120)
    max_workers: int = Field(2, ge=1, le=3)
    include_cps: bool = True


class ClassificationUpdate(BaseModel):
    audience_category: str
    setting_category: str


def first_text(mapping: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def ensure_table_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, definition in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


@contextmanager
def open_db():
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@dataclass
class AuthData:
    token: str = ""
    cookie_header: str = ""
    source_name: str = ""

    @property
    def usable(self) -> bool:
        return bool(self.token)


def load_auth() -> AuthData:
    if not AUTH_FILE.exists():
        return AuthData()
    try:
        raw = json.loads(AUTH_FILE.read_text(encoding="utf-8"))
        return AuthData(
            token=str(raw.get("token") or "").strip(),
            cookie_header=str(raw.get("cookie_header") or "").strip(),
            source_name=str(raw.get("source_name") or "").strip(),
        )
    except Exception:
        return AuthData()


def _find_token(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("token", "authorization", "Authorization", "access_token", "accessToken"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        if value.get("name") == "token" and isinstance(value.get("value"), str):
            return value["value"].strip()
        for child in value.values():
            found = _find_token(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_token(child)
            if found:
                return found
    return ""


def parse_auth_file(content: bytes, source_name: str) -> AuthData:
    text = content.decode("utf-8-sig", errors="replace").strip()
    cookies: dict[str, str] = {}
    token = ""

    try:
        payload = json.loads(text)
        token = _find_token(payload)
        cookie_items = payload if isinstance(payload, list) else payload.get("cookies", []) if isinstance(payload, dict) else []
        if isinstance(cookie_items, list):
            for item in cookie_items:
                if isinstance(item, dict) and item.get("name") and item.get("value") is not None:
                    cookies[str(item["name"])] = str(item["value"])
    except json.JSONDecodeError:
        payload = None

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            cookies[parts[-2]] = parts[-1]
            continue
        match = re.match(r"(?i)authorization\s*[:=]\s*(.+)$", line)
        if match and not token:
            token = match.group(1).strip()

    if not cookies and ";" in text and "=" in text and "\n" not in text:
        for part in text.split(";"):
            if "=" in part:
                name, value = part.split("=", 1)
                cookies[name.strip()] = value.strip()

    if not token:
        token = cookies.get("token", "").strip()
    if not token and text and "\n" not in text and "\t" not in text and "=" not in text:
        token = text

    cookie_header = "; ".join(f"{key}={value}" for key, value in cookies.items())
    return AuthData(token=token, cookie_header=cookie_header, source_name=source_name)


def save_auth(auth: AuthData) -> None:
    AUTH_FILE.write_text(
        json.dumps(
            {"token": auth.token, "cookie_header": auth.cookie_header, "source_name": auth.source_name},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def api_headers(auth_required: bool = False) -> dict[str, str]:
    auth = load_auth()
    if auth_required and not auth.usable:
        raise SiteError("登录文件中没有识别到 token。请上传包含 localStorage token 的 JSON，或上传纯 token 文本文件。")
    headers = {
        "Accept": "application/json, text/plain, */*",
        "Origin": SITE_BASE,
        "Referer": f"{SITE_BASE}/task",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/136 Safari/537.36",
    }
    if auth.token:
        headers["Authorization"] = auth.token
    if auth.cookie_header:
        headers["Cookie"] = auth.cookie_header
    return headers


def request_json(method: str, path: str, *, params: dict[str, Any] | None = None, json_body: dict[str, Any] | None = None, auth_required: bool = False) -> dict[str, Any]:
    try:
        with httpx.Client(timeout=30, follow_redirects=True, headers=api_headers(auth_required)) as client:
            response = client.request(method, f"{API_BASE}{path}", params=params, json=json_body)
        response.raise_for_status()
        data = response.json()
    except SiteError:
        raise
    except Exception as exc:
        raise SiteError(f"请求北斗接口失败：{exc}") from exc
    if isinstance(data, dict) and data.get("code") not in (None, 0):
        if data.get("code") == 401:
            raise SiteError("登录已失效，请重新上传登录文件。")
        raise SiteError(str(data.get("msg") or f"接口错误 {data.get('code')}"))
    return data


def init_db() -> None:
    with open_db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS episodes (
                task_id INTEGER NOT NULL,
                episode_order INTEGER NOT NULL,
                file_path TEXT NOT NULL,
                status TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (task_id, episode_order)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS processed_library (
                series_id TEXT PRIMARY KEY,
                series_path TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                title_zh TEXT NOT NULL DEFAULT '',
                bio TEXT NOT NULL DEFAULT '',
                platform TEXT NOT NULL DEFAULT '',
                audience_category TEXT NOT NULL,
                setting_category TEXT NOT NULL,
                classification_locked INTEGER NOT NULL DEFAULT 0,
                download_count INTEGER NOT NULL DEFAULT 0,
                published_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS dramas (
                task_id INTEGER PRIMARY KEY,
                title TEXT NOT NULL,
                title_zh TEXT NOT NULL DEFAULT '',
                bio TEXT NOT NULL DEFAULT '',
                platform TEXT NOT NULL DEFAULT '',
                app_id TEXT NOT NULL DEFAULT '',
                app_name TEXT NOT NULL DEFAULT '',
                task_type INTEGER NOT NULL DEFAULT 1,
                serial_id TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT '',
                language_name TEXT NOT NULL DEFAULT '',
                publish_at TEXT NOT NULL DEFAULT '',
                description TEXT NOT NULL DEFAULT '',
                episode_count INTEGER NOT NULL DEFAULT 0,
                accessible_count INTEGER NOT NULL DEFAULT 0,
                output_dir TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'discovered',
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                total_attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                last_job_id TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                first_seen_at TEXT NOT NULL,
                last_attempt_at TEXT NOT NULL DEFAULT '',
                completed_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )
        ensure_table_columns(conn, "processed_library", {
            "title_zh": "TEXT NOT NULL DEFAULT ''",
            "bio": "TEXT NOT NULL DEFAULT ''",
            "platform": "TEXT NOT NULL DEFAULT ''",
            "bio_zh": "TEXT NOT NULL DEFAULT ''",
            "source_language": "TEXT NOT NULL DEFAULT ''",
            "is_ai_generated": "TEXT NOT NULL DEFAULT 'unknown'",
            "publication_title": "TEXT NOT NULL DEFAULT ''",
            "publication_bio": "TEXT NOT NULL DEFAULT ''",
            "hashtags_json": "TEXT NOT NULL DEFAULT '[]'",
            "classification_confidence": "REAL NOT NULL DEFAULT 0",
            "classification_rationale": "TEXT NOT NULL DEFAULT ''",
            "source_job_id": "TEXT NOT NULL DEFAULT ''",
            "processed_path": "TEXT NOT NULL DEFAULT ''",
            "metadata_json": "TEXT NOT NULL DEFAULT '{}'",
            "synced_at": "TEXT NOT NULL DEFAULT ''",
        })
        ensure_table_columns(conn, "dramas", {
            "title_zh": "TEXT NOT NULL DEFAULT ''",
            "bio": "TEXT NOT NULL DEFAULT ''",
            "platform": "TEXT NOT NULL DEFAULT ''",
        })
        conn.execute("CREATE INDEX IF NOT EXISTS idx_dramas_status ON dramas(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_processed_library_categories ON processed_library(audience_category, setting_category)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_processed_library_platform ON processed_library(platform)")


def deduplicate_catalog(catalog: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    unique: list[dict[str, Any]] = []
    seen: set[int] = set()
    duplicates = 0
    for item in catalog:
        task_id = int(item.get("task_id") or 0)
        if not task_id or task_id in seen:
            duplicates += 1
            continue
        seen.add(task_id)
        unique.append(item)
    return unique, duplicates


def save_drama_metadata(detail: dict[str, Any], job_id: str, output_dir: Path, app_name: str = "") -> sqlite3.Row:
    task_id = int(detail.get("task_id") or 0)
    if not task_id:
        raise SiteError("短剧缺少 task_id，无法保存到数据库。")
    now = datetime.now().isoformat(timespec="seconds")
    title = str(detail.get("title") or f"短剧_{task_id}")
    title_zh = first_text(detail, "title_zh", "zh_title", "cn_title", "title_cn", "chinese_title")
    bio = first_text(detail, "bio", "description", "intro", "introduction", "summary")
    platform = app_name or first_text(detail, "platform", "platform_name", "app_name")
    total_episodes = int(detail.get("episode_count") or 0)
    locked_point = int(detail.get("locked_point") or 0)
    accessible_count = min(total_episodes, locked_point - 1) if locked_point > 1 else total_episodes
    values = (
        task_id,
        title,
        title_zh,
        bio,
        platform,
        str(detail.get("app_id") or ""),
        app_name,
        int(detail.get("task_type") or 1),
        str(detail.get("serial_id") or ""),
        str(detail.get("language") or ""),
        str(detail.get("language_name") or ""),
        str(detail.get("publish_at") or ""),
        str(detail.get("description") or ""),
        total_episodes,
        accessible_count,
        str(output_dir),
        job_id,
        json.dumps(detail, ensure_ascii=False, default=str),
        now,
        now,
    )
    with open_db() as conn:
        conn.row_factory = sqlite3.Row
        conn.execute(
            """
            INSERT INTO dramas (
                task_id, title, title_zh, bio, platform, app_id, app_name, task_type, serial_id, language, language_name,
                publish_at, description, episode_count, accessible_count, output_dir, last_job_id,
                metadata_json, first_seen_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                title=excluded.title,
                title_zh=CASE WHEN excluded.title_zh='' THEN dramas.title_zh ELSE excluded.title_zh END,
                bio=CASE WHEN excluded.bio='' THEN dramas.bio ELSE excluded.bio END,
                platform=CASE WHEN excluded.platform='' THEN dramas.platform ELSE excluded.platform END,
                app_id=excluded.app_id,
                app_name=CASE WHEN excluded.app_name='' THEN dramas.app_name ELSE excluded.app_name END,
                task_type=excluded.task_type,
                serial_id=CASE WHEN excluded.serial_id='' THEN dramas.serial_id ELSE excluded.serial_id END,
                language=excluded.language,
                language_name=CASE WHEN excluded.language_name='' THEN dramas.language_name ELSE excluded.language_name END,
                publish_at=excluded.publish_at,
                description=CASE WHEN excluded.description='' THEN dramas.description ELSE excluded.description END,
                episode_count=excluded.episode_count,
                accessible_count=excluded.accessible_count,
                output_dir=excluded.output_dir,
                last_job_id=excluded.last_job_id,
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at
            """,
            values,
        )
        return conn.execute("SELECT * FROM dramas WHERE task_id=?", (task_id,)).fetchone()


def get_drama_record(task_id: int) -> sqlite3.Row | None:
    with open_db() as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM dramas WHERE task_id=?", (task_id,)).fetchone()


def begin_drama_attempt(task_id: int, job_id: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with open_db() as conn:
        conn.execute(
            """
            UPDATE dramas
            SET status='downloading', total_attempts=total_attempts+1, last_job_id=?, last_attempt_at=?, updated_at=?
            WHERE task_id=?
            """,
            (job_id, now, now, task_id),
        )


def register_drama_failure(task_id: int, job_id: str, error: str) -> tuple[int, str]:
    now = datetime.now().isoformat(timespec="seconds")
    with open_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT consecutive_failures FROM dramas WHERE task_id=?", (task_id,)).fetchone()
        failures = int(row[0] if row else 0) + 1
        status = "abandoned" if failures >= 3 else "retry_pending"
        conn.execute(
            """
            UPDATE dramas
            SET status=?, consecutive_failures=?, last_error=?, last_job_id=?, updated_at=?
            WHERE task_id=?
            """,
            (status, failures, str(error)[-4000:], job_id, now, task_id),
        )
    return failures, status


def mark_drama_complete(task_id: int, job_id: str) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    with open_db() as conn:
        conn.execute(
            """
            UPDATE dramas
            SET status='complete', consecutive_failures=0, last_error='', last_job_id=?, completed_at=?, updated_at=?
            WHERE task_id=?
            """,
            (job_id, now, now, task_id),
        )


def library_series_id(series_path: Path) -> str:
    normalized = str(series_path.resolve()).lower().encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()[:20]


def read_series_text(series_path: Path) -> str:
    chunks = [series_path.name]
    for md_path in sorted((*series_path.glob("*.md"), *series_path.glob("*.txt")))[:5]:
        try:
            chunks.append(md_path.read_text(encoding="utf-8-sig", errors="replace")[:10000])
        except OSError:
            continue
    return "\n".join(chunks)


def processed_directory(series_path: Path) -> Path | None:
    """Prefer the main application's `processed` output, with legacy fallback."""
    for name in ("processed", "process"):
        candidate = series_path / name
        if candidate.is_dir():
            return candidate
    return None


def iter_series_paths(root: Path, maximum_depth: int = 4) -> list[Path]:
    """Find projects without descending into result/cache internals indefinitely."""
    ignored = {".video-service", "任务记录", "字幕终稿", "解说", "recap_cache", "assets", "logs"}
    found: dict[str, Path] = {}
    pending: list[tuple[Path, int]] = [(root, 0)]
    while pending:
        current, depth = pending.pop()
        if processed_directory(current):
            found[str(current.resolve()).casefold()] = current
            continue
        if depth >= maximum_depth:
            continue
        try:
            children = [item for item in current.iterdir() if item.is_dir() and item.name not in ignored]
        except OSError:
            continue
        pending.extend((item, depth + 1) for item in children)
    return sorted(found.values(), key=lambda item: str(item).casefold())


def publishing_metadata(process_path: Path) -> dict[str, Any]:
    path = process_path / "publishing_metadata.json"
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def markdown_field(text: str, *labels: str, multiline: bool = False) -> str:
    escaped = "|".join(re.escape(label) for label in labels)
    match = re.search(rf"(?mi)^\s*(?:{escaped})\s*[：:]\s*(.*)$", text)
    if not match:
        return ""
    inline = match.group(1).strip()
    if inline or not multiline:
        return "" if inline in {"未设置", "未获取", "未知"} else inline
    remainder = text[match.end():].lstrip("\r\n")
    stop = re.search(
        r"(?mi)^\s*(?:中文标题|BIO|简介|语言|归属平台|平台ID|发布时间|总集数|站点授权可下载集数|URL|对应CPS链接|本地文件|生成时间)\s*[：:]",
        remainder,
    )
    value = (remainder[:stop.start()] if stop else remainder).strip()
    return value


def linked_drama_record(conn: sqlite3.Connection, series_path: Path, text: str) -> sqlite3.Row | None:
    task_match = re.search(r"(?:task_id=|北斗任务ID\s*[：:])\s*(\d+)", text)
    if task_match:
        row = conn.execute("SELECT * FROM dramas WHERE task_id=?", (int(task_match.group(1)),)).fetchone()
        if row:
            return row
    return conn.execute("SELECT * FROM dramas WHERE output_dir=? ORDER BY updated_at DESC LIMIT 1", (str(series_path.resolve()),)).fetchone()


def infer_classification(text: str) -> tuple[str, str]:
    lowered = text.lower()
    female_words = (
        "女频", "霸总", "闪婚", "萌宝", "千金", "王妃", "皇后", "新娘", "夫人", "妻子", "离婚",
        "爱情", "甜宠", "总裁", "bride", "wife", "romance", "زوجة", "عروس", "حب", "أميرة",
    )
    male_words = (
        "男频", "战神", "兵王", "神医", "赘婿", "龙王", "逆袭", "首富", "少爷", "兄弟", "热血",
        "功夫", "王者", "warrior", "king", "revenge", "محارب", "ملك", "انتقام",
    )
    magic_words = (
        "魔幻", "魔法", "狼人", "吸血鬼", "异能", "玄幻", "修仙", "仙尊", "妖", "精灵", "神龙",
        "magic", "werewolf", "vampire", "fantasy", "سحر", "ذئب", "مصاص دماء", "خيال",
    )
    ancient_words = (
        "古装", "王爷", "王妃", "皇帝", "皇后", "公主", "将军", "宫廷", "朝代", "武侠", "江湖",
        "ancient", "emperor", "palace", "dynasty", "إمبراطور", "قصر", "أمير", "ملكة",
    )
    female_score = sum(1 for word in female_words if word in lowered)
    male_score = sum(1 for word in male_words if word in lowered)
    audience = "女频" if female_score > male_score else "男频" if male_score > female_score else "中性"
    magic_score = sum(1 for word in magic_words if word in lowered)
    ancient_score = sum(1 for word in ancient_words if word in lowered)
    setting = "魔幻" if magic_score >= ancient_score and magic_score else "古装" if ancient_score else "现代"
    return audience, setting


def series_publish_date(series_path: Path, text: str) -> str:
    match = re.search(r"发布时间\s*[：:]\s*(\d{4}-\d{1,2}-\d{1,2})", text)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y-%m-%d").date().isoformat()
        except ValueError:
            pass
    return datetime.fromtimestamp(series_path.stat().st_mtime).date().isoformat()


def process_files(process_path: Path) -> list[Path]:
    return sorted(
        (path for path in process_path.rglob("*") if path.is_file() and path.suffix.lower() in LIBRARY_EXTENSIONS),
        key=lambda path: str(path.relative_to(process_path)).lower(),
    )


def choose_cover(series_path: Path, process_path: Path) -> Path | None:
    process_images = [path for path in process_files(process_path) if path.suffix.lower() in IMAGE_EXTENSIONS]
    parent_images = [path for path in series_path.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS]
    candidates = process_images + sorted(parent_images)
    if not candidates:
        return None
    return min(candidates, key=lambda path: (0 if any(key in path.stem.lower() for key in ("cover", "poster", "封面")) else 1, str(path).lower()))


def scan_processed_library(root: Path | None = None) -> list[dict[str, Any]]:
    library_root = allowed_library_root(root or DEFAULT_LIBRARY_SCAN_ROOT)
    if not library_root.exists() or not library_root.is_dir():
        raise HTTPException(404, f"视频库目录不存在：{library_root}")
    now = datetime.now().isoformat(timespec="seconds")
    found: list[dict[str, Any]] = []
    with open_db() as conn:
        conn.row_factory = sqlite3.Row
        for series_path in iter_series_paths(library_root):
            process_path = processed_directory(series_path)
            if process_path is None:
                continue
            files = process_files(process_path)
            text = read_series_text(series_path)
            metadata = publishing_metadata(process_path)
            classification = metadata.get("classification") if isinstance(metadata.get("classification"), dict) else {}
            series_id = library_series_id(series_path)
            audience, setting = infer_classification(text)
            if classification.get("audience") in {"男频", "女频", "中性"}:
                audience = str(classification["audience"])
            if classification.get("setting") in {"魔幻", "古装", "现代"}:
                setting = str(classification["setting"])
            published_at = series_publish_date(series_path, text)
            existing = conn.execute("SELECT * FROM processed_library WHERE series_id=?", (series_id,)).fetchone()
            drama = linked_drama_record(conn, series_path, text)
            title_zh = str(metadata.get("title_zh") or "") or (str(existing["title_zh"]) if existing and existing["title_zh"] else "") or (str(drama["title_zh"]) if drama and drama["title_zh"] else "") or markdown_field(text, "中文标题")
            bio_zh = str(metadata.get("bio_zh") or "")
            publication_title = str(metadata.get("title") or "")
            publication_bio = str(metadata.get("bio") or "")
            bio = publication_bio or (str(existing["bio"]) if existing and existing["bio"] else "") or (str(drama["bio"]) if drama and drama["bio"] else "") or markdown_field(text, "BIO", "简介", multiline=True)
            platform = str(metadata.get("platform") or "") or (str(existing["platform"]) if existing and existing["platform"] else "") or (str(drama["platform"]) if drama and drama["platform"] else "") or markdown_field(text, "归属平台", "平台")
            hashtags = metadata.get("hashtags") if isinstance(metadata.get("hashtags"), list) else []
            if existing and existing["classification_locked"]:
                audience = existing["audience_category"]
                setting = existing["setting_category"]
            conn.execute(
                """
                INSERT INTO processed_library
                    (series_id, series_path, title, title_zh, bio, platform, audience_category, setting_category,
                     classification_locked, download_count, published_at, updated_at, bio_zh, source_language,
                     is_ai_generated, publication_title, publication_bio, hashtags_json,
                     classification_confidence, classification_rationale, source_job_id, processed_path,
                     metadata_json, synced_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(series_id) DO UPDATE SET
                    series_path=excluded.series_path,
                    title=excluded.title,
                    title_zh=CASE WHEN excluded.title_zh<>'' THEN excluded.title_zh ELSE processed_library.title_zh END,
                    bio=CASE WHEN excluded.bio<>'' THEN excluded.bio ELSE processed_library.bio END,
                    platform=CASE WHEN excluded.platform<>'' THEN excluded.platform ELSE processed_library.platform END,
                    audience_category=CASE WHEN processed_library.classification_locked=1 THEN processed_library.audience_category ELSE excluded.audience_category END,
                    setting_category=CASE WHEN processed_library.classification_locked=1 THEN processed_library.setting_category ELSE excluded.setting_category END,
                    published_at=excluded.published_at,
                    updated_at=excluded.updated_at,
                    bio_zh=CASE WHEN excluded.bio_zh<>'' THEN excluded.bio_zh ELSE processed_library.bio_zh END,
                    source_language=CASE WHEN excluded.source_language<>'' THEN excluded.source_language ELSE processed_library.source_language END,
                    is_ai_generated=excluded.is_ai_generated,
                    publication_title=CASE WHEN excluded.publication_title<>'' THEN excluded.publication_title ELSE processed_library.publication_title END,
                    publication_bio=CASE WHEN excluded.publication_bio<>'' THEN excluded.publication_bio ELSE processed_library.publication_bio END,
                    hashtags_json=CASE WHEN excluded.hashtags_json<>'[]' THEN excluded.hashtags_json ELSE processed_library.hashtags_json END,
                    classification_confidence=excluded.classification_confidence,
                    classification_rationale=excluded.classification_rationale,
                    source_job_id=CASE WHEN excluded.source_job_id<>'' THEN excluded.source_job_id ELSE processed_library.source_job_id END,
                    processed_path=excluded.processed_path,
                    metadata_json=CASE WHEN excluded.metadata_json<>'{}' THEN excluded.metadata_json ELSE processed_library.metadata_json END,
                    synced_at=excluded.synced_at
                """,
                (
                    series_id, str(series_path), series_path.name, title_zh, bio, platform, audience, setting,
                    published_at, now, bio_zh, str(metadata.get("language") or ""),
                    str(metadata.get("is_ai_generated") or "unknown"), publication_title,
                    publication_bio, json.dumps(hashtags, ensure_ascii=False),
                    float(classification.get("confidence") or 0), str(classification.get("rationale") or ""),
                    str(metadata.get("external_job_id") or ""), str(process_path),
                    json.dumps(metadata, ensure_ascii=False), now,
                ),
            )
            row = conn.execute("SELECT * FROM processed_library WHERE series_id=?", (series_id,)).fetchone()
            cover = choose_cover(series_path, process_path)
            video_count = sum(path.suffix.lower() in VIDEO_EXTENSIONS for path in files)
            found.append({
                "id": series_id,
                "title": row["title"],
                "title_zh": row["title_zh"],
                "display_title": row["title_zh"] or row["title"],
                "bio": row["bio"],
                "bio_zh": row["bio_zh"],
                "platform": row["platform"],
                "source_language": row["source_language"],
                "is_ai_generated": row["is_ai_generated"],
                "publication_title": row["publication_title"],
                "publication_bio": row["publication_bio"],
                "hashtags": json.loads(row["hashtags_json"] or "[]"),
                "audience_category": row["audience_category"],
                "setting_category": row["setting_category"],
                "classification_confidence": row["classification_confidence"],
                "classification_locked": bool(row["classification_locked"]),
                "download_count": row["download_count"],
                "published_at": row["published_at"],
                "file_count": len(files),
                "video_count": video_count,
                "has_cover": cover is not None,
                "cover_url": f"{PORTAL_PREFIX}/api/library/{series_id}/cover" if cover else None,
                "preview_url": f"{PORTAL_PREFIX}/library/{series_id}",
                "download_url": f"{PORTAL_PREFIX}/api/library/{series_id}/download",
            })
    return found


def get_library_record(series_id: str) -> tuple[sqlite3.Row, Path, Path]:
    with open_db() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM processed_library WHERE series_id=?", (series_id,)).fetchone()
    if not row:
        raise HTTPException(404, "短剧不存在，请先刷新视频库。")
    series_path = Path(row["series_path"]).resolve()
    allowed_library_root(series_path)
    stored_processed = str(row["processed_path"] or "").strip()
    process_path = Path(stored_processed).resolve() if stored_processed else (processed_directory(series_path) or series_path / "processed").resolve()
    if not series_path.is_dir() or not process_path.is_dir() or process_path.parent != series_path:
        raise HTTPException(404, "短剧的 processed/process 文件夹已不存在。")
    return row, series_path, process_path


def safe_archive_name(title: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")
    return cleaned or "短剧"


def archived(task_id: int, episode_order: int, path: Path) -> bool:
    if not path.exists() or path.stat().st_size <= 0:
        return False
    with open_db() as conn:
        row = conn.execute(
            "SELECT status FROM episodes WHERE task_id=? AND episode_order=?",
            (task_id, episode_order),
        ).fetchone()
    return bool(row and row[0] == "complete")


def mark_archive(task_id: int, episode_order: int, path: Path, status: str) -> None:
    with open_db() as conn:
        conn.execute(
            """
            INSERT INTO episodes(task_id, episode_order, file_path, status, updated_at)
            VALUES(?,?,?,?,?)
            ON CONFLICT(task_id, episode_order) DO UPDATE SET
              file_path=excluded.file_path, status=excluded.status, updated_at=excluded.updated_at
            """,
            (task_id, episode_order, str(path), status, datetime.now().isoformat(timespec="seconds")),
        )


def safe_name(value: str, fallback: str = "未命名短剧") -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or "")).strip().rstrip(".")
    value = re.sub(r"\s+", " ", value)
    return (value or fallback)[:120]


def find_media_url(value: Any) -> str:
    if isinstance(value, str):
        candidate = value.strip()
        if candidate.startswith(("http://", "https://")) and any(mark in candidate.lower() for mark in (".mp4", ".m3u8", ".mov")):
            return candidate
    elif isinstance(value, dict):
        for child in value.values():
            found = find_media_url(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_media_url(child)
            if found:
                return found
    return ""


def parse_date(value: str) -> str:
    if not value:
        return ""
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d").date().isoformat()
    except ValueError:
        return ""


catalog_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
catalog_lock = threading.Lock()


def fetch_catalog(language: int, date_from: str = "", date_to: str = "", app_id: str = "", search: str = "") -> list[dict[str, Any]]:
    date_from = parse_date(date_from)
    date_to = parse_date(date_to)
    cache_key = json.dumps([language, date_from, date_to, app_id, search], ensure_ascii=False)
    with catalog_lock:
        cached = catalog_cache.get(cache_key)
        if cached and time.time() - cached[0] < 180:
            return list(cached[1])

    results: list[dict[str, Any]] = []
    page_num = 1
    page_size = 200
    while True:
        params = {
            "task_type": 1,
            "page_num": page_num,
            "page_size": page_size,
            "app_id": app_id,
            "order_field": "publish_at",
            "order_dir": "desc",
            "language": language,
            "search_title": search,
            "campaign_status": 0,
        }
        body = request_json("GET", "/agent/v1/task/page", params=params).get("body") or {}
        batch = body.get("data") or []
        for item in batch:
            published = parse_date(str(item.get("publish_at") or item.get("created_at") or ""))
            if date_from and published and published < date_from:
                continue
            if date_to and published and published > date_to:
                continue
            results.append(item)
        page = body.get("page") or {}
        total = int(page.get("total_count") or 0)
        if not batch or page_num * page_size >= total:
            break
        if date_from:
            oldest = min((parse_date(str(x.get("publish_at") or "")) for x in batch), default="")
            if oldest and oldest < date_from:
                break
        page_num += 1

    with catalog_lock:
        catalog_cache[cache_key] = (time.time(), list(results))
    return results


def get_enums() -> dict[str, Any]:
    enum_body = request_json("GET", "/agent/v1/enum").get("body") or {}
    app_body = request_json("GET", "/agent/v1/app/map").get("body") or {}
    apps = []
    for key, value in app_body.items():
        if isinstance(value, dict) and value.get("app_id"):
            apps.append(value)
    apps.sort(key=lambda x: (x.get("sort", 999), x.get("app_name", "")))
    return {
        "languages": [x for x in enum_body.get("language", []) if x.get("serial")],
        "apps": apps,
        "cover_host": enum_body.get("cover_host") or "https://bj-play.inbeidou.cn",
    }


jobs: dict[str, dict[str, Any]] = {}
job_lock = threading.Lock()


def job_snapshot(job_id: str) -> dict[str, Any]:
    with job_lock:
        job = jobs.get(job_id)
        if not job:
            raise KeyError(job_id)
        return json.loads(json.dumps({k: v for k, v in job.items() if k != "cancel_event"}, ensure_ascii=False))


def update_job(job_id: str, **values: Any) -> None:
    with job_lock:
        jobs[job_id].update(values)
        jobs[job_id]["updated_at"] = datetime.now().isoformat(timespec="seconds")


def bump_job(job_id: str, **deltas: int) -> None:
    with job_lock:
        for key, delta in deltas.items():
            jobs[job_id][key] = int(jobs[job_id].get(key) or 0) + delta
        jobs[job_id]["updated_at"] = datetime.now().isoformat(timespec="seconds")


def log_job(job_id: str, message: str, level: str = "info") -> None:
    entry = {"time": datetime.now().strftime("%H:%M:%S"), "level": level, "message": message}
    with job_lock:
        jobs[job_id]["logs"].append(entry)
        jobs[job_id]["logs"] = jobs[job_id]["logs"][-600:]
        jobs[job_id]["updated_at"] = datetime.now().isoformat(timespec="seconds")


def is_cancelled(job_id: str) -> bool:
    with job_lock:
        return jobs[job_id]["cancel_event"].is_set()


class DramaStartLimiter:
    def __init__(self, interval_seconds: int) -> None:
        self.interval_seconds = interval_seconds
        self.next_start = 0.0
        self.lock = threading.Lock()

    def wait(self, job_id: str) -> None:
        with self.lock:
            now = time.monotonic()
            scheduled = max(now, self.next_start)
            self.next_start = scheduled + self.interval_seconds
            delay = scheduled - now
        if delay <= 0:
            return
        with job_lock:
            cancel_event = jobs[job_id]["cancel_event"]
        if cancel_event.wait(delay):
            raise SiteError("任务已取消")


def set_active_drama(job_id: str, title: str, active: bool) -> None:
    with job_lock:
        titles = jobs[job_id].setdefault("active_titles", [])
        if active and title not in titles:
            titles.append(title)
        elif not active and title in titles:
            titles.remove(title)
        jobs[job_id]["current_title"] = " ｜ ".join(titles)
        jobs[job_id]["updated_at"] = datetime.now().isoformat(timespec="seconds")


def run_series_worker(
    limiter: DramaStartLimiter,
    job_id: str,
    request: JobRequest,
    item: dict[str, Any],
    output_root: Path,
    enums: dict[str, Any],
    language_map: dict[int, str],
    app_map: dict[str, dict[str, Any]],
    label: str,
    count_episode_total: bool,
) -> None:
    limiter.wait(job_id)
    title = str(item.get("title") or f"短剧_{item.get('task_id')}")
    set_active_drama(job_id, title, True)
    try:
        download_series_once(job_id, request, item, output_root, enums, language_map, app_map, label, count_episode_total)
    finally:
        set_active_drama(job_id, title, False)


def download_http(url: str, target: Path, job_id: str) -> None:
    temp = target.with_suffix(target.suffix + ".part")
    if temp.exists():
        temp.unlink()
    headers = {
        "Referer": f"{SITE_BASE}/",
        "Origin": SITE_BASE,
        "User-Agent": api_headers().get("User-Agent", "Mozilla/5.0"),
    }
    with httpx.Client(timeout=httpx.Timeout(30, read=300), follow_redirects=True, headers=headers) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            with temp.open("wb") as handle:
                for chunk in response.iter_bytes(1024 * 1024):
                    if is_cancelled(job_id):
                        raise SiteError("任务已取消")
                    handle.write(chunk)
    temp.replace(target)


def download_hls(url: str, target: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise SiteError("未找到 ffmpeg，无法下载 m3u8 视频。")
    temp = target.with_suffix(".part.mp4")
    if temp.exists():
        temp.unlink()
    command = [
        ffmpeg,
        "-y",
        "-headers",
        f"Referer: {SITE_BASE}/\r\nOrigin: {SITE_BASE}\r\n",
        "-i",
        url,
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(temp),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=1800, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    if result.returncode != 0 or not temp.exists():
        error = (result.stderr or "ffmpeg 下载失败")[-1200:]
        raise SiteError(error)
    temp.replace(target)


def download_blob_endpoint(params: dict[str, Any], target: Path, job_id: str) -> None:
    temp = target.with_suffix(target.suffix + ".part")
    if temp.exists():
        temp.unlink()
    with httpx.Client(timeout=httpx.Timeout(30, read=600), follow_redirects=True, headers=api_headers(True)) as client:
        with client.stream("GET", f"{API_BASE}/agent/v1/episode/download", params=params) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if "json" in content_type:
                raise SiteError(str(response.json().get("msg") or "视频下载接口返回错误"))
            with temp.open("wb") as handle:
                for chunk in response.iter_bytes(1024 * 1024):
                    if is_cancelled(job_id):
                        raise SiteError("任务已取消")
                    handle.write(chunk)
    temp.replace(target)


def fetch_cps(task: dict[str, Any], supported: list[int], job_id: str) -> dict[str, str]:
    links: dict[str, str] = {}
    for platform in supported:
        if platform not in PLATFORM_NAMES or is_cancelled(job_id):
            continue
        try:
            response = request_json(
                "POST",
                "/agent/v1/task/receive",
                json_body={
                    "task_id": int(task["task_id"]),
                    "task_type": int(task.get("task_type") or 1),
                    "platform": platform,
                },
                auth_required=True,
            )
            body = response.get("body") or {}
            link = body.get("serial_link") or body.get("tiktok_dramago_link") or body.get("app_link") or ""
            links[PLATFORM_NAMES[platform]] = str(link)
        except Exception as exc:
            links[PLATFORM_NAMES[platform]] = ""
            log_job(job_id, f"{PLATFORM_NAMES[platform]} CPS 链接获取失败：{exc}", "warning")
    return links


def write_markdown(series_dir: Path, detail: dict[str, Any], app_name: str, cps: dict[str, str], files: list[str], accessible_count: int) -> None:
    title = str(detail.get("title") or "未命名短剧")
    title_zh = first_text(detail, "title_zh", "zh_title", "cn_title", "title_cn", "chinese_title")
    bio = first_text(detail, "bio", "description", "intro", "introduction", "summary")
    task_id = detail.get("task_id")
    app_id = detail.get("app_id") or ""
    task_type = detail.get("task_type") or 1
    source_url = f"{SITE_BASE}/task-detail?task_id={task_id}&app_id={app_id}&task_type={task_type}"
    lines = [
        f"# {title}",
        "",
        f"标题：{title}",
        "",
        f"中文标题：{title_zh or '未设置'}",
        "",
        f"BIO：{bio}",
        "",
        "简介：",
        bio,
        "",
        f"语言：{detail.get('language_name') or detail.get('language') or ''}",
        "",
        f"归属平台：{app_name or app_id}",
        "",
        f"平台ID：{detail.get('third_serial_id') or ''}",
        "",
        f"发布时间：{detail.get('publish_at') or ''}",
        "",
        f"总集数：{detail.get('episode_count') or 0}",
        "",
        f"站点授权可下载集数：1-{accessible_count}",
        "",
        f"URL：{source_url}",
        "",
        "对应CPS链接：",
        "",
    ]
    for platform in ("TikTok", "Facebook", "Instagram", "YouTube"):
        lines.append(f"- {platform}：{cps.get(platform) or '未获取/不支持'}")
    lines.extend(["", "本地文件：", ""])
    lines.extend(f"- {name}" for name in files)
    lines.extend(["", f"生成时间：{datetime.now().isoformat(timespec='seconds')}", ""])
    (series_dir / f"{safe_name(title)}.md").write_text("\n".join(lines), encoding="utf-8")


def download_series_once(
    job_id: str,
    request: JobRequest,
    item: dict[str, Any],
    output_root: Path,
    enums: dict[str, Any],
    language_map: dict[int, str],
    app_map: dict[str, dict[str, Any]],
    label: str,
    count_episode_total: bool,
) -> None:
    task_id = int(item.get("task_id") or 0)
    begin_drama_attempt(task_id, job_id)
    params = {"task_id": task_id, "app_id": item.get("app_id"), "task_type": item.get("task_type") or 1}
    detail = request_json("GET", "/agent/v1/task/info", params=params, auth_required=True).get("body") or item
    detail = {**item, **detail}
    detail["language_name"] = language_map.get(int(detail.get("language") or 0), "")
    title = str(detail.get("title") or f"短剧_{task_id}")
    folder = output_root / safe_name(title)
    folder.mkdir(parents=True, exist_ok=True)
    app_info = app_map.get(str(detail.get("app_id")), {})
    app_name = str(app_info.get("app_name") or detail.get("app_id") or "未知平台")
    save_drama_metadata(detail, job_id, folder, app_name)
    log_job(job_id, f"{label}《{title}》 · {app_name}")

    cover_url = str(detail.get("cover") or "")
    if cover_url and not cover_url.startswith("http"):
        cover_url = f"{enums['cover_host'].rstrip('/')}/{cover_url.lstrip('/')}"
    cover_suffix = ".webp" if "format%2cwebp" in cover_url.lower() or "format,webp" in cover_url.lower() else Path(urlparse(cover_url).path).suffix.lower() or ".webp"
    if cover_suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        cover_suffix = ".webp"
    cover_path = folder / f"cover{cover_suffix}"
    if cover_url and not cover_path.exists():
        try:
            download_http(cover_url, cover_path, job_id)
        except Exception as exc:
            log_job(job_id, f"封面下载失败：{exc}", "warning")

    supported_raw = app_info.get("supported_platforms") or "[1,2,3,4]"
    try:
        supported = [int(x) for x in (json.loads(supported_raw) if isinstance(supported_raw, str) else supported_raw or [])]
    except Exception:
        supported = [1, 2, 3, 4]
    cps = fetch_cps(detail, supported, job_id) if request.include_cps else {}

    total_episodes = int(detail.get("episode_count") or 0)
    locked_point = int(detail.get("locked_point") or 0)
    accessible_count = min(total_episodes, locked_point - 1) if locked_point > 1 else total_episodes
    if accessible_count < total_episodes:
        log_job(job_id, f"站点当前授权可下载 1-{accessible_count} 集；其余 {total_episodes - accessible_count} 集在原站标记为锁定。", "warning")
    if count_episode_total:
        bump_job(job_id, episode_total=accessible_count)
    digits = max(2, len(str(total_episodes)))
    local_files: list[str] = []

    for episode in range(1, accessible_count + 1):
        if is_cancelled(job_id):
            raise SiteError("任务已取消")
        filename = f"{safe_name(title)}_第{episode:0{digits}d}集.mp4"
        target = folder / filename
        if archived(task_id, episode, target):
            local_files.append(filename)
            bump_job(job_id, episode_skipped=1)
            log_job(job_id, f"  第 {episode} 集已存在，跳过。")
            continue

        episode_params = {
            "serial_id": detail.get("serial_id"),
            "episode_order": episode,
            "need_play": 1,
            "app_id": detail.get("app_id"),
            "task_type": detail.get("task_type") or 1,
        }
        try:
            info = request_json("GET", "/agent/v1/episode/info", params=episode_params, auth_required=True).get("body") or {}
            play_url = str(info.get("play_url") or "") or find_media_url(info.get("play_list"))
            if play_url:
                if ".m3u8" in play_url.lower():
                    download_hls(play_url, target)
                else:
                    download_http(play_url, target, job_id)
            elif int(app_info.get("support_video") or 0) == 2:
                download_blob_endpoint(episode_params, target, job_id)
            else:
                raise SiteError("接口没有返回视频地址")
            mark_archive(task_id, episode, target, "complete")
            local_files.append(filename)
            bump_job(job_id, episode_complete=1)
            log_job(job_id, f"  第 {episode} 集下载完成。")
        except Exception as exc:
            mark_archive(task_id, episode, target, "failed")
            bump_job(job_id, episode_failed=1)
            raise SiteError(f"第 {episode} 集下载失败：{exc}") from exc
    if cover_path.exists():
        local_files.append(cover_path.name)
    write_markdown(folder, detail, app_name, cps, local_files, accessible_count)
    mark_drama_complete(task_id, job_id)


def process_job(job_id: str, request: JobRequest) -> None:
    try:
        update_job(job_id, status="scanning")
        log_job(job_id, "正在读取符合筛选条件的短剧目录……")
        catalog = fetch_catalog(request.language, request.date_from, request.date_to, request.app_id, request.search)
        if request.mode == "selected":
            selected = set(request.task_ids)
            catalog = [item for item in catalog if int(item.get("task_id") or 0) in selected]
        catalog, duplicate_count = deduplicate_catalog(catalog)
        if not catalog:
            raise SiteError("没有符合条件的短剧。")
        if duplicate_count:
            log_job(job_id, f"数据库去重前发现 {duplicate_count} 条重复或无效记录，已自动排除。", "warning")

        auth = load_auth()
        if not auth.usable:
            raise SiteError("请先上传包含 token 的登录文件。")
        enums = get_enums()
        language_map = {int(x["id"]): x.get("name") or x.get("en_name") for x in enums["languages"]}
        app_map = {str(x["app_id"]): x for x in enums["apps"]}
        output_root = Path(request.output_dir).expanduser().resolve()
        output_root.mkdir(parents=True, exist_ok=True)

        for item in catalog:
            save_drama_metadata(item, job_id, output_root / safe_name(str(item.get("title") or f"短剧_{item.get('task_id')}")))

        update_job(job_id, status="running", series_total=len(catalog), output_dir=str(output_root), max_workers=request.max_workers)
        log_job(
            job_id,
            f"共找到 {len(catalog)} 部唯一短剧，开始首轮下载。并发 {request.max_workers} 部；每隔至少 {request.sleep_seconds} 秒启动一部新短剧。",
        )
        retry_queue: list[dict[str, Any]] = []
        limiter = DramaStartLimiter(request.sleep_seconds)
        with ThreadPoolExecutor(max_workers=request.max_workers, thread_name_prefix=f"drama-{job_id}") as executor:
            first_futures: dict[Future[None], tuple[dict[str, Any], str]] = {}
            for series_index, item in enumerate(catalog, start=1):
                if is_cancelled(job_id):
                    break
                task_id = int(item.get("task_id") or 0)
                title = str(item.get("title") or f"短剧_{task_id}")
                record = get_drama_record(task_id)
                if record and record["status"] == "complete":
                    bump_job(job_id, series_current=1, series_complete=1, series_deduplicated=1)
                    log_job(job_id, f"[{series_index}/{len(catalog)}] 《{title}》数据库已标记完成，去重跳过。")
                    continue
                if record and record["status"] == "abandoned":
                    bump_job(job_id, series_current=1, series_failed=1, series_abandoned=1)
                    log_job(job_id, f"[{series_index}/{len(catalog)}] 《{title}》已连续失败 3 次，数据库标记为放弃。", "error")
                    continue
                failures_before = int(record["consecutive_failures"] if record else 0)
                label = f"[{series_index}/{len(catalog)}] " if failures_before == 0 else f"[{series_index}/{len(catalog)} · 第 {failures_before + 1}/3 次尝试] "
                future = executor.submit(
                    run_series_worker, limiter, job_id, request, item, output_root, enums, language_map, app_map, label, True,
                )
                first_futures[future] = (item, title)

            for future in as_completed(first_futures):
                item, title = first_futures[future]
                task_id = int(item.get("task_id") or 0)
                try:
                    future.result()
                    bump_job(job_id, series_current=1, series_complete=1)
                except Exception as exc:
                    if is_cancelled(job_id):
                        continue
                    bump_job(job_id, series_current=1)
                    failures, status = register_drama_failure(task_id, job_id, str(exc))
                    if status == "abandoned":
                        bump_job(job_id, series_failed=1, series_abandoned=1)
                        log_job(job_id, f"《{title}》连续第 {failures} 次失败，已放弃：{exc}", "error")
                    else:
                        retry_queue.append(item)
                        update_job(job_id, series_retry_pending=len(retry_queue))
                        log_job(job_id, f"《{title}》第 {failures} 次失败，后台标记为待重试；其他并发任务继续：{exc}", "warning")

            retry_round = 1
            while retry_queue and not is_cancelled(job_id):
                retry_round += 1
                update_job(job_id, status="retrying", retry_round=retry_round, series_retry_pending=len(retry_queue))
                log_job(job_id, f"首轮已经结束，开始第 {retry_round}/3 次尝试：并发重试 {len(retry_queue)} 部。", "warning")
                retry_futures: dict[Future[None], tuple[dict[str, Any], str]] = {}
                for retry_index, item in enumerate(retry_queue, start=1):
                    task_id = int(item.get("task_id") or 0)
                    title = str(item.get("title") or f"短剧_{task_id}")
                    record = get_drama_record(task_id)
                    attempt_number = min(3, int(record["consecutive_failures"] if record else retry_round - 1) + 1)
                    future = executor.submit(
                        run_series_worker, limiter, job_id, request, item, output_root, enums, language_map, app_map,
                        f"[重试 {retry_index}/{len(retry_queue)} · 第 {attempt_number}/3 次尝试] ", False,
                    )
                    retry_futures[future] = (item, title)
                next_queue: list[dict[str, Any]] = []
                remaining = len(retry_futures)
                for future in as_completed(retry_futures):
                    item, title = retry_futures[future]
                    task_id = int(item.get("task_id") or 0)
                    remaining -= 1
                    update_job(job_id, series_retry_pending=remaining)
                    try:
                        future.result()
                        bump_job(job_id, series_complete=1, series_retry_success=1)
                        log_job(job_id, f"《{title}》重试成功。")
                    except Exception as exc:
                        if is_cancelled(job_id):
                            continue
                        failures, status = register_drama_failure(task_id, job_id, str(exc))
                        if status == "abandoned":
                            bump_job(job_id, series_failed=1, series_abandoned=1)
                            log_job(job_id, f"《{title}》连续第 {failures} 次失败，已放弃：{exc}", "error")
                        else:
                            next_queue.append(item)
                            log_job(job_id, f"《{title}》连续第 {failures} 次失败，保留到下一轮：{exc}", "warning")
                retry_queue = next_queue
                update_job(job_id, series_retry_pending=len(retry_queue))

        if is_cancelled(job_id):
            update_job(job_id, status="cancelled")
            log_job(job_id, "任务已取消，已下载文件、短剧状态和断点记录都会保留。", "warning")
        else:
            snapshot = job_snapshot(job_id)
            if snapshot.get("series_abandoned"):
                update_job(job_id, status="complete_with_errors", current_title="")
                log_job(job_id, f"下载流程结束：完成 {snapshot['series_complete']} 部，连续失败 3 次后放弃 {snapshot['series_abandoned']} 部。", "warning")
            else:
                update_job(job_id, status="complete", current_title="")
                log_job(job_id, "全部短剧下载完成。")
    except Exception as exc:
        update_job(job_id, status="failed", error=str(exc))
        log_job(job_id, str(exc), "error")


app = FastAPI(title="北斗短剧下载器", version="1.0.0")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
portal_sessions: dict[str, tuple[str, float]] = {}
portal_session_lock = threading.Lock()


def _gateway_key(request: Request) -> dict[str, Any] | None:
    if GATEWAY_DATABASE is None:
        return None

    value = request.headers.get("x-api-key", "")
    authorization = request.headers.get("authorization", "")
    if not value and authorization.casefold().startswith("bearer "):
        value = authorization[7:].strip()
    if value:
        return GATEWAY_DATABASE.authenticate_access_key(value)
    token = request.cookies.get("beidou_portal_session", "")
    if not token:
        return None
    with portal_session_lock:
        session = portal_sessions.get(token)
        if not session or session[1] <= time.time():
            portal_sessions.pop(token, None)
            return None
    try:
        record = GATEWAY_DATABASE.get_access_key(session[0])
    except KeyError:
        return None
    if not record.get("enabled"):
        return None
    expires_at = str(record.get("expires_at") or "").strip()
    if expires_at:
        try:
            if datetime.fromisoformat(expires_at.replace("Z", "+00:00")).timestamp() <= time.time():
                return None
        except ValueError:
            return None
    return record


@app.middleware("http")
async def require_main_gateway_key(request: Request, call_next):
    request_path = request.url.path
    is_api = request_path.startswith("/api/") or request_path.startswith(f"{PORTAL_PREFIX}/api/")
    is_session = request_path in {"/api/session", f"{PORTAL_PREFIX}/api/session"}
    if is_api and not is_session:
        if not _gateway_key(request):
            return JSONResponse({"detail": "请先在主工作台填写并保存有效访问密钥"}, status_code=401)
    return await call_next(request)


@app.post("/api/session")
def create_portal_session(request: Request, response: Response) -> dict[str, Any]:
    key = _gateway_key(request)
    if not key:
        raise HTTPException(401, "无效或已过期的主程序访问密钥")
    token = secrets.token_urlsafe(32)
    expires = time.time() + 12 * 60 * 60
    with portal_session_lock:
        portal_sessions[token] = (str(key["id"]), expires)
        for candidate, (_, deadline) in list(portal_sessions.items()):
            if deadline <= time.time():
                portal_sessions.pop(candidate, None)
    response.set_cookie(
        "beidou_portal_session",
        token,
        max_age=12 * 60 * 60,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        path=PORTAL_PREFIX,
    )
    return {"ok": True, "access_key_id": key["id"]}


@app.get("/")
def home() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/library")
def library_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "library.html")


@app.get("/library/{series_id}")
def preview_page(series_id: str) -> FileResponse:
    return FileResponse(STATIC_DIR / "preview.html")


@app.get("/api/library")
def library_catalog(
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=30),
    root: str = "",
    search: str = "",
    audience: str = "",
    setting: str = "",
) -> dict[str, Any]:
    selected_root = allowed_library_root(Path(root) if root.strip() else DEFAULT_LIBRARY_SCAN_ROOT)
    items = scan_processed_library(selected_root)
    needle = search.strip().lower()
    if needle:
        items = [item for item in items if any(needle in str(item.get(field) or "").lower() for field in ("title", "title_zh", "bio", "platform"))]
    if audience:
        items = [item for item in items if item["audience_category"] == audience]
    if setting:
        items = [item for item in items if item["setting_category"] == setting]
    items.sort(key=lambda item: (item["published_at"], item["title"]), reverse=True)
    total = len(items)
    start = (page - 1) * page_size
    return {
        "total": total,
        "items": items[start : start + page_size],
        "page": page,
        "page_size": page_size,
        "pages": max(1, (total + page_size - 1) // page_size),
        "root": str(selected_root),
    }


@app.get("/api/library/{series_id}")
def library_detail(series_id: str) -> dict[str, Any]:
    row, series_path, process_path = get_library_record(series_id)
    files = process_files(process_path)
    payload_files = []
    for path in files:
        relative = path.relative_to(process_path).as_posix()
        suffix = path.suffix.lower()
        kind = "video" if suffix in VIDEO_EXTENSIONS else "image" if suffix in IMAGE_EXTENSIONS else "text"
        payload_files.append({
            "name": path.name,
            "relative_path": relative,
            "kind": kind,
            "size": path.stat().st_size,
            "media_url": f"{PORTAL_PREFIX}/api/library/{series_id}/media/{quote(relative, safe='/')}",
        })
    cover = choose_cover(series_path, process_path)
    return {
        "id": series_id,
        "title": row["title"],
        "title_zh": row["title_zh"],
        "display_title": row["title_zh"] or row["title"],
        "bio": row["bio"],
        "platform": row["platform"],
        "source_language": row["source_language"],
        "is_ai_generated": row["is_ai_generated"],
        "publication_title": row["publication_title"],
        "publication_bio": row["publication_bio"],
        "hashtags": json.loads(row["hashtags_json"] or "[]"),
        "title_zh_platform": row["title_zh"],
        "bio_zh": row["bio_zh"],
        "classification_confidence": row["classification_confidence"],
        "classification_rationale": row["classification_rationale"],
        "audience_category": row["audience_category"],
        "setting_category": row["setting_category"],
        "classification_locked": bool(row["classification_locked"]),
        "download_count": row["download_count"],
        "published_at": row["published_at"],
        "cover_url": f"{PORTAL_PREFIX}/api/library/{series_id}/cover" if cover else None,
        "download_url": f"{PORTAL_PREFIX}/api/library/{series_id}/download",
        "files": payload_files,
    }


@app.patch("/api/library/{series_id}/classification")
def update_library_classification(series_id: str, update: ClassificationUpdate) -> dict[str, Any]:
    if update.audience_category not in {"男频", "女频", "中性"}:
        raise HTTPException(400, "受众分类必须是男频、女频或中性。")
    if update.setting_category not in {"魔幻", "古装", "现代"}:
        raise HTTPException(400, "题材分类必须是魔幻、古装或现代。")
    get_library_record(series_id)
    with open_db() as conn:
        conn.execute(
            "UPDATE processed_library SET audience_category=?, setting_category=?, classification_locked=1, updated_at=? WHERE series_id=?",
            (update.audience_category, update.setting_category, datetime.now().isoformat(timespec="seconds"), series_id),
        )
    return {"ok": True, "audience_category": update.audience_category, "setting_category": update.setting_category}


@app.get("/api/library/{series_id}/cover")
def library_cover(series_id: str) -> FileResponse:
    _, series_path, process_path = get_library_record(series_id)
    cover = choose_cover(series_path, process_path)
    if not cover:
        raise HTTPException(404, "没有找到封面图片。")
    return FileResponse(cover)


@app.get("/api/library/{series_id}/media/{relative_path:path}")
def library_media(series_id: str, relative_path: str) -> FileResponse:
    _, _, process_path = get_library_record(series_id)
    requested = (process_path / relative_path).resolve()
    try:
        requested.relative_to(process_path)
    except ValueError as exc:
        raise HTTPException(400, "文件路径无效。") from exc
    if not requested.is_file() or requested.suffix.lower() not in LIBRARY_EXTENSIONS:
        raise HTTPException(404, "文件不存在或格式不受支持。")
    media_type = mimetypes.guess_type(requested.name)[0]
    return FileResponse(requested, media_type=media_type)


@app.get("/api/library/{series_id}/download")
def download_library_series(series_id: str) -> FileResponse:
    row, _, process_path = get_library_record(series_id)
    files = process_files(process_path)
    if not files:
        raise HTTPException(404, "process 文件夹中没有可打包的视频、图片或 TXT 文件。")
    archive_root = safe_archive_name(row["title"])
    temp_file = tempfile.NamedTemporaryFile(prefix=f"{series_id}-", suffix=".zip", dir=DATA_DIR, delete=False)
    temp_file.close()
    archive_path = Path(temp_file.name)
    try:
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for path in files:
                relative = path.relative_to(process_path)
                archive.write(path, (Path(archive_root) / relative).as_posix())
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise
    with open_db() as conn:
        conn.execute(
            "UPDATE processed_library SET download_count=download_count+1, updated_at=? WHERE series_id=?",
            (datetime.now().isoformat(timespec="seconds"), series_id),
        )
    return FileResponse(
        archive_path,
        media_type="application/zip",
        filename=f"{archive_root}.zip",
        background=BackgroundTask(archive_path.unlink, missing_ok=True),
    )


@app.get("/api/status")
def status() -> dict[str, Any]:
    auth = load_auth()
    return {
        "auth": {"uploaded": bool(auth.source_name), "usable": auth.usable, "source_name": auth.source_name},
        "default_output": str(DEFAULT_OUTPUT),
        "default_library_root": str(DEFAULT_LIBRARY_SCAN_ROOT),
        "sleep_seconds": 5,
        "max_workers": 2,
    }


@app.post("/api/auth/upload")
async def upload_auth(file: UploadFile = File(...)) -> dict[str, Any]:
    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(400, "登录文件不能超过 5 MB。")
    auth = parse_auth_file(content, file.filename or "登录文件")
    save_auth(auth)
    return {
        "ok": True,
        "usable": auth.usable,
        "has_cookies": bool(auth.cookie_header),
        "message": "登录文件可用。" if auth.usable else "已读取 Cookie，但没有发现 token；北斗详情接口仍会返回 401。请上传包含 token 的 JSON 或纯 token 文本。",
    }


@app.get("/api/enums")
def enums() -> dict[str, Any]:
    try:
        return get_enums()
    except SiteError as exc:
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/catalog")
def catalog(query: CatalogQuery) -> dict[str, Any]:
    try:
        items = fetch_catalog(query.language, query.date_from, query.date_to, query.app_id, query.search)
    except SiteError as exc:
        raise HTTPException(502, str(exc)) from exc
    start = (query.page - 1) * query.page_size
    return {"total": len(items), "items": items[start : start + query.page_size], "page": query.page, "page_size": query.page_size}


@app.post("/api/jobs")
def create_job(request: JobRequest) -> dict[str, Any]:
    if request.mode not in {"selected", "filtered"}:
        raise HTTPException(400, "mode 必须是 selected 或 filtered。")
    if request.mode == "selected" and not request.task_ids:
        raise HTTPException(400, "请至少选择一部短剧。")
    request = request.model_copy(
        update={"output_dir": str(allowed_download_root(Path(request.output_dir)))}
    )
    job_id = uuid.uuid4().hex[:12]
    job = {
        "id": job_id,
        "status": "queued",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "current_title": "",
        "series_total": 0,
        "series_current": 0,
        "series_complete": 0,
        "series_failed": 0,
        "series_deduplicated": 0,
        "series_retry_pending": 0,
        "series_retry_success": 0,
        "series_abandoned": 0,
        "retry_round": 0,
        "max_workers": request.max_workers,
        "active_titles": [],
        "episode_total": 0,
        "episode_complete": 0,
        "episode_skipped": 0,
        "episode_failed": 0,
        "output_dir": request.output_dir,
        "error": "",
        "logs": [],
        "cancel_event": threading.Event(),
    }
    with job_lock:
        active = [x for x in jobs.values() if x["status"] in {"queued", "scanning", "running", "retrying"}]
        if active:
            raise HTTPException(409, "已有下载任务正在运行，请先等待完成或取消。")
        jobs[job_id] = job
    threading.Thread(target=process_job, args=(job_id, request), daemon=True, name=f"inbeidou-{job_id}").start()
    return job_snapshot(job_id)


@app.get("/api/jobs")
def list_jobs() -> list[dict[str, Any]]:
    with job_lock:
        ids = list(jobs.keys())[::-1]
    return [job_snapshot(job_id) for job_id in ids[:20]]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict[str, Any]:
    try:
        return job_snapshot(job_id)
    except KeyError as exc:
        raise HTTPException(404, "任务不存在。") from exc


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> dict[str, Any]:
    with job_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "任务不存在。")
        if job["status"] in {"queued", "scanning", "running", "retrying"}:
            job["cancel_event"].set()
    return job_snapshot(job_id)


if __name__ == "__main__":
    import uvicorn

    configure(
        data_root=DATA_DIR,
        database_path=DB_FILE,
        library_root=DEFAULT_OUTPUT,
        gateway_database_path=APP_DIR / "gateway.sqlite3",
    )
    uvicorn.run(app, host="127.0.0.1", port=8767)
