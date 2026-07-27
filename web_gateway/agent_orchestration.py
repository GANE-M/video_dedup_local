from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any


RECAP_STAGES = (
    "story_analysis",
    "recap_draft",
    "independent_review",
    "final_revision",
    "final_verification",
)

ROLE_FOR_STAGE = {
    "story_analysis": "story_analyst",
    "recap_draft": "recap_writer",
    "independent_review": "independent_reviewer",
    "final_revision": "reviser",
    "final_verification": "final_verifier",
}


def canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def stage_token(stage: str, payload: dict[str, Any]) -> str:
    return f"{stage}:{canonical_digest(payload)}"


def require_text(
    payload: dict[str, Any],
    key: str,
    *,
    minimum: int = 40,
) -> str:
    value = str(payload.get(key) or "").strip()
    if len(value) < minimum:
        raise ValueError(f"{key} 内容过短，至少需要 {minimum} 个字符")
    if value.count("?") + value.count("？") >= max(5, len(value) // 3):
        raise ValueError(f"{key} 疑似占位符或乱码")
    return value


def validate_execution(
    stage: str,
    payload: dict[str, Any],
    completed: list[dict[str, Any]],
) -> dict[str, Any]:
    execution = payload.get("execution")
    if not isinstance(execution, dict):
        raise ValueError(f"{stage} 缺少 execution 运行信息")
    expected_role = ROLE_FOR_STAGE[stage]
    if str(execution.get("role") or "") != expected_role:
        raise ValueError(f"{stage} 必须由角色 {expected_role} 提交")
    run_id = str(execution.get("agent_run_id") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{5,127}", run_id):
        raise ValueError(f"{stage} 的 agent_run_id 无效")
    if stage in {"independent_review", "final_verification"}:
        if execution.get("context_isolated") is not True:
            raise ValueError(f"{stage} 必须声明 context_isolated=true")
        disallowed = {
            str(item.get("agent_run_id") or "")
            for item in completed
            if str(item.get("agent_run_id") or "")
        }
        if run_id in disallowed:
            raise ValueError(f"{stage} 不能复用创作或修订 Agent 的 run id")
    return {
        "role": expected_role,
        "agent_run_id": run_id,
        "context_isolated": bool(execution.get("context_isolated")),
    }


def validate_episode_indexes(payload: dict[str, Any], expected: list[int]) -> None:
    received = payload.get("episode_indexes")
    if not isinstance(received, list):
        raise ValueError("story_analysis 必须返回 episode_indexes")
    normalized = sorted({int(value) for value in received})
    if normalized != sorted(expected):
        raise ValueError(
            f"story_analysis 集数不完整：expected={sorted(expected)}, received={normalized}"
        )


def validate_review_payload(
    payload: dict[str, Any],
    *,
    final: bool,
) -> tuple[float, str, list[dict[str, Any]]]:
    score = float(payload.get("score", 0))
    if score < 0 or score > 100:
        raise ValueError("审核 score 必须在 0-100 之间")
    verdict = str(payload.get("verdict") or "").strip().casefold()
    if verdict not in {"pass", "revise"}:
        raise ValueError("审核 verdict 必须是 pass 或 revise")
    issues = payload.get("issues")
    if not isinstance(issues, list):
        raise ValueError("审核必须返回结构化 issues 数组")
    normalized: list[dict[str, Any]] = []
    issue_ids: set[str] = set()
    for index, item in enumerate(issues, 1):
        if not isinstance(item, dict):
            raise ValueError(f"审核问题 {index} 必须是对象")
        issue_id = str(item.get("issue_id") or "").strip()
        severity = str(item.get("severity") or "").strip().casefold()
        if not issue_id or issue_id in issue_ids:
            raise ValueError("审核 issue_id 必须非空且唯一")
        if severity not in {"critical", "major", "minor"}:
            raise ValueError(f"审核问题 {issue_id} severity 无效")
        evidence_refs = item.get("evidence_refs")
        if not isinstance(evidence_refs, list) or not any(
            str(value).strip() for value in evidence_refs
        ):
            raise ValueError(f"审核问题 {issue_id} 缺少证据引用")
        require_text(item, "required_patch", minimum=8)
        issue_ids.add(issue_id)
        normalized.append(item)
    if final:
        blocking = [
            item["issue_id"]
            for item in normalized
            if str(item["severity"]).casefold() in {"critical", "major"}
        ]
        if verdict != "pass" or score < 85 or blocking:
            raise ValueError(
                "最终独立审核未通过：必须 verdict=pass、score>=85 且无 critical/major 问题"
            )
    return score, verdict, normalized


def stage_rule_path(project_root: Path, stage: str) -> Path:
    if stage not in RECAP_STAGES:
        raise KeyError(stage)
    role = ROLE_FOR_STAGE[stage]
    return Path(project_root) / "agent_roles" / f"{RECAP_STAGES.index(stage) + 2:02d}_{role.upper()}.md"
