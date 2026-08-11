from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def hash_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class GatewayDatabase:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._schema_lock = threading.Lock()
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._schema_lock, self.connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS access_keys (
                    id TEXT PRIMARY KEY,
                    label TEXT NOT NULL,
                    secret_hash TEXT NOT NULL UNIQUE,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    maximum_active_jobs INTEGER NOT NULL DEFAULT 3,
                    expires_at TEXT,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    access_key_id TEXT NOT NULL REFERENCES access_keys(id),
                    series_name TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    settings_json TEXT NOT NULL,
                    agent_token_hash TEXT NOT NULL,
                    work_directory TEXT NOT NULL,
                    queue_position INTEGER,
                    process_pid INTEGER,
                    process_started_at_epoch REAL,
                    process_executable TEXT,
                    progress_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    cancelled_at TEXT,
                    UNIQUE(series_name, version)
                );

                CREATE TABLE IF NOT EXISTS agent_sessions (
                    access_key_id TEXT PRIMARY KEY REFERENCES access_keys(id) ON DELETE CASCADE,
                    token_hash TEXT NOT NULL,
                    generation INTEGER NOT NULL DEFAULT 1,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    maximum_parallel INTEGER NOT NULL DEFAULT 3,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_heartbeat_at TEXT,
                    capabilities_json TEXT,
                    capability_probe_nonce TEXT,
                    capabilities_verified_at TEXT,
                    listener_lease_id TEXT,
                    listener_started_at TEXT,
                    idle_started_at TEXT,
                    idle_deadline_at TEXT
                );

                CREATE TABLE IF NOT EXISTS uploads (
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    original_name TEXT NOT NULL,
                    stored_name TEXT NOT NULL,
                    expected_size INTEGER NOT NULL,
                    expected_sha256 TEXT,
                    total_chunks INTEGER NOT NULL,
                    position INTEGER NOT NULL DEFAULT 0,
                    role TEXT NOT NULL DEFAULT 'video',
                    status TEXT NOT NULL,
                    assembled_size INTEGER,
                    assembled_sha256 TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE(job_id, stored_name)
                );

                CREATE TABLE IF NOT EXISTS upload_chunks (
                    upload_id TEXT NOT NULL REFERENCES uploads(id) ON DELETE CASCADE,
                    chunk_index INTEGER NOT NULL,
                    byte_size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(upload_id, chunk_index)
                );

                CREATE TABLE IF NOT EXISTS job_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    data_json TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs(status, created_at);
                CREATE INDEX IF NOT EXISTS idx_events_job_id ON job_events(job_id, id);

                CREATE TABLE IF NOT EXISTS agent_notifications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    access_key_id TEXT NOT NULL REFERENCES access_keys(id) ON DELETE CASCADE,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    title TEXT NOT NULL,
                    message TEXT NOT NULL,
                    data_json TEXT,
                    created_at TEXT NOT NULL,
                    acknowledged_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_agent_notifications_pending
                ON agent_notifications(access_key_id, acknowledged_at, id);
                """
            )
            upload_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(uploads)").fetchall()
            }
            if "position" not in upload_columns:
                connection.execute("ALTER TABLE uploads ADD COLUMN position INTEGER NOT NULL DEFAULT 0")
            if "role" not in upload_columns:
                connection.execute("ALTER TABLE uploads ADD COLUMN role TEXT NOT NULL DEFAULT 'video'")
            job_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(jobs)").fetchall()
            }
            for name, declaration in (
                ("process_started_at_epoch", "REAL"),
                ("process_executable", "TEXT"),
            ):
                if name not in job_columns:
                    connection.execute(f"ALTER TABLE jobs ADD COLUMN {name} {declaration}")
            session_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(agent_sessions)").fetchall()
            }
            if "last_heartbeat_at" not in session_columns:
                connection.execute("ALTER TABLE agent_sessions ADD COLUMN last_heartbeat_at TEXT")
            for name, declaration in (
                ("capabilities_json", "TEXT"),
                ("capability_probe_nonce", "TEXT"),
                ("capabilities_verified_at", "TEXT"),
                ("listener_lease_id", "TEXT"),
                ("listener_started_at", "TEXT"),
                ("idle_started_at", "TEXT"),
                ("idle_deadline_at", "TEXT"),
            ):
                if name not in session_columns:
                    connection.execute(
                        f"ALTER TABLE agent_sessions ADD COLUMN {name} {declaration}"
                    )

    def create_access_key(
        self,
        label: str,
        maximum_active_jobs: int = 3,
        expires_at: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        key_id = secrets.token_hex(8)
        secret = "vdl_" + secrets.token_urlsafe(32)
        created_at = now_iso()
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO access_keys(id,label,secret_hash,maximum_active_jobs,expires_at,created_at) VALUES(?,?,?,?,?,?)",
                (key_id, label.strip() or "unnamed", hash_secret(secret), max(1, int(maximum_active_jobs)), expires_at, created_at),
            )
        return secret, self.get_access_key(key_id)

    def get_access_key(self, key_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM access_keys WHERE id=?", (key_id,)).fetchone()
        if row is None:
            raise KeyError(key_id)
        return dict(row)

    def authenticate_access_key(self, secret: str) -> dict[str, Any] | None:
        digest = hash_secret(secret)
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM access_keys WHERE secret_hash=? AND enabled=1", (digest,)
            ).fetchone()
            if row is None:
                return None
            payload = dict(row)
            if payload.get("expires_at"):
                try:
                    expiry = datetime.fromisoformat(str(payload["expires_at"]).replace("Z", "+00:00"))
                    if expiry.tzinfo is None:
                        expiry = expiry.replace(tzinfo=timezone.utc)
                    if expiry.astimezone(timezone.utc) <= datetime.now(timezone.utc):
                        return None
                except ValueError:
                    return None
            connection.execute("UPDATE access_keys SET last_used_at=? WHERE id=?", (now_iso(), payload["id"]))
            return payload

    def next_series_version(self, series_name: str, connection: sqlite3.Connection | None = None) -> int:
        own_connection = connection is None
        connection = connection or self.connect()
        try:
            row = connection.execute(
                "SELECT COALESCE(MAX(version),0)+1 AS next_version FROM jobs WHERE series_name=?",
                (series_name,),
            ).fetchone()
            return int(row["next_version"])
        finally:
            if own_connection:
                connection.close()

    def create_job(self, payload: dict[str, Any], uploads: list[dict[str, Any]]) -> dict[str, Any]:
        with self.transaction(immediate=True) as connection:
            active = connection.execute(
                """SELECT COUNT(*) AS count FROM jobs
                WHERE access_key_id=? AND status IN (
                    'uploading','queued','starting','running','waiting_agent',
                    'waiting_publishing_agent','waiting_recap_agent','recap_ready','recap_rendering',
                    'cancellation_requested'
                )""",
                (payload["access_key_id"],),
            ).fetchone()["count"]
            maximum = connection.execute(
                "SELECT maximum_active_jobs FROM access_keys WHERE id=?", (payload["access_key_id"],)
            ).fetchone()["maximum_active_jobs"]
            if int(active) >= int(maximum):
                raise RuntimeError(f"该密钥最多允许 {maximum} 个未结束任务")
            version = self.next_series_version(payload["series_name"], connection)
            timestamp = now_iso()
            connection.execute(
                """INSERT INTO jobs(
                    id,access_key_id,series_name,version,status,settings_json,agent_token_hash,
                    work_directory,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    payload["id"], payload["access_key_id"], payload["series_name"], version,
                    "uploading", json.dumps(payload["settings"], ensure_ascii=False),
                    payload["agent_token_hash"], payload["work_directory"], timestamp, timestamp,
                ),
            )
            for position, upload in enumerate(uploads):
                connection.execute(
                    """INSERT INTO uploads(
                        id,job_id,original_name,stored_name,expected_size,expected_sha256,
                        total_chunks,position,role,status,created_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        upload["id"], payload["id"], upload["original_name"], upload["stored_name"],
                        upload["expected_size"], upload.get("expected_sha256"), upload["total_chunks"],
                        position, upload.get("role", "video"), "pending", timestamp,
                    ),
                )
        return self.get_job(payload["id"])

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        payload = dict(row)
        payload["settings"] = json.loads(payload.pop("settings_json"))
        progress_json = payload.pop("progress_json")
        payload["progress"] = json.loads(progress_json) if progress_json else None
        return payload

    def list_jobs(self, access_key_id: str, limit: int = 100) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id FROM jobs WHERE access_key_id=? ORDER BY created_at DESC LIMIT ?",
                (access_key_id, max(1, min(500, int(limit)))),
            ).fetchall()
        return [self.get_job(str(row["id"])) for row in rows]

    def list_all_jobs(self, limit: int = 5000) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?",
                (max(1, min(20000, int(limit))),),
            ).fetchall()
        return [self.get_job(str(row["id"])) for row in rows]

    def pending_upload_bytes(self, access_key_id: str) -> int:
        """Return declared bytes that are not yet represented by completed uploads."""
        with self.connection() as connection:
            row = connection.execute(
                """SELECT COALESCE(SUM(uploads.expected_size), 0) AS total
                FROM uploads
                JOIN jobs ON jobs.id=uploads.job_id
                WHERE jobs.access_key_id=?
                  AND uploads.status!='completed'
                  AND jobs.status IN (
                    'uploading','queued','starting','running','waiting_agent',
                    'waiting_publishing_agent','waiting_recap_agent','recap_ready','recap_rendering',
                    'cancellation_requested'
                  )""",
                (access_key_id,),
            ).fetchone()
        return int(row["total"] if row else 0)

    def project_ownership_records(self) -> list[dict[str, str]]:
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT access_key_id,work_directory,MIN(created_at) AS first_created_at
                FROM jobs
                GROUP BY access_key_id,work_directory
                ORDER BY first_created_at ASC
                """
            ).fetchall()
        return [
            {
                "access_key_id": str(row["access_key_id"]),
                "work_directory": str(row["work_directory"]),
            }
            for row in rows
        ]

    def queue_summary(self) -> dict[str, int]:
        with self.connection() as connection:
            rows = connection.execute("SELECT status,COUNT(*) AS count FROM jobs GROUP BY status").fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        return {
            "waiting": counts.get("queued", 0),
            "active": sum(
                counts.get(value, 0)
                for value in (
                    "starting", "running", "waiting_agent",
                    "waiting_publishing_agent", "waiting_recap_agent", "recap_ready", "recap_rendering",
                )
            ),
            "waiting_recap_agent": counts.get("waiting_recap_agent", 0),
            "waiting_publishing_agent": counts.get("waiting_publishing_agent", 0),
            "recap_ready": counts.get("recap_ready", 0),
            "recap_rendering": counts.get("recap_rendering", 0),
            "uploading": counts.get("uploading", 0),
        }

    def recover_interrupted_jobs(self) -> int:
        """Pause work left behind by an unclean gateway shutdown.

        The same immutable job can then resume from its durable workflow
        checkpoint. It is not auto-started because an orphan encoder may still
        be alive immediately after an abnormal service restart.
        """
        with self.connection() as connection:
            interrupted = connection.execute(
                "SELECT id FROM jobs WHERE status IN ('starting','running','waiting_agent','recap_rendering')"
            ).fetchall()
            for row in interrupted:
                connection.execute(
                    "UPDATE jobs SET status='paused',error=?,completed_at=NULL,updated_at=?,process_pid=NULL WHERE id=?",
                    ("网页服务异常退出；已保留阶段检查点，可手动续做", now_iso(), row["id"]),
                )
            connection.execute(
                "UPDATE jobs SET status='cancelled',cancelled_at=?,updated_at=?,process_pid=NULL WHERE status='cancellation_requested'",
                (now_iso(), now_iso()),
            )
        return len(interrupted)

    def interrupted_processes(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                """SELECT id,process_pid,process_started_at_epoch,process_executable FROM jobs
                WHERE status IN ('starting','running','waiting_agent','recap_rendering')
                AND process_pid IS NOT NULL"""
            ).fetchall()
        return [dict(row) for row in rows]

    def disable_access_key(self, key_id: str) -> None:
        with self.connection() as connection:
            cursor = connection.execute("UPDATE access_keys SET enabled=0 WHERE id=?", (key_id,))
            if cursor.rowcount != 1:
                raise KeyError(key_id)

    def list_access_keys(self) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT id,label,enabled,maximum_active_jobs,expires_at,created_at,last_used_at FROM access_keys ORDER BY created_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def upsert_agent_session(self, access_key_id: str, secret: str, maximum_parallel: int = 3) -> dict[str, Any]:
        timestamp = now_iso()
        with self.transaction(immediate=True) as connection:
            previous = connection.execute(
                "SELECT generation FROM agent_sessions WHERE access_key_id=?", (access_key_id,)
            ).fetchone()
            generation = int(previous["generation"]) + 1 if previous else 1
            connection.execute(
                """INSERT INTO agent_sessions(
                    access_key_id,token_hash,generation,enabled,maximum_parallel,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(access_key_id) DO UPDATE SET
                    token_hash=excluded.token_hash,generation=excluded.generation,enabled=1,
                    maximum_parallel=excluded.maximum_parallel,updated_at=excluded.updated_at,
                    capabilities_json=NULL,capability_probe_nonce=NULL,
                    capabilities_verified_at=NULL,listener_lease_id=NULL,
                    listener_started_at=NULL,idle_started_at=NULL,idle_deadline_at=NULL""",
                (
                    access_key_id, hash_secret(secret), generation, 1,
                    max(1, min(3, int(maximum_parallel))), timestamp, timestamp,
                ),
            )
        return self.get_agent_session(access_key_id)

    def get_agent_session(self, access_key_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM agent_sessions WHERE access_key_id=?", (access_key_id,)
            ).fetchone()
        if row is None:
            raise KeyError(access_key_id)
        return dict(row)

    def authenticate_agent_session(self, access_key_id: str, secret: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """SELECT s.* FROM agent_sessions s
                JOIN access_keys k ON k.id=s.access_key_id
                WHERE s.access_key_id=? AND s.token_hash=? AND s.enabled=1 AND k.enabled=1""",
                (access_key_id, hash_secret(secret)),
            ).fetchone()
        return dict(row) if row else None

    def touch_agent_session(self, access_key_id: str) -> None:
        timestamp = now_iso()
        with self.connection() as connection:
            connection.execute(
                "UPDATE agent_sessions SET last_heartbeat_at=?,updated_at=? WHERE access_key_id=? AND enabled=1",
                (timestamp, timestamp, access_key_id),
            )

    def create_agent_capability_probe(self, access_key_id: str) -> dict[str, Any]:
        nonce = secrets.token_urlsafe(24)
        timestamp = now_iso()
        with self.connection() as connection:
            cursor = connection.execute(
                """UPDATE agent_sessions
                SET capability_probe_nonce=?,capabilities_json=NULL,
                    capabilities_verified_at=NULL,updated_at=?
                WHERE access_key_id=? AND enabled=1""",
                (nonce, timestamp, access_key_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(access_key_id)
        return {
            "probe_nonce": nonce,
            "instruction": (
                "Create one context-isolated child Agent with no inherited turns. "
                "Pass it this nonce and ask it to return the nonce unchanged plus its role name. "
                "Submit native_subagents=true, context_isolation=true, max_child_agents, "
                "probe_role, the real child_agent_run_id, the isolated_context_id, and "
                "probe_result formatted exactly as '<nonce> <role name>'."
            ),
        }

    def verify_agent_capabilities(
        self,
        access_key_id: str,
        nonce: str,
        capabilities: dict[str, Any],
    ) -> dict[str, Any]:
        normalized = {
            "native_subagents": bool(capabilities.get("native_subagents")),
            "context_isolation": bool(capabilities.get("context_isolation")),
            "max_child_agents": max(
                0, min(3, int(capabilities.get("max_child_agents") or 0))
            ),
            "probe_role": str(capabilities.get("probe_role") or "").strip()[:80],
            "child_agent_run_id": str(capabilities.get("child_agent_run_id") or "").strip()[:128],
            "isolated_context_id": str(capabilities.get("isolated_context_id") or "").strip()[:128],
            "probe_result": str(capabilities.get("probe_result") or "").strip()[:512],
        }
        if not normalized["native_subagents"] or not normalized["context_isolation"]:
            raise ValueError("当前 Agent 未证明支持原生子 Agent 和隔离上下文")
        if normalized["max_child_agents"] < 1:
            raise ValueError("当前 Agent 没有可用的子 Agent 并发槽")
        if len(normalized["child_agent_run_id"]) < 6 or len(normalized["isolated_context_id"]) < 6:
            raise ValueError("Capability proof is missing a child run id or isolated context id")
        timestamp = now_iso()
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                """SELECT capability_probe_nonce FROM agent_sessions
                WHERE access_key_id=? AND enabled=1""",
                (access_key_id,),
            ).fetchone()
            if row is None:
                raise KeyError(access_key_id)
            expected = str(row["capability_probe_nonce"] or "")
            if not expected or not secrets.compare_digest(expected, str(nonce or "")):
                raise ValueError("子 Agent 能力探针 nonce 无效或已过期")
            expected_result = f"{expected} {normalized['probe_role']}"
            if not normalized["probe_role"] or not secrets.compare_digest(
                normalized["probe_result"], expected_result
            ):
                raise ValueError("Child Agent probe result does not match the server challenge")
            connection.execute(
                """UPDATE agent_sessions
                SET capabilities_json=?,capabilities_verified_at=?,
                    capability_probe_nonce=NULL,updated_at=?
                WHERE access_key_id=?""",
                (
                    json.dumps(normalized, ensure_ascii=False),
                    timestamp,
                    timestamp,
                    access_key_id,
                ),
            )
        return self.get_agent_session(access_key_id)

    def start_agent_listener_lease(
        self,
        access_key_id: str,
        idle_timeout_seconds: int,
    ) -> dict[str, Any]:
        timestamp = datetime.now(timezone.utc).astimezone()
        lease_id = secrets.token_urlsafe(18)
        deadline = timestamp + timedelta(seconds=max(60, int(idle_timeout_seconds)))
        with self.connection() as connection:
            cursor = connection.execute(
                """UPDATE agent_sessions
                SET listener_lease_id=?,listener_started_at=?,idle_started_at=?,
                    idle_deadline_at=?,last_heartbeat_at=?,updated_at=?
                WHERE access_key_id=? AND enabled=1""",
                (
                    lease_id,
                    timestamp.isoformat(timespec="seconds"),
                    timestamp.isoformat(timespec="seconds"),
                    deadline.isoformat(timespec="seconds"),
                    timestamp.isoformat(timespec="seconds"),
                    timestamp.isoformat(timespec="seconds"),
                    access_key_id,
                ),
            )
            if cursor.rowcount != 1:
                raise KeyError(access_key_id)
        return self.get_agent_session(access_key_id)

    def ensure_agent_listener_lease(
        self,
        access_key_id: str,
        idle_timeout_seconds: int,
    ) -> dict[str, Any]:
        record = self.get_agent_session(access_key_id)
        if not record.get("listener_lease_id"):
            return self.start_agent_listener_lease(access_key_id, idle_timeout_seconds)
        return record

    def mark_agent_session_work(self, access_key_id: str) -> dict[str, Any]:
        timestamp = now_iso()
        with self.connection() as connection:
            connection.execute(
                """UPDATE agent_sessions
                SET idle_started_at=NULL,idle_deadline_at=NULL,
                    last_heartbeat_at=?,updated_at=?
                WHERE access_key_id=? AND enabled=1""",
                (timestamp, timestamp, access_key_id),
            )
        return self.get_agent_session(access_key_id)

    def start_agent_idle_window(
        self,
        access_key_id: str,
        idle_timeout_seconds: int,
    ) -> dict[str, Any]:
        record = self.ensure_agent_listener_lease(
            access_key_id, idle_timeout_seconds
        )
        if record.get("idle_started_at") and record.get("idle_deadline_at"):
            return record
        timestamp = datetime.now(timezone.utc).astimezone()
        deadline = timestamp + timedelta(seconds=max(60, int(idle_timeout_seconds)))
        with self.connection() as connection:
            connection.execute(
                """UPDATE agent_sessions
                SET idle_started_at=?,idle_deadline_at=?,updated_at=?
                WHERE access_key_id=? AND enabled=1""",
                (
                    timestamp.isoformat(timespec="seconds"),
                    deadline.isoformat(timespec="seconds"),
                    timestamp.isoformat(timespec="seconds"),
                    access_key_id,
                ),
            )
        return self.get_agent_session(access_key_id)

    def disable_agent_session(self, access_key_id: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE agent_sessions SET enabled=0,updated_at=? WHERE access_key_id=?",
                (now_iso(), access_key_id),
            )

    def owned_job(self, job_id: str, access_key_id: str) -> dict[str, Any] | None:
        try:
            job = self.get_job(job_id)
        except KeyError:
            return None
        return job if job["access_key_id"] == access_key_id else None

    def authenticate_agent(self, job_id: str, secret: str) -> dict[str, Any] | None:
        try:
            job = self.get_job(job_id)
        except KeyError:
            return None
        return job if secrets.compare_digest(job["agent_token_hash"], hash_secret(secret)) else None

    def list_uploads(self, job_id: str) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute("SELECT * FROM uploads WHERE job_id=? ORDER BY position,id", (job_id,)).fetchall()
        return [dict(row) for row in rows]

    def get_upload(self, upload_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM uploads WHERE id=?", (upload_id,)).fetchone()
        if row is None:
            raise KeyError(upload_id)
        return dict(row)

    def record_chunk(self, upload_id: str, index: int, byte_size: int, sha256: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO upload_chunks(upload_id,chunk_index,byte_size,sha256,created_at) VALUES(?,?,?,?,?)",
                (upload_id, int(index), int(byte_size), sha256, now_iso()),
            )

    def chunk_indexes(self, upload_id: str) -> list[int]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT chunk_index FROM upload_chunks WHERE upload_id=? ORDER BY chunk_index", (upload_id,)
            ).fetchall()
        return [int(row["chunk_index"]) for row in rows]

    def complete_upload(self, upload_id: str, byte_size: int, sha256: str) -> None:
        with self.connection() as connection:
            connection.execute(
                "UPDATE uploads SET status='completed',assembled_size=?,assembled_sha256=?,completed_at=? WHERE id=?",
                (int(byte_size), sha256, now_iso(), upload_id),
            )

    def set_job_status(self, job_id: str, status: str, **values: Any) -> None:
        allowed = {
            "queue_position", "process_pid", "process_started_at_epoch",
            "process_executable", "progress_json", "error", "started_at",
            "completed_at", "cancelled_at",
        }
        updates = {key: value for key, value in values.items() if key in allowed}
        updates["status"] = status
        timestamp = now_iso()
        updates["updated_at"] = timestamp
        fields = ",".join(f"{key}=?" for key in updates)
        with self.connection() as connection:
            previous = connection.execute(
                "SELECT access_key_id,series_name,status FROM jobs WHERE id=?", (job_id,)
            ).fetchone()
            connection.execute(f"UPDATE jobs SET {fields} WHERE id=?", (*updates.values(), job_id))
            if previous is not None and previous["status"] != status and status in {
                "recap_ready", "completed", "failed", "cancelled",
            }:
                labels = {
                    "recap_ready": (
                        "RECAP_PLAN_READY", "解说方案已完成",
                        "解说时间轴已经通过校验，请在网页端预览并生成最终成片。",
                    ),
                    "completed": (
                        "JOB_COMPLETED", "任务已全部完成",
                        "最终产物已经生成并发布，请在网页端检查或保存。",
                    ),
                    "failed": (
                        "JOB_FAILED", "任务执行失败",
                        str(values.get("error") or "任务失败，请在网页端查看日志。"),
                    ),
                    "cancelled": (
                        "JOB_CANCELLED", "任务已取消",
                        "任务已停止，未完成的阶段不会继续运行。",
                    ),
                }
                kind, title, message = labels[status]
                payload = {
                    "series_name": previous["series_name"],
                    "previous_status": previous["status"],
                    "status": status,
                    "error": values.get("error"),
                }
                connection.execute(
                    """INSERT INTO agent_notifications(
                    access_key_id,job_id,kind,status,title,message,data_json,created_at
                    ) VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        previous["access_key_id"], job_id, kind, status, title, message,
                        json.dumps(payload, ensure_ascii=False), timestamp,
                    ),
                )

    def next_agent_notification(self, access_key_id: str) -> dict[str, Any] | None:
        with self.connection() as connection:
            row = connection.execute(
                """SELECT * FROM agent_notifications
                WHERE access_key_id=? AND acknowledged_at IS NULL
                ORDER BY id LIMIT 1""",
                (access_key_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["data"] = json.loads(result.pop("data_json")) if result.get("data_json") else None
        return result

    def acknowledge_agent_notification(self, access_key_id: str, notification_id: int) -> dict[str, Any]:
        timestamp = now_iso()
        with self.connection() as connection:
            row = connection.execute(
                "SELECT * FROM agent_notifications WHERE id=? AND access_key_id=?",
                (int(notification_id), access_key_id),
            ).fetchone()
            if row is None:
                raise KeyError(notification_id)
            connection.execute(
                """UPDATE agent_notifications SET acknowledged_at=COALESCE(acknowledged_at,?)
                WHERE id=? AND access_key_id=?""",
                (timestamp, int(notification_id), access_key_id),
            )
        result = dict(row)
        result["acknowledged_at"] = result.get("acknowledged_at") or timestamp
        result["data"] = json.loads(result.pop("data_json")) if result.get("data_json") else None
        return result

    def queue_job(self, job_id: str) -> int:
        with self.transaction(immediate=True) as connection:
            uploads = connection.execute("SELECT status FROM uploads WHERE job_id=?", (job_id,)).fetchall()
            if not uploads or any(row["status"] != "completed" for row in uploads):
                raise RuntimeError("所有视频上传完成后才能加入队列")
            job = connection.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None or job["status"] != "uploading":
                raise RuntimeError("任务当前状态不能加入队列")
            ahead = connection.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE status IN ('queued','starting','running','waiting_agent')"
            ).fetchone()["count"]
            connection.execute(
                "UPDATE jobs SET status='queued',queue_position=?,updated_at=? WHERE id=?",
                (int(ahead) + 1, now_iso(), job_id),
            )
            return int(ahead) + 1

    def resume_job(self, job_id: str) -> int:
        """Requeue a stopped job without discarding uploads or checkpoints."""
        with self.transaction(immediate=True) as connection:
            uploads = connection.execute("SELECT status FROM uploads WHERE job_id=?", (job_id,)).fetchall()
            if not uploads or any(row["status"] != "completed" for row in uploads):
                raise RuntimeError("上传尚未完整，不能断点续做")
            row = connection.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            if row["status"] not in {"failed", "cancelled", "paused"}:
                raise RuntimeError(f"任务状态 {row['status']} 不能断点续做")
            ahead = connection.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE status IN ('queued','starting','running','waiting_agent')"
            ).fetchone()["count"]
            connection.execute(
                """UPDATE jobs SET status='queued',queue_position=?,process_pid=NULL,error=NULL,
                completed_at=NULL,cancelled_at=NULL,updated_at=? WHERE id=?""",
                (int(ahead) + 1, now_iso(), job_id),
            )
            return int(ahead) + 1

    def claim_next_job(self) -> dict[str, Any] | None:
        with self.transaction(immediate=True) as connection:
            active = connection.execute(
                "SELECT COUNT(*) AS count FROM jobs WHERE status IN ('starting','running','waiting_agent')"
            ).fetchone()["count"]
            if int(active) >= 1:
                return None
            row = connection.execute(
                "SELECT id FROM jobs WHERE status='queued' ORDER BY created_at,id LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE jobs SET status='starting',started_at=?,updated_at=? WHERE id=? AND status='queued'",
                (now_iso(), now_iso(), row["id"]),
            )
            connection.execute(
                "UPDATE jobs SET queue_position=(SELECT COUNT(*) FROM jobs j2 WHERE j2.status='queued' AND (j2.created_at<jobs.created_at OR (j2.created_at=jobs.created_at AND j2.id<=jobs.id))) WHERE status='queued'"
            )
        return self.get_job(row["id"])

    def add_event(self, job_id: str, message: str, level: str = "info", data: Any = None) -> None:
        with self.connection() as connection:
            connection.execute(
                "INSERT INTO job_events(job_id,level,message,data_json,created_at) VALUES(?,?,?,?,?)",
                (job_id, level, message, json.dumps(data, ensure_ascii=False) if data is not None else None, now_iso()),
            )

    def events(self, job_id: str, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM job_events WHERE job_id=? AND id>? ORDER BY id LIMIT ?",
                (job_id, max(0, int(after)), max(1, min(2000, int(limit)))),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["data"] = json.loads(item.pop("data_json")) if item.get("data_json") else None
            result.append(item)
        return result
