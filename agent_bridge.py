#!/usr/bin/env python3
"""File based bridge between the desktop video tool and one Codex conversation.

The bridge deliberately contains no credentials and does not try to wake a
finished Codex turn.  A registered conversation polls ``listen`` while it is
active, claims folder jobs, and submits one consolidated response per job.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


PROTOCOL_VERSION = 6
MIN_AGENT_QUALITY_SCORE = 8.5
ADVANCED_AGENT_QUALITY_SCORE = 9.5
EPISODE_QUALITY_DIMENSIONS = (
    "meaning_accuracy",
    "target_language_fluency",
    "ocr_asr_noise_cleanup",
    "context_consistency",
    "watermark_ui_removal",
)
SERIES_QUALITY_DIMENSIONS = (
    "entity_consistency",
    "terminology_consistency",
    "cross_episode_context",
    "artifact_recheck",
    "output_completeness",
)
# Polling once per minute is sufficient for a human-operated desktop workflow.
# Keep the registration usable while the user spends time selecting material
# and adjusting settings, without requiring a continuously busy loop.
LISTEN_POLL_SECONDS = 55.0
# One local listen invocation may remain idle for at most twenty minutes.  A
# claimed job still has no processing deadline; this limit exists only to keep
# an empty listener from growing one Codex conversation forever.  A zero
# timeout remains available to HTTP adapters and tests that need one
# non-blocking poll.
IDLE_LISTEN_TIMEOUT_SECONDS = 20 * 60
HEARTBEAT_FRESH_SECONDS = 3 * 60
CLAIM_RESUME_AFTER_SECONDS = 2 * 60


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return default


def ensure_layout(root: Path) -> Path:
    root = Path(root).resolve()
    for name in ("control", "registrations", "runtime"):
        (root / name).mkdir(parents=True, exist_ok=True)
    state_path = root / "control" / "state.json"
    if not state_path.exists():
        atomic_write_json(
            state_path,
            {
                "protocol_version": PROTOCOL_VERSION,
                "generation": 0,
                "cancel_epoch": 0,
                "agent_enabled": False,
                "updated_at": now_iso(),
            },
        )
    return root


def state(root: Path) -> dict[str, Any]:
    root = ensure_layout(root)
    return read_json(root / "control" / "state.json", {})


def _registration_path(root: Path, registration_id: str) -> Path:
    return root / "registrations" / f"agent-init-{registration_id}.json"


def console_python_executable(value: str) -> str:
    """Use python.exe for bridge CLI output even when GUI runs as pythonw.exe."""
    path = Path(value)
    if path.stem.casefold() == "pythonw":
        suffix = path.suffix or (".exe" if os.name == "nt" else "")
        candidate = path.with_name("python" + suffix)
        if candidate.exists():
            return str(candidate)
    return str(value)


def rules_text(root: Path, project_root: Path, python_executable: str) -> str:
    bridge_script = project_root / "agent_bridge.py"
    return f"""# Local subtitle translation Agent rules

You are the coordinator for the local video subtitle bridge. The rules below
apply only while the registration generation remains active.

## Registration and polling

1. Run the registration command from the initialization JSON once.
2. Run its `listen_command`. One invocation checks for work about once per
   minute and returns `IDLE` after twenty consecutive idle minutes. `IDLE`
   ends this idle listening session cleanly; do not immediately create another
   automatic turn. The user may restart listening with the same still-valid
   registration when new work is expected.
   A claimed job, `STOP_ALL`, or `REGISTRATION_INVALID` returns immediately.
   `JOB_RESUME` means this same registration previously claimed a job but its
   work heartbeat stopped; read its `progress_path` and resume that exact job.
   When invoking it through a shell tool, if the tool yields a still-running
   process/cell, resume that same process until it returns an event; do not
   mistake a tool yield for `IDLE`.
3. `STOP_ALL` means cancel current child work at the next safe checkpoint,
   submit no late result, report the cancellation, and then resume listening.
4. `REGISTRATION_INVALID` means stop listening completely. A newer conversation
   has replaced this one or the user explicitly stopped Agent listening.

## Claimed-job lifecycle (non-negotiable)

- Once `JOB` or `JOB_RESUME` is returned, the task has no 20-minute processing
  deadline. Continue until the bridge returns `SUBMITTED`, cancellation is
  confirmed, or registration becomes invalid.
- Never send a final answer, yield the turn, or merely say that work is still
  running while a claimed job is incomplete. Progress messages are commentary
  only and must be followed immediately by continued tool work in the same
  turn. Waiting for delegated children is part of the active turn.
- Run `status_command` at least once per minute while working, as well as before
  and after every episode, review, checkpoint, and final write. This refreshes
  the work lease and checks cancellation.
- After each completed episode, write all completed episodes to the supplied
  `checkpoint_input_path` and run `checkpoint_command`. Checkpoints must contain
  the exact final episode subtitles and episode quality review. On `JOB_RESUME`,
  load the accepted `progress_path`, reuse completed episodes, and continue the
  missing episodes instead of restarting or pretending background work exists.

## Folder-job contract

- One job represents one input folder/series, possibly containing many videos.
- Read the request JSON named by `request_path`. Never infer paths or settings
  from earlier conversation memory.
- Treat `expected_episode_indexes` and each episode's
  `expected_subtitle_indexes` as a mandatory completion checklist. Create a
  result map keyed by episode and subtitle index before doing any translation.
- Every episode contains locally aligned `items` and a
  `translation_contract_id`. Resolve that ID in the parent request's
  `translation_contracts`; its prompt is shared with API mode and is
  authoritative. Follow it completely. Do not realign raw OCR/ASR yourself and
  do not change start/end timestamps. Contracts are stored once per source
  type to avoid wasting context by repeating the same long prompt per episode.
- Every episode also contains a `source_output` contract. Reconstruct one
  repaired source-language subtitle for every aligned item and return it in
  `source_subtitles`. This is not a second target translation: it is the best
  source-language reading supported by OCR/soft-subtitle, ASR, neighboring
  context, and series context. Repair recognition errors and remove
  watermark/UI/noise, but do not route it through the requested target
  translation. Preserve its indexes and timestamps exactly.
- Respect `target_language`, `localization_strategy`, selected glossary,
  episode order, source types, and the meaning of continuous neighboring context. The glossary is guidance
  for matching terms, not permission to invent absent entities.
- `translation_quality` is either `fast` or `advanced`. Fast mode keeps the
  existing translation, episode self-review, and whole-series self-review flow
  with an 8.5 gate. It must still discover and return a compact
  `series_entities` table, but the table is not authoritative input during that
  same fast job.
- Advanced mode is a real three-stage workflow, not three copies of the same
  translation: (1) create a reliable draft, (2) perform native-localization
  refinement using the draft, source evidence, `series_entity_table`, and
  read-only `existing_final_subtitles`, then (3) use a fresh independent review
  context/worker for the final whole-series audit. The final reviewer must see
  source evidence and both draft/refined text, must not trust earlier scores,
  and may return indexed corrections. Apply corrections and repeat the
  refinement/final-review loop at most `maximum_revision_cycles` times.
- `existing_final_subtitles` are read-only continuity evidence. Never return or
  overwrite their episodes unless those episodes are also present in the
  current request's `expected_episode_indexes`.

## Translation and review requirements (same contract as API mode)

### Quality floor and prohibited shortcuts

- These rules and each job's authoritative translation contract are a minimum
  quality floor, not optional guidance. You may make the review more careful
  and context-aware, but never replace it with a coarser approximation.
- Scripts may be used only to parse files, preserve indexes/timestamps,
  validate completeness, and assemble the final JSON. Do not use regex-only
  classifiers, blanket source-selection rules, third-party/web translators,
  browser translation, or unrequested translation APIs to generate or approve
  subtitle text. Agent mode exists specifically to perform evidence-aware Agent
  translation and review without silently substituting another service.
- Every subtitle requires a semantic decision using its visual evidence, ASR
  evidence, neighboring dialogue, and series context. Rules such as "ASR is
  present, therefore copy ASR" or "ASR is empty, therefore keep OCR" are
  forbidden. Empty ASR is absence of evidence, not proof that OCR is dialogue.
- Delegation does not lower the standard. Give every child the complete
  authoritative contract and episode context. The coordinator must review the
  returned episode semantically before accepting it; structural completeness
  alone is not a review.

### Watermarks, overlays, and non-dialogue text

- Never translate, transliterate, preserve, or reconstruct platform branding,
  watermarks, logos, usernames, URLs, email addresses, player controls, UI
  labels, or repeated overlay artifacts. This explicitly includes variants and
  OCR corruption of names such as `ReelShort`, `ReelShorl`, `ReelShor`,
  `RealShort`, isolated branding letters such as `R`, and repeated mixtures of
  those fragments.
- Use combined evidence rather than one keyword alone. Strong overlay evidence
  includes repetition at unrelated times, multiple near-identical brand/logo
  variants, empty or unrelated ASR, and text unrelated to neighboring plot or
  dialogue. Genuine story-relevant phone messages, signs, titles, and onscreen
  narrative text may still be translated when context supports them.
- If an item contains only watermark/UI/overlay noise, keep its stable index
  and timestamps but return an empty `text`. If real dialogue and overlay text
  are mixed, remove only the overlay fragments and translate the supported
  dialogue. Never output an overlay in Latin script or phonetic target-language
  spelling merely to avoid returning an empty string.
- During the episode and whole-series reviews, search the actual final subtitle
  texts for recurring overlay variants and unsupported OCR-only fragments.
  Claims in `review` or `series_review` must match real edits in the submitted
  subtitles; never report that artifacts were removed when they remain.

- Each aligned item may contain visual OCR/soft-subtitle evidence and audio ASR
  evidence. Visual timing defines the subtitle boundary; ASR is supporting
  evidence used to repair OCR corruption. Never move neighboring dialogue into
  the current index or concatenate alternative readings.
- OCR may contain broken word order, duplicate snapshots, lone digits, symbols,
  UI text and stray fragments. ASR may contain homophones, punctuation and
  segmentation errors. Correct only errors supported by the other evidence or
  surrounding context. An empty source means that source missed the line.
- Translate the intended contextual meaning naturally and concisely into only
  the requested target language. For English, explicitly recognize idioms,
  phrasal verbs, slang, euphemisms, sarcasm and figurative expressions; never
  preserve irrelevant literal imagery or English word order.
- Preserve plot facts, speaker intent, emotion and tone. Do not invent dialogue,
  names, relationships, events or explanations. Do not add timestamps,
  numbering, notes or commentary inside subtitle text.
- For Arabic output, use normal unvocalized subtitle Arabic and omit harakat.
  Track speaker and addressee gender from named entities and neighboring
  dialogue. Use gendered verbs, imperatives, adjectives, and pronouns only
  when the evidence establishes that gender. Never guess gender from tone or
  scene stereotypes. When uncertain, rewrite naturally in a gender-neutral
  form, such as `مهلا` or `لحظة` instead of guessing `انتظر/انتظري`, and
  `ما خطبك؟` instead of adding a guessed suffix vowel.
- During episode and whole-series review, run a targeted Arabic grammatical-
  gender audit. In advanced mode, make only indexed patch corrections for
  supported gender errors; do not rewrite already-correct subtitles merely
  for stylistic variation. Gender corrections may not change indexes or
  timestamps and must be reflected in the submitted subtitle text.
- Keep every stable subtitle index exactly once and in request order. Never
  merge, renumber, omit or invent indexes. If evidence is genuinely unusable,
  return that index with an empty text string rather than dropping it.
- Apply the selected glossary only where its term or alias genuinely matches.
  Keep recurring people, families, places, organizations, ranks, titles and
  forms of address consistent, while preserving genuine nicknames and distinct
  short names. A shared prefix is not proof that two people are the same.
- After initial episode translation, reread the complete ordered episode and
  repair only genuine recognition, meaning, idiom, fluency or consistency
  errors. Then perform one whole-series consistency review across all episodes.
  Review may change text but never indexes or timestamps.

## Bounded parallel planning

- The coordinator owns the parent folder job and is the only worker allowed to
  create or submit the parent response. Never submit a child episode result as
  if it were the completed folder response.
- The initialized conversation is the coordinator. It may claim and delegate
  multiple folder jobs to background agents, then continue polling, but the
  bridge will never let this registration exceed its configured global
  `max_parallel` claimed jobs. If fewer Agent slots exist, leave excess jobs
  waiting or process serially. Never fail a job because a slot is unavailable.
- Inside one folder job, episodes may also be delegated when spare slots exist;
  use at most the `max_parallel` value returned by `listen`. Give each child one
  complete episode, its authoritative prompt, target language and glossary.
  If child slots are unavailable, process remaining episodes serially; lack of
  slots is never an error.
- Record child results in the coordinator's result map. Wait until every
  `expected_episode_indexes` entry has returned and every episode contains its
  exact `expected_subtitle_indexes`. Only then run whole-series review and
  construct the parent response.
- Before and after each episode/review/write, run the supplied `status_command`.
  If it reports cancellation, discard unfinished/late output.
- Perform a final whole-series consistency pass for recurring people, places,
  organizations, ranks and forms of address. Do not merge different short
  names merely because one is a prefix of another.

## Mandatory self-review quality gate

- After generating subtitles, reread the actual final text of every episode
  against OCR/ASR evidence and context, then score it conservatively from 0 to
  10. A score is a claim about the submitted text, not effort or confidence.
- Use `quality_policy.minimum_score` as the required score (8.5 fast, 9.5
  advanced). Every episode must reach it overall and in all five
  checks: `meaning_accuracy`, `target_language_fluency`,
  `ocr_asr_noise_cleanup`, `context_consistency`, and
  `watermark_ui_removal`. Revise and rescore until all checks pass.
- After episode checks, perform the whole-series review and require the same
  configured minimum overall and in `entity_consistency`, `terminology_consistency`,
  `cross_episode_context`, `artifact_recheck`, and `output_completeness`.
- 10 means publication-ready with no known defect; 9 means only negligible
  stylistic imperfections; 8.5 means publishable with no known meaning,
  language, watermark/UI, continuity, or completeness failure. Any known
  mistranslation, untranslated garbage, watermark, unsupported text, broken
  target language, inconsistent entity, or missing line requires a score below
  8.5 until the actual subtitle is corrected.
- Do not inflate scores to pass the gate. The bridge rejects missing scores,
  missing dimensions, or any score below the configured minimum and keeps the same job claimed for
  revision. Only a response that passes this gate may be stored as final SRT.
- For Arabic, a supported but unresolved speaker/addressee gender mismatch is
  a real `target_language_fluency` and `context_consistency` failure, not a
  negligible style issue, and cannot receive a passing score.

## Response

Write one UTF-8 JSON response with this shape and submit it using the supplied
`submit_command`. The single episode below illustrates field shape only; it is
never permission to return one episode when the request lists more:

```json
{{
  "protocol_version": {PROTOCOL_VERSION},
  "job_id": "same as request",
  "generation": 1,
  "cancel_epoch": 0,
  "status": "completed",
  "target_language": "same as request",
  "translation_quality": "same as request",
  "episodes": [
    {{
      "index": 1,
      "source_language": "same as episode source_output.language",
      "source_subtitles": [{{"index": 1, "start": "00:00:00,000", "end": "00:00:02,000", "text": "repaired source subtitle"}}],
      "subtitles": [{{"index": 1, "start": "00:00:00,000", "end": "00:00:02,000", "text": "..."}}],
      "review": {{
        "summary": "what was checked and corrected",
        "warnings": [],
        "quality_score": 9.0,
        "quality_checks": {{
          "meaning_accuracy": 9.0,
          "target_language_fluency": 9.0,
          "ocr_asr_noise_cleanup": 9.0,
          "context_consistency": 9.0,
          "watermark_ui_removal": 9.0
        }}
      }}
    }}
  ],
  "series_review": {{
    "summary": "...",
    "changes": [],
    "warnings": [],
    "quality_score": 9.0,
    "quality_checks": {{
      "entity_consistency": 9.0,
      "terminology_consistency": 9.0,
      "cross_episode_context": 9.0,
      "artifact_recheck": 9.0,
      "output_completeness": 9.0
    }}
  }},
  "series_entities": [
    {{"source":"Hawkins","target":"...","type":"family","aliases":["Huggins"],"confidence":0.94,"evidence_episodes":[5,6]}}
  ],
  "advanced_review": {{
    "stages_completed": ["reliable_draft", "native_localization_refinement", "independent_series_final_review"],
    "revision_cycles": 1,
    "independent_final_review": true,
    "summary": "required only in advanced mode"
  }},
  "glossary_suggestions": [],
  "token_estimate": {{"input": 0, "output": 0, "method": "visible-text estimate"}}
}}
```

Copy `generation` and `cancel_epoch` exactly from the request. The episode count
and ordered indexes must exactly match `expected_episode_indexes`. Within every
episode, copy every ordered index/start/end exactly from its request `items` and
change only `text`. Before writing the response, explicitly compare the result
map against both completion checklists. Do not include API keys. Glossary
suggestions are advisory only: never edit the shared glossary automatically.

Run `submit_command` only after the checklist passes. The bridge independently
validates episode completeness, subtitle completeness, order, target language,
and timestamps. An `INCOMPLETE_RESPONSE` error does not finish or fail the
parent job: keep the job claimed, repair the response, and run the same submit
command again. Report success in the conversation only after `SUBMITTED` is
returned.

After a successful submit, report translation quality, material corrections,
warnings, glossary suggestions and approximate visible token use in this
conversation, then continue polling.

Bridge root: `{root}`
Bridge script: `{bridge_script}`
Python: `{python_executable}`
"""


def generate_initialization(
    root: Path,
    project_root: Path,
    python_executable: str = sys.executable,
    max_parallel: int = 5,
) -> dict[str, Any]:
    root = ensure_layout(root)
    python_executable = console_python_executable(python_executable)
    current = state(root)
    generation = int(current.get("generation", 0)) + 1
    registration_id = uuid.uuid4().hex
    new_state = {
        **current,
        "protocol_version": PROTOCOL_VERSION,
        "generation": generation,
        "agent_enabled": True,
        "active_registration_id": registration_id,
        "updated_at": now_iso(),
    }
    atomic_write_json(root / "control" / "state.json", new_state)
    rules_path = root / "AGENT_RULES.md"
    rules_path.write_text(rules_text(root, Path(project_root).resolve(), python_executable), encoding="utf-8")
    script = Path(project_root).resolve() / "agent_bridge.py"
    init_path = _registration_path(root, registration_id)
    registration = {
        "protocol_version": PROTOCOL_VERSION,
        "registration_id": registration_id,
        "generation": generation,
        "created_at": now_iso(),
        "status": "awaiting_agent",
        "bridge_root": str(root),
        "rules_path": str(rules_path),
        "max_parallel": max(1, int(max_parallel)),
        "register_command": [python_executable, str(script), "register", "--init", str(init_path)],
        "listen_command": [python_executable, str(script), "listen", "--init", str(init_path), "--timeout", str(IDLE_LISTEN_TIMEOUT_SECONDS)],
    }
    atomic_write_json(init_path, registration)
    command = (
        "请将本对话初始化为本地字幕翻译 Agent。读取并严格执行以下规则与注册文件；"
        "完成握手后持续监听任务。领取任务后不得结束回合，必须持续到提交成功或确认取消；每个任务完成后写回程序报告、在本对话报告，然后继续监听。\n"
        f"规则文件：{rules_path}\n注册文件：{init_path}\n"
        f"首先执行：{' '.join(f'\"{part}\"' if ' ' in part else part for part in registration['register_command'])}"
    )
    return {"rules_path": str(rules_path), "init_path": str(init_path), "command": command, **registration}


def _validate_registration(init_path: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    registration = read_json(Path(init_path).resolve())
    if not isinstance(registration, dict):
        raise RuntimeError(f"Registration file is missing or invalid: {init_path}")
    root = ensure_layout(Path(registration["bridge_root"]))
    current = state(root)
    if (
        int(registration.get("protocol_version", -1)) != PROTOCOL_VERSION
        or int(current.get("protocol_version", -1)) != PROTOCOL_VERSION
        or
        not current.get("agent_enabled")
        or int(registration.get("generation", -1)) != int(current.get("generation", 0))
        or registration.get("registration_id") != current.get("active_registration_id")
    ):
        raise RuntimeError("REGISTRATION_INVALID")
    return root, registration, current


def register(init_path: Path) -> dict[str, Any]:
    root, registration, _ = _validate_registration(init_path)
    registration.update({"status": "ready", "registered_at": now_iso(), "heartbeat_at": now_iso()})
    atomic_write_json(Path(init_path).resolve(), registration)
    atomic_write_json(
        root / "control" / "active-agent.json",
        {
            "protocol_version": PROTOCOL_VERSION,
            "registration_id": registration["registration_id"],
            "generation": registration["generation"],
            "status": "ready",
            "heartbeat_at": now_iso(),
        },
    )
    return {"event": "REGISTERED", "generation": registration["generation"], "bridge_root": str(root)}


def _heartbeat(root: Path, registration: dict[str, Any], status_value: str = "listening") -> None:
    path = root / "control" / "active-agent.json"
    payload = read_json(path, {}) or {}
    payload.update(
        {
            "protocol_version": PROTOCOL_VERSION,
            "registration_id": registration["registration_id"],
            "generation": registration["generation"],
            "status": status_value,
            "heartbeat_at": now_iso(),
        }
    )
    atomic_write_json(path, payload)
    registration["heartbeat_at"] = now_iso()
    registration["status"] = status_value
    atomic_write_json(_registration_path(root, registration["registration_id"]), registration)


def _seconds_since(value: Any) -> float | None:
    try:
        stamp = datetime.fromisoformat(str(value))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - stamp.astimezone(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


def _job_event(
    root: Path,
    registration: dict[str, Any],
    request_path: Path,
    request: dict[str, Any],
    event: str,
) -> dict[str, Any]:
    job_dir = request_path.parent
    response_path = job_dir / "response.json"
    progress_path = job_dir / "progress.json"
    checkpoint_input_path = job_dir / "checkpoint-input.json"
    script = Path(__file__).resolve()
    init_path = _registration_path(root, registration["registration_id"])
    return {
        "event": event,
        "job_id": request["job_id"],
        "request_path": str(request_path),
        "response_path": str(response_path),
        "progress_path": str(progress_path),
        "checkpoint_input_path": str(checkpoint_input_path),
        "status_command": [sys.executable, str(script), "status", "--init", str(init_path), "--job-id", request["job_id"]],
        "checkpoint_command": [sys.executable, str(script), "checkpoint", "--init", str(init_path), "--job-id", request["job_id"], "--progress", str(checkpoint_input_path)],
        "submit_command": [sys.executable, str(script), "submit", "--init", str(init_path), "--job-id", request["job_id"], "--response", str(response_path)],
        "max_parallel": min(int(registration.get("max_parallel", 5)), int(request.get("max_parallel", 5))),
        "resume": event == "JOB_RESUME",
    }


def _claim_next_job(root: Path, registration: dict[str, Any], current: dict[str, Any]) -> dict[str, Any] | None:
    claimed_count = 0
    resumable: list[tuple[Path, dict[str, Any]]] = []
    for request_path in (root / "runtime").glob("*/request.json"):
        existing = read_json(request_path, {}) or {}
        if existing.get("status") == "claimed" and existing.get("claimed_by") == registration["registration_id"]:
            claimed_count += 1
            activity_age = _seconds_since(existing.get("last_agent_activity_at") or existing.get("claimed_at"))
            if activity_age is None or activity_age >= CLAIM_RESUME_AFTER_SECONDS:
                resumable.append((request_path, existing))
    if resumable:
        request_path, request = min(
            resumable,
            key=lambda value: value[1].get("last_agent_activity_at") or value[1].get("claimed_at") or "",
        )
        request["last_agent_activity_at"] = now_iso()
        request["resume_count"] = int(request.get("resume_count", 0)) + 1
        request["last_resumed_at"] = now_iso()
        atomic_write_json(request_path, request)
        return _job_event(root, registration, request_path, request, "JOB_RESUME")
    if claimed_count >= max(1, int(registration.get("max_parallel", 5))):
        return None
    for job_dir in sorted((root / "runtime").glob("*"), key=lambda p: p.stat().st_mtime):
        request_path = job_dir / "request.json"
        request = read_json(request_path)
        if not isinstance(request, dict) or request.get("status") != "pending":
            continue
        if int(request.get("generation", -1)) != int(current.get("generation", 0)):
            continue
        request["status"] = "claimed"
        request["claimed_at"] = now_iso()
        request["claimed_by"] = registration["registration_id"]
        request["last_agent_activity_at"] = now_iso()
        atomic_write_json(request_path, request)
        return _job_event(root, registration, request_path, request, "JOB")
    return None


def listen(init_path: Path, timeout: float = IDLE_LISTEN_TIMEOUT_SECONDS) -> dict[str, Any]:
    deadline = None if timeout < 0 else time.monotonic() + timeout
    while True:
        try:
            root, registration, current = _validate_registration(init_path)
        except RuntimeError as exc:
            return {"event": str(exc)}
        _heartbeat(root, registration)
        ping_path = root / "control" / "ping.json"
        ping = read_json(ping_path)
        if isinstance(ping, dict) and ping.get("generation") == current.get("generation") and not ping.get("acknowledged_at"):
            ping["acknowledged_at"] = now_iso()
            ping["registration_id"] = registration["registration_id"]
            atomic_write_json(ping_path, ping)
        job = _claim_next_job(root, registration, current)
        if job:
            _heartbeat(root, registration, "working")
            return job
        if deadline is not None and time.monotonic() >= deadline:
            return {"event": "IDLE", "heartbeat_at": now_iso()}
        # A subtitle job is manually started, so sub-second pickup is not
        # useful. Sleeping until the end of this roughly one-minute long poll
        # avoids touching the bridge directory every second while idle.
        if deadline is None:
            time.sleep(LISTEN_POLL_SECONDS)
        else:
            remaining = max(0.0, deadline - time.monotonic())
            time.sleep(min(LISTEN_POLL_SECONDS, remaining))


def job_status(init_path: Path, job_id: str) -> dict[str, Any]:
    try:
        root, registration, current = _validate_registration(init_path)
    except RuntimeError as exc:
        return {"event": str(exc), "cancelled": True}
    request_path = root / "runtime" / job_id / "request.json"
    request = read_json(request_path, {}) or {}
    cancelled = (
        int(request.get("cancel_epoch", -1)) != int(current.get("cancel_epoch", 0))
        or request.get("status") == "cancellation_requested"
    )
    if request.get("claimed_by") == registration["registration_id"] and not cancelled:
        request["last_agent_activity_at"] = now_iso()
        atomic_write_json(request_path, request)
        _heartbeat(root, registration, "working")
    return {"event": "STATUS", "job_id": job_id, "cancelled": cancelled, "status": request.get("status")}


def validate_checkpoint(request: dict[str, Any], progress: dict[str, Any]) -> list[int]:
    if progress.get("job_id") != request.get("job_id") or progress.get("status") != "in_progress":
        raise RuntimeError("Checkpoint job_id/status is invalid")
    if int(progress.get("generation", -1)) != int(request.get("generation", 0)):
        raise RuntimeError("Checkpoint generation does not match the job")
    if int(progress.get("cancel_epoch", -1)) != int(request.get("cancel_epoch", 0)):
        raise RuntimeError("Checkpoint belongs to a cancelled task")
    expected_episodes = {
        int(episode["index"]): episode
        for episode in request.get("episodes", [])
        if isinstance(episode, dict) and "index" in episode
    }
    episodes = progress.get("episodes")
    if not isinstance(episodes, list) or not episodes:
        raise RuntimeError("Checkpoint episodes must be a non-empty array")
    received_indexes = [int(episode.get("index", -1)) for episode in episodes if isinstance(episode, dict)]
    if len(received_indexes) != len(episodes) or len(set(received_indexes)) != len(received_indexes):
        raise RuntimeError("Checkpoint contains invalid or duplicate episode indexes")
    expected_order = [index for index in expected_episodes if index in set(received_indexes)]
    if received_indexes != expected_order:
        raise RuntimeError("Checkpoint episode indexes must be an ordered subset of the request")
    for episode in episodes:
        episode_index = int(episode["index"])
        source_episode = expected_episodes.get(episode_index)
        if source_episode is None:
            raise RuntimeError(f"Checkpoint contains unexpected episode {episode_index}")
        minimum_score = float((request.get("quality_policy") or {}).get("minimum_score", MIN_AGENT_QUALITY_SCORE))
        _validated_quality_review(
            episode.get("review"), f"checkpoint episode {episode_index}", EPISODE_QUALITY_DIMENSIONS, minimum_score
        )
        expected_rows = source_episode.get("items") if isinstance(source_episode.get("items"), list) else []
        expected_by_index = {int(row["index"]): row for row in expected_rows if isinstance(row, dict)}
        subtitles = episode.get("subtitles")
        if not isinstance(subtitles, list):
            raise RuntimeError(f"Checkpoint episode {episode_index} subtitles must be an array")
        received_rows = [int(row.get("index", -1)) for row in subtitles if isinstance(row, dict)]
        if received_rows != list(expected_by_index):
            raise RuntimeError(f"Checkpoint episode {episode_index} subtitle indexes are incomplete or unordered")
        for row in subtitles:
            subtitle_index = int(row["index"])
            if not isinstance(row.get("text"), str):
                raise RuntimeError(f"Checkpoint episode {episode_index} subtitle {subtitle_index} text must be a string")
            expected = expected_by_index[subtitle_index]
            if str(row.get("start", "")) != str(expected.get("start", "")) or str(row.get("end", "")) != str(expected.get("end", "")):
                raise RuntimeError(f"Checkpoint episode {episode_index} subtitle {subtitle_index} changed its timestamp")
    return received_indexes


def checkpoint(init_path: Path, job_id: str, progress_input_path: Path) -> dict[str, Any]:
    root, registration, current = _validate_registration(init_path)
    request_path = root / "runtime" / job_id / "request.json"
    request = read_json(request_path)
    progress = read_json(Path(progress_input_path))
    if not isinstance(request, dict) or not isinstance(progress, dict):
        raise RuntimeError("Request or checkpoint JSON is invalid")
    if request.get("status") != "claimed" or request.get("claimed_by") != registration["registration_id"]:
        raise RuntimeError("Checkpoint rejected: job is not claimed by this Agent")
    if int(request.get("cancel_epoch", -1)) != int(current.get("cancel_epoch", 0)):
        raise RuntimeError("Checkpoint rejected: task was cancelled")
    episode_indexes = validate_checkpoint(request, progress)
    accepted_path = request_path.parent / "progress.json"
    atomic_write_json(accepted_path, progress)
    request["progress_episode_indexes"] = episode_indexes
    request["progress_at"] = now_iso()
    request["last_agent_activity_at"] = now_iso()
    atomic_write_json(request_path, request)
    _heartbeat(root, registration, "working")
    return {"event": "CHECKPOINTED", "job_id": job_id, "episodes": episode_indexes, "progress_path": str(accepted_path)}


def _validated_quality_review(
    value: Any,
    label: str,
    dimensions: tuple[str, ...],
    minimum_score: float = MIN_AGENT_QUALITY_SCORE,
) -> float:
    if not isinstance(value, dict):
        raise RuntimeError(f"QUALITY_GATE_FAILED: {label} review is missing")
    try:
        score = float(value.get("quality_score"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"QUALITY_GATE_FAILED: {label} quality_score is missing or invalid") from exc
    checks = value.get("quality_checks")
    if not isinstance(checks, dict):
        raise RuntimeError(f"QUALITY_GATE_FAILED: {label} quality_checks are missing")
    failed = []
    for dimension in dimensions:
        try:
            dimension_score = float(checks.get(dimension))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"QUALITY_GATE_FAILED: {label} quality check {dimension} is missing or invalid"
            ) from exc
        if not 0.0 <= dimension_score <= 10.0:
            raise RuntimeError(f"QUALITY_GATE_FAILED: {label} quality check {dimension} must be 0-10")
        if dimension_score < minimum_score:
            failed.append(f"{dimension}={dimension_score:.1f}")
    if not 0.0 <= score <= 10.0:
        raise RuntimeError(f"QUALITY_GATE_FAILED: {label} quality_score must be 0-10")
    if score < minimum_score or failed:
        details = ", ".join(failed) or f"overall={score:.1f}"
        raise RuntimeError(
            f"QUALITY_GATE_FAILED: {label} did not reach {minimum_score:.1f}; {details}. "
            "Revise the actual subtitles, run self-review again, and resubmit the same job."
        )
    return score


def validate_agent_quality_gate(
    response: dict[str, Any], minimum_score: float = MIN_AGENT_QUALITY_SCORE
) -> dict[str, Any]:
    episode_scores = {}
    episodes = response.get("episodes")
    if not isinstance(episodes, list):
        raise RuntimeError("QUALITY_GATE_FAILED: response episodes are missing")
    for episode in episodes:
        if not isinstance(episode, dict):
            raise RuntimeError("QUALITY_GATE_FAILED: response contains an invalid episode")
        episode_index = int(episode.get("index", -1))
        episode_scores[episode_index] = _validated_quality_review(
            episode.get("review"), f"episode {episode_index}", EPISODE_QUALITY_DIMENSIONS, minimum_score
        )
    series_score = _validated_quality_review(
        response.get("series_review"), "whole series", SERIES_QUALITY_DIMENSIONS, minimum_score
    )
    return {"episode_scores": episode_scores, "series_score": series_score, "threshold": minimum_score}


def validate_job_response(request: dict[str, Any], response: dict[str, Any]) -> None:
    """Reject incomplete Agent work before the waiting video process sees it."""
    expected_episode_indexes = request.get("expected_episode_indexes")
    if not isinstance(expected_episode_indexes, list):
        expected_episode_indexes = [episode.get("index") for episode in request.get("episodes", []) if isinstance(episode, dict)]
    try:
        expected_episode_indexes = [int(value) for value in expected_episode_indexes]
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Request expected_episode_indexes is invalid") from exc
    response_episodes = response.get("episodes")
    if not isinstance(response_episodes, list):
        raise RuntimeError("Response episodes must be an array")
    try:
        received_episode_indexes = [int(episode.get("index")) for episode in response_episodes if isinstance(episode, dict)]
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Response contains an invalid episode index") from exc
    if len(received_episode_indexes) != len(response_episodes) or received_episode_indexes != expected_episode_indexes:
        raise RuntimeError(
            f"INCOMPLETE_RESPONSE: expected episodes {sorted(expected_episode_indexes)}, "
            f"received {sorted(received_episode_indexes)}. Complete every episode and resubmit the same parent job."
        )
    if len(set(received_episode_indexes)) != len(received_episode_indexes):
        raise RuntimeError("Response contains duplicate episode indexes")
    if str(response.get("target_language", "")).casefold() != str(request.get("target_language", "")).casefold():
        raise RuntimeError("Response target_language does not match the request")
    quality_mode = str(request.get("translation_quality", "fast"))
    minimum_score = float((request.get("quality_policy") or {}).get("minimum_score", MIN_AGENT_QUALITY_SCORE))
    if minimum_score not in (MIN_AGENT_QUALITY_SCORE, ADVANCED_AGENT_QUALITY_SCORE):
        raise RuntimeError("Request quality threshold is invalid")
    if str(response.get("translation_quality", "fast")) != quality_mode:
        raise RuntimeError("Response translation_quality does not match the request")
    entities = response.get("series_entities")
    if "quality_policy" in request and not isinstance(entities, list):
        raise RuntimeError("Response series_entities must be an array")
    validate_agent_quality_gate(response, minimum_score)
    if quality_mode == "advanced":
        advanced_review = response.get("advanced_review")
        required_stages = (request.get("quality_policy") or {}).get("required_stages") or []
        if not isinstance(advanced_review, dict):
            raise RuntimeError("ADVANCED_QUALITY_GATE_FAILED: advanced_review is missing")
        if advanced_review.get("stages_completed") != required_stages:
            raise RuntimeError("ADVANCED_QUALITY_GATE_FAILED: required stages are incomplete or unordered")
        if advanced_review.get("independent_final_review") is not True:
            raise RuntimeError("ADVANCED_QUALITY_GATE_FAILED: independent final review was not confirmed")
        try:
            revision_cycles = int(advanced_review.get("revision_cycles", 0))
        except (TypeError, ValueError) as exc:
            raise RuntimeError("ADVANCED_QUALITY_GATE_FAILED: revision_cycles is invalid") from exc
        maximum_cycles = int((request.get("quality_policy") or {}).get("maximum_revision_cycles", 3))
        if not 1 <= revision_cycles <= maximum_cycles:
            raise RuntimeError("ADVANCED_QUALITY_GATE_FAILED: revision_cycles is outside the allowed range")

    request_by_index = {int(episode["index"]): episode for episode in request.get("episodes", []) if isinstance(episode, dict)}
    for response_episode in response_episodes:
        episode_index = int(response_episode["index"])
        request_episode = request_by_index.get(episode_index, {})
        expected_rows = request_episode.get("items") if isinstance(request_episode.get("items"), list) else []
        expected_indexes = request_episode.get("expected_subtitle_indexes")
        if not isinstance(expected_indexes, list):
            expected_indexes = [row.get("index") for row in expected_rows if isinstance(row, dict)]
        expected_indexes = [int(value) for value in expected_indexes]
        subtitles = response_episode.get("subtitles")
        if not isinstance(subtitles, list):
            raise RuntimeError(f"Episode {episode_index} subtitles must be an array")
        try:
            received_indexes = [int(row.get("index")) for row in subtitles if isinstance(row, dict)]
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"Episode {episode_index} contains an invalid subtitle index") from exc
        if len(received_indexes) != len(subtitles) or received_indexes != expected_indexes:
            missing = sorted(set(expected_indexes) - set(received_indexes))
            extra = sorted(set(received_indexes) - set(expected_indexes))
            raise RuntimeError(
                f"INCOMPLETE_RESPONSE: episode {episode_index} subtitle indexes mismatch; "
                f"missing={missing[:30]}, extra={extra[:30]}. Repair and resubmit."
            )
        if len(set(received_indexes)) != len(received_indexes):
            raise RuntimeError(f"Episode {episode_index} contains duplicate subtitle indexes")
        source_contract = request_episode.get("source_output")
        if isinstance(source_contract, dict) and source_contract.get("required"):
            source_language = str(source_contract.get("language") or "")
            if str(response_episode.get("source_language") or "").casefold() != source_language.casefold():
                raise RuntimeError(
                    f"Episode {episode_index} repaired source language does not match the request"
                )
            source_subtitles = response_episode.get("source_subtitles")
            if not isinstance(source_subtitles, list):
                raise RuntimeError(f"Episode {episode_index} source_subtitles must be an array")
            try:
                source_indexes = [
                    int(row.get("index")) for row in source_subtitles if isinstance(row, dict)
                ]
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"Episode {episode_index} contains an invalid repaired source index"
                ) from exc
            if len(source_indexes) != len(source_subtitles) or source_indexes != expected_indexes:
                raise RuntimeError(
                    f"INCOMPLETE_RESPONSE: episode {episode_index} repaired source indexes mismatch"
                )
            for source_row, expected_row in zip(source_subtitles, expected_rows):
                if not isinstance(source_row.get("text"), str):
                    raise RuntimeError(
                        f"Episode {episode_index} repaired source subtitle text is invalid"
                    )
                if (
                    str(source_row.get("start", "")) != str(expected_row.get("start", ""))
                    or str(source_row.get("end", "")) != str(expected_row.get("end", ""))
                ):
                    raise RuntimeError(
                        f"Episode {episode_index} repaired source subtitle changed its timing"
                    )
        expected_by_index = {int(row["index"]): row for row in expected_rows if isinstance(row, dict) and "index" in row}
        for row in subtitles:
            subtitle_index = int(row["index"])
            if not isinstance(row.get("text"), str):
                raise RuntimeError(f"Episode {episode_index} subtitle {subtitle_index} text must be a string")
            expected = expected_by_index.get(subtitle_index)
            if expected and (str(row.get("start", "")) != str(expected.get("start", "")) or str(row.get("end", "")) != str(expected.get("end", ""))):
                raise RuntimeError(
                    f"Episode {episode_index} subtitle {subtitle_index} changed its timestamp; "
                    "copy start/end exactly from the request and resubmit."
                )


def submit(init_path: Path, job_id: str, response_path: Path) -> dict[str, Any]:
    root, registration, current = _validate_registration(init_path)
    _heartbeat(root, registration, "working")
    request_path = root / "runtime" / job_id / "request.json"
    request = read_json(request_path)
    response = read_json(Path(response_path))
    if not isinstance(request, dict) or not isinstance(response, dict):
        raise RuntimeError("Request or response JSON is invalid")
    if response.get("job_id") != job_id or response.get("status") != "completed":
        raise RuntimeError("Response job_id/status is invalid")
    if int(response.get("generation", -1)) != int(current.get("generation", 0)):
        raise RuntimeError("Late response rejected: generation changed")
    if int(response.get("cancel_epoch", -1)) != int(current.get("cancel_epoch", 0)):
        raise RuntimeError("Late response rejected: task was cancelled")
    try:
        validate_job_response(request, response)
    except RuntimeError as exc:
        request["status"] = "claimed"
        request["last_agent_activity_at"] = now_iso()
        request["submission_rejections"] = int(request.get("submission_rejections", 0)) + 1
        request["last_submission_error"] = str(exc)
        request["last_submission_rejected_at"] = now_iso()
        atomic_write_json(request_path, request)
        raise
    request["status"] = "response_ready"
    request["response_ready_at"] = now_iso()
    atomic_write_json(request_path, request)
    _heartbeat(root, registration, "listening")
    return {"event": "SUBMITTED", "job_id": job_id}


def request_stop_all(root: Path) -> dict[str, Any]:
    root = ensure_layout(root)
    current = state(root)
    current["cancel_epoch"] = int(current.get("cancel_epoch", 0)) + 1
    current["updated_at"] = now_iso()
    atomic_write_json(root / "control" / "state.json", current)
    for request_path in (root / "runtime").glob("*/request.json"):
        request = read_json(request_path)
        if isinstance(request, dict) and request.get("status") not in ("completed", "failed", "cancelled"):
            request["status"] = "cancellation_requested"
            request["cancelled_at"] = now_iso()
            atomic_write_json(request_path, request)
    return {"event": "STOP_ALL", "cancel_epoch": current["cancel_epoch"]}


def stop_agent(root: Path) -> dict[str, Any]:
    root = ensure_layout(root)
    current = state(root)
    current["generation"] = int(current.get("generation", 0)) + 1
    current["agent_enabled"] = False
    current.pop("active_registration_id", None)
    current["updated_at"] = now_iso()
    atomic_write_json(root / "control" / "state.json", current)
    return {"event": "AGENT_STOPPED", "generation": current["generation"]}


def bridge_status(root: Path) -> dict[str, Any]:
    root = ensure_layout(root)
    current = state(root)
    agent = read_json(root / "control" / "active-agent.json", {}) or {}
    age = None
    try:
        stamp = datetime.fromisoformat(agent["heartbeat_at"])
        age = max(0.0, (datetime.now(timezone.utc).astimezone() - stamp).total_seconds())
    except (KeyError, TypeError, ValueError):
        pass
    connected = bool(
        current.get("agent_enabled")
        and int(current.get("protocol_version", -1)) == PROTOCOL_VERSION
        and int(agent.get("protocol_version", -1)) == PROTOCOL_VERSION
        and agent.get("generation") == current.get("generation")
        and age is not None
        and age <= HEARTBEAT_FRESH_SECONDS
    )
    return {"event": "BRIDGE_STATUS", "connected": connected, "heartbeat_age_seconds": age, "state": current, "agent": agent}


def create_job(root: Path, payload: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    root = ensure_layout(root)
    current = state(root)
    if not current.get("agent_enabled"):
        raise RuntimeError("Agent mode is not initialized. Generate and run the initialization command first.")
    job_id = str(payload.get("job_id") or f"job-{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}")
    job_dir = root / "runtime" / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    request = {
        **payload,
        "protocol_version": PROTOCOL_VERSION,
        "job_id": job_id,
        "generation": int(current.get("generation", 0)),
        "cancel_epoch": int(current.get("cancel_epoch", 0)),
        "status": "pending",
        "created_at": now_iso(),
    }
    atomic_write_json(job_dir / "request.json", request)
    return job_dir, request


def wait_for_response(root: Path, job_id: str, poll_seconds: float = 1.0, timeout_seconds: float = 0.0) -> dict[str, Any]:
    root = ensure_layout(root)
    request_path = root / "runtime" / job_id / "request.json"
    response_path = root / "runtime" / job_id / "response.json"
    started = time.monotonic()
    while True:
        current = state(root)
        request = read_json(request_path, {}) or {}
        if int(request.get("cancel_epoch", -1)) != int(current.get("cancel_epoch", 0)) or request.get("status") == "cancellation_requested":
            raise RuntimeError("Agent subtitle task was cancelled")
        if request.get("status") == "response_ready":
            response = read_json(response_path)
            if not isinstance(response, dict):
                raise RuntimeError("Agent response file is invalid")
            return response
        if timeout_seconds > 0 and time.monotonic() - started >= timeout_seconds:
            raise RuntimeError(f"Timed out waiting for Agent response after {timeout_seconds:.0f} seconds")
        time.sleep(max(0.1, poll_seconds))


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def cleanup_job(job_dir: Path) -> None:
    shutil.rmtree(Path(job_dir), ignore_errors=True)


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local subtitle Agent file bridge")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("generate-init")
    p.add_argument("--root", required=True)
    p.add_argument("--project-root", required=True)
    p.add_argument("--max-parallel", type=int, default=5)
    p = sub.add_parser("register")
    p.add_argument("--init", required=True)
    p = sub.add_parser("listen")
    p.add_argument("--init", required=True)
    p.add_argument("--timeout", type=float, default=IDLE_LISTEN_TIMEOUT_SECONDS)
    p = sub.add_parser("status")
    p.add_argument("--init", required=True)
    p.add_argument("--job-id", required=True)
    p = sub.add_parser("submit")
    p.add_argument("--init", required=True)
    p.add_argument("--job-id", required=True)
    p.add_argument("--response", required=True)
    p = sub.add_parser("checkpoint")
    p.add_argument("--init", required=True)
    p.add_argument("--job-id", required=True)
    p.add_argument("--progress", required=True)
    p = sub.add_parser("bridge-status")
    p.add_argument("--root", required=True)
    p = sub.add_parser("stop-all")
    p.add_argument("--root", required=True)
    p = sub.add_parser("stop-agent")
    p.add_argument("--root", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = make_parser().parse_args(argv)
    try:
        if args.command == "generate-init":
            result = generate_initialization(Path(args.root), Path(args.project_root), max_parallel=args.max_parallel)
        elif args.command == "register":
            result = register(Path(args.init))
        elif args.command == "listen":
            result = listen(Path(args.init), args.timeout)
        elif args.command == "status":
            result = job_status(Path(args.init), args.job_id)
        elif args.command == "submit":
            result = submit(Path(args.init), args.job_id, Path(args.response))
        elif args.command == "checkpoint":
            result = checkpoint(Path(args.init), args.job_id, Path(args.progress))
        elif args.command == "bridge-status":
            result = bridge_status(Path(args.root))
        elif args.command == "stop-all":
            result = request_stop_all(Path(args.root))
        else:
            result = stop_agent(Path(args.root))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"event": "ERROR", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
