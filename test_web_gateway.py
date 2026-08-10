from __future__ import annotations

import hashlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient
from PIL import Image

import agent_bridge
import video_dedup
from web_gateway.agent_http import RemoteAgentBridge
from web_gateway.agent_orchestration import (
    canonical_digest,
    validate_execution,
    validate_review_payload,
)
from web_gateway.app import create_app
from web_gateway.database import GatewayDatabase
from web_gateway.recap_service import RecapService
from web_gateway.publishing_materials import TIKTOK_COPY_NAME, validate_publishing_plan
from web_gateway.security import normalize_public_recap_rendering, validate_public_http_url
from web_gateway.settings import GatewaySettings
from web_gateway.storage import JobStorage, safe_component
from web_gateway.worker import GatewayWorker, normalize_settings
from web_gateway.workflows import (
    DEDUP_STAGE,
    RECAP_PLANNING_STAGE,
    SUBTITLE_STAGE,
    WorkflowCheckpointStore,
    WorkflowPlan,
)
from recap.models import RecapProject, RecapSegment


class WebGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.settings = GatewaySettings(
            project_root=Path(__file__).resolve().parent,
            storage_root=root / "videos",
            service_root=root / "service",
            database_path=root / "service" / "gateway.sqlite3",
            public_base_url="https://video.example.test",
            chunk_size=4,
            maximum_chunk_size=8,
            maximum_video_workers=10,
            maximum_subtitle_workers=3,
            maximum_processing_jobs=1,
        )
        self.settings.ensure_directories()
        self.database = GatewayDatabase(self.settings.database_path)
        self.key, _record = self.database.create_access_key("test", maximum_active_jobs=5)
        self.client = TestClient(create_app(self.settings, start_worker=False))
        self.headers = {"X-API-Key": self.key}

    def tearDown(self) -> None:
        self.client.close()
        self.temporary.cleanup()

    def create_job(self, data: bytes = b"0123456789") -> tuple[dict, bytes]:
        response = self.client.post(
            "/api/v1/jobs",
            headers=self.headers,
            json={
                "series_name": "测试剧",
                "files": [{"name": "第一集.mp4", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}],
                "settings": {"pipeline": {"enable_subtitles": True, "translation_backend": "agent"}},
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json(), data

    def upload_all(self, job: dict, data: bytes) -> None:
        upload = job["uploads"][0]
        for index in range(upload["total_chunks"]):
            chunk = data[index * job["chunk_size"]:(index + 1) * job["chunk_size"]]
            response = self.client.put(
                f"/api/v1/jobs/{job['id']}/uploads/{upload['id']}/chunks/{index}",
                headers={**self.headers, "X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest()},
                content=chunk,
            )
            self.assertEqual(response.status_code, 200, response.text)
        response = self.client.post(
            f"/api/v1/jobs/{job['id']}/uploads/{upload['id']}/complete", headers=self.headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        repeated = self.client.post(
            f"/api/v1/jobs/{job['id']}/uploads/{upload['id']}/complete", headers=self.headers
        )
        self.assertEqual(repeated.status_code, 200, repeated.text)
        self.assertTrue(repeated.json()["idempotent"])

    def upload_declared(self, job: dict, upload: dict, data: bytes) -> None:
        for index in range(upload["total_chunks"]):
            chunk = data[index * job["chunk_size"]:(index + 1) * job["chunk_size"]]
            response = self.client.put(
                f"/api/v1/jobs/{job['id']}/uploads/{upload['id']}/chunks/{index}",
                headers={**self.headers, "X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest()},
                content=chunk,
            )
            self.assertEqual(response.status_code, 200, response.text)
        response = self.client.post(
            f"/api/v1/jobs/{job['id']}/uploads/{upload['id']}/complete", headers=self.headers
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_independent_roles_cannot_reuse_any_previous_agent_run(self) -> None:
        completed = [
            {"role": "story_analyst", "agent_run_id": "story-agent-001"},
            {"role": "independent_reviewer", "agent_run_id": "review-agent-001"},
        ]
        with self.assertRaisesRegex(ValueError, "不能复用"):
            validate_execution(
                "final_verification",
                {
                    "execution": {
                        "role": "final_verifier",
                        "agent_run_id": "review-agent-001",
                        "context_isolated": True,
                    }
                },
                completed,
            )

    def test_final_review_rejects_blocking_evidence_linked_issue(self) -> None:
        with self.assertRaisesRegex(ValueError, "最终独立审核未通过"):
            validate_review_payload(
                {
                    "score": 91,
                    "verdict": "pass",
                    "issues": [{
                        "issue_id": "plot-1",
                        "severity": "major",
                        "evidence_refs": ["subtitles/ep1.srt#00:12"],
                        "required_patch": "修正角色因果关系",
                    }],
                },
                final=True,
            )

    def test_publishing_plan_enforces_platform_fyp_order_and_quality(self) -> None:
        valid = {
            "task_type": "publishing_materials",
            "language": "English",
            "platform": "reelshort",
            "platform_evidence": "The uploaded cover visibly says ReelShort.",
            "title": "A promise that changed everything",
            "bio": "One secret forces two enemies to trust each other.",
            "hashtags": ["#reelshort", "#fyp", "#shortdrama", "#romance", "#drama"],
            "cover": {"episode_number_position": "bottom_right"},
            "quality_score": 9.1,
            "quality_notes": "Platform evidence and tag order were checked.",
        }
        self.assertEqual(validate_publishing_plan(valid)["hashtags"][:2], ["#reelshort", "#fyp"])
        with self.assertRaisesRegex(ValueError, "顺序"):
            validate_publishing_plan({**valid, "hashtags": ["#fyp", "#reelshort", "#shortdrama", "#romance", "#drama"]})
        unknown = {**valid, "platform": None, "platform_evidence": None, "hashtags": ["#fyp", "#shortdrama", "#romance", "#drama", "#series"]}
        self.assertEqual(validate_publishing_plan(unknown)["hashtags"][0], "#fyp")

    def test_generic_publishing_agent_then_dedup_publishes_complete_processed_folder(self) -> None:
        video = b"fake-video"
        image_buffer = io.BytesIO()
        Image.new("RGB", (1, 1), "navy").save(image_buffer, format="PNG")
        cover = image_buffer.getvalue()
        information = "The Test Drama\nA hidden promise puts an entire family at risk.".encode("utf-8")
        declarations = [
            ("第一集.mp4", "video", video),
            ("封面.png", "cover", cover),
            ("剧名简介.txt", "series_info", information),
        ]
        response = self.client.post(
            "/api/v1/jobs",
            headers=self.headers,
            json={
                "series_name": "The Test Drama",
                "files": [
                    {"name": name, "role": role, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                    for name, role, data in declarations
                ],
                "settings": {"pipeline": {
                    "enable_subtitles": False,
                    "enable_recap": False,
                    "enable_publishing": True,
                    "enable_dedup": True,
                    "publishing_language": "English",
                }},
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        created = response.json()
        for upload, (_name, _role, data) in zip(created["uploads"], declarations, strict=True):
            self.upload_declared(created, upload, data)
        queued = self.client.post(f"/api/v1/jobs/{created['id']}/start", headers=self.headers)
        self.assertEqual(queued.status_code, 200, queued.text)
        worker = self.client.app.state.worker
        first = self.database.claim_next_job()
        self.assertIsNotNone(first)
        worker._run_job(first)
        self.assertEqual(self.database.get_job(created["id"])["status"], "waiting_publishing_agent")

        bootstrap = created["agent_bootstrap"]
        agent_headers = {"Authorization": f"Bearer {bootstrap['agent_token']}"}
        event = self.client.post(
            bootstrap["listen_url"].replace("https://video.example.test", ""),
            headers=agent_headers,
        ).json()
        self.assertEqual(event["event"], "PUBLISHING_JOB")
        self.assertEqual(event["request"]["required_artifacts"], [
            "assets/cover/封面.png", "assets/series_info/剧名简介.txt",
        ])
        for artifact in event["request"]["required_artifacts"]:
            fetched = self.client.get(
                f"/api/v1/agent/jobs/{created['id']}/artifacts/{artifact}",
                headers=agent_headers,
            )
            self.assertEqual(fetched.status_code, 200, fetched.text)
        plan = {
            "schema_version": 1,
            "task_type": "publishing_materials",
            "language": "English",
            "platform": None,
            "platform_evidence": None,
            "title": "One promise changes everything",
            "bio": "A dangerous family secret turns love into a fight for survival.",
            "hashtags": ["#fyp", "#shortdrama", "#familydrama", "#romance", "#series"],
            "cover": {"episode_number_position": "bottom_right"},
            "quality_score": 9.2,
            "quality_notes": "No platform was invented; output order and copy were checked.",
        }
        submitted = self.client.post(
            event["submit_endpoint"].replace("https://video.example.test", ""),
            headers=agent_headers,
            json={"claim_id": event["claim_id"], "response": plan},
        )
        self.assertEqual(submitted.status_code, 200, submitted.text)
        second = self.database.claim_next_job()
        self.assertIsNotNone(second)

        def fake_dedup(_job, paths, _settings, _stage, **_kwargs):
            (paths.videos / "第一集_local.mp4").write_bytes(b"deduped-video")
            return True

        with mock.patch.object(worker, "_run_batch_process", side_effect=fake_dedup):
            worker._run_job(second)
        completed = self.database.get_job(created["id"])
        self.assertEqual(completed["status"], "completed")
        project = JobStorage(self.settings).project_paths(JobStorage(self.settings).paths_from_job(completed))
        self.assertTrue((project.processed / "第一集_local.mp4").is_file())
        self.assertTrue((project.processed / "第一集_local_cover.png").is_file())
        copy_lines = (project.processed / TIKTOK_COPY_NAME).read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(copy_lines), 3)
        self.assertTrue(copy_lines[2].startswith("#fyp "))

    def test_remote_security_rejects_private_llm_and_executable_rendering_fields(self) -> None:
        for url in (
            "http://127.0.0.1:8000/v1/chat/completions",
            "http://10.0.0.2/v1/chat/completions",
            "http://169.254.169.254/latest/meta-data",
        ):
            with self.assertRaises(ValueError):
                validate_public_http_url(url, resolve_dns=False)
        with self.assertRaisesRegex(ValueError, "fish_s2_python"):
            normalize_public_recap_rendering({"fish_s2_python": "C:/malware.exe"})
        with self.assertRaisesRegex(ValueError, "ffprobe"):
            normalize_public_recap_rendering({"ffprobe": "C:/malware.exe"})

    def test_job_upload_declaration_respects_server_capacity_limits(self) -> None:
        response = self.client.post(
            "/api/v1/jobs",
            headers=self.headers,
            json={
                "series_name": "oversized",
                "files": [{"name": "episode.mp4", "size": self.settings.maximum_file_size + 1}],
                "settings": {"pipeline": {"enable_subtitles": False}},
            },
        )
        self.assertEqual(response.status_code, 413, response.text)

    def test_pending_upload_bytes_reserve_declared_account_capacity(self) -> None:
        first, data = self.create_job(b"pending-capacity")
        self.assertEqual(self.database.pending_upload_bytes(self.database.get_job(first["id"])["access_key_id"]), len(data))
        self.upload_all(first, data)
        self.assertEqual(self.database.pending_upload_bytes(self.database.get_job(first["id"])["access_key_id"]), 0)

    def test_orphan_process_is_only_terminated_when_identity_matches(self) -> None:
        process = mock.Mock()
        process.create_time.return_value = 1234.0
        process.exe.return_value = sys.executable
        record = {
            "process_pid": 4321,
            "process_started_at_epoch": 1234.0,
            "process_executable": sys.executable,
        }
        with mock.patch("psutil.Process", return_value=process), mock.patch.object(
            GatewayWorker, "_terminate_pid_tree"
        ) as terminate:
            self.assertTrue(GatewayWorker._terminate_recorded_process(record))
            terminate.assert_called_once_with(4321)
            record["process_started_at_epoch"] = 1000.0
            self.assertFalse(GatewayWorker._terminate_recorded_process(record))
            terminate.assert_called_once()

    def test_remote_job_response_does_not_expose_host_paths(self) -> None:
        job, _data = self.create_job(b"1234")
        internal = self.database.get_job(job["id"])
        leaked = str(self.settings.service_root / "jobs" / "secret.txt")
        self.database.set_job_status(job["id"], "failed", error=f"failed at {leaked}")
        self.database.add_event(job["id"], f"encoder failed at {leaked}", data={"path": leaked})
        job = self.client.get(f"/api/v1/jobs/{job['id']}", headers=self.headers).json()
        serialized = json.dumps(job["result"], ensure_ascii=False)
        self.assertNotIn(str(self.settings.storage_root), serialized)
        self.assertNotIn("work_directory", serialized)
        self.assertEqual(job["result"]["runtime"]["logs"], "logs")
        self.assertNotIn(leaked, str(job.get("error")))
        events = self.client.get(f"/api/v1/jobs/{job['id']}/events", headers=self.headers).json()
        self.assertNotIn(leaked, json.dumps(events, ensure_ascii=False))
        self.assertEqual(internal["id"], job["id"])

    def test_chunked_upload_queue_and_project_result_layout(self) -> None:
        job, data = self.create_job()
        self.assertIn("Agent会话令牌：agent_session_", job["agent_bootstrap"]["command"])
        self.upload_all(job, data)
        queued = self.client.post(f"/api/v1/jobs/{job['id']}/start", headers=self.headers)
        self.assertEqual(queued.status_code, 200, queued.text)
        current = self.client.get(f"/api/v1/jobs/{job['id']}", headers=self.headers).json()
        self.assertEqual(current["status"], "queued")
        self.assertEqual(current["result"]["videos"], "processed")
        self.assertEqual(current["result"]["subtitles"], "字幕终稿")
        self.assertIn("测试剧_", current["result"]["record_name"])
        source = Path(self.database.get_job(job["id"])["work_directory"]) / "input" / "第一集.mp4"
        self.assertEqual(source.read_bytes(), data)

    def test_editable_task_name_does_not_change_selected_project_root(self) -> None:
        response = self.client.post(
            "/api/v1/jobs",
            headers=self.headers,
            json={
                "series_name": "用户修改后的任务名",
                "project_name": "The Mafia's Captive",
                "files": [{"name": "episode_01.mp4", "size": 4}],
                "settings": {"pipeline": {"enable_subtitles": False}},
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        job = response.json()
        self.assertEqual(job["series_name"], "用户修改后的任务名")
        self.assertEqual(job["result"]["project_root"], "project")
        internal_root = Path(self.database.get_job(job["id"])["work_directory"])
        self.assertEqual(internal_root.parents[2].name, "The Mafia's Captive")
        self.assertIn("用户修改后的任务名_", job["result"]["record_name"])

    def test_publish_results_archives_previous_owned_outputs(self) -> None:
        first_job = {
            "id": "job-first12345678",
            "series_name": "发布测试",
            "version": 1,
            "created_at": "2026-07-24T10:00:00+08:00",
            "completed_at": "2026-07-24T10:05:00+08:00",
        }
        first = self.client.app.state.storage.paths("发布测试", 1, first_job["id"]).ensure()
        (first.videos / "第一集_local.mp4").write_bytes(b"old-video")
        (first.subtitles / "第一集.final.en.srt").write_text("old subtitle", encoding="utf-8")
        (first.logs / "process.log").write_text("old log", encoding="utf-8")
        first_publication = self.client.app.state.storage.publish_results(first_job, first)
        project_root = Path(first_publication["directories"]["project_root"])
        first_record = Path(first_publication["directories"]["record"])
        self.assertEqual((project_root / "processed" / "第一集_local.mp4").read_bytes(), b"old-video")
        self.assertEqual(
            (project_root / "字幕终稿" / "第一集.final.en.srt").read_text(encoding="utf-8"),
            "old subtitle",
        )
        self.assertEqual((first_record / "logs" / "process.log").read_text(encoding="utf-8"), "old log")

        second_job = {
            "id": "job-second87654321",
            "series_name": "发布测试",
            "version": 2,
            "created_at": "2026-07-24T11:00:00+08:00",
            "completed_at": "2026-07-24T11:05:00+08:00",
        }
        second = self.client.app.state.storage.paths("发布测试", 2, second_job["id"]).ensure()
        (second.videos / "第一集_local.mp4").write_bytes(b"new-video")
        (second.subtitles / "第一集.final.en.srt").write_text("new subtitle", encoding="utf-8")
        second_publication = self.client.app.state.storage.publish_results(second_job, second)
        self.assertEqual((project_root / "processed" / "第一集_local.mp4").read_bytes(), b"new-video")
        self.assertEqual(
            (first_record / "processed" / "第一集_local.mp4").read_bytes(),
            b"old-video",
        )
        self.assertEqual(
            (first_record / "字幕终稿" / "第一集.final.en.srt").read_text(encoding="utf-8"),
            "old subtitle",
        )
        second_manifest = json.loads(
            (Path(second_publication["directories"]["record"]) / "manifest.json").read_text(encoding="utf-8")
        )
        self.assertEqual(second_manifest["job_id"], second_job["id"])
        self.assertEqual(second_manifest["job_name"], "发布测试")

    def test_publish_results_never_overwrites_untracked_project_file(self) -> None:
        job = {
            "id": "job-collision12345678",
            "series_name": "冲突测试",
            "version": 1,
            "created_at": "2026-07-24T12:00:00+08:00",
        }
        paths = self.client.app.state.storage.paths("冲突测试", 1, job["id"]).ensure()
        project = self.client.app.state.storage.project_paths(paths)
        (project.processed / "第一集_local.mp4").write_bytes(b"user-file")
        (paths.videos / "第一集_local.mp4").write_bytes(b"generated")
        publication = self.client.app.state.storage.publish_results(job, paths, categories=("videos",))
        self.assertEqual((project.processed / "第一集_local.mp4").read_bytes(), b"user-file")
        published_name = publication["published_files"]["videos"][0]
        self.assertNotEqual(published_name, "第一集_local.mp4")
        self.assertEqual((project.processed / published_name).read_bytes(), b"generated")

    def test_publish_recap_results_to_project_recap_folder(self) -> None:
        job = {
            "id": "job-recap12345678",
            "series_name": "解说发布测试",
            "version": 1,
            "created_at": "2026-07-24T13:00:00+08:00",
        }
        paths = self.client.app.state.storage.paths("解说工程", 1, job["id"]).ensure()
        recap = paths.videos / "recap" / "v0001"
        cache = paths.videos / "recap" / ".recap_cache"
        recap.mkdir(parents=True)
        cache.mkdir(parents=True)
        (recap / "final.mp4").write_bytes(b"recap-video")
        (cache / "temporary.bin").write_bytes(b"cache")
        publication = self.client.app.state.storage.publish_results(job, paths, categories=("recap",))
        project = Path(publication["directories"]["project_root"])
        self.assertEqual((project / "解说" / "v0001" / "final.mp4").read_bytes(), b"recap-video")
        self.assertFalse((project / "解说" / ".recap_cache").exists())

    def test_recap_project_and_segment_versioning_api(self) -> None:
        job, data = self.create_job()
        self.upload_all(job, data)
        created = self.client.post(
            f"/api/v1/jobs/{job['id']}/recap",
            headers=self.headers,
            json={
                "project_name": "解说测试",
                "voice_id": "calm_female",
                "target_duration_seconds": 450,
                "narration_preset": "fast",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["project"]["current_version"], 1)
        self.assertEqual(created.json()["project"]["narration_target_loudness"], "keep_original")
        self.assertEqual(created.json()["project"]["narration_preset"], "fast")
        self.assertNotIn("source_root", created.json()["project"])
        segment = self.client.post(
            f"/api/v1/jobs/{job['id']}/recap/segments",
            headers=self.headers,
            json={"episode": 1, "source_start": 1.0, "source_end": 3.0, "mode": "narration", "narration_text": "测试"},
        )
        self.assertEqual(segment.status_code, 201, segment.text)
        project = segment.json()["project"]
        self.assertEqual(project["current_version"], 2)
        segment_id = project["segments"][0]["segment_id"]
        updated = self.client.put(
            f"/api/v1/jobs/{job['id']}/recap/segments/{segment_id}",
            headers=self.headers,
            json={"changes": {"narration_text": "修改后的测试"}},
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertEqual(updated.json()["project"]["current_version"], 3)
        deleted = self.client.delete(
            f"/api/v1/jobs/{job['id']}/recap/segments/{segment_id}", headers=self.headers
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json()["project"]["segments"], [])

    def test_recap_agent_builds_and_submits_complete_timeline(self) -> None:
        video_data = b"video"
        subtitle_data = b"1"
        source_subtitle_data = b"3"
        created_job = self.client.post(
            "/api/v1/jobs",
            headers=self.headers,
            json={
                "series_name": "解说 Agent 测试",
                "files": [
                    {
                        "name": "第1集.mp4",
                        "size": len(video_data),
                        "sha256": hashlib.sha256(video_data).hexdigest(),
                        "role": "video",
                    },
                    {
                        "name": "第1集.final.en.srt",
                        "size": len(subtitle_data),
                        "sha256": hashlib.sha256(subtitle_data).hexdigest(),
                        "role": "subtitle_final",
                    },
                    {
                        "name": "第1集.source.zh.srt",
                        "size": len(source_subtitle_data),
                        "sha256": hashlib.sha256(source_subtitle_data).hexdigest(),
                        "role": "subtitle_final",
                    },
                ],
                "settings": {
                    "pipeline": {
                        "enable_subtitles": False,
                        "enable_recap": True,
                        "enable_dedup": False,
                    },
                    "recap": {"target_language": "English"},
                },
            },
        )
        self.assertEqual(created_job.status_code, 201, created_job.text)
        job = created_job.json()
        for upload, data in zip(
            job["uploads"],
            (video_data, subtitle_data, source_subtitle_data),
            strict=True,
        ):
            self.upload_declared(job, upload, data)

        stored_job = self.database.get_job(job["id"])
        paths = JobStorage(self.settings).paths_from_job(stored_job)
        ffmpeg = video_dedup.find_binary("ffmpeg")
        subprocess.run(
            [
                ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=c=black:s=160x120:d=1",
                "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono",
                "-shortest", "-c:v", "mpeg4", "-c:a", "aac",
                str(paths.input / "第1集.mp4"),
            ],
            check=True,
        )

        created_recap = self.client.post(
            f"/api/v1/jobs/{job['id']}/recap",
            headers=self.headers,
            json={
                "project_name": "解说 Agent 测试",
                "target_language": "English",
                "voice_id": "calm_female",
                "tts_engine": "auto",
            },
        )
        self.assertEqual(created_recap.status_code, 201, created_recap.text)
        recap_payload = created_recap.json()["project"]
        # Container/FFmpeg builds may mux the nominal one-second fixture as
        # 1.0-1.152 seconds. Verify the invariant (automatic 50% mode), not a
        # codec-specific duration.
        self.assertGreater(recap_payload["target_duration_seconds"], 0)
        self.assertEqual(recap_payload["rendering"]["target_duration_ratio"], 0.5)
        self.assertEqual(recap_payload["narration_preset"], "standard")
        self.assertEqual(created_recap.json()["planning"]["status"], "pending")

        bootstrap = job["agent_bootstrap"]
        agent_headers = {"Authorization": f"Bearer {bootstrap['agent_token']}"}
        probe = self.client.post(
            bootstrap["capability_probe_url"].replace(
                "https://video.example.test", ""
            ),
            headers=agent_headers,
        )
        self.assertEqual(probe.status_code, 200, probe.text)
        probe_nonce = probe.json()["probe_nonce"]
        verified = self.client.post(
            bootstrap["capability_verify_url"].replace(
                "https://video.example.test", ""
            ),
            headers=agent_headers,
            json={
                "probe_nonce": probe_nonce,
                "capabilities": {
                    "native_subagents": True,
                    "context_isolation": True,
                    "max_child_agents": 3,
                    "probe_role": "capability_probe",
                    "child_agent_run_id": "probe-child-001",
                    "isolated_context_id": "probe-context-001",
                    "probe_result": f"{probe_nonce} capability_probe",
                },
            },
        )
        self.assertEqual(verified.status_code, 200, verified.text)
        claimed = self.client.post(
            bootstrap["listen_url"].replace("https://video.example.test", ""),
            headers=agent_headers,
        )
        self.assertEqual(claimed.status_code, 200, claimed.text)
        event = claimed.json()
        self.assertEqual(event["event"], "RECAP_JOB")
        self.assertEqual(event["planning_attempt"], 1)
        self.assertTrue(event["planning_attempt_id"])
        self.assertIn(
            f"planning_attempt_id={event['planning_attempt_id']}",
            event["heartbeat_endpoint"],
        )
        self.assertEqual(event["request"]["narration_preset"], "standard")
        self.assertEqual(event["request"]["duration_budget"]["speech_rate"]["unit"], "wps")
        self.assertTrue(event["execution_contract"]["atomic"])
        self.assertTrue(event["execution_contract"]["continue_without_user_prompt"])
        self.assertIn("submit_confirmed", event["execution_contract"]["terminal_outcomes"])
        self.assertEqual(
            event["completion_contract"]["expected_episode_indexes"],
            [1],
        )
        self.assertEqual(
            set(event["completion_contract"]["required_artifacts"]),
            {
                "subtitles/第1集.final.en.srt",
                "subtitles/第1集.source.zh.srt",
            },
        )
        self.assertTrue(event["completion_contract"]["hardcoded_counts_forbidden"])
        self.assertEqual(event["request"]["episodes"][0]["subtitle_artifact"], "subtitles/第1集.final.en.srt")
        subtitle_assets = event["request"]["episodes"][0]["subtitle_assets"]
        self.assertEqual(subtitle_assets["primary"]["language_code"], "en")
        self.assertEqual(
            subtitle_assets["primary"]["asset_kind"],
            "translation_final",
        )
        self.assertEqual(
            subtitle_assets["sources"][0]["artifact"],
            "subtitles/第1集.source.zh.srt",
        )
        self.assertEqual(
            [asset["language_code"] for asset in subtitle_assets["translations"]],
            ["en"],
        )
        self.assertNotIn(
            "subtitles/第1集.final.ar.srt",
            event["request"]["subtitle_artifacts"],
        )
        rules = self.client.get(
            event["rules_endpoint"].replace("https://video.example.test", ""),
            headers=agent_headers,
        )
        self.assertEqual(rules.status_code, 200, rules.text)
        self.assertIn("00_COMMON_CONTRACT.md", rules.text)
        self.assertIn("01_COORDINATOR.md", rules.text)
        self.assertNotIn("RECAP_ENGINE_IMPLEMENTATION.md", rules.text)
        for artifact in event["completion_contract"]["required_artifacts"]:
            fetched = self.client.get(
                event["artifacts_endpoint"].replace(
                    "https://video.example.test", ""
                ) + "/" + artifact,
                headers=agent_headers,
            )
            self.assertEqual(fetched.status_code, 200, fetched.text)

        materials = {
            "story_bible.md": "# Story",
            "subtitle_evidence.md": "# Evidence",
            "beat_sheet.md": "# Beats",
            "hook_candidates.md": "# Hooks",
            "timeline_notes.md": "# Timeline",
            "revision_log.md": "# Revisions",
        }
        segment_payload = [{
            "segment_id": "seg-001",
            "episode": 1,
            "source_start": 0.0,
            "source_end": 0.5,
            "mode": "original",
            "narration_text": "",
            "purpose": "hook",
            "rendering": {},
        }]
        stage_token_value = ""
        stage_payloads = [
            {
                "schema_version": 1,
                "stage": "story_analysis",
                "previous_stage_token": "",
                "execution": {
                    "role": "story_analyst",
                    "agent_run_id": "story-agent-001",
                    "context_isolated": False,
                },
                "episode_indexes": [1],
                "story_bible": "完整剧情分析。" * 50,
                "subtitle_evidence": "第1集字幕证据与时间点。" * 50,
            },
            {
                "schema_version": 1,
                "stage": "recap_draft",
                "execution": {
                    "role": "recap_writer",
                    "agent_run_id": "writer-agent-001",
                    "context_isolated": False,
                },
                "beat_sheet": "剧情节拍与因果。" * 30,
                "hook_candidates": "候选钩子及其证据。" * 30,
                "timeline_notes": "时间轴选择说明。" * 30,
                "segments": segment_payload,
            },
            {
                "schema_version": 1,
                "stage": "independent_review",
                "execution": {
                    "role": "independent_reviewer",
                    "agent_run_id": "review-agent-001",
                    "context_isolated": True,
                },
                "score": 90,
                "verdict": "pass",
                "issues": [],
                "review_summary": "独立审核确认剧情、证据、时间轴与目标语言均符合要求。" * 3,
            },
            {
                "schema_version": 1,
                "stage": "final_revision",
                "execution": {
                    "role": "reviser",
                    "agent_run_id": "reviser-agent-001",
                    "context_isolated": False,
                },
                "segments": segment_payload,
                "resolved_issue_ids": [],
                "revision_log": "已依据独立审核逐项检查，本次没有阻断问题，保留已验证片段。" * 2,
            },
            {
                "schema_version": 1,
                "stage": "final_verification",
                "execution": {
                    "role": "final_verifier",
                    "agent_run_id": "final-review-agent-001",
                    "context_isolated": True,
                },
                "score": 92,
                "verdict": "pass",
                "issues": [],
                "review_summary": "最终隔离审核确认修订版可提交，未发现严重或主要问题。" * 3,
            },
        ]
        for stage_payload in stage_payloads:
            stage_payload["planning_attempt_id"] = event["planning_attempt_id"]
            stage_payload["previous_stage_token"] = stage_token_value
            role_url = event["role_rules_endpoint_template"].replace(
                "https://video.example.test", ""
            ).replace("{stage}", stage_payload["stage"])
            role_response = self.client.get(role_url, headers=agent_headers)
            self.assertEqual(role_response.status_code, 200, role_response.text)
            if stage_payload["stage"] == "final_verification":
                self.assertIn('"review_target"', role_response.text)
                self.assertIn(stage_token_value, role_response.text)
                stage_payload["reviewed_stage_token"] = stage_token_value
                artifact_url = (
                    event["artifacts_endpoint"]
                    + "/agent/recap-stages/04-final_revision.json"
                ).replace("https://video.example.test", "")
                artifact_response = self.client.get(
                    artifact_url,
                    headers={
                        **agent_headers,
                        "X-Agent-Run-ID": stage_payload["execution"]["agent_run_id"],
                    },
                )
                self.assertEqual(artifact_response.status_code, 200, artifact_response.text)
            stage_url = event["stage_endpoint_template"].replace(
                "https://video.example.test", ""
            ).replace("{stage}", stage_payload["stage"])
            stage_response = self.client.post(
                stage_url,
                headers=agent_headers,
                json={"payload": stage_payload},
            )
            self.assertEqual(stage_response.status_code, 200, stage_response.text)
            stage_token_value = stage_response.json()["stage_token"]
        submitted = self.client.post(
            event["submit_endpoint"].replace("https://video.example.test", ""),
            headers=agent_headers,
            json={
                "response": {
                    "schema_version": 1,
                    "task_type": "recap_plan",
                    "project_id": event["request"]["project_id"],
                    "planning_attempt_id": event["planning_attempt_id"],
                    "segments": segment_payload,
                    "creative_materials": materials,
                    "quality": {"score": 90, "summary": "checked", "known_risks": []},
                },
            },
        )
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.assertEqual(submitted.json()["planning"]["status"], "completed")
        self.assertEqual(
            submitted.json()["planning"]["verified_input_manifest"][
                "fetched_episode_indexes"
            ],
            [1],
        )
        self.assertEqual(submitted.json()["project"]["segments"][0]["segment_id"], "seg-001")
        self.assertEqual(
            (paths.agent / "recap-creative" / "story_bible.md").read_text(
                encoding="utf-8"
            ),
            stage_payloads[0]["story_bible"],
        )
        self.assertEqual(
            (paths.agent / "recap-creative" / "revision_log.md").read_text(
                encoding="utf-8"
            ),
            stage_payloads[3]["revision_log"],
        )

        retried = self.client.post(
            f"/api/v1/jobs/{job['id']}/recap/agent/queue",
            headers=self.headers,
        )
        self.assertEqual(retried.status_code, 202, retried.text)
        retry_state = retried.json()["planning"]
        self.assertEqual(retry_state["planning_attempt"], 2)
        self.assertNotEqual(
            retry_state["planning_attempt_id"], event["planning_attempt_id"]
        )
        checkpoint = WorkflowCheckpointStore(paths).read()["stages"][
            RECAP_PLANNING_STAGE
        ]
        self.assertEqual(checkpoint["status"], "invalidated")
        stale_stage = self.client.post(
            event["stage_endpoint_template"].replace(
                "https://video.example.test", ""
            ).replace("{stage}", "story_analysis"),
            headers=agent_headers,
            json={"payload": stage_payloads[0]},
        )
        self.assertEqual(stale_stage.status_code, 422, stale_stage.text)
        self.assertIn("planning_attempt_id", stale_stage.text)

    def test_recap_completion_manifest_uses_dynamic_sets(self) -> None:
        request = {
            "completion_contract": {
                "rules_bundle_required": True,
                "expected_episode_indexes": [2, 7],
                "required_artifacts": [
                    "subtitles/episode-2.source.srt",
                    "subtitles/episode-7.final.srt",
                    "subtitles/episode-7.source.srt",
                ],
            },
        }
        incomplete = {
            "rules_fetched_at": "2026-07-25T12:00:00+08:00",
            "fetched_episode_indexes": [2],
            "fetched_artifacts": ["subtitles/episode-2.source.srt"],
        }
        with self.assertRaisesRegex(ValueError, "episode-7.final.srt"):
            RecapService._validate_completion_manifest(request, incomplete)
        RecapService._validate_completion_manifest(
            request,
            {
                "rules_fetched_at": "2026-07-25T12:00:00+08:00",
                "fetched_episode_indexes": [2, 7],
                "fetched_artifacts": request["completion_contract"]["required_artifacts"],
            },
        )

    def test_arabic_recap_rejects_qwen_at_web_boundary(self) -> None:
        job, data = self.create_job()
        self.upload_all(job, data)
        response = self.client.post(
            f"/api/v1/jobs/{job['id']}/recap",
            headers=self.headers,
            json={
                "project_name": "Arabic route",
                "target_language": "Arabic",
                "target_duration_seconds": 10,
                "voice_id": "ar_calm_male",
                "tts_engine": "qwen3_tts",
            },
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("Qwen3 TTS 不支持阿拉伯语", response.json()["detail"])

    def test_web_ui_folder_glossary_and_preview_contract(self) -> None:
        static = Path(__file__).resolve().parent / "web_gateway" / "static"
        html = (static / "index.html").read_text(encoding="utf-8")
        live_html = self.client.get("/")
        self.assertEqual(live_html.status_code, 200)
        self.assertIn("no-store", live_html.headers.get("cache-control", ""))
        self.assertIn('content="20260810-01"', live_html.text)
        self.assertIn('/static/app.js?v=20260810-01', live_html.text)
        self.assertNotIn("__WEB_BUILD_VERSION__", live_html.text)
        health = self.client.get("/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json()["build_version"], "20260810-01")
        self.assertIn("no-store", health.headers.get("cache-control", ""))
        script = "\n".join(
            path.read_text(encoding="utf-8")
            for path in [*sorted((static / "js").glob("*.js")), static / "app.js"]
        )
        self.assertIn('id="folderFiles"', html)
        self.assertIn("webkitdirectory", html)
        self.assertIn('id="chooseOutputFolder"', html)
        self.assertIn('id="chooseSourceFolder"', html)
        self.assertIn('id="autoSaveOutputs"', html)
        self.assertIn("function isDirectFolderVideo", script)
        self.assertIn("parts.length===2", script)
        self.assertIn('id="recapPreviewCanvas"', html)
        self.assertIn('id="previewVideo" class="preview-source-video"', html)
        self.assertIn('id="recapPreviewVideo" class="preview-source-video"', html)
        self.assertIn("function capturePreviewFrame", script)
        self.assertIn("function captureRecapFrame", script)
        self.assertIn("'recapCaptionPreviewPanel').addEventListener('toggle'", script)
        self.assertIn("function refreshRecapEngines", script)
        self.assertIn('id="recapQueueAgent"', html)
        self.assertIn("function pollRecapPlanning", script)
        self.assertIn("recapProjectJobId", script)
        self.assertIn("await loadRecap(jobId)", script)
        self.assertIn("current.settings?.pipeline?.enable_recap", script)
        self.assertIn("await loadRecap(id)", script)
        self.assertIn("AbortSignal.timeout(120000)", script)
        self.assertIn("uploadChunkMaximumAttempts=5", script)
        self.assertIn("queue.push(task)", script)
        self.assertIn("已排到队尾重试", script)
        self.assertIn("completeUploadWithRetry", script)
        self.assertNotIn("async function uploadOne", script)
        self.assertIn("检测到素材已全部上传，正在续建解说项目", script)
        voices = self.client.get("/api/v1/recap/voices", headers=self.headers)
        self.assertEqual(voices.status_code, 200, voices.text)
        payload = voices.json()
        self.assertEqual(len(payload["voices"]), 28)
        self.assertNotIn(
            "qwen3_tts",
            {item["value"] for item in payload["engines_by_language"]["Arabic"]},
        )
        self.assertIn("window.showDirectoryPicker", script)
        self.assertIn("for await(const entry of handle.values())", script)
        self.assertIn("function activeSubtitleFiles", script)
        self.assertIn("每集最多原文+目标译文", script)
        self.assertIn("!/__[^.]*\\.srt$/i.test(entry.name)", script)
        self.assertNotIn("/\\.(srt|json)$/i.test(entry.name)", script)
        self.assertIn("if(!outputDirectoryHandle)throw new Error('请选择成片保存文件夹')", script)
        self.assertIn("saveVideosToFolder", script)
        self.assertIn("readSubtitleFinals", script)
        self.assertIn("getDirectoryHandle('processed',{create:true})", script)
        self.assertIn("字幕终稿/", script)
        self.assertIn("任务记录/", script)
        self.assertIn("current?.result?.record_name", script)
        self.assertIn("解说/", script)
        self.assertIn("videos/recap/", script)
        self.assertIn('id="refreshGlossaries"', html)
        self.assertIn("function selectedVideoFiles()", script)
        self.assertIn("function inferredSeriesName(files)", script)
        self.assertIn("glossary:'glossary_name'", script)
        self.assertIn("layout==='replace'&&value('subtitleCover')", script)
        self.assertNotIn("Original subtitle", script)
        self.assertNotIn("pendingRecapJobId", script)
        self.assertIn("return{pipeline,video_config,recap:recapProjectBody()}", script)
        self.assertIn("job.status==='waiting_recap_agent'", script)
        self.assertIn("job.status==='recap_ready'", script)
        self.assertIn('id="runPreflight"', html)
        self.assertIn('id="refreshStorage"', html)
        self.assertIn('id="resume"', html)
        self.assertIn('data-tab="dedup"', html)
        self.assertIn('class="dedup-section"', html)
        self.assertNotIn('data-tab="picture"', html)
        self.assertIn("__WEB_BUILD_VERSION__", html)
        self.assertIn("versionRefreshNotice", html)
        self.assertIn("meta[name=\"app-build\"]", script)
        self.assertIn("setInterval(checkBackendVersion,60000)", script)
        self.assertIn("网页脚本版本不一致", script)
        self.assertIn("from the last", script.replace("从最后完成的阶段检查点", "from the last"))

    def test_server_rejects_archived_json_duplicate_and_wrong_language_subtitles(self) -> None:
        base = {
            "series_name": "字幕上传规则",
            "files": [{"name": "第一集.mp4", "size": 5, "role": "video"}],
            "settings": {
                "pipeline": {
                    "enable_subtitles": False,
                    "enable_recap": True,
                },
                "recap": {"target_language": "Arabic"},
            },
        }
        invalid_names = [
            "manifest.json",
            "第一集.mp4.final.ar__oldjob.srt",
            "第一集.mp4.final.en.srt",
            "其他集.mp4.final.ar.srt",
        ]
        for name in invalid_names:
            body = {**base, "files": [*base["files"], {"name": name, "size": 5, "role": "subtitle_final"}]}
            response = self.client.post("/api/v1/jobs", headers=self.headers, json=body)
            self.assertEqual(response.status_code, 422, (name, response.text))

        duplicate = {
            **base,
            "files": [
                *base["files"],
                {"name": "第一集.mp4.source.en.srt", "size": 5, "role": "subtitle_final"},
                {"name": "第一集.source.zh.srt", "size": 5, "role": "subtitle_final"},
            ],
        }
        response = self.client.post("/api/v1/jobs", headers=self.headers, json=duplicate)
        self.assertEqual(response.status_code, 422, response.text)

    def test_server_accepts_one_source_and_current_target_per_video(self) -> None:
        response = self.client.post(
            "/api/v1/jobs",
            headers=self.headers,
            json={
                "series_name": "活动字幕",
                "files": [
                    {"name": "第一集.mp4", "size": 5, "role": "video"},
                    {"name": "第一集.mp4.source.en.srt", "size": 5, "role": "subtitle_final"},
                    {"name": "第一集.mp4.final.ar.srt", "size": 5, "role": "subtitle_final"},
                ],
                "settings": {
                    "pipeline": {"enable_subtitles": False, "enable_recap": True},
                    "recap": {"target_language": "Arabic"},
                },
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(
            [item["name"] for item in response.json()["uploads"]],
            ["第一集.mp4", "第一集.mp4.source.en.srt", "第一集.mp4.final.ar.srt"],
        )

    def test_batch_upload_order_matches_browser_file_order(self) -> None:
        response = self.client.post(
            "/api/v1/jobs",
            headers=self.headers,
            json={
                "series_name": "顺序测试",
                "files": [
                    {"name": "第十集.mp4", "size": 5},
                    {"name": "第一集.mp4", "size": 7},
                    {"name": "第二集.mp4", "size": 9},
                ],
                "settings": {},
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(
            [item["name"] for item in response.json()["uploads"]],
            ["第十集.mp4", "第一集.mp4", "第二集.mp4"],
        )

    def test_access_key_isolation_and_path_traversal(self) -> None:
        job, _data = self.create_job()
        other_key, _record = self.database.create_access_key("other")
        self.assertEqual(
            self.client.get(f"/api/v1/jobs/{job['id']}", headers={"X-API-Key": other_key}).status_code,
            404,
        )
        response = self.client.get(
            f"/api/v1/jobs/{job['id']}/artifacts/logs/%2E%2E/%2E%2E/input/第一集.mp4",
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 404)

    def test_same_project_name_is_isolated_between_access_keys(self) -> None:
        other_key, _record = self.database.create_access_key("other")
        body = {
            "series_name": "同名任务",
            "project_name": "Shared Drama",
            "files": [{"name": "episode.mp4", "size": 4}],
            "settings": {"pipeline": {"enable_subtitles": False, "enable_dedup": True}},
        }
        first = self.client.post("/api/v1/jobs", headers=self.headers, json=body)
        second = self.client.post("/api/v1/jobs", headers={"X-API-Key": other_key}, json=body)
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(second.status_code, 201, second.text)
        first_root = Path(self.database.get_job(first.json()["id"])["work_directory"]).parents[2]
        second_root = Path(self.database.get_job(second.json()["id"])["work_directory"]).parents[2]
        self.assertNotEqual(first_root, second_root)
        self.assertEqual(first_root.name, "Shared Drama")
        self.assertEqual(second_root.name, "Shared Drama")
        self.assertTrue(first_root.parent.name.startswith("test__"))
        self.assertTrue(second_root.parent.name.startswith("other__"))

    def test_interrupted_job_is_paused_and_resumes_from_workflow_checkpoint(self) -> None:
        job, data = self.create_job()
        self.upload_all(job, data)
        stored = self.database.get_job(job["id"])
        paths = self.client.app.state.storage.paths_from_job(stored)
        plan = WorkflowPlan.from_settings(stored["settings"])
        WorkflowCheckpointStore(paths).initialize(plan)
        WorkflowCheckpointStore(paths).complete(SUBTITLE_STAGE, {"subtitle_files": ["episode.srt"]})
        self.database.set_job_status(job["id"], "running", process_pid=98765)

        self.assertEqual(self.database.recover_interrupted_jobs(), 1)
        paused = self.database.get_job(job["id"])
        self.assertEqual(paused["status"], "paused")
        resumed = self.client.post(f"/api/v1/jobs/{job['id']}/resume", headers=self.headers)
        self.assertEqual(resumed.status_code, 200, resumed.text)
        self.assertEqual(resumed.json()["event"], "RESUMED")
        current = self.client.get(f"/api/v1/jobs/{job['id']}", headers=self.headers).json()
        self.assertEqual(current["status"], "queued")
        self.assertIn(SUBTITLE_STAGE, current["workflow"]["completed_stages"])

    def test_cleaned_source_cannot_be_falsely_resumed(self) -> None:
        job, data = self.create_job()
        self.upload_all(job, data)
        stored = self.database.get_job(job["id"])
        paths = self.client.app.state.storage.paths_from_job(stored)
        self.database.set_job_status(job["id"], "failed", error="test failure")
        for upload in self.database.list_uploads(job["id"]):
            self.client.app.state.storage.upload_target(
                paths, upload["stored_name"], upload.get("role", "video")
            ).unlink()

        resumed = self.client.post(f"/api/v1/jobs/{job['id']}/resume", headers=self.headers)
        self.assertEqual(resumed.status_code, 409, resumed.text)
        self.assertIn("重新上传", resumed.json()["detail"]["message"])
        self.assertEqual(self.database.get_job(job["id"])["status"], "failed")

    def test_server_storage_report_and_cleanup_are_scoped_to_access_key(self) -> None:
        first, first_data = self.create_job(b"first-user")
        self.upload_all(first, first_data)
        other_key, other_record = self.database.create_access_key("other")
        other_headers = {"X-API-Key": other_key}
        response = self.client.post(
            "/api/v1/jobs",
            headers=other_headers,
            json={
                "series_name": "其他用户",
                "files": [{"name": "other.mp4", "size": 4}],
                "settings": {"pipeline": {"enable_subtitles": False, "enable_dedup": True}},
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        other_job = response.json()
        first_paths = self.client.app.state.storage.paths_from_job(self.database.get_job(first["id"]))
        other_paths = self.client.app.state.storage.paths_from_job(self.database.get_job(other_job["id"]))
        (first_paths.chunks / "stale").mkdir(parents=True)
        (first_paths.chunks / "stale" / "a.part").write_bytes(b"a" * 17)
        (other_paths.chunks / "keep").mkdir(parents=True)
        (other_paths.chunks / "keep" / "b.part").write_bytes(b"b" * 19)

        report = self.client.get("/api/v1/storage", headers=self.headers)
        self.assertEqual(report.status_code, 200, report.text)
        self.assertEqual(report.json()["owner_id"], self.database.authenticate_access_key(self.key)["id"])
        preview = self.client.post(
            "/api/v1/storage/cleanup",
            headers=self.headers,
            json={"categories": ["chunks"], "older_than_days": 0, "dry_run": True},
        )
        self.assertGreaterEqual(preview.json()["reclaimable_bytes"], 17)
        execute = self.client.post(
            "/api/v1/storage/cleanup",
            headers=self.headers,
            json={"categories": ["chunks"], "older_than_days": 0, "dry_run": False},
        )
        self.assertEqual(execute.status_code, 200, execute.text)
        self.assertFalse((first_paths.chunks / "stale").exists())
        self.assertTrue((other_paths.chunks / "keep" / "b.part").is_file())

    def test_preflight_endpoint_reports_selected_server_environment(self) -> None:
        response = self.client.post(
            "/api/v1/preflight",
            headers=self.headers,
            json={
                "settings": {
                    "pipeline": {
                        "enable_subtitles": False,
                        "enable_recap": False,
                        "enable_dedup": True,
                        "hardware_acceleration": "cpu",
                    }
                }
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertIn("ok", payload)
        self.assertIn("ffmpeg", {item["name"] for item in payload["checks"]})
        self.assertEqual(payload["selected"]["dedup"], True)

    def test_combined_subtitle_then_dedup_runs_as_resumable_modules(self) -> None:
        data = b"video-data"
        response = self.client.post(
            "/api/v1/jobs",
            headers=self.headers,
            json={
                "series_name": "组合流程",
                "files": [{"name": "episode.mp4", "size": len(data)}],
                "settings": {
                    "pipeline": {
                        "enable_subtitles": True,
                        "enable_recap": False,
                        "enable_dedup": True,
                        "translation_backend": "agent",
                    }
                },
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        external = response.json()
        self.upload_all(external, data)
        job = self.database.get_job(external["id"])
        paths = self.client.app.state.storage.paths_from_job(job)
        stages = []

        def complete_stage(_job, _paths, _settings, stage, **_kwargs):
            stages.append(stage)
            if stage == SUBTITLE_STAGE:
                final_dir = paths.input / "字幕终稿"
                final_dir.mkdir(parents=True, exist_ok=True)
                (final_dir / "episode.mp4.final.en.srt").write_text(
                    "1\n00:00:00,000 --> 00:00:01,000\nHello\n",
                    encoding="utf-8",
                )
            elif stage == DEDUP_STAGE:
                (paths.videos / "episode_local.mp4").write_bytes(b"encoded")
            return True

        worker = GatewayWorker(self.settings, self.database, self.client.app.state.storage)
        with mock.patch.object(worker, "_run_batch_process", side_effect=complete_stage):
            worker._run_job(job)
        self.assertEqual(stages, [SUBTITLE_STAGE, DEDUP_STAGE])
        completed = self.database.get_job(job["id"])
        self.assertEqual(completed["status"], "completed")
        summary = WorkflowCheckpointStore(paths).summary(WorkflowPlan.from_settings(job["settings"]))
        self.assertEqual(summary["completed_stages"], [SUBTITLE_STAGE, DEDUP_STAGE])

    def test_cancel_queued_job_does_not_wait_for_worker(self) -> None:
        job, data = self.create_job()
        self.upload_all(job, data)
        self.client.post(f"/api/v1/jobs/{job['id']}/start", headers=self.headers)
        cancelled = self.client.post(f"/api/v1/jobs/{job['id']}/cancel", headers=self.headers)
        self.assertEqual(cancelled.json()["event"], "CANCELLED")
        current = self.client.get(f"/api/v1/jobs/{job['id']}", headers=self.headers).json()
        self.assertEqual(current["status"], "cancelled")

    def test_remote_agent_receives_same_request_payload(self) -> None:
        job_response, _data = self.create_job()
        job = self.database.get_job(job_response["id"])
        storage = JobStorage(self.settings)
        paths = storage.paths_from_job(job)
        remote = RemoteAgentBridge(self.database, storage, self.settings.project_root)
        session = remote.initialize(job, paths, maximum_parallel=3)
        payload = {
            "task_type": "folder_subtitle_translation",
            "title": "测试剧",
            "target_language": "Arabic",
            "translation_quality": "fast",
            "max_parallel": 3,
            "expected_episode_indexes": [],
            "episodes": [],
        }
        _job_dir, request = agent_bridge.create_job(Path(session["bridge_root"]), payload)
        token = job_response["agent_bootstrap"]["agent_token"]
        response = self.client.post(
            f"/api/v1/agent/jobs/{job['id']}/listen",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        event = response.json()
        self.assertEqual(event["event"], "JOB")
        self.assertEqual(event["request"]["job_id"], request["job_id"])
        self.assertEqual(event["request"]["episodes"], request["episodes"])
        self.assertEqual(event["request"]["status"], "claimed")
        self.assertEqual(event["max_parallel"], 3)
        self.assertTrue(event["heartbeat_endpoint"].startswith("https://video.example.test/"))

    def test_worker_concurrency_routes_by_subtitle_usage(self) -> None:
        storage = JobStorage(self.settings)
        worker = GatewayWorker(self.settings, self.database, storage)
        for enabled, expected in ((True, "3"), (False, "5")):
            job_response, _data = self.create_job(data=b"video" + bytes([enabled]))
            job = self.database.get_job(job_response["id"])
            job["settings"]["pipeline"]["enable_subtitles"] = enabled
            command = worker._command(job, storage.paths_from_job(job), None)
            self.assertEqual(command[command.index("--video-workers") + 1], expected)

    def test_worker_adds_subtitle_only_when_dedup_is_disabled(self) -> None:
        storage = JobStorage(self.settings)
        worker = GatewayWorker(self.settings, self.database, storage)
        job_response, _data = self.create_job()
        job = self.database.get_job(job_response["id"])
        job["settings"]["pipeline"]["enable_subtitles"] = True
        job["settings"]["pipeline"]["enable_dedup"] = False
        command = worker._command(job, storage.paths_from_job(job), None)
        self.assertIn("--subtitle-only", command)

    def test_combined_subtitle_and_recap_settings_survive_normalization(self) -> None:
        normalized = normalize_settings(
            {
                "pipeline": {
                    "enable_subtitles": True,
                    "enable_recap": True,
                    "enable_dedup": False,
                },
                "recap": {
                    "project_name": "组合任务",
                    "target_language": "English",
                    "target_duration_seconds": 300,
                    "voice_id": "calm_female",
                    "rendering": {"crf": 23, "caption_y_percent": 14},
                },
            }
        )
        self.assertTrue(normalized["pipeline"]["enable_subtitles"])
        self.assertTrue(normalized["pipeline"]["enable_recap"])
        self.assertFalse(normalized["pipeline"]["enable_dedup"])
        self.assertEqual(normalized["recap"]["project_name"], "组合任务")
        self.assertEqual(normalized["recap"]["target_duration_seconds"], 300)
        self.assertEqual(normalized["recap"]["narration_preset"], "standard")
        self.assertEqual(normalized["recap"]["rendering"]["caption_y_percent"], 14)

    def test_recap_rejects_truncated_or_corrupt_agent_timeline(self) -> None:
        service = RecapService(JobStorage(self.settings))
        project = RecapProject(
            schema_version=1,
            project_id="recap-validation",
            project_name="全集完整性校验",
            source_root=str(self.settings.storage_root),
            episode_pattern="*.mp4",
            subtitle_root=str(self.settings.storage_root),
            output_root=str(self.settings.storage_root),
            target_language="Arabic",
            target_duration_seconds=500,
            voice_id="ar_fish_female",
            tts_engine="fish_s2",
            narration_speed=1.0,
            narration_target_loudness="keep_original",
            segments=[],
            current_version=1,
            created_at="2026-07-24T00:00:00+08:00",
            updated_at="2026-07-24T00:00:00+08:00",
        )
        bad = RecapSegment(
            segment_id="only-one",
            episode=1,
            source_start=0,
            source_end=12,
            mode="narration",
            narration_text="???? ????",
            purpose="hook",
        )
        with self.assertRaisesRegex(ValueError, "明显不完整"):
            service._validate_agent_timeline(project, [bad], {"episode_count": 10})

        project.target_duration_seconds = 10
        with self.assertRaisesRegex(ValueError, "乱码或问号占位"):
            service._validate_agent_timeline(project, [bad], {"episode_count": 1})

        project.narration_preset = "standard"
        underfilled = RecapSegment(
            segment_id="underfilled",
            episode=1,
            source_start=0,
            source_end=10,
            mode="narration",
            narration_text="قرار واحد غيّر كل شيء",
            purpose="hook",
        )
        with self.assertRaisesRegex(ValueError, "文案与画面时长不匹配"):
            service._validate_agent_timeline(
                project, [underfilled], {"episode_count": 1}
            )

    def test_remote_agent_submit_uses_local_quality_and_index_validation(self) -> None:
        job_response, _data = self.create_job()
        job = self.database.get_job(job_response["id"])
        storage = JobStorage(self.settings)
        paths = storage.paths_from_job(job)
        remote = RemoteAgentBridge(self.database, storage, self.settings.project_root)
        session = remote.initialize(job, paths, maximum_parallel=3)
        item = {"index": 1, "start": "00:00:01,000", "end": "00:00:02,000", "visual_text": "Hello", "audio_text": "Hello"}
        payload = {
            "task_type": "folder_subtitle_translation",
            "title": "测试剧",
            "target_language": "Arabic",
            "translation_quality": "fast",
            "quality_policy": {"minimum_score": 8.5},
            "expected_episode_indexes": [1],
            "episodes": [{"index": 1, "expected_subtitle_indexes": [1], "items": [item]}],
        }
        _job_dir, request_payload = agent_bridge.create_job(Path(session["bridge_root"]), payload)
        token = job_response["agent_bootstrap"]["agent_token"]
        headers = {"Authorization": f"Bearer {token}"}
        claimed = self.client.post(f"/api/v1/agent/jobs/{job['id']}/listen", headers=headers).json()
        dimensions = {name: 9.0 for name in agent_bridge.EPISODE_QUALITY_DIMENSIONS}
        series_dimensions = {name: 9.0 for name in agent_bridge.SERIES_QUALITY_DIMENSIONS}
        response_payload = {
            "job_id": claimed["job_id"],
            "status": "completed",
            "generation": claimed["request"]["generation"],
            "cancel_epoch": claimed["request"]["cancel_epoch"],
            "target_language": "Arabic",
            "translation_quality": "fast",
            "episodes": [{
                "index": 1,
                "subtitles": [{"index": 1, "start": item["start"], "end": item["end"], "text": "مرحبا"}],
                "review": {"quality_score": 9.0, "quality_checks": dimensions},
            }],
            "series_entities": [],
            "series_review": {"quality_score": 9.0, "quality_checks": series_dimensions},
        }
        submitted = self.client.post(
            f"/api/v1/agent/jobs/{job['id']}/submit",
            headers=headers,
            json={"internal_job_id": claimed["job_id"], "response": response_payload},
        )
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.assertEqual(submitted.json()["event"], "SUBMITTED")
        archived = paths.agent / "jobs" / claimed["job_id"] / "response.json"
        self.assertTrue(archived.is_file())

    def test_account_agent_session_persists_and_rotation_invalidates_old_token(self) -> None:
        first = self.client.get("/api/v1/agent-session", headers=self.headers)
        self.assertEqual(first.status_code, 200, first.text)
        first_payload = first.json()
        again = self.client.get("/api/v1/agent-session", headers=self.headers).json()
        self.assertEqual(again["agent_token"], first_payload["agent_token"])
        rotated = self.client.post("/api/v1/agent-session/rotate", headers=self.headers).json()
        self.assertNotEqual(rotated["agent_token"], first_payload["agent_token"])
        old_rules = self.client.get(
            first_payload["rules_url"].replace("https://video.example.test", ""),
            headers={"Authorization": f"Bearer {first_payload['agent_token']}"},
        )
        self.assertEqual(old_rules.status_code, 401)
        new_rules = self.client.get(
            rotated["rules_url"].replace("https://video.example.test", ""),
            headers={"Authorization": f"Bearer {rotated['agent_token']}"},
        )
        self.assertEqual(new_rules.status_code, 200)

    def test_agent_session_status_heartbeat_and_stop(self) -> None:
        bootstrap = self.client.get("/api/v1/agent-session", headers=self.headers).json()
        before = self.client.get("/api/v1/agent-session/status", headers=self.headers).json()
        self.assertTrue(before["initialized"])
        self.assertFalse(before["connected"])
        listen_path = bootstrap["listen_url"].replace("https://video.example.test", "")
        listened = self.client.post(
            listen_path, headers={"Authorization": f"Bearer {bootstrap['agent_token']}"}
        )
        self.assertEqual(listened.status_code, 200, listened.text)
        after = self.client.get("/api/v1/agent-session/status", headers=self.headers).json()
        self.assertTrue(after["connected"])
        stopped = self.client.post("/api/v1/agent-session/stop", headers=self.headers)
        self.assertEqual(stopped.status_code, 200, stopped.text)
        self.assertFalse(self.client.get("/api/v1/agent-session/status", headers=self.headers).json()["enabled"])

    def test_agent_listener_reports_and_acknowledges_job_status_notifications(self) -> None:
        job_response, _data = self.create_job()
        bootstrap = self.client.get("/api/v1/agent-session", headers=self.headers).json()
        headers = {"Authorization": f"Bearer {bootstrap['agent_token']}"}
        listen_path = bootstrap["listen_url"].replace("https://video.example.test", "")

        self.database.set_job_status(
            job_response["id"], "recap_ready", completed_at=None,
        )
        first = self.client.post(listen_path, headers=headers)
        self.assertEqual(first.status_code, 200, first.text)
        notice = first.json()
        self.assertEqual(notice["event"], "JOB_STATUS_NOTIFICATION")
        self.assertEqual(notice["kind"], "RECAP_PLAN_READY")
        self.assertEqual(notice["job_id"], job_response["id"])
        self.assertIn("网页端预览", notice["message"])

        repeated = self.client.post(listen_path, headers=headers)
        self.assertEqual(repeated.json()["notification_id"], notice["notification_id"])
        acknowledged = self.client.post(
            notice["ack_endpoint"].replace("https://video.example.test", ""),
            headers=headers,
        )
        self.assertEqual(acknowledged.status_code, 200, acknowledged.text)
        self.assertEqual(acknowledged.json()["event"], "NOTIFICATION_ACKNOWLEDGED")
        after = self.client.post(listen_path, headers=headers)
        self.assertNotEqual(after.json()["event"], "JOB_STATUS_NOTIFICATION")

        self.database.set_job_status(job_response["id"], "completed", completed_at="now")
        completed = self.client.post(listen_path, headers=headers).json()
        self.assertEqual(completed["kind"], "JOB_COMPLETED")
        self.assertEqual(completed["status"], "completed")

    def test_agent_capability_probe_requires_exact_nonce_and_records_isolation(self) -> None:
        bootstrap = self.client.get("/api/v1/agent-session", headers=self.headers).json()
        headers = {"Authorization": f"Bearer {bootstrap['agent_token']}"}
        probe_path = bootstrap["capability_probe_url"].replace(
            "https://video.example.test", ""
        )
        verify_path = bootstrap["capability_verify_url"].replace(
            "https://video.example.test", ""
        )
        probe = self.client.post(probe_path, headers=headers)
        self.assertEqual(probe.status_code, 200, probe.text)
        capabilities = {
            "native_subagents": True,
            "context_isolation": True,
            "max_child_agents": 3,
            "probe_role": "isolated-probe",
            "child_agent_run_id": "probe-child-001",
            "isolated_context_id": "probe-context-001",
        }
        capabilities["probe_result"] = f"{probe.json()['probe_nonce']} isolated-probe"
        rejected = self.client.post(
            verify_path,
            headers=headers,
            json={"probe_nonce": "wrong-nonce", "capabilities": capabilities},
        )
        self.assertEqual(rejected.status_code, 422, rejected.text)
        accepted = self.client.post(
            verify_path,
            headers=headers,
            json={
                "probe_nonce": probe.json()["probe_nonce"],
                "capabilities": capabilities,
            },
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        status_payload = self.client.get(
            "/api/v1/agent-session/status", headers=self.headers
        ).json()
        self.assertTrue(status_payload["capabilities_verified"])
        self.assertTrue(status_payload["capabilities"]["context_isolation"])
        self.assertEqual(status_payload["capabilities"]["max_child_agents"], 3)

        command = bootstrap["command"]
        self.assertIn('"native_subagents":true', command)
        self.assertIn('"context_isolation":true', command)
        self.assertIn('"max_child_agents":3', command)
        self.assertIn('"child_agent_run_id"', command)
        self.assertIn("旧nonce立即失效", command)

    def test_server_listener_lease_expires_and_can_be_restarted(self) -> None:
        bootstrap = self.client.get("/api/v1/agent-session", headers=self.headers).json()
        headers = {"Authorization": f"Bearer {bootstrap['agent_token']}"}
        started = self.client.post(
            bootstrap["listener_start_url"].replace(
                "https://video.example.test", ""
            ),
            headers=headers,
        )
        self.assertEqual(started.status_code, 200, started.text)
        old_lease = started.json()["lease_id"]
        with self.database.connection() as connection:
            connection.execute(
                """UPDATE agent_sessions
                SET idle_deadline_at='2000-01-01T00:00:00+00:00'
                WHERE access_key_id=?""",
                (bootstrap["access_key_id"],),
            )
        expired = self.client.post(
            bootstrap["listen_url"].replace("https://video.example.test", ""),
            headers=headers,
        )
        self.assertEqual(expired.status_code, 200, expired.text)
        self.assertEqual(expired.json()["event"], "LISTEN_EXPIRED")
        restarted = self.client.post(
            bootstrap["listener_start_url"].replace(
                "https://video.example.test", ""
            ),
            headers=headers,
        )
        self.assertEqual(restarted.status_code, 200, restarted.text)
        self.assertNotEqual(restarted.json()["lease_id"], old_lease)

    def test_account_session_listen_claims_owned_ready_job(self) -> None:
        job_response, _data = self.create_job()
        job = self.database.get_job(job_response["id"])
        storage = JobStorage(self.settings)
        paths = storage.paths_from_job(job)
        remote = RemoteAgentBridge(self.database, storage, self.settings.project_root)
        session = remote.initialize(job, paths, maximum_parallel=3)
        _job_dir, request_payload = agent_bridge.create_job(
            Path(session["bridge_root"]),
            {"task_type": "folder_subtitle_translation", "target_language": "Arabic", "translation_quality": "fast", "episodes": []},
        )
        self.database.set_job_status(job["id"], "waiting_agent")
        bootstrap = job_response["agent_bootstrap"]
        listen_path = bootstrap["listen_url"].replace("https://video.example.test", "")
        response = self.client.post(
            listen_path, headers={"Authorization": f"Bearer {bootstrap['agent_token']}"}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["event"], "JOB")
        self.assertEqual(response.json()["external_job_id"], job["id"])
        self.assertEqual(response.json()["request"]["job_id"], request_payload["job_id"])

    def test_invalid_settings_and_windows_names_are_rejected_or_sanitized(self) -> None:
        with self.assertRaises(ValueError):
            normalize_settings({"pipeline": {"hardware_acceleration": "quantum"}})
        with self.assertRaisesRegex(ValueError, "本机路径"):
            normalize_settings({"video_config": {"background_music": r"C:\\private.mp3"}})
        normalized = normalize_settings({"pipeline": {"enable_subtitles": True, "translation_backend": "api"}})
        self.assertEqual(normalized["pipeline"]["translation_backend"], "api")
        self.assertEqual(safe_component("../CON"), "_CON")

    def test_api_mode_requires_secret_and_never_echoes_it(self) -> None:
        body = {
            "series_name": "API测试",
            "files": [{"name": "第一集.mp4", "size": 5}],
            "settings": {"pipeline": {"enable_subtitles": True, "translation_backend": "api"}},
        }
        missing = self.client.post("/api/v1/jobs", headers=self.headers, json=body)
        self.assertEqual(missing.status_code, 422)
        body["llm_api_key"] = "rk_test_secret_123456"
        created = self.client.post("/api/v1/jobs", headers=self.headers, json=body)
        self.assertEqual(created.status_code, 201, created.text)
        payload = created.json()
        self.assertNotIn("rk_test_secret", json.dumps(payload, ensure_ascii=False))
        job = self.database.get_job(payload["id"])
        self.assertNotIn("rk_test_secret", json.dumps(job, ensure_ascii=False))
        secret = Path(job["work_directory"]) / ".secrets" / "llm-api-key.txt"
        self.assertEqual(secret.read_text(encoding="utf-8"), "rk_test_secret_123456")

    def test_remote_assets_are_separated_from_input_videos(self) -> None:
        response = self.client.post(
            "/api/v1/jobs",
            headers=self.headers,
            json={
                "series_name": "素材测试",
                "files": [
                    {"name": "第一集.mp4", "size": 5, "role": "video"},
                    {"name": "第一集.mp4.final.ar.srt", "size": 5, "role": "subtitle_final"},
                    {"name": "音乐.mp3", "size": 5, "role": "music"},
                    {"name": "流星.mp4", "size": 5, "role": "effect_pool"},
                ],
                "settings": {"pipeline": {"enable_subtitles": False}},
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        job = response.json()
        self.assertEqual(
            [item["role"] for item in job["uploads"]],
            ["video", "subtitle_final", "music", "effect_pool"],
        )
        for upload in job["uploads"]:
            data = b"12345"
            for index in range(upload["total_chunks"]):
                chunk = data[index * job["chunk_size"]:(index + 1) * job["chunk_size"]]
                self.client.put(
                    f"/api/v1/jobs/{job['id']}/uploads/{upload['id']}/chunks/{index}",
                    headers={**self.headers, "X-Chunk-SHA256": hashlib.sha256(chunk).hexdigest()},
                    content=chunk,
                )
            completed = self.client.post(
                f"/api/v1/jobs/{job['id']}/uploads/{upload['id']}/complete", headers=self.headers
            )
            self.assertEqual(completed.status_code, 200, completed.text)
        paths = JobStorage(self.settings).paths_from_job(self.database.get_job(job["id"]))
        self.assertTrue((paths.input / "第一集.mp4").is_file())
        self.assertTrue((paths.input / "字幕终稿" / "第一集.mp4.final.ar.srt").is_file())
        self.assertTrue((paths.assets / "music" / "音乐.mp3").is_file())
        self.assertTrue((paths.assets / "effect_pool" / "流星.mp4").is_file())

    def test_database_allows_only_one_claimed_folder_job(self) -> None:
        first, first_data = self.create_job(b"first")
        second, second_data = self.create_job(b"second")
        for job, data in ((first, first_data), (second, second_data)):
            self.upload_all(job, data)
            self.client.post(f"/api/v1/jobs/{job['id']}/start", headers=self.headers)
        claimed = self.database.claim_next_job()
        self.assertIsNotNone(claimed)
        self.assertIsNone(self.database.claim_next_job())
        summary = self.database.queue_summary()
        self.assertEqual(summary["active"], 1)
        self.assertEqual(summary["waiting"], 1)

    def test_worker_lease_rejects_second_gateway_worker(self) -> None:
        storage = JobStorage(self.settings)
        first = GatewayWorker(self.settings, self.database, storage)
        second = GatewayWorker(self.settings, self.database, storage)
        first.start()
        try:
            with self.assertRaisesRegex(RuntimeError, "另一个网页处理Worker"):
                second.start()
        finally:
            first.stop()


if __name__ == "__main__":
    unittest.main()
