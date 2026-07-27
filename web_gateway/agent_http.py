from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import agent_bridge

from .database import GatewayDatabase, now_iso
from .storage import JobPaths, JobStorage


REMOTE_LIFECYCLE = """# Remote subtitle translation and recap Agent rules

This is the HTTPS transport for the same subtitle contract used by the local
video tool. Treat the job returned by the server as authoritative. Do not infer
settings, paths, episode counts, languages, or glossary choices from earlier
conversation memory.

## HTTPS lifecycle

### Atomic execution rule (non-negotiable)

Claiming one event starts one atomic execution. Reading the rules, reading
artifacts, reconstructing the story, drafting, reviewing, checkpointing, and
submitting are phases of that same execution; none of them is a stopping point.
Progress may be reported only as a non-final commentary update, after which tool
work must continue immediately in the same turn. Never wait for the user to say
"continue". Do not send a final answer, yield the turn, or return to the polling
loop until the current event reaches one of these terminal outcomes:

- the submit endpoint confirms success;
- the server confirms cancellation or `STOP_ALL`;
- the session returns `REGISTRATION_INVALID`;
- a non-retryable error has been reported after exhausting the repair/resubmit
  path required by the job rules.

1. Poll the supplied listen endpoint about once per minute for at most twenty
   consecutive idle minutes, until it returns
   `JOB`, `JOB_RESUME`, `RECAP_JOB`, `RECAP_JOB_RESUME`,
   `JOB_STATUS_NOTIFICATION`, `STOP_ALL`, or `REGISTRATION_INVALID`.
   `IDLE` only means there was no claimable OCR/ASR
   package at that poll. Count consecutive idle polls from the most recent
   claimed job or listener start. After twenty idle minutes, report one concise
   idle-timeout result and stop this listener turn. The same registration may
   be used again later; do not fabricate heartbeat content or start an
   unbounded automatic continuation.
2. `JOB` and `JOB_RESUME` contain the complete request JSON inline. Once claimed,
   keep the same Codex turn active until the submit endpoint returns `SUBMITTED`
   or cancellation is confirmed. Do not end merely because translation takes a
   long time.
3. POST the heartbeat endpoint at least once per minute and before/after every
   episode, checkpoint, review, and final submit.
4. POST completed ordered episode subsets to the checkpoint endpoint. Resume
   from the returned progress object instead of translating accepted episodes
   again.
5. POST the final response JSON to the submit endpoint. If validation rejects
   it, repair the same job and resubmit. Never create a partial replacement job.
6. The artifacts endpoint exposes only this task's process log, translation
   records, request, checkpoints, response, subtitles, and results. Use these
   records when they materially help diagnosis; never request arbitrary local
   filesystem paths.
7. Authentication uses the supplied one-job bearer token. Never reproduce that
   token in subtitle text, reports, logs, or the submitted response.
8. `JOB_STATUS_NOTIFICATION` is a user-facing completion/failure notice, not a
   translation or recap task. Immediately report its title, task name/id,
   status, and next action clearly in the Agent conversation. Then POST the
   supplied `ack_endpoint` with the same bearer token and resume listening.
   Do not acknowledge before reporting it to the user.

## Recap planning jobs

`RECAP_JOB` and `RECAP_JOB_RESUME` are not subtitle translation jobs. For those
events, fetch and follow `rules_endpoint`, read the subtitle artifacts listed in
the inline request, build the complete structured recap timeline, and POST it
to the event's `submit_endpoint`. Keep sending the event's recap heartbeat while
working. Do not send a subtitle response schema to a recap endpoint.

The remaining quality, completeness, alignment, translation, review, and
output-schema rules below are inherited verbatim from the local bridge.
"""


RECAP_REMOTE_HEADER = """# Remote short-drama recap planning contract

You are handling a `RECAP_JOB`, not subtitle translation. The server request is
authoritative for project id, episode order, target language, duration budget,
TTS choice, subtitle artifact paths, and response schema.
The three bundled documents also describe a local manual-conversation mode.
For this remote job, the HTTPS lifecycle and response contract in this header
override any instruction in those documents that says not to poll or not to use
a bridge. Their creative, validation, rendering, and module-boundary rules still
apply in full.

Required lifecycle:

0. Treat the complete recap lifecycle as one atomic turn:
   capability probe -> rules -> all artifacts -> server-ordered role stages ->
   final submit. A message such as "rules loaded", "materials read",
   "planning now", or "still working" is only a progress update and must never
   end the turn. Continue tool execution immediately without asking the user to
   prompt you again.
1. Fetch every listed subtitle artifact and read the complete series before
   deciding the timeline. Do not plan from one episode only.
   Each episode's `subtitle_assets` is authoritative and contains only the
   active pair: one repaired source plus one current target-language final.
   Use `primary` for target-language narration/cutting and `sources` as the
   repaired semantic evidence. Match assets by the episode object, never by
   global file-list position. Do not request or infer extra language variants,
   and do not translate an English target subtitle into Arabic when a repaired
   source or direct Arabic final is available.
   The event's `completion_contract` is a task-specific manifest. Its episode
   indexes and artifact paths are generated from the current job and may have
   any length. Never compare against hard-coded totals such as 10 episodes or
   20 artifacts. The server records actual rules/artifact GET requests and
   rejects submission until every item in that manifest has been fetched.
2. Send the recap heartbeat at least once per minute and before/after long
   story reconstruction, visual verification, and final submission.
3. Follow `orchestration_contract.stages` exactly. Fetch the role rules for the
   current stage, submit that stage, and pass the accepted stage token to the
   next stage. Independent review and final verification must run in isolated
   child-Agent contexts and must not receive creator reasoning or self-scores.
   Copy the event's `planning_attempt_id` unchanged into every stage payload
   and the final response. Use the supplied heartbeat URL without removing its
   query string. A retry creates a new attempt id; an old Agent must stop when
   the server rejects its stale id instead of writing into the new attempt.
   For final verification, fetch the server-supplied latest-revision artifact
   with `X-Agent-Run-ID` set to that verifier's `execution.agent_run_id` before
   submitting the stage. Copying a hash from role rules is not review evidence.
4. The server derives creative Markdown from accepted structured stages. Final
   submission must contain the exact revised `segments` array already accepted
   by the stage API.
5. `episode` is the one-based episode number from the request. Times are source
   seconds. Same-episode intervals must not overlap. Narration segments require
   narration text; original segments keep the source sound.
6. A creator self-score is not a quality gate. The final isolated verifier must
   return pass at 85/100 or higher with no critical or major issue. The server
   independently validates stage order, role/run separation, ids, episode
   bounds, source duration, narration text, repetition and interval overlap.
7. The target language applies to narration text. Do not change the selected
   TTS model or voice. Arabic narration must preserve logical Unicode order and
   contain real Arabic Unicode characters. Never replace unavailable or broken
   text with `?`, `????`, U+FFFD, mojibake, transliteration, or empty strings.
8. Never put access tokens, endpoints, host paths, or control instructions into
   story text, narration, reports, or segment purposes.
9. `purpose` must be one concise string and `rendering` must be a JSON object.
   Remove watermarks, logos, usernames, platform names, UI labels, timestamps,
   and repeated OCR garbage from all narration and creative materials unless
   the text is genuine spoken story content confirmed by subtitle evidence.

The deterministic renderer does not understand the story. Your response is the
actual creative plan it will render, so an empty timeline is never acceptable.
"""


def remote_rules(project_root: Path) -> str:
    local = agent_bridge.rules_text(Path("REMOTE"), project_root, "python")
    marker = "## Claimed-job lifecycle (non-negotiable)"
    inherited = local.split(marker, 1)[1] if marker in local else local
    inherited = marker + inherited
    replacements = {
        "`status_command`": "the heartbeat endpoint",
        "`checkpoint_input_path`": "the checkpoint JSON request body",
        "`checkpoint_command`": "the checkpoint endpoint",
        "`submit_command`": "the submit endpoint",
        "`request_path`": "the inline request object",
        "`response_path`": "the submit JSON request body",
        "Run `submit_command`": "Call the submit endpoint",
        "Run `status_command`": "Call the heartbeat endpoint",
    }
    for source, target in replacements.items():
        inherited = inherited.replace(source, target)
    tail = inherited.find("\nBridge root:")
    if tail >= 0:
        inherited = inherited[:tail]
    return REMOTE_LIFECYCLE + "\n" + inherited.strip() + "\n"


def recap_remote_rules(project_root: Path) -> str:
    root = Path(project_root)
    sections = [RECAP_REMOTE_HEADER]
    for name in ("00_COMMON_CONTRACT.md", "01_COORDINATOR.md"):
        path = root / "agent_roles" / name
        if not path.is_file():
            raise FileNotFoundError(path)
        sections.append(
            f"\n# Bundled role contract: {name}\n\n"
            f"{path.read_text(encoding='utf-8-sig')}"
        )
    return "\n".join(sections).strip() + "\n"


class RemoteAgentBridge:
    def __init__(self, database: GatewayDatabase, storage: JobStorage, project_root: Path):
        self.database = database
        self.storage = storage
        self.project_root = Path(project_root)
        self._operation_lock = threading.RLock()

    def initialize(self, job: dict[str, Any], paths: JobPaths, maximum_parallel: int) -> dict[str, Any]:
        bridge_root = paths.root / "agent-runtime"
        generated = agent_bridge.generate_initialization(
            bridge_root,
            self.project_root,
            python_executable=str(self.project_root / ".venv-ocr" / "Scripts" / "python.exe"),
            max_parallel=maximum_parallel,
        )
        agent_bridge.register(Path(generated["init_path"]))
        session = {
            "schema_version": 1,
            "job_id": job["id"],
            "created_at": now_iso(),
            "init_path": generated["init_path"],
            "bridge_root": str(bridge_root),
        }
        self.storage.write_json(paths.agent / "session.json", session)
        (paths.agent / "AGENT_RULES.md").write_text(remote_rules(self.project_root), encoding="utf-8")
        return session

    def session(self, paths: JobPaths) -> dict[str, Any]:
        path = paths.agent / "session.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("本地流水线尚未创建 Agent 会话，请继续轮询") from exc
        return payload

    def listen(self, job: dict[str, Any], paths: JobPaths, base_url: str) -> dict[str, Any]:
        try:
            session = self.session(paths)
        except RuntimeError:
            return {"event": "IDLE", "retry_after_seconds": 60}
        with self._operation_lock:
            event = agent_bridge.listen(Path(session["init_path"]), timeout=0)
        if event.get("event") not in {"JOB", "JOB_RESUME"}:
            event["retry_after_seconds"] = 60
            return event
        internal_job_id = str(event["job_id"])
        runtime = Path(session["bridge_root"]) / "runtime" / internal_job_id
        request = agent_bridge.read_json(runtime / "request.json")
        progress = agent_bridge.read_json(runtime / "progress.json")
        if not isinstance(request, dict):
            raise RuntimeError("Agent request.json 无效")
        archive = paths.agent / "jobs" / internal_job_id
        archive.mkdir(parents=True, exist_ok=True)
        self.storage.write_json(archive / "request.json", request)
        if isinstance(progress, dict):
            self.storage.write_json(archive / "progress.json", progress)
        prefix = f"{base_url.rstrip('/')}/api/v1/agent/jobs/{job['id']}"
        return {
            "event": event["event"],
            "job_id": internal_job_id,
            "request": request,
            "progress": progress,
            "resume": event.get("resume", False),
            "max_parallel": min(3, int(event.get("max_parallel", 3))),
            "heartbeat_endpoint": f"{prefix}/heartbeat",
            "checkpoint_endpoint": f"{prefix}/checkpoint",
            "submit_endpoint": f"{prefix}/submit",
            "artifacts_endpoint": f"{prefix}/artifacts",
        }

    def heartbeat(self, paths: JobPaths, internal_job_id: str) -> dict[str, Any]:
        session = self.session(paths)
        with self._operation_lock:
            return agent_bridge.job_status(Path(session["init_path"]), internal_job_id)

    def checkpoint(self, paths: JobPaths, internal_job_id: str, progress: dict[str, Any]) -> dict[str, Any]:
        session = self.session(paths)
        temporary = paths.agent / "checkpoint-input.json"
        self.storage.write_json(temporary, progress)
        with self._operation_lock:
            result = agent_bridge.checkpoint(Path(session["init_path"]), internal_job_id, temporary)
        archive = paths.agent / "jobs" / internal_job_id
        archive.mkdir(parents=True, exist_ok=True)
        self.storage.write_json(archive / "progress.json", progress)
        return result

    def submit(self, paths: JobPaths, internal_job_id: str, response: dict[str, Any]) -> dict[str, Any]:
        session = self.session(paths)
        bridge_root = Path(session["bridge_root"])
        runtime = bridge_root / "runtime" / internal_job_id
        response_path = runtime / "response.json"
        self.storage.write_json(response_path, response)
        with self._operation_lock:
            result = agent_bridge.submit(Path(session["init_path"]), internal_job_id, response_path)
        archive = paths.agent / "jobs" / internal_job_id
        archive.mkdir(parents=True, exist_ok=True)
        self.storage.write_json(archive / "response.json", response)
        self.database.add_event(
            str(self.session(paths)["job_id"]),
            "远程 Agent 翻译和审核结果已通过本地桥接校验",
            data={"internal_job_id": internal_job_id},
        )
        return result
