import unittest
import tempfile
from pathlib import Path
from unittest import mock

import batch_pipeline
import video_dedup


class SubtitleTimingTests(unittest.TestCase):
    def test_hard_ocr_mode_does_not_request_global_asr_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with (
                mock.patch.object(batch_pipeline.subtitle_tool, "subtitle_streams", return_value=[]),
                mock.patch.object(batch_pipeline, "global_asr_slot") as slot,
                mock.patch.object(batch_pipeline, "_run_source_subprocess") as run_source,
            ):
                result = batch_pipeline.make_subtitle_sources(
                    Path("input.mp4"), root / "visual.srt", root / "asr.srt", "hard-ocr", "en", "English",
                    "medium", "cuda", "ffmpeg", "ffprobe", 600, 600,
                )

        self.assertEqual(set(result), {"ocr"})
        slot.assert_not_called()
        self.assertEqual(run_source.call_count, 1)

    def test_asr_mode_uses_machine_wide_slot_before_starting_whisper(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lease = mock.MagicMock()
            lease.__enter__.return_value = 1
            with (
                mock.patch.object(batch_pipeline.subtitle_tool, "subtitle_streams", return_value=[]),
                mock.patch.object(batch_pipeline, "global_asr_slot", return_value=lease) as slot,
                mock.patch.object(batch_pipeline, "_run_source_subprocess") as run_source,
            ):
                result = batch_pipeline.make_subtitle_sources(
                    Path("input.mp4"), root / "visual.srt", root / "asr.srt", "asr", "en", "English",
                    "medium", "cuda", "ffmpeg", "ffprobe", 600, 600, global_asr_workers=5,
                )

        self.assertEqual(set(result), {"asr"})
        slot.assert_called_once()
        self.assertEqual(slot.call_args.args[:2], (5, "音频 ASR"))
        self.assertEqual(run_source.call_count, 1)

    def test_auto_mode_uses_soft_subtitle_alone_and_skips_ocr_asr(self) -> None:
        def fake_extract(_video, output, _stream, _ffmpeg, dry_run):
            self.assertFalse(dry_run)
            batch_pipeline.subtitle_tool.write_srt(
                [batch_pipeline.subtitle_tool.SubtitleItem(1, "00:00:00,000", "00:00:01,000", "soft")],
                output,
            )

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(batch_pipeline.subtitle_tool, "subtitle_streams", return_value=[{"index": 0}]),
            mock.patch.object(batch_pipeline.subtitle_tool, "extract_subtitle", side_effect=fake_extract),
            mock.patch.object(batch_pipeline, "_run_source_subprocess") as run_source,
        ):
            root = Path(directory)
            result = batch_pipeline.make_subtitle_sources(
                Path("input.mp4"), root / "visual.srt", root / "asr.srt", "auto", "en", "Chinese",
                "medium", "cuda", "ffmpeg", "ffprobe", 600, 600,
            )

        self.assertEqual(set(result), {"soft"})
        run_source.assert_not_called()

    def test_auto_mode_runs_ocr_and_asr_and_returns_both_sources(self) -> None:
        def fake_source_process(command, _label, _timeout):
            if "hard-ocr" in command:
                output = Path(command[command.index("hard-ocr") + 2])
                text = "visual"
            else:
                output = Path(command[command.index("transcribe") + 2])
                text = "audio"
                words_output = Path(command[command.index("--word-timestamps-output") + 1])
                words_output.write_text('{"words": [{"text": " audio", "start": 0.0, "end": 1.0, "probability": 0.9}]}', encoding="utf-8")
            batch_pipeline.subtitle_tool.write_srt(
                [batch_pipeline.subtitle_tool.SubtitleItem(1, "00:00:00,000", "00:00:01,000", text)],
                output,
            )

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(batch_pipeline.subtitle_tool, "subtitle_streams", return_value=[]),
            mock.patch.object(batch_pipeline, "_run_source_subprocess", side_effect=fake_source_process) as run_source,
        ):
            root = Path(directory)
            result = batch_pipeline.make_subtitle_sources(
                Path("input.mp4"), root / "visual.srt", root / "asr.srt", "auto", "en", "Chinese",
                "medium", "cuda", "ffmpeg", "ffprobe", 600, 600,
            )

        self.assertEqual(set(result), {"ocr", "asr", "asr_words"})
        self.assertEqual(run_source.call_count, 2)

    def test_soft_asr_mode_uses_soft_track_and_asr_without_ocr(self) -> None:
        def fake_extract(_video, output, _stream, _ffmpeg, dry_run):
            self.assertFalse(dry_run)
            batch_pipeline.subtitle_tool.write_srt(
                [batch_pipeline.subtitle_tool.SubtitleItem(1, "00:00:00,000", "00:00:01,000", "soft")],
                output,
            )

        def fake_source_process(command, _label, _timeout):
            self.assertIn("transcribe", command)
            output = Path(command[command.index("transcribe") + 2])
            batch_pipeline.subtitle_tool.write_srt(
                [batch_pipeline.subtitle_tool.SubtitleItem(1, "00:00:00,000", "00:00:01,000", "audio")],
                output,
            )

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(batch_pipeline.subtitle_tool, "subtitle_streams", return_value=[{"index": 0}]),
            mock.patch.object(batch_pipeline.subtitle_tool, "extract_subtitle", side_effect=fake_extract),
            mock.patch.object(batch_pipeline, "_run_source_subprocess", side_effect=fake_source_process) as run_source,
        ):
            root = Path(directory)
            result = batch_pipeline.make_subtitle_sources(
                Path("input.mp4"), root / "visual.srt", root / "asr.srt", "soft-asr", "en", "Chinese",
                "medium", "cuda", "ffmpeg", "ffprobe", 600, 600,
            )

        self.assertEqual(set(result), {"soft", "asr"})
        self.assertEqual(run_source.call_count, 1)

    def test_ocr_asr_mode_ignores_embedded_soft_track(self) -> None:
        def fake_source_process(command, _label, _timeout):
            subcommand = "hard-ocr" if "hard-ocr" in command else "transcribe"
            output = Path(command[command.index(subcommand) + 2])
            batch_pipeline.subtitle_tool.write_srt(
                [batch_pipeline.subtitle_tool.SubtitleItem(1, "00:00:00,000", "00:00:01,000", subcommand)],
                output,
            )

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(batch_pipeline.subtitle_tool, "subtitle_streams", return_value=[{"index": 0}]),
            mock.patch.object(batch_pipeline.subtitle_tool, "extract_subtitle") as extract,
            mock.patch.object(batch_pipeline, "_run_source_subprocess", side_effect=fake_source_process) as run_source,
        ):
            root = Path(directory)
            result = batch_pipeline.make_subtitle_sources(
                Path("input.mp4"), root / "visual.srt", root / "asr.srt", "ocr-asr", "en", "Chinese",
                "medium", "cuda", "ffmpeg", "ffprobe", 600, 600, ocr_device="cuda",
            )

        self.assertEqual(set(result), {"ocr", "asr"})
        self.assertEqual(run_source.call_count, 2)
        extract.assert_not_called()
        ocr_command = next(call.args[0] for call in run_source.call_args_list if "hard-ocr" in call.args[0])
        self.assertEqual(ocr_command[ocr_command.index("--device") + 1], "cuda")

    def test_auto_mode_degrades_to_asr_when_ocr_fails(self) -> None:
        def fake_source_process(command, _label, _timeout):
            if "hard-ocr" in command:
                raise RuntimeError("ocr timeout")
            output = Path(command[command.index("transcribe") + 2])
            batch_pipeline.subtitle_tool.write_srt(
                [batch_pipeline.subtitle_tool.SubtitleItem(1, "00:00:00,000", "00:00:01,000", "audio")],
                output,
            )

        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(batch_pipeline.subtitle_tool, "subtitle_streams", return_value=[]),
            mock.patch.object(batch_pipeline, "_run_source_subprocess", side_effect=fake_source_process),
        ):
            root = Path(directory)
            result = batch_pipeline.make_subtitle_sources(
                Path("input.mp4"), root / "visual.srt", root / "asr.srt", "auto", "en", "Chinese",
                "medium", "cuda", "ffmpeg", "ffprobe", 600, 600,
            )

        self.assertEqual(set(result), {"asr"})

    def test_auto_ocr_uses_arabic_when_source_language_is_arabic(self) -> None:
        self.assertEqual(batch_pipeline.resolved_ocr_language("auto", "Arabic"), "arabic")
        self.assertEqual(batch_pipeline.resolved_ocr_language("auto", "阿拉伯语"), "arabic")
        self.assertEqual(batch_pipeline.resolved_ocr_language("ch", "Arabic"), "ch")

    def test_ffprobe_dictionary_duration_is_used(self) -> None:
        config = video_dedup.TransformConfig(trim_start=1.0, trim_end=2.0, speed=1.25)
        with (
            mock.patch.object(batch_pipeline.video_dedup, "probe_video", return_value={"duration": 120.5}),
            mock.patch.object(batch_pipeline.subtitle_tool, "adjust_srt_timing") as adjust,
        ):
            result = batch_pipeline.adjusted_subtitle_for_transform(
                Path("source.srt"), Path("timed.srt"), Path("input.mp4"), config, "ffprobe"
            )

        self.assertEqual(result, Path("timed.srt"))
        adjust.assert_called_once_with(
            Path("source.srt"),
            Path("timed.srt"),
            trim_start=1.0,
            trim_end=2.0,
            speed=1.25,
            source_duration=120.5,
        )

    def test_transform_command_forwards_hardware_and_binary_paths(self) -> None:
        with mock.patch.object(batch_pipeline.subprocess, "run") as run:
            batch_pipeline.run_video_transform(
                Path("input.mp4"), Path("output.mp4"), "medium", Path("config.json"), 2026,
                "nvidia", "custom-ffmpeg", "custom-ffprobe",
            )

        command = run.call_args.args[0]
        self.assertEqual(command[command.index("--hardware-acceleration") + 1], "nvidia")
        self.assertEqual(command[command.index("--ffmpeg") + 1], "custom-ffmpeg")
        self.assertEqual(command[command.index("--ffprobe") + 1], "custom-ffprobe")

    def test_subtitle_directory_runs_prepare_audit_then_encode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.json"
            config.write_text("{}", encoding="utf-8")
            inputs = [root / "episode1.mp4", root / "episode2.mp4"]
            events = []

            def fake_prepare(_args, input_video, output_video, seed, index, total):
                events.append(f"prepare-{index}")
                translated = root / f"translated-{index}.srt"
                record = root / f"record-{index}.json"
                translated.write_text("1\n00:00:00,000 --> 00:00:01,000\ntext\n", encoding="utf-8")
                record.write_text("{}", encoding="utf-8")
                return {
                    "index": index, "total": total, "input_video": input_video,
                    "output_video": output_video, "seed": seed,
                    "translated_srt": translated, "translation_record_path": record,
                }

            def fake_audit(records, subtitles, *_args):
                self.assertEqual(len(records), 2)
                self.assertEqual(len(subtitles), 2)
                self.assertEqual(sum(event.startswith("prepare-") for event in events), 2)
                events.append("audit")

            def fake_encode(_args, prepared):
                self.assertIn("audit", events)
                events.append(f"encode-{prepared['index']}")

            args = batch_pipeline.make_parser().parse_args([
                str(root / "input"), str(root / "output"), "--config", str(config),
                "--enable-subtitles", "--enable-llm-review", "--video-workers", "2",
                "--translation-log-dir", str(root / "logs"),
            ])
            with (
                mock.patch.object(batch_pipeline, "collect_pipeline_inputs", return_value=inputs),
                mock.patch.object(batch_pipeline, "prepare_video_subtitles", side_effect=fake_prepare),
                mock.patch.object(batch_pipeline.subtitle_tool, "review_series_consistency_openai_compatible", side_effect=fake_audit),
                mock.patch.object(batch_pipeline, "encode_prepared_subtitle_video", side_effect=fake_encode),
            ):
                result = batch_pipeline.process(args)

        self.assertEqual(result, 0)
        self.assertEqual(sum(event.startswith("encode-") for event in events), 2)


if __name__ == "__main__":
    unittest.main()
