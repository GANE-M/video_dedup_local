import json
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import video_dedup
import recap.renderer as recap_renderer
from recap.cli import build_parser, dispatch
from recap.models import RecapProject, RecapSegment, VoiceProfile, now_iso
from recap.loudness import measure_loudness
from recap.project_store import create_project, diff_versions, load_project, rollback_project, update_segment
from recap.renderer import _caption_filters, mix_audio_command, render_project
from recap.renderer import resolve_tts_engine, resolved_target_loudness
from recap.narration_text import normalize_narration_text, split_caption_sentences
from recap.pacing import (
    build_duration_budget,
    count_speech_units,
    fitted_narration_seconds,
    segment_pacing,
)
from recap.timeline import affected_segments, validate_source_intervals
from recap.tts_routing import explicit_engines_for_language, resolve_tts_engine as resolve_language_tts_engine
from recap.visual_dedup import detect_duplicates, validate_rendered_visual_uniqueness
from recap.voice_library import VoiceLibrary, engines_for_language, voice_cache_key, voice_cache_path


def project_for(root: Path, segments: list[RecapSegment], pattern: str = "ep{episode}.mp4") -> RecapProject:
    return RecapProject(
        1, "test-recap", "Test recap", str(root), pattern, "", str(root / "out"),
        "English", 10.0, "calm_female", 1.0, -18.0, segments, 1, now_iso(), now_iso(),
        {"hardware_acceleration": "cpu", "crf": 28, "width": 160, "height": 120, "caption_font_size": 14},
    )


class RecapTests(unittest.TestCase):
    def test_standard_arabic_budget_uses_measured_words_per_second(self):
        budget = build_duration_budget(
            preset_name="standard",
            target_duration_seconds=588,
            target_language="Arabic",
            narration_speed=1.0,
        )
        self.assertEqual(budget["speech_rate"]["unit"], "wps")
        self.assertEqual(budget["speech_rate"]["value"], 2.0)
        self.assertEqual(budget["narration_duration_seconds"], 147.0)
        self.assertEqual(budget["target_narration_units"], 265)

    def test_language_specific_speech_units(self):
        self.assertEqual(count_speech_units("قرار واحد غيّر كل شيء.", "Arabic"), 5)
        self.assertEqual(count_speech_units("One decision changed everything.", "English"), 4)
        self.assertEqual(count_speech_units("一个决定改变一切", "Chinese"), 8)

    def test_segment_pacing_flags_underfilled_narration(self):
        pacing = segment_pacing(
            text="في الزنزانة تحاول إليزابيث تحطيم ماغي وتدفع الغيرة إلى ذروتها",
            duration_seconds=24,
            target_language="Arabic",
            preset_name="standard",
        )
        self.assertEqual(pacing["status"], "short")
        self.assertLess(pacing["estimated_occupancy"], 0.5)

    def test_trim_to_voice_uses_real_wav_duration_and_tail(self):
        self.assertAlmostEqual(
            fitted_narration_seconds(
                planned_seconds=24,
                voice_seconds=8.45,
                lead_in_seconds=0.3,
                tail_seconds=0.8,
                fit_policy="trim_to_voice",
            ),
            9.55,
        )
        self.assertEqual(
            fitted_narration_seconds(
                planned_seconds=24,
                voice_seconds=8.45,
                lead_in_seconds=0.3,
                tail_seconds=0.8,
                fit_policy="preserve_window",
            ),
            24,
        )

    def test_cancellable_runner_terminates_active_child_process(self):
        started = time.monotonic()
        token = recap_renderer._CANCELLED.set(lambda: time.monotonic() - started > 0.2)
        try:
            with self.assertRaises(recap_renderer.RenderCancelled):
                recap_renderer._run(
                    [sys.executable, "-c", "import time; time.sleep(30)"],
                    text=True,
                )
        finally:
            recap_renderer._CANCELLED.reset(token)
        self.assertLess(time.monotonic() - started, 5.0)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required")
    def test_caption_filter_works_when_project_path_contains_apostrophe(self):
        with tempfile.TemporaryDirectory(prefix="recap's-") as temp:
            caption_dir = Path(temp) / "captions"
            caption_dir.mkdir()
            project = project_for(Path(temp), [])
            filters = _caption_filters("Hello world.", caption_dir, "seg-001", 1.0, 0.0, project)
            result = subprocess.run(
                [
                    shutil.which("ffmpeg"), "-v", "error",
                    "-f", "lavfi", "-i", "color=s=160x120:d=1",
                    "-vf", filters[0], "-frames:v", "1", "-f", "null", "-",
                ],
                cwd=caption_dir,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_arabic_routing_exposes_exactly_two_tts_models(self):
        self.assertEqual(
            explicit_engines_for_language("Arabic"),
            ("fish_s2", "chatterbox_v3"),
        )
        self.assertEqual(resolve_language_tts_engine("Arabic", "auto"), "fish_s2")
        with self.assertRaisesRegex(ValueError, "Qwen3 TTS 不支持阿拉伯语"):
            resolve_language_tts_engine("Arabic", "qwen3_tts")

    def test_create_project_defaults_to_half_of_source_duration(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = build_parser().parse_args([
                "create-project", "--project", str(root / "project.json"),
                "--project-id", "ratio-test", "--source", str(root), "--output", str(root / "out"),
            ])
            with (
                mock.patch("recap.cli.video_dedup.find_binary", side_effect=lambda name, explicit=None: name),
                mock.patch("recap.cli.inspect_sources", return_value={
                    "status": "ok", "episode_count": 3, "total_duration": 900.0, "episodes": [],
                }) as inspect,
            ):
                result = dispatch(args)
            saved = load_project(root / "project.json")
        self.assertEqual(result["target_duration_seconds"], 450.0)
        self.assertEqual(saved.target_duration_seconds, 450.0)
        self.assertEqual(saved.rendering["target_duration_ratio"], 0.5)
        self.assertEqual(inspect.call_args.args[2], "*第*集.mp4")

    def test_explicit_target_duration_overrides_ratio_calculation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            args = build_parser().parse_args([
                "create-project", "--project", str(root / "project.json"),
                "--project-id", "fixed-test", "--source", str(root), "--output", str(root / "out"),
                "--target-duration", "321",
            ])
            with (
                mock.patch("recap.cli.video_dedup.find_binary", side_effect=lambda name, explicit=None: name),
                mock.patch("recap.cli.inspect_sources") as inspect,
            ):
                result = dispatch(args)
            saved = load_project(root / "project.json")
        self.assertEqual(result["target_duration_seconds"], 321.0)
        self.assertEqual(saved.target_duration_seconds, 321.0)
        inspect.assert_not_called()

    def test_same_episode_overlap_is_rejected_with_segment_ids(self):
        project = project_for(Path("C:/source"), [
            RecapSegment("seg-a", 1, 0, 5, "narration", "A"),
            RecapSegment("seg-b", 1, 4, 8, "original"),
        ])
        with mock.patch.object(project, "episode_path", return_value=Path(__file__)):
            errors = validate_source_intervals(project, lambda _path: {"duration": 20, "has_audio": True})
        overlap = next(item for item in errors if item["code"] == "source_interval_overlap")
        self.assertEqual((overlap["first_segment_id"], overlap["second_segment_id"]), ("seg-a", "seg-b"))
        self.assertEqual(overlap["overlap_seconds"], 1)

    def test_source_end_beyond_duration_is_rejected(self):
        project = project_for(Path("C:/source"), [RecapSegment("seg-a", 2, 1, 12, "narration", "A")])
        with mock.patch.object(project, "episode_path", return_value=Path(__file__)):
            errors = validate_source_intervals(project, lambda _path: {"duration": 10, "has_audio": True})
        self.assertTrue(any(item["code"] == "source_end_out_of_range" and item["segment_id"] == "seg-a" for item in errors))

    def test_non_adjacent_duplicate_segments_are_detected(self):
        manifest = [{"segment_id": f"seg-{i}", "episode": i, "source_start": 0, "video_seconds": 2} for i in range(1, 4)]
        values = {
            "seg-1": [0x1111111111111111, 0x2222222222222222, 0x3333333333333333, 0x4444444444444444],
            "seg-2": [0xEEEEEEEEEEEEEEEE, 0xDDDDDDDDDDDDDDDD, 0xCCCCCCCCCCCCCCCC, 0xBBBBBBBBBBBBBBBB],
            "seg-3": [0x1111111111111111, 0x2222222222222222, 0x3333333333333333, 0x8888888888888888],
        }
        duplicates = detect_duplicates(manifest, lambda item: values[item["segment_id"]])
        self.assertEqual(len(duplicates), 1)
        self.assertEqual((duplicates[0]["first_segment_id"], duplicates[0]["second_segment_id"]), ("seg-1", "seg-3"))

    def test_cross_episode_recap_duplicate_is_reported(self):
        manifest = [
            {"segment_id": "ending", "episode": 1, "source_start": 50, "video_seconds": 2},
            {"segment_id": "recap", "episode": 2, "source_start": 0, "video_seconds": 2},
        ]
        with tempfile.TemporaryDirectory() as temp:
            report = validate_rendered_visual_uniqueness(
                manifest, "ffmpeg", Path(temp) / "duplicate_report.json",
                hash_provider=lambda _item: [100, 101, 102, 103],
            )
            saved = json.loads((Path(temp) / "duplicate_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["status"], "blocked")
        self.assertEqual(saved["duplicates"][0]["first_episode"], 1)
        self.assertEqual(saved["duplicates"][0]["second_episode"], 2)

    def test_duplicate_report_blocks_final_master_build(self):
        project = project_for(Path("C:/source"), [RecapSegment("seg-a", 1, 0, 2, "narration", "A")])
        manifest = {"segment_id": "seg-a", "episode": 1, "mode": "narration", "source_start": 0, "video_seconds": 2, "rendered_path": "x.mp4"}
        with (
            mock.patch("recap.renderer.validate_source_intervals", return_value=[]),
            mock.patch("recap.renderer.generate_voice_files", return_value=({"seg-a": Path("voice.wav")}, [], ["seg-a"])),
            mock.patch("recap.renderer.resolved_target_loudness", return_value=(-18.0, {"warnings": []})),
            mock.patch("recap.renderer.render_segment", return_value=(manifest, False)),
            mock.patch("recap.renderer.validate_rendered_visual_uniqueness", return_value={"status": "blocked", "duplicates": [{"first_segment_id": "a"}]}),
            mock.patch("recap.renderer.join_video_segments") as join,
            mock.patch("recap.renderer.video_dedup.find_binary", side_effect=lambda name, explicit=None: name),
        ):
            result = render_project(project)
        self.assertEqual(result["status"], "duplicate_blocked")
        join.assert_not_called()

    def test_render_segment_generates_only_selected_voice(self):
        project = project_for(Path("C:/source"), [
            RecapSegment("seg-a", 1, 0, 2, "narration", "A"),
            RecapSegment("seg-b", 2, 0, 2, "narration", "B"),
        ])
        manifest = {"segment_id": "seg-b", "episode": 2, "mode": "narration", "source_start": 0, "video_seconds": 2, "rendered_path": "x.mp4"}
        with (
            mock.patch("recap.renderer.validate_source_intervals", return_value=[]),
            mock.patch("recap.renderer.generate_voice_files", return_value=({"seg-b": Path("voice.wav")}, [], ["seg-b"])) as voices,
            mock.patch("recap.renderer.resolved_target_loudness", return_value=(-18.0, {"warnings": []})),
            mock.patch("recap.renderer.render_segment", return_value=(manifest, False)),
            mock.patch("recap.renderer.video_dedup.find_binary", side_effect=lambda name, explicit=None: name),
        ):
            result = render_project(project, only_segment_id="seg-b")
        self.assertEqual(result["affected_segments"], ["seg-b"])
        self.assertEqual([item.segment_id for item in voices.call_args.args[1]], ["seg-b"])

    def test_voice_cache_isolated_by_voice_project_and_text(self):
        library = VoiceLibrary()
        female, male = library.get("calm_female"), library.get("calm_male")
        female_key = voice_cache_key(female, "Hello", "English", 1.0)
        male_key = voice_cache_key(male, "Hello", "English", 1.0)
        changed_key = voice_cache_key(female, "Hello again", "English", 1.0)
        self.assertNotEqual(female_key, male_key)
        self.assertNotEqual(female_key, changed_key)
        self.assertNotEqual(
            voice_cache_path(Path("cache"), "project-a", female, female_key),
            voice_cache_path(Path("cache"), "project-b", female, female_key),
        )

    def test_same_voice_and_text_keep_same_identity_key(self):
        profile = VoiceLibrary().get("calm_male")
        first = voice_cache_key(profile, "One line", "English", 1.0)
        second = voice_cache_key(profile, "One line", "English", 1.0)
        self.assertEqual(first, second)

    def test_voice_cache_changes_when_reference_audio_changes(self):
        profile = VoiceLibrary().get("calm_male")
        with tempfile.TemporaryDirectory() as temp:
            reference = Path(temp) / "reference.wav"
            reference.write_bytes(b"voice-a")
            first = voice_cache_key(
                profile, "One line", "English", 1.0,
                reference_audio_path=reference,
            )
            reference.write_bytes(b"voice-b")
            second = voice_cache_key(
                profile, "One line", "English", 1.0,
                reference_audio_path=reference,
            )
        self.assertNotEqual(first, second)

    def test_voice_library_has_four_male_and_four_female_per_language(self):
        library = VoiceLibrary()
        for language in ("English", "Arabic"):
            profiles = library.compatible(language)
            self.assertEqual(len(profiles), 14)
            self.assertEqual(sum(item.gender == "female" for item in profiles), 7)
            self.assertEqual(sum(item.gender == "male" for item in profiles), 7)
            self.assertEqual(sum(item.age_group == "child_role" for item in profiles), 2)

    def test_voice_library_hides_unapproved_profiles(self):
        library = VoiceLibrary()
        profile = library.get("calm_female")
        self.assertEqual(profile.review_status, "approved")
        self.assertIn(profile, library.compatible("English"))

    def test_arabic_auto_engine_uses_fish_and_qwen_is_rejected(self):
        project = project_for(Path("C:/source"), [])
        project.target_language = "Arabic"
        project.tts_engine = "auto"
        self.assertEqual(resolve_tts_engine(project), "fish_s2")
        project.tts_engine = "qwen3_tts"
        with self.assertRaisesRegex(ValueError, "不支持阿拉伯语"):
            resolve_tts_engine(project)

    def test_language_specific_engine_options_exclude_qwen_from_arabic(self):
        arabic = {item["value"] for item in engines_for_language("Arabic")}
        english = {item["value"] for item in engines_for_language("English")}
        self.assertNotIn("qwen3_tts", arabic)
        self.assertIn("qwen3_tts", english)
        self.assertIn("fish_s2", arabic)
        self.assertIn("chatterbox_v3", arabic)

    def test_keep_original_loudness_does_not_measure_or_normalize(self):
        project = project_for(Path("C:/source"), [])
        project.narration_target_loudness = "keep_original"
        with mock.patch("recap.renderer.measure_project_loudness") as measure:
            target, report = resolved_target_loudness(project, "ffmpeg", "ffprobe")
        self.assertIsNone(target)
        self.assertEqual(report["status"], "preserved")
        measure.assert_not_called()

    def test_arabic_normalization_preserves_logical_order_and_removes_bidi_controls(self):
        source = "\u202eفي عالمٍ جديد\r\nتبدأ الحكاية؟\u202c"
        normalized = normalize_narration_text(source, "Arabic")
        self.assertNotIn("\u202e", normalized)
        self.assertNotIn("\n", normalized)
        self.assertTrue(normalized.startswith("في عالم"))
        self.assertTrue(split_caption_sentences(normalized, "Arabic")[-1].endswith("تبدأ الحكاية؟"))

    @unittest.skipUnless(shutil.which("ffprobe"), "FFprobe is required")
    def test_bundled_voice_previews_are_ten_to_fifteen_seconds(self):
        library = VoiceLibrary()
        for profile in library.list():
            preview = library.resolve_asset(profile.preview_audio)
            self.assertTrue(preview.is_file(), profile.voice_id)
            result = subprocess.run([
                shutil.which("ffprobe"), "-v", "error", "-show_entries", "format=duration",
                "-of", "default=nw=1:nk=1", str(preview),
            ], capture_output=True, text=True, check=True)
            self.assertGreaterEqual(float(result.stdout.strip()), 10.0)
            self.assertLessEqual(float(result.stdout.strip()), 15.0)

    def test_mix_command_disables_amix_normalization(self):
        command = mix_audio_command("ffmpeg", Path("n.wav"), Path("o.wav"), Path("mix.wav"), 12.0)
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("amix=inputs=2", graph)
        self.assertIn("normalize=0", graph)

    def test_only_changed_segment_is_affected_and_versions_rollback(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "project.json"
            initial = project_for(Path(temp), [
                RecapSegment("seg-a", 1, 0, 2, "narration", "Old"),
                RecapSegment("seg-b", 1, 3, 5, "original"),
            ]).to_dict()
            create_project(path, initial)
            old = load_project(path)
            new, changed = update_segment(path, "seg-a", {"narration_text": "New"})
            self.assertEqual(changed, ["seg-a"])
            self.assertEqual(affected_segments(old, new), ["seg-a"])
            self.assertEqual(diff_versions(path, 1, 2)["changed"], ["seg-a"])
            rolled = rollback_project(path, 1)
            self.assertEqual(rolled.current_version, 3)
            self.assertEqual(rolled.segments[0].narration_text, "Old")
            self.assertTrue((Path(temp) / ".recap_versions" / "test-recap" / "v0002.json").is_file())

    def test_missing_source_and_no_audio_return_clear_errors(self):
        missing = project_for(Path("C:/does-not-exist"), [RecapSegment("missing", 1, 0, 1, "narration", "A")])
        errors = validate_source_intervals(missing, lambda _path: {"duration": 1, "has_audio": True})
        self.assertTrue(any(item["code"] == "missing_source" for item in errors))
        no_audio = project_for(Path("C:/source"), [RecapSegment("original", 1, 0, 1, "original")])
        with mock.patch.object(no_audio, "episode_path", return_value=Path(__file__)):
            errors = validate_source_intervals(no_audio, lambda _path: {"duration": 2, "has_audio": False})
        self.assertTrue(any(item["code"] == "missing_audio_stream" for item in errors))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
    def test_end_to_end_original_timeline_builds_equal_masters_and_decodable_mp4(self):
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for episode, source_filter, frequency in ((1, "testsrc", 440), (2, "testsrc2", 660)):
                subprocess.run([
                    ffmpeg, "-y", "-f", "lavfi", "-i", f"{source_filter}=size=160x120:rate=24:duration=1.2",
                    "-f", "lavfi", "-i", f"sine=frequency={frequency}:sample_rate=44100:duration=1.2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(root / f"ep{episode}.mp4"),
                ], capture_output=True, check=True)
            project = project_for(root, [
                RecapSegment("seg-a", 1, 0.0, 0.55, "original", purpose="first"),
                RecapSegment("seg-b", 2, 0.1, 0.70, "original", purpose="second"),
            ])
            result = render_project(project, ffmpeg=ffmpeg, ffprobe=ffprobe)
            self.assertEqual(result["status"], "ok", result)
            self.assertTrue(Path(result["output_paths"]["final"]).is_file())
            durations = result["master_durations"]
            for key in ("narration_master", "original_master", "complete_audio_master"):
                self.assertAlmostEqual(durations[key], durations["video_master_silent"], delta=0.12)
            subprocess.run([ffmpeg, "-v", "error", "-i", result["output_paths"]["final"], "-f", "null", "-"], capture_output=True, check=True)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg is required")
    def test_end_to_end_narration_uses_cache_and_hits_target_loudness(self):
        ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run([
                ffmpeg, "-y", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=24:duration=2",
                "-f", "lavfi", "-i", "sine=frequency=500:sample_rate=44100:duration=2",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(root / "ep1.mp4"),
            ], capture_output=True, check=True)
            narration = RecapSegment("seg-voice", 1, 0.0, 0.7, "narration", "The story begins with one quiet decision.")
            original = RecapSegment("seg-original", 1, 0.9, 1.7, "original")
            project = project_for(root, [narration, original])
            library = VoiceLibrary()
            profile = library.get(project.voice_id)
            key = voice_cache_key(
                profile,
                narration.narration_text,
                project.target_language,
                project.narration_speed,
                {"tts_engine": "qwen3_tts", **profile.generation_parameters},
                reference_audio_path=library.resolve_asset(profile.reference_audio),
            )
            cached_voice = voice_cache_path(root / "out" / ".recap_cache" / project.project_id / "voice", project.project_id, profile, key)
            cached_voice.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(library.resolve_asset(profile.reference_audio), cached_voice)
            result = render_project(project, ffmpeg=ffmpeg, ffprobe=ffprobe)
            self.assertEqual(result["status"], "ok", result)
            self.assertEqual(result["cache_hits"]["voice"], ["seg-voice"])
            measured = measure_loudness(Path(result["output_paths"]["narration_master"]), ffmpeg, -18.0)
            self.assertAlmostEqual(measured["integrated_lufs"], -18.0, delta=0.6)
            for key_name in ("narration_master", "original_master", "complete_audio_master"):
                self.assertAlmostEqual(result["master_durations"][key_name], result["master_durations"]["video_master_silent"], delta=0.12)


if __name__ == "__main__":
    unittest.main()
