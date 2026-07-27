import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import agent_bridge
import batch_pipeline


def episode_review(score=9.0):
    return {
        "summary": "checked",
        "warnings": [],
        "quality_score": score,
        "quality_checks": {name: score for name in agent_bridge.EPISODE_QUALITY_DIMENSIONS},
    }


def series_review(score=9.0):
    return {
        "summary": "consistent",
        "changes": [],
        "warnings": [],
        "quality_score": score,
        "quality_checks": {name: score for name in agent_bridge.SERIES_QUALITY_DIMENSIONS},
    }


class AgentBridgeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "bridge"
        self.project = Path(__file__).resolve().parent

    def tearDown(self):
        self.temporary.cleanup()

    def initialize_and_register(self):
        init = agent_bridge.generate_initialization(self.root, self.project, max_parallel=3)
        registered = agent_bridge.register(Path(init["init_path"]))
        self.assertEqual(registered["event"], "REGISTERED")
        return init

    def test_folder_job_roundtrip(self):
        init = self.initialize_and_register()
        job_dir, request = agent_bridge.create_job(
            self.root,
            {
                "target_language": "Arabic",
                "max_parallel": 5,
                "expected_episode_indexes": [1],
                "episodes": [
                    {
                        "index": 1,
                        "expected_subtitle_indexes": [1],
                        "items": [{"index": 1, "start": "00:00:00,000", "end": "00:00:01,000"}],
                    }
                ],
            },
        )
        event = agent_bridge.listen(Path(init["init_path"]), timeout=0)
        self.assertEqual(event["event"], "JOB")
        self.assertEqual(event["job_id"], request["job_id"])
        self.assertEqual(event["max_parallel"], 3)
        response = {
            "protocol_version": 1,
            "job_id": request["job_id"],
            "generation": request["generation"],
            "cancel_epoch": request["cancel_epoch"],
            "status": "completed",
            "target_language": "Arabic",
            "episodes": [{"index": 1, "subtitles": [{"index": 1, "start": "00:00:00,000", "end": "00:00:01,000", "text": "مرحبا"}], "review": episode_review()}],
            "series_review": series_review(),
        }
        response_path = job_dir / "response.json"
        agent_bridge.atomic_write_json(response_path, response)
        result = agent_bridge.submit(Path(init["init_path"]), request["job_id"], response_path)
        self.assertEqual(result["event"], "SUBMITTED")
        self.assertEqual(agent_bridge.wait_for_response(self.root, request["job_id"])["status"], "completed")

    def test_stop_all_rejects_late_result(self):
        init = self.initialize_and_register()
        job_dir, request = agent_bridge.create_job(self.root, {"target_language": "English", "episodes": []})
        agent_bridge.listen(Path(init["init_path"]), timeout=0)
        agent_bridge.request_stop_all(self.root)
        response_path = job_dir / "response.json"
        agent_bridge.atomic_write_json(
            response_path,
            {
                "job_id": request["job_id"],
                "generation": request["generation"],
                "cancel_epoch": request["cancel_epoch"],
                "status": "completed",
            },
        )
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            agent_bridge.submit(Path(init["init_path"]), request["job_id"], response_path)

    def test_new_initialization_invalidates_old_conversation(self):
        first = self.initialize_and_register()
        second = agent_bridge.generate_initialization(self.root, self.project)
        self.assertEqual(agent_bridge.listen(Path(first["init_path"]), timeout=0)["event"], "REGISTRATION_INVALID")
        self.assertEqual(agent_bridge.register(Path(second["init_path"]))["event"], "REGISTERED")

    def test_generated_rules_require_api_contract_and_complete_parent_aggregation(self):
        init = agent_bridge.generate_initialization(self.root, self.project)
        rules = Path(init["rules_path"]).read_text(encoding="utf-8")
        self.assertIn("same contract as API mode", rules)
        self.assertIn("Never submit a child episode result", rules)
        self.assertIn("INCOMPLETE_RESPONSE", rules)
        self.assertIn("expected_subtitle_indexes", rules)
        self.assertIn("once per minute", rules)
        self.assertIn("twenty consecutive idle minutes", rules)
        self.assertIn("ends this idle listening session cleanly", rules)
        self.assertIn("Never send a final answer", rules)
        self.assertIn("JOB_RESUME", rules)
        self.assertIn("checkpoint_command", rules)
        self.assertIn("quality floor", rules)
        self.assertIn("third-party/web translators", rules)
        self.assertIn("Never translate, transliterate, preserve", rules)
        self.assertIn("ReelShorl", rules)
        self.assertIn("structural completeness", rules)
        self.assertIn("Mandatory self-review quality gate", rules)
        self.assertIn("Only a response that passes this gate may be stored as final SRT", rules)
        self.assertIn("native-localization", rules)
        self.assertIn("independent review", rules)
        self.assertIn("series_entities", rules)
        self.assertIn("Gender corrections may not change indexes", rules)
        self.assertIn("Never guess gender from tone", rules)

    def test_advanced_mode_requires_three_stages_and_95_gate(self):
        init = self.initialize_and_register()
        job_dir, request = agent_bridge.create_job(
            self.root,
            {
                "target_language": "Arabic",
                "translation_quality": "advanced",
                "quality_policy": {
                    "minimum_score": 9.5,
                    "maximum_revision_cycles": 3,
                    "required_stages": [
                        "reliable_draft",
                        "native_localization_refinement",
                        "independent_series_final_review",
                    ],
                },
                "expected_episode_indexes": [1],
                "episodes": [{
                    "index": 1,
                    "expected_subtitle_indexes": [1],
                    "items": [{"index": 1, "start": "00:00:00,000", "end": "00:00:01,000"}],
                }],
            },
        )
        agent_bridge.listen(Path(init["init_path"]), timeout=0)
        response = {
            "job_id": request["job_id"],
            "generation": request["generation"],
            "cancel_epoch": request["cancel_epoch"],
            "status": "completed",
            "target_language": "Arabic",
            "translation_quality": "advanced",
            "episodes": [{
                "index": 1,
                "subtitles": [{"index": 1, "start": "00:00:00,000", "end": "00:00:01,000", "text": "مرحبا"}],
                "review": episode_review(9.5),
            }],
            "series_review": series_review(9.5),
            "series_entities": [],
            "advanced_review": {
                "stages_completed": request["quality_policy"]["required_stages"],
                "revision_cycles": 1,
                "independent_final_review": True,
                "summary": "fresh-context final audit",
            },
        }
        response_path = job_dir / "advanced-response.json"
        agent_bridge.atomic_write_json(response_path, response)
        self.assertEqual(
            agent_bridge.submit(Path(init["init_path"]), request["job_id"], response_path)["event"],
            "SUBMITTED",
        )

    def test_idle_listener_uses_human_scale_poll_with_twenty_minute_deadline(self):
        self.assertEqual(agent_bridge.LISTEN_POLL_SECONDS, 55.0)
        self.assertEqual(agent_bridge.IDLE_LISTEN_TIMEOUT_SECONDS, 20 * 60)
        self.assertEqual(agent_bridge.HEARTBEAT_FRESH_SECONDS, 3 * 60)
        self.assertEqual(agent_bridge.CLAIM_RESUME_AFTER_SECONDS, 2 * 60)

    def test_listener_keeps_polling_until_work_arrives_before_deadline(self):
        init = self.initialize_and_register()
        expected = {"event": "JOB", "job_id": "job-after-idle"}
        with (
            mock.patch.object(agent_bridge, "_claim_next_job", side_effect=[None, expected]),
            mock.patch.object(agent_bridge.time, "sleep") as sleep,
        ):
            result = agent_bridge.listen(Path(init["init_path"]), timeout=1200)
        self.assertEqual(result, expected)
        sleep.assert_called_once_with(agent_bridge.LISTEN_POLL_SECONDS)

    def test_initialization_uses_console_python_instead_of_pythonw(self):
        bin_dir = Path(self.temporary.name) / "venv" / "Scripts"
        bin_dir.mkdir(parents=True)
        pythonw = bin_dir / "pythonw.exe"
        python = bin_dir / "python.exe"
        pythonw.write_bytes(b"")
        python.write_bytes(b"")
        init = agent_bridge.generate_initialization(
            self.root,
            self.project,
            python_executable=str(pythonw),
        )
        self.assertEqual(init["register_command"][0], str(python))
        self.assertEqual(init["listen_command"][0], str(python))
        self.assertEqual(init["listen_command"][-1], "1200")
        rules = Path(init["rules_path"]).read_text(encoding="utf-8")
        self.assertIn("twenty consecutive idle minutes", rules)
        self.assertNotIn("without an idle", rules)

    def test_stale_claim_is_resumed_by_same_registration(self):
        init = self.initialize_and_register()
        job_dir, request = agent_bridge.create_job(
            self.root,
            {"target_language": "Arabic", "episodes": [], "expected_episode_indexes": []},
        )
        claimed = agent_bridge.listen(Path(init["init_path"]), timeout=0)
        self.assertEqual(claimed["event"], "JOB")
        request_path = job_dir / "request.json"
        stored = agent_bridge.read_json(request_path)
        stored["last_agent_activity_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=agent_bridge.CLAIM_RESUME_AFTER_SECONDS + 1)
        ).astimezone().isoformat(timespec="seconds")
        agent_bridge.atomic_write_json(request_path, stored)

        resumed = agent_bridge.listen(Path(init["init_path"]), timeout=0)

        self.assertEqual(resumed["event"], "JOB_RESUME")
        self.assertEqual(resumed["job_id"], request["job_id"])
        self.assertTrue(resumed["resume"])
        self.assertTrue(resumed["progress_path"].endswith("progress.json"))
        self.assertEqual(agent_bridge.read_json(request_path)["resume_count"], 1)

    def test_checkpoint_persists_completed_episode_and_refreshes_activity(self):
        init = self.initialize_and_register()
        job_dir, request = agent_bridge.create_job(
            self.root,
            {
                "target_language": "Arabic",
                "expected_episode_indexes": [1],
                "episodes": [{
                    "index": 1,
                    "expected_subtitle_indexes": [1],
                    "items": [{"index": 1, "start": "00:00:00,000", "end": "00:00:01,000"}],
                }],
            },
        )
        event = agent_bridge.listen(Path(init["init_path"]), timeout=0)
        checkpoint_input = Path(event["checkpoint_input_path"])
        agent_bridge.atomic_write_json(
            checkpoint_input,
            {
                "job_id": request["job_id"],
                "generation": request["generation"],
                "cancel_epoch": request["cancel_epoch"],
                "status": "in_progress",
                "target_language": "Arabic",
                "episodes": [{
                    "index": 1,
                    "subtitles": [{"index": 1, "start": "00:00:00,000", "end": "00:00:01,000", "text": "مرحبا"}],
                    "review": episode_review(),
                }],
            },
        )

        result = agent_bridge.checkpoint(Path(init["init_path"]), request["job_id"], checkpoint_input)

        self.assertEqual(result["event"], "CHECKPOINTED")
        self.assertEqual(result["episodes"], [1])
        self.assertTrue((job_dir / "progress.json").is_file())
        stored = agent_bridge.read_json(job_dir / "request.json")
        self.assertEqual(stored["progress_episode_indexes"], [1])
        self.assertIn("last_agent_activity_at", stored)

    def test_claims_wait_at_configured_global_parallel_limit(self):
        init = agent_bridge.generate_initialization(self.root, self.project, max_parallel=2)
        agent_bridge.register(Path(init["init_path"]))
        jobs = [agent_bridge.create_job(self.root, {"target_language": "English", "episodes": []}) for _ in range(3)]
        first = agent_bridge.listen(Path(init["init_path"]), timeout=0)
        second = agent_bridge.listen(Path(init["init_path"]), timeout=0)
        blocked = agent_bridge.listen(Path(init["init_path"]), timeout=0)
        self.assertEqual([first["event"], second["event"], blocked["event"]], ["JOB", "JOB", "IDLE"])
        job_dir, request = jobs[0]
        response_path = job_dir / "response.json"
        agent_bridge.atomic_write_json(
            response_path,
            {
                "job_id": request["job_id"],
                "generation": request["generation"],
                "cancel_epoch": request["cancel_epoch"],
                "status": "completed",
                "target_language": "English",
                "episodes": [],
                "series_review": series_review(),
            },
        )
        agent_bridge.submit(Path(init["init_path"]), request["job_id"], response_path)
        self.assertEqual(agent_bridge.listen(Path(init["init_path"]), timeout=0)["event"], "JOB")

    def test_incomplete_parent_response_is_rejected_and_can_be_resubmitted(self):
        init = self.initialize_and_register()
        episode = lambda index: {
            "index": index,
            "expected_subtitle_indexes": [1],
            "items": [{"index": 1, "start": "00:00:00,000", "end": "00:00:01,000"}],
        }
        job_dir, request = agent_bridge.create_job(
            self.root,
            {
                "target_language": "Arabic",
                "expected_episode_indexes": [1, 2, 3, 4, 5],
                "episodes": [episode(index) for index in range(1, 6)],
            },
        )
        agent_bridge.listen(Path(init["init_path"]), timeout=0)

        def response_for(indexes):
            return {
                "job_id": request["job_id"],
                "generation": request["generation"],
                "cancel_epoch": request["cancel_epoch"],
                "status": "completed",
                "target_language": "Arabic",
                "episodes": [
                    {
                        "index": index,
                        "subtitles": [{"index": 1, "start": "00:00:00,000", "end": "00:00:01,000", "text": f"episode {index}"}],
                        "review": episode_review(),
                    }
                    for index in indexes
                ],
                "series_review": series_review(),
            }

        response_path = job_dir / "response.json"
        agent_bridge.atomic_write_json(response_path, response_for([1]))
        with self.assertRaisesRegex(RuntimeError, "INCOMPLETE_RESPONSE"):
            agent_bridge.submit(Path(init["init_path"]), request["job_id"], response_path)
        rejected_request = agent_bridge.read_json(job_dir / "request.json")
        self.assertEqual(rejected_request["status"], "claimed")
        self.assertEqual(rejected_request["submission_rejections"], 1)

        agent_bridge.atomic_write_json(response_path, response_for([1, 2, 3, 4, 5]))
        self.assertEqual(
            agent_bridge.submit(Path(init["init_path"]), request["job_id"], response_path)["event"],
            "SUBMITTED",
        )

    def test_quality_gate_rejects_low_score_and_keeps_job_for_revision(self):
        init = self.initialize_and_register()
        job_dir, request = agent_bridge.create_job(
            self.root,
            {
                "target_language": "Arabic",
                "expected_episode_indexes": [1],
                "episodes": [{
                    "index": 1,
                    "expected_subtitle_indexes": [1],
                    "items": [{"index": 1, "start": "00:00:00,000", "end": "00:00:01,000"}],
                }],
            },
        )
        agent_bridge.listen(Path(init["init_path"]), timeout=0)
        response = {
            "job_id": request["job_id"],
            "generation": request["generation"],
            "cancel_epoch": request["cancel_epoch"],
            "status": "completed",
            "target_language": "Arabic",
            "episodes": [{
                "index": 1,
                "subtitles": [{"index": 1, "start": "00:00:00,000", "end": "00:00:01,000", "text": "مرحبا"}],
                "review": episode_review(8.4),
            }],
            "series_review": series_review(9.0),
        }
        response_path = job_dir / "response.json"
        agent_bridge.atomic_write_json(response_path, response)

        with self.assertRaisesRegex(RuntimeError, "QUALITY_GATE_FAILED"):
            agent_bridge.submit(Path(init["init_path"]), request["job_id"], response_path)
        rejected = agent_bridge.read_json(job_dir / "request.json")
        self.assertEqual(rejected["status"], "claimed")
        self.assertEqual(rejected["submission_rejections"], 1)

        response["episodes"][0]["review"] = episode_review(8.5)
        response["series_review"] = series_review(8.5)
        agent_bridge.atomic_write_json(response_path, response)
        self.assertEqual(
            agent_bridge.submit(Path(init["init_path"]), request["job_id"], response_path)["event"],
            "SUBMITTED",
        )

    def test_stop_agent_does_not_delete_bridge_history(self):
        init = self.initialize_and_register()
        result = agent_bridge.stop_agent(self.root)
        self.assertEqual(result["event"], "AGENT_STOPPED")
        self.assertEqual(agent_bridge.listen(Path(init["init_path"]), timeout=0)["event"], "REGISTRATION_INVALID")
        self.assertTrue((self.root / "AGENT_RULES.md").exists())

    def test_pipeline_materializes_agent_subtitles_and_two_reports(self):
        run_dir = Path(self.temporary.name) / "records"
        work_dir = Path(self.temporary.name) / "work"
        run_dir.mkdir()
        work_dir.mkdir()
        visual = work_dir / "visual.srt"
        translated = work_dir / "translated.srt"
        repaired_source = work_dir / "repaired-source.srt"
        batch_pipeline.subtitle_tool.write_srt(
            [batch_pipeline.subtitle_tool.SubtitleItem(1, "00:00:00,000", "00:00:01,500", "Hello")],
            visual,
        )
        args = SimpleNamespace(
            agent_bridge_root=str(self.root),
            agent_wait_timeout_seconds=0,
            agent_task_title="Series",
            input=str(work_dir),
            output=str(work_dir / "output"),
            target_language="Arabic",
            source_language="English",
            ocr_language="en",
            subtitle_source="hard-ocr",
            video_workers=4,
            glossary_data=None,
            translation_run_dir=str(run_dir),
        )
        job_dir = Path(self.temporary.name) / "runtime-job"
        job_dir.mkdir()
        request = {"job_id": "job-test", "created_at": agent_bridge.now_iso()}
        response = {
            "job_id": "job-test",
            "target_language": "Arabic",
            "episodes": [
                {
                    "index": 1,
                    "source_subtitles": [
                        {
                            "index": 1,
                            "start": "00:00:00,000",
                            "end": "00:00:01,500",
                            "text": "Hello",
                        }
                    ],
                    "subtitles": [{"index": 1, "start": "00:00:00,000", "end": "00:00:01,500", "text": "مَرْحَبًا"}],
                    "review": episode_review(),
                }
            ],
            "series_review": series_review(),
            "series_entities": [],
            "glossary_suggestions": [],
        }
        prepared = [
            {
                "index": 1,
                "input_video": work_dir / "one.mp4",
                "output_video": work_dir / "one_local.mp4",
                "translated_srt": translated,
                "repaired_source_srt": repaired_source,
                "visual_kind": "ocr",
                "visual_path": visual,
                "audio_path": None,
                "audio_words_path": None,
            }
        ]
        with (
            mock.patch.object(batch_pipeline.agent_bridge, "create_job", return_value=(job_dir, request)) as create_job,
            mock.patch.object(batch_pipeline.agent_bridge, "wait_for_response", return_value=response),
            mock.patch.object(batch_pipeline.agent_bridge, "cleanup_job") as cleanup,
        ):
            json_path, md_path = batch_pipeline.run_agent_translation(args, prepared)
        submitted_payload = create_job.call_args.args[1]
        self.assertEqual(submitted_payload["expected_episode_indexes"], [1])
        self.assertEqual(submitted_payload["translation_quality"], "fast")
        self.assertEqual(submitted_payload["quality_policy"]["minimum_score"], 8.5)
        self.assertEqual(submitted_payload["series_entity_table"]["entities"], [])
        self.assertEqual(submitted_payload["episodes"][0]["expected_subtitle_indexes"], [1])
        contract_id = submitted_payload["episodes"][0]["translation_contract_id"]
        self.assertIn("fall right into my lap", submitted_payload["translation_contracts"][contract_id])
        self.assertIn("Never phoneticize a watermark", submitted_payload["translation_contracts"][contract_id])
        self.assertEqual(batch_pipeline.subtitle_tool.parse_srt(translated)[0].text, "مرحبا")
        self.assertEqual(batch_pipeline.subtitle_tool.parse_srt(repaired_source)[0].text, "Hello")
        self.assertEqual({path.name for path in run_dir.iterdir()}, {json_path.name, md_path.name})
        self.assertIn("token_estimate", agent_bridge.read_json(json_path))
        cleanup.assert_called_once_with(job_dir)


if __name__ == "__main__":
    unittest.main()
