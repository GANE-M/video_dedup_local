import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import subtitle_tool


class SubtitleToolTests(unittest.TestCase):
    def test_llm_http_request_uses_machine_wide_slot(self) -> None:
        lease = mock.MagicMock()
        lease.__enter__.return_value = 1
        with (
            mock.patch.object(subtitle_tool, "global_llm_slot", return_value=lease) as slot,
            mock.patch.object(subtitle_tool, "fetch_chat_completion_json", return_value={"ok": True}) as fetch,
            mock.patch.dict("os.environ", {"VIDEO_DEDUP_GLOBAL_LLM_WORKERS": "5"}),
        ):
            result = subtitle_tool.fetch_chat_completion_json_with_slot(
                "https://example.invalid", b"{}", "key", 30, "AI 测试"
            )
        self.assertEqual(result, {"ok": True})
        slot.assert_called_once_with(5, "AI 测试")
        fetch.assert_called_once()

    def test_llm_http_request_can_use_smaller_retry_slot_pool(self) -> None:
        lease = mock.MagicMock()
        lease.__enter__.return_value = 1
        with (
            mock.patch.object(subtitle_tool, "global_llm_slot", return_value=lease) as slot,
            mock.patch.object(subtitle_tool, "fetch_chat_completion_json", return_value={"ok": True}),
        ):
            result = subtitle_tool.fetch_chat_completion_json_with_slot(
                "https://example.invalid", b"{}", "key", 30, "AI retry", slot_limit=2
            )
        self.assertEqual(result, {"ok": True})
        slot.assert_called_once_with(2, "AI retry")

    def test_chinese_history_glossary_loads_and_builds_prompt(self) -> None:
        glossary = subtitle_tool.load_glossary_file(
            Path(__file__).with_name("glossaries") / "chinese_history_zh_en_ar.json"
        )
        prompt = subtitle_tool.build_glossary_prompt(glossary)

        self.assertEqual(glossary["id"], "chinese_history_zh_en_ar")
        self.assertGreaterEqual(len(glossary["terms"]), 40)
        self.assertIn('"zh":"状元"', prompt)
        self.assertIn('"en":"prince consort"', prompt)
        self.assertIn('"ar":"زوج الأميرة"', prompt)

    def test_western_fantasy_glossary_is_compact_and_contains_core_terms(self) -> None:
        glossary = subtitle_tool.load_glossary_file(
            Path(__file__).with_name("glossaries") / "western_fantasy_zh_en_ar.json"
        )
        prompt = subtitle_tool.build_glossary_prompt(glossary)
        english_terms = {term["en"] for term in glossary["terms"]}

        self.assertEqual(glossary["id"], "western_fantasy_zh_en_ar")
        self.assertLessEqual(len(glossary["terms"]), 40)
        self.assertIn("Fenrir", english_terms)
        self.assertIn("annual beast pact ceremony", english_terms)
        self.assertIn("slit pupil", english_terms)
        self.assertIn("فنرير", prompt)
        self.assertNotIn("Sera", prompt)

    def test_visual_and_asr_subtitles_align_by_time_not_index(self) -> None:
        visual = [
            subtitle_tool.SubtitleItem(1, "00:00:01,000", "00:00:02,000", "visual one"),
            subtitle_tool.SubtitleItem(2, "00:00:04,000", "00:00:05,000", "visual two"),
        ]
        audio = [
            subtitle_tool.SubtitleItem(1, "00:00:00,900", "00:00:02,100", "audio one"),
            subtitle_tool.SubtitleItem(2, "00:00:02,500", "00:00:03,000", "audio only"),
            subtitle_tool.SubtitleItem(3, "00:00:04,100", "00:00:04,900", "audio two"),
        ]

        pairs = subtitle_tool.align_visual_and_audio_subtitles(visual, audio)

        self.assertEqual(len(pairs), 3)
        self.assertEqual((pairs[0].visual_text, pairs[0].audio_text), ("visual one", "audio one"))
        self.assertEqual((pairs[1].visual_text, pairs[1].audio_text), ("", "audio only"))
        self.assertEqual((pairs[2].visual_text, pairs[2].audio_text), ("visual two", "audio two"))

    def test_dual_source_translation_sends_ocr_and_asr_together(self) -> None:
        pair = subtitle_tool.AlignedSubtitlePair(
            1, "00:00:01,000", "00:00:02,000", "OCR text", "ASR text"
        )
        with mock.patch.object(
            subtitle_tool,
            "chat_indexed_translations_openai_compatible",
            return_value=["final"],
        ) as chat:
            result = subtitle_tool.translate_dual_source_texts_openai_compatible(
                [pair], "Arabic", "English", "Chinese", "model-a"
            )

        self.assertEqual(result, ["final"])
        payload = chat.call_args.kwargs["user_payload"]
        self.assertEqual(payload["items"][0]["visual_source"], "OCR text")
        self.assertEqual(payload["items"][0]["audio_asr_source"], "ASR text")
        self.assertEqual(payload["target_language"], "Arabic")

    def test_indexed_translation_reorders_and_refills_only_missing_indexes(self) -> None:
        requests = []

        def fake_fetch(_url, request_data, _api_key, _timeout_seconds):
            payload = json.loads(request_data.decode("utf-8"))
            user = json.loads(payload["messages"][1]["content"])
            requests.append(user["requested_indexes"])
            if len(requests) == 1:
                translations = [
                    {"index": 3, "text": "three"},
                    {"index": 1, "text": "one"},
                ]
            else:
                translations = [{"index": 2, "text": "two"}]
            return {"choices": [{"message": {"content": json.dumps({"translations": translations})}}]}

        with (
            mock.patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}, clear=False),
            mock.patch.object(subtitle_tool, "fetch_chat_completion_json", side_effect=fake_fetch),
            mock.patch.object(subtitle_tool.time, "sleep"),
        ):
            result = subtitle_tool.chat_indexed_translations_openai_compatible(
                prompt="Return indexed JSON.",
                user_payload={"items": [{"index": 1}, {"index": 2}, {"index": 3}]},
                model="model",
                item_indexes=[1, 2, 3],
                log_label="AI 测试",
            )

        self.assertEqual(result, ["one", "two", "three"])
        self.assertEqual(requests, [[1, 2, 3], [2]])

    def test_indexed_translation_retries_malformed_json(self) -> None:
        responses = [
            {"choices": [{"message": {"content": '{"translations":[{"index":1,"text":"brok'}}]},
            {"choices": [{"message": {"content": json.dumps({"translations": [{"index": 1, "text": "fixed"}]})}}]},
        ]
        with (
            mock.patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}, clear=False),
            mock.patch.object(subtitle_tool, "fetch_chat_completion_json", side_effect=responses) as fetch,
            mock.patch.object(subtitle_tool.time, "sleep"),
        ):
            result = subtitle_tool.chat_indexed_translations_openai_compatible(
                prompt="Return indexed JSON.", user_payload={"items": [{"index": 1}]},
                model="model", item_indexes=[1], log_label="AI 测试",
            )
        self.assertEqual(result, ["fixed"])
        self.assertEqual(fetch.call_count, 2)

    def test_indexed_translation_salvages_complete_rows_and_keeps_full_context(self) -> None:
        requests = []
        responses = [
            {"choices": [{"message": {"content": (
                '{"translations":['
                '{"index":1,"text":"one"},'
                '{"index":2,"text":"two"},'
                '{"index":3,"text":"thr'
            )}}]},
            {"choices": [{"message": {"content": json.dumps({
                "translations": [{"index": 3, "text": "three"}]
            })}}]},
        ]

        def fake_fetch(_url, request_data, _api_key, _timeout_seconds):
            payload = json.loads(request_data.decode("utf-8"))
            user = json.loads(payload["messages"][1]["content"])
            requests.append((user["requested_indexes"], user["items"]))
            return responses[len(requests) - 1]

        full_items = [{"index": 1, "source": "a"}, {"index": 2, "source": "b"}, {"index": 3, "source": "c"}]
        with (
            mock.patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}, clear=False),
            mock.patch.object(subtitle_tool, "fetch_chat_completion_json", side_effect=fake_fetch),
            mock.patch.object(subtitle_tool.time, "sleep"),
        ):
            result = subtitle_tool.chat_indexed_translations_openai_compatible(
                prompt="Return indexed JSON.", user_payload={"items": full_items},
                model="model", item_indexes=[1, 2, 3], log_label="AI test",
            )

        self.assertEqual(result, ["one", "two", "three"])
        self.assertEqual([request[0] for request in requests], [[1, 2, 3], [3]])
        self.assertEqual(requests[0][1], full_items)
        self.assertEqual(requests[1][1], full_items)

    def test_indexed_translation_starts_recovery_round_after_empty_first_round(self) -> None:
        responses = [
            {"choices": [{"message": {"content": ""}}]},
            {"choices": [{"message": {"content": json.dumps({
                "translations": [{"index": 1, "text": "fixed"}]
            })}}]},
        ]
        lease = mock.MagicMock()
        lease.__enter__.return_value = 1
        with (
            mock.patch.dict("os.environ", {
                "OPENAI_API_KEY": "test-key",
                "LLM_MAX_ATTEMPTS": "1",
                "LLM_TRANSLATION_ROUNDS": "2",
            }, clear=False),
            mock.patch.object(subtitle_tool, "fetch_chat_completion_json", side_effect=responses),
            mock.patch.object(subtitle_tool, "global_llm_slot", return_value=lease) as slot,
            mock.patch.object(subtitle_tool.time, "sleep") as sleep,
        ):
            result = subtitle_tool.chat_indexed_translations_openai_compatible(
                prompt="Return indexed JSON.", user_payload={"items": [{"index": 1}]},
                model="model", item_indexes=[1], log_label="AI test",
            )

        self.assertEqual(result, ["fixed"])
        self.assertEqual(slot.call_args_list[0].args[0], 5)
        self.assertEqual(slot.call_args_list[1].args[0], 2)
        sleep.assert_called_once_with(5)

    def test_indexed_translation_rejects_duplicate_index(self) -> None:
        content = json.dumps({"translations": [
            {"index": 1, "text": "first"},
            {"index": 1, "text": "second"},
        ]})
        with self.assertRaisesRegex(RuntimeError, "重复返回"):
            subtitle_tool._parse_indexed_translation_content(content, [1])

    def test_srt_time_rounding_carries_into_next_minute(self) -> None:
        self.assertEqual(subtitle_tool.seconds_to_srt_time(59.9996), "00:01:00,000")

    def test_small_ocr_jitter_is_treated_as_same_subtitle(self) -> None:
        self.assertTrue(subtitle_tool.ocr_texts_match("pipelinesmoketest", "pipelinesmoketest0"))
        self.assertFalse(subtitle_tool.ocr_texts_match("first subtitle", "a different line"))

    def test_line_break_marker_only_protects_lf(self) -> None:
        source = "first\nsecond\rthird"
        protected = subtitle_tool.protect_line_breaks_for_llm(source)
        self.assertEqual(protected, f"first{subtitle_tool.LINE_BREAK_MARKER}second\rthird")
        self.assertEqual(subtitle_tool.restore_line_breaks_from_llm(protected), source)

    def test_clean_translation_replaces_ocr_line_breaks_with_spaces(self) -> None:
        self.assertEqual(subtitle_tool.clean_text_for_translation("Mom\nare you\nhere ?"), "Mom are you here ?")
        self.assertEqual(subtitle_tool.clean_text_for_translation("  7  "), "")

    def test_clean_translation_removes_isolated_ocr_digits(self) -> None:
        self.assertEqual(subtitle_tool.clean_text_for_translation("يبدو أننا 5 9"), "يبدو أننا")
        self.assertEqual(subtitle_tool.clean_text_for_translation("سيارة فراري ليس 9"), "سيارة فراري ليس")
        self.assertEqual(subtitle_tool.clean_text_for_translation("room 9"), "room 9")

    def test_asr_cleaning_preserves_standalone_digits(self) -> None:
        self.assertEqual(subtitle_tool.clean_text_for_translation("I need 5", "asr"), "I need 5")
        self.assertEqual(subtitle_tool.clean_text_for_translation("  7  ", "asr"), "7")

    def test_ocr_match_ignores_isolated_digit_noise(self) -> None:
        self.assertTrue(subtitle_tool.ocr_texts_match(
            subtitle_tool.normalize_ocr_text("يبدو أننا"),
            subtitle_tool.normalize_ocr_text("يبدو أننا 5 9"),
        ))

    def test_wrap_subtitle_text_limits_to_two_lines(self) -> None:
        wrapped = subtitle_tool.wrap_subtitle_text("one two three four five six seven", 14)
        self.assertLessEqual(len(wrapped.splitlines()), 2)
        self.assertIn("\n", wrapped)

    def test_entire_video_is_sent_in_one_translation_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, "source.srt")
            output = Path(directory, "output.srt")
            items = [
                subtitle_tool.SubtitleItem(index, "00:00:00,000", "00:00:01,000", f"line {index}")
                for index in range(1, 243)
            ]
            subtitle_tool.write_srt(items, source)
            with mock.patch.object(
                subtitle_tool,
                "translate_texts_openai_compatible",
                side_effect=lambda texts, *_args: [f"translated {index}" for index, _ in enumerate(texts, 1)],
            ) as translate:
                subtitle_tool.translate_srt(source, output, "English", "auto", "openai-compatible", "model", 99)

            self.assertEqual(translate.call_count, 1)
            self.assertEqual(len(translate.call_args.args[0]), 242)
            self.assertEqual(len(subtitle_tool.parse_srt(output)), 242)

    def test_subtitles_over_500_are_sent_in_500_item_batches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, "source.srt")
            output = Path(directory, "output.srt")
            items = [
                subtitle_tool.SubtitleItem(index, "00:00:00,000", "00:00:01,000", f"line {index}")
                for index in range(1, 1002)
            ]
            subtitle_tool.write_srt(items, source)
            with mock.patch.object(
                subtitle_tool,
                "translate_texts_openai_compatible",
                side_effect=lambda texts, *_args: [f"translated {index}" for index, _ in enumerate(texts, 1)],
            ) as translate:
                subtitle_tool.translate_srt(source, output, "English", "auto", "openai-compatible", "model", 99)

            self.assertEqual([len(call.args[0]) for call in translate.call_args_list], [500, 500, 1])
            self.assertEqual(len(subtitle_tool.parse_srt(output)), 1001)

    def test_translation_review_uses_one_initial_model_and_one_reviewer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory, "source.srt")
            output = Path(directory, "output.srt")
            subtitle_tool.write_srt(
                [subtitle_tool.SubtitleItem(1, "00:00:00,000", "00:00:01,000", "source line")],
                source,
            )
            with (
                mock.patch.object(
                    subtitle_tool,
                    "translate_texts_openai_compatible",
                    return_value=["translation a"],
                ) as translate,
                mock.patch.object(
                    subtitle_tool,
                    "review_single_translation_openai_compatible",
                    return_value=["final subtitle"],
                ) as review,
            ):
                subtitle_tool.translate_srt(
                    source,
                    output,
                    "English",
                    "auto",
                    "openai-compatible",
                    "model-a",
                    1,
                    True,
                    "model-b",
                    "model-c",
                )

            self.assertEqual([call.args[3] for call in translate.call_args_list], ["model-a"])
            review.assert_called_once()
            self.assertEqual(review.call_args.args[3], "model-c")
            self.assertEqual(subtitle_tool.parse_srt(output)[0].text, "final subtitle")

    def test_word_alignment_splits_asr_without_expanding_visual_timing(self) -> None:
        visual = [
            subtitle_tool.SubtitleItem(1, "00:00:01,000", "00:00:02,000", "hello world"),
            subtitle_tool.SubtitleItem(2, "00:00:02,000", "00:00:03,000", "next line"),
        ]
        words = [
            subtitle_tool.AsrWord(" Hello", 1.10, 1.35, 0.98, 1),
            subtitle_tool.AsrWord(" world", 1.35, 1.75, 0.96, 1),
            subtitle_tool.AsrWord(" Next", 2.10, 2.35, 0.95, 1),
            subtitle_tool.AsrWord(" line", 2.35, 2.70, 0.94, 1),
        ]

        pairs = subtitle_tool.align_visual_and_audio_subtitles(visual, [], audio_words=words)

        self.assertEqual([pair.audio_text for pair in pairs], ["Hello world", "Next line"])
        self.assertEqual(pairs[0].start, "00:00:01,000")
        self.assertEqual(pairs[0].end, "00:00:02,000")
        self.assertGreater(pairs[0].audio_confidence, 0.9)

    def test_confidence_scoring_separates_agreement_from_conflict(self) -> None:
        agreed = subtitle_tool.AlignedSubtitlePair(
            1, "00:00:01,000", "00:00:02,000", "Don't be a hero", "Don't be a hero", 0.96, 1.0
        )
        conflicted = subtitle_tool.AlignedSubtitlePair(
            2, "00:00:02,000", "00:00:03,000", "dumb luck 9", "give just a dumb luck", 0.62, 0.7
        )

        subtitle_tool.score_aligned_pair(agreed, "English", "English")
        subtitle_tool.score_aligned_pair(conflicted, "English", "English")

        self.assertGreaterEqual(agreed.confidence_score, 0.82)
        self.assertLess(conflicted.confidence_score, 0.82)

    def test_cross_language_confidence_uses_evidence_quality_not_text_similarity(self) -> None:
        clean = subtitle_tool.AlignedSubtitlePair(
            1, "00:00:01,000", "00:00:02,000", "I know you hate me", "我知道你恨我", 0.97, 1.0
        )
        noisy = subtitle_tool.AlignedSubtitlePair(
            2, "00:00:02,000", "00:00:03,000", "S", "我现在就走", 0.96, 1.0
        )

        subtitle_tool.score_aligned_pair(clean, "English", "Chinese")
        subtitle_tool.score_aligned_pair(noisy, "English", "Chinese")

        self.assertGreaterEqual(clean.confidence_score, 0.82)
        self.assertLess(noisy.confidence_score, 0.82)
        self.assertIn("cross-language", clean.confidence_reason)

    def test_output_cleanup_drops_empty_and_merges_only_exact_adjacent_text(self) -> None:
        items = [
            subtitle_tool.SubtitleItem(1, "00:00:00,000", "00:00:00,500", ""),
            subtitle_tool.SubtitleItem(2, "00:00:00,500", "00:00:01,000", "مرحبا"),
            subtitle_tool.SubtitleItem(3, "00:00:01,000", "00:00:01,500", "  مرحبا  "),
            subtitle_tool.SubtitleItem(4, "00:00:01,500", "00:00:02,000", "مرحبا؟"),
            subtitle_tool.SubtitleItem(5, "00:00:03,000", "00:00:03,500", "مرحبا؟"),
        ]

        output, stats = subtitle_tool.clean_and_merge_output_subtitles(items)

        self.assertEqual([(item.start, item.end, item.text) for item in output], [
            ("00:00:00,500", "00:00:01,500", "مرحبا"),
            ("00:00:01,500", "00:00:02,000", "مرحبا؟"),
            ("00:00:03,000", "00:00:03,500", "مرحبا؟"),
        ])
        self.assertEqual(stats["removed_empty"], 1)
        self.assertEqual(stats["merged_adjacent_duplicates"], 1)

    def test_episode_review_sends_all_items_and_keeps_risk_diagnostics(self) -> None:
        pairs = [
            subtitle_tool.AlignedSubtitlePair(1, "00:00:01,000", "00:00:02,000", "same", "same", 0.98, 1.0, 0.95, "high"),
            subtitle_tool.AlignedSubtitlePair(2, "00:00:02,000", "00:00:03,000", "broken 9", "spoken", 0.6, 0.7, 0.5, "conflict"),
        ]
        response = {
            "review": {"bigger_problem": "visual_source", "summary": "fixed", "examples": []},
            "edits": [
                {"action": "replace", "indexes": [1], "text": "reviewed first"},
                {"action": "replace", "indexes": [2], "text": "reviewed second"},
            ],
            "subtitles": [],
        }
        with mock.patch.object(subtitle_tool, "chat_json_object_openai_compatible", return_value=response) as chat:
            result = subtitle_tool.review_risky_dual_source_translations_openai_compatible(
                pairs, "Arabic", "English", "English", "deepseek-v4-flash",
                ["first", "initial second"], 0.82,
            )

        self.assertEqual(result, ["reviewed first", "reviewed second"])
        self.assertEqual(chat.call_args.kwargs["expected_count"], 0)
        self.assertEqual([item["index"] for item in chat.call_args.kwargs["user_payload"]["items"]], [1, 2])
        self.assertIn("word-for-word", chat.call_args.kwargs["prompt"])
        self.assertIn("sparse edit operations", chat.call_args.kwargs["prompt"])

    def test_full_episode_reviewer_may_delete_contextual_noise(self) -> None:
        pair = subtitle_tool.AlignedSubtitlePair(
            1, "00:00:01,000", "00:00:02,000", "心", "", 0.0, 1.0, 0.1, "visual-only"
        )
        response = {"review": {}, "edits": [{"action": "delete", "indexes": [1], "text": ""}], "subtitles": []}
        with mock.patch.object(subtitle_tool, "chat_json_object_openai_compatible", return_value=response):
            result = subtitle_tool.review_risky_dual_source_translations_openai_compatible(
                [pair], "Arabic", "English", "Chinese", "deepseek-v4-pro", ["قلب"], 0.82
            )

        self.assertEqual(result, [""])

    def test_full_episode_reviewer_may_delete_misaligned_asr_fragment(self) -> None:
        pair = subtitle_tool.AlignedSubtitlePair(
            1, "00:00:01,000", "00:00:02,000", "S", "走", 0.4, 1.0, 0.1, "conflict"
        )
        response = {"review": {}, "edits": [{"action": "delete", "indexes": [1], "text": ""}], "subtitles": []}
        with mock.patch.object(subtitle_tool, "chat_json_object_openai_compatible", return_value=response):
            result = subtitle_tool.review_risky_dual_source_translations_openai_compatible(
                [pair], "Arabic", "English", "Chinese", "deepseek-v4-pro", ["اذهب"], 0.82
            )

        self.assertEqual(result, [""])

    def test_episode_review_can_merge_fragments_by_returning_identical_text(self) -> None:
        pairs = [
            subtitle_tool.AlignedSubtitlePair(1, "00:00:01,000", "00:00:01,500", "I", "我"),
            subtitle_tool.AlignedSubtitlePair(2, "00:00:01,500", "00:00:02,000", "feel much", "已经好"),
            subtitle_tool.AlignedSubtitlePair(3, "00:00:02,000", "00:00:02,500", "better", "多了"),
        ]
        response = {"review": {"entities": []}, "edits": [{
            "action": "merge", "indexes": [1, 2, 3], "text": "أشعر بتحسن كبير"
        }], "subtitles": []}
        with mock.patch.object(subtitle_tool, "chat_json_object_openai_compatible", return_value=response):
            final = subtitle_tool.review_risky_dual_source_translations_openai_compatible(
                pairs, "Arabic", "English", "Chinese", "review-model", ["أنا", "أفضل", "الآن"], 0.82
            )
        output, stats = subtitle_tool.clean_and_merge_output_subtitles(
            [subtitle_tool.SubtitleItem(i + 1, pair.start, pair.end, text) for i, (pair, text) in enumerate(zip(pairs, final))]
        )
        self.assertEqual([(item.start, item.end, item.text) for item in output], [
            ("00:00:01,000", "00:00:02,500", "أشعر بتحسن كبير")
        ])
        self.assertEqual(stats["merged_adjacent_duplicates"], 2)

    def test_episode_review_rejects_bulk_delete_response(self) -> None:
        pairs = [
            subtitle_tool.AlignedSubtitlePair(
                index, f"00:00:{index - 1:02d},000", f"00:00:{index:02d},000", "visual", "audio"
            )
            for index in range(1, 21)
        ]
        initial = [f"line {index}" for index in range(1, 21)]
        final, stats = subtitle_tool.apply_episode_review_edits(
            pairs,
            initial,
            [{"action": "delete", "indexes": list(range(1, 21)), "text": ""}],
        )
        self.assertEqual(final, initial)
        self.assertEqual(stats["delete"], 0)
        self.assertEqual(stats["safety_rejected"], 1)

    def test_episode_review_caps_total_rewrite_surface_at_forty_percent(self) -> None:
        timed = [
            subtitle_tool.SubtitleItem(
                index, f"00:00:{(index - 1) % 60:02d},000", f"00:00:{index % 60:02d},000", "source"
            )
            for index in range(1, 101)
        ]
        initial = [f"line {index}" for index in range(1, 101)]
        edits = [
            {"action": "replace", "indexes": [index], "text": f"reviewed {index}"}
            for index in range(1, 42)
        ]

        final, stats = subtitle_tool.apply_episode_review_edits(timed, initial, edits)

        self.assertEqual(stats["replace"], 40)
        self.assertEqual(stats["safety_rejected"], 1)
        self.assertEqual(final[39], "reviewed 40")
        self.assertEqual(final[40], "line 41")

    def test_single_source_reviewer_uses_sparse_edits(self) -> None:
        timed = [
            subtitle_tool.SubtitleItem(1, "00:00:01,000", "00:00:02,000", "hello"),
            subtitle_tool.SubtitleItem(2, "00:00:02,000", "00:00:03,000", "world"),
        ]
        response = {
            "review": {"entities": []},
            "edits": [{"action": "replace", "indexes": [2], "text": "العالم"}],
            "subtitles": [],
        }
        with mock.patch.object(
            subtitle_tool, "chat_json_object_openai_compatible", return_value=response
        ) as chat:
            result = subtitle_tool.review_single_translation_openai_compatible(
                ["hello", "world"], "Arabic", "English", "review-model",
                ["مرحبا", "دنيا"], timed_items=timed,
            )
        self.assertEqual(result, ["مرحبا", "العالم"])
        self.assertEqual(chat.call_args.kwargs["expected_count"], 0)
        self.assertIn("sparse edits", chat.call_args.kwargs["prompt"])

    def test_series_replacements_use_exact_unicode_word_boundaries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "episode.srt"
            subtitle_tool.write_srt([
                subtitle_tool.SubtitleItem(1, "00:00:01,000", "00:00:02,000", "آنسة شين، زوي هنا"),
                subtitle_tool.SubtitleItem(2, "00:00:02,000", "00:00:03,000", "كلمة شينكو لا تتغير"),
            ], path)
            evidence = {
                "1": {"rows": {1: {"target": ["آنسة شين، زوي هنا"]}}, "reports": [{"entities": [{
                    "kind": "family", "target_variants": ["شين", "سبنس"], "evidence_indexes": [1]
                }]}]},
                "2": {"rows": {1: {"target": ["عائلة سبنس"]}}, "reports": [{"entities": [{
                    "kind": "family", "target_variants": ["شين", "سبنس"], "evidence_indexes": [1]
                }]}]},
            }
            stats = subtitle_tool.apply_series_consistency_replacements(
                [path], [{
                    "kind": "family", "from": "شين", "to": "سبنس", "confidence": 0.95,
                    "evidence": [{"episode": 1, "indexes": [1]}, {"episode": 2, "indexes": [1]}],
                }], evidence,
            )
            texts = [item.text for item in subtitle_tool.parse_srt(path)]
        self.assertEqual(texts, ["آنسة سبنس، زوي هنا", "كلمة شينكو لا تتغير"])
        self.assertEqual(stats["changed_occurrences"], 1)

    def test_series_consistency_uses_episode_reviewer_entities(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            record_path = root / "record-1.json"
            record_path_2 = root / "record-2.json"
            subtitle_path = root / "episode-1.srt"
            subtitle_path_2 = root / "episode-2.srt"
            report_path = root / "series.json"
            record_path.write_text(json.dumps({
                "video": {"index": 1, "input": "episode.mp4"},
                "pipeline": {"visual_language": "English"},
                "items": [{"index": 1, "final_translation": "عائلة شين"}],
                "reviews": [{"report": {"entities": [{
                    "kind": "family", "source_aliases": ["Spence", "沈家"],
                    "target_variants": ["سبنس", "شين"], "preferred_target": "سبنس",
                    "evidence_indexes": [1], "confidence": 0.95,
                }]}}],
            }, ensure_ascii=False), encoding="utf-8")
            record_path_2.write_text(json.dumps({
                "video": {"index": 2, "input": "episode-2.mp4"},
                "pipeline": {"visual_language": "English"},
                "items": [{"index": 1, "final_translation": "عائلة سبنس"}],
                "reviews": [{"report": {"entities": [{
                    "kind": "family", "source_aliases": ["Spence", "沈家"],
                    "target_variants": ["سبنس", "شين"], "preferred_target": "سبنس",
                    "evidence_indexes": [1], "confidence": 0.95,
                }]}}],
            }, ensure_ascii=False), encoding="utf-8")
            subtitle_tool.write_srt([
                subtitle_tool.SubtitleItem(1, "00:00:01,000", "00:00:02,000", "عائلة شين")
            ], subtitle_path)
            subtitle_tool.write_srt([
                subtitle_tool.SubtitleItem(1, "00:00:01,000", "00:00:02,000", "عائلة سبنس")
            ], subtitle_path_2)
            response = {
                "review": {"summary": "unified"},
                "consistency": {
                    "decisions": [{"kind": "family", "canonical_target": "سبنس"}],
                    "replacements": [{
                        "kind": "family", "from": "شين", "to": "سبنس",
                        "confidence": 0.95,
                        "evidence": [{"episode": 1, "indexes": [1]}, {"episode": 2, "indexes": [1]}],
                    }],
                },
                "subtitles": [],
            }
            with mock.patch.object(subtitle_tool, "chat_json_object_openai_compatible", return_value=response) as chat:
                report = subtitle_tool.review_series_consistency_openai_compatible(
                    [record_path, record_path_2], [subtitle_path, subtitle_path_2], "Arabic", "review-model", report_path
                )

            self.assertEqual(chat.call_args.kwargs["expected_count"], 0)
            self.assertEqual(subtitle_tool.parse_srt(subtitle_path)[0].text, "عائلة سبنس")
            self.assertEqual(report["apply_stats"]["changed_occurrences"], 1)
            synced = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(synced["items"][0]["final_translation"], "عائلة سبنس")
            self.assertEqual(json.loads(report_path.read_text(encoding="utf-8"))["status"], "completed")

    def test_series_consistency_rejects_single_episode_or_low_confidence_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "episode.srt"
            subtitle_tool.write_srt([
                subtitle_tool.SubtitleItem(1, "00:00:01,000", "00:00:02,000", "زوي وجوليا")
            ], path)
            stats = subtitle_tool.apply_series_consistency_replacements([path], [
                {"kind": "person", "from": "جوليا", "to": "زوي", "confidence": 0.99, "evidence_episode_count": 1},
                {"kind": "person", "from": "زوي", "to": "جوليا", "confidence": 0.7, "evidence_episode_count": 5},
            ])
            text = subtitle_tool.parse_srt(path)[0].text
        self.assertEqual(text, "زوي وجوليا")
        self.assertEqual(stats["validated_replacements"], 0)

    def test_series_consistency_rejects_chained_replacements(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            path = Path(temp_name) / "episode.srt"
            subtitle_tool.write_srt([
                subtitle_tool.SubtitleItem(1, "00:00:01,000", "00:00:02,000", "Alice meets Bob")
            ], path)
            evidence = {
                "1": {"rows": {1: {"target": ["Alice meets Bob"]}}, "reports": [{"entities": [{
                    "kind": "person", "target_variants": ["Alice", "Bob", "Carol"], "evidence_indexes": [1]
                }]}]},
                "2": {"rows": {1: {"target": ["Alice and Bob"]}}, "reports": [{"entities": [{
                    "kind": "person", "target_variants": ["Alice", "Bob", "Carol"], "evidence_indexes": [1]
                }]}]},
            }
            refs = [{"episode": 1, "indexes": [1]}, {"episode": 2, "indexes": [1]}]
            stats = subtitle_tool.apply_series_consistency_replacements([path], [
                {"kind": "person", "from": "Alice", "to": "Bob", "confidence": 0.99, "evidence": refs},
                {"kind": "person", "from": "Bob", "to": "Carol", "confidence": 0.99, "evidence": refs},
            ], evidence)
            text = subtitle_tool.parse_srt(path)[0].text
        self.assertEqual(text, "Alice meets Bob")
        self.assertEqual(stats["validated_replacements"], 0)
        self.assertEqual(stats["rejected_replacements"], 2)

    def test_review_may_delete_unmistakable_ocr_debris_without_asr(self) -> None:
        pair = subtitle_tool.AlignedSubtitlePair(
            1, "00:00:01,000", "00:00:02,000", "GT", "", 0.0, 1.0, 0.1, "visual-only"
        )
        response = {"review": {}, "edits": [{"action": "delete", "indexes": [1], "text": ""}], "subtitles": []}
        with mock.patch.object(subtitle_tool, "chat_json_object_openai_compatible", return_value=response):
            result = subtitle_tool.review_risky_dual_source_translations_openai_compatible(
                [pair], "Arabic", "English", "Chinese", "deepseek-v4-pro", ["GT"], 0.82
            )

        self.assertEqual(result, [""])

    def test_dual_translation_prompt_requires_idiom_meaning(self) -> None:
        pair = subtitle_tool.AlignedSubtitlePair(
            1,
            "00:00:01,000",
            "00:00:03,000",
            "10 million just fell right into my lap",
            "Ten million just fell right into my lap.",
        )
        with mock.patch.object(
            subtitle_tool, "chat_indexed_translations_openai_compatible", return_value=["حصلت على عشرة ملايين بسهولة"]
        ) as chat:
            result = subtitle_tool.translate_dual_source_texts_openai_compatible(
                [pair], "Arabic", "English", "English", "deepseek-v4-flash"
            )

        self.assertEqual(result, ["حصلت على عشرة ملايين بسهولة"])
        prompt = chat.call_args.kwargs["prompt"]
        self.assertIn("idioms", prompt)
        self.assertIn("fall right into my lap", prompt)
        self.assertIn("not the literal image", prompt)
        self.assertIn("Infer the drama's genre", prompt)
        self.assertNotIn("متصدر الامتحان الإمبراطوري", prompt)

    def test_selected_glossary_is_injected_into_dual_translation_prompt(self) -> None:
        pair = subtitle_tool.AlignedSubtitlePair(
            1, "00:00:01,000", "00:00:03,000", "The prince consort is here", "驸马到了"
        )
        glossary = subtitle_tool.load_glossary_file(
            Path(__file__).with_name("glossaries") / "chinese_history_zh_en_ar.json"
        )
        glossary_prompt = subtitle_tool.build_glossary_prompt(glossary)
        with mock.patch.object(
            subtitle_tool, "chat_indexed_translations_openai_compatible", return_value=["وصل زوج الأميرة"]
        ) as chat:
            subtitle_tool.translate_dual_source_texts_openai_compatible(
                [pair], "Arabic", "English", "Chinese", "deepseek-v4-flash", "ocr", glossary_prompt
            )

        prompt = chat.call_args.kwargs["prompt"]
        self.assertIn("GLOSSARY_JSON=", prompt)
        self.assertIn('"zh":"驸马"', prompt)
        self.assertIn('"ar":"زوج الأميرة"', prompt)

    def test_review_model_returns_report_and_subtitles(self) -> None:
        captured = {}

        def fake_fetch(_url, request_data, _api_key, _timeout_seconds):
            captured.update(json.loads(request_data.decode("utf-8")))
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "review": {
                                        "bigger_problem": "source",
                                        "summary": "OCR has obvious digit noise.",
                                        "examples": [{"index": 1, "issue": "removed stray 9"}],
                                    },
                                    "subtitles": ["It seems we are"],
                                }
                            )
                        }
                    }
                ]
            }

        with (
            mock.patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}, clear=False),
            mock.patch.object(subtitle_tool, "fetch_chat_completion_json", side_effect=fake_fetch),
        ):
            result = subtitle_tool.review_translations_openai_compatible(
                ["يبدو أننا 9"],
                "English",
                "Arabic",
                "review-model",
                ["It seems we are 9"],
                ["It seems we are"],
                "ocr",
            )

        self.assertEqual(result, ["It seems we are"])
        self.assertIn("review", captured["messages"][0]["content"])
        user_payload = json.loads(captured["messages"][1]["content"])
        self.assertEqual(user_payload["task"], "review_two_subtitle_translations")
        self.assertEqual(user_payload["source_kind"], "ocr")

    def test_invalid_api_shape_has_readable_error(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"error": "bad response"}).encode()
        with (
            mock.patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}, clear=False),
            mock.patch("subtitle_tool.urllib.request.urlopen", return_value=response),
            mock.patch.object(subtitle_tool.time, "sleep"),
        ):
            with self.assertRaisesRegex(RuntimeError, "返回格式无效"):
                subtitle_tool.translate_texts_openai_compatible(["hello"], "English", "auto", "model")

    def test_translation_prompt_allows_ocr_cleanup(self) -> None:
        captured = {}

        def fake_fetch(_url, request_data, _api_key, _timeout_seconds):
            captured.update(json.loads(request_data.decode("utf-8")))
            return {"choices": [{"message": {"content": json.dumps(["It seems we are"])}}]}

        with (
            mock.patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}, clear=False),
            mock.patch.object(subtitle_tool, "fetch_chat_completion_json", side_effect=fake_fetch),
        ):
            result = subtitle_tool.translate_texts_openai_compatible(["يبدو أننا 5 9"], "English", "Arabic", "model")

        self.assertEqual(result, ["It seems we are"])
        self.assertEqual(captured["temperature"], 0.1)
        self.assertIn("OCR subtitle cleaner", captured["messages"][0]["content"])
        user_payload = json.loads(captured["messages"][1]["content"])
        self.assertEqual(user_payload["task"], "translate_subtitles")
        self.assertEqual(user_payload["source_kind"], "ocr")

    def test_asr_translation_prompt_uses_light_cleanup(self) -> None:
        captured = {}

        def fake_fetch(_url, request_data, _api_key, _timeout_seconds):
            captured.update(json.loads(request_data.decode("utf-8")))
            return {"choices": [{"message": {"content": json.dumps(["I need 5"])}}]}

        with (
            mock.patch.dict("os.environ", {"OPENAI_API_KEY": "test-key"}, clear=False),
            mock.patch.object(subtitle_tool, "fetch_chat_completion_json", side_effect=fake_fetch),
        ):
            result = subtitle_tool.translate_texts_openai_compatible(["I need 5"], "English", "English", "model", "asr")

        self.assertEqual(result, ["I need 5"])
        self.assertIn("ASR subtitle translator", captured["messages"][0]["content"])
        user_payload = json.loads(captured["messages"][1]["content"])
        self.assertEqual(user_payload["source_kind"], "asr")
        self.assertEqual(user_payload["items"], [{"index": 1, "source": "I need 5"}])
        self.assertEqual(user_payload["requested_indexes"], [1])

    def test_render_uses_requested_quality(self) -> None:
        commands = []

        def capture(command, dry_run=False):
            commands.append(command)

        with tempfile.TemporaryDirectory() as directory:
            subtitle = Path(directory, "subtitle.srt")
            subtitle_tool.write_srt([subtitle_tool.SubtitleItem(1, "00:00:00,000", "00:00:01,000", "hello world")], subtitle)
            with (
                mock.patch.object(subtitle_tool.video_dedup, "resolve_hardware_acceleration", return_value="nvidia"),
                mock.patch.object(subtitle_tool, "run", side_effect=capture),
            ):
                subtitle_tool.render_subtitle(
                    Path("video.mp4"), subtitle, Path("output.mp4"),
                    "burn", "replace", "bottom", False, 0.0, 74.0, 100.0, 11.0, 0.72, "white", False,
                    "auto", "Arial", 28, 15, "nvidia", "ffmpeg", False,
                )

        self.assertEqual(len(commands), 1)
        cq_index = commands[0].index("-cq")
        self.assertEqual(commands[0][cq_index + 1], "15")

    def test_cover_mask_is_enabled_only_during_subtitle_times(self) -> None:
        commands = []

        def capture(command, dry_run=False):
            commands.append(command)

        with tempfile.TemporaryDirectory() as directory:
            subtitle = Path(directory, "subtitle.srt")
            subtitle_tool.write_srt(
                [
                    subtitle_tool.SubtitleItem(1, "00:00:01,000", "00:00:02,000", "hello world"),
                    subtitle_tool.SubtitleItem(2, "00:00:04,000", "00:00:05,000", "next line"),
                ],
                subtitle,
            )
            with (
                mock.patch.object(subtitle_tool.video_dedup, "resolve_hardware_acceleration", return_value="cpu"),
                mock.patch.object(subtitle_tool, "run", side_effect=capture),
            ):
                subtitle_tool.render_subtitle(
                    Path("video.mp4"), subtitle, Path("output.mp4"),
                    "burn", "replace", "bottom", True, 0.0, 74.0, 100.0, 11.0, 0.82, "white", False,
                    "auto", "Arial", 28, 15, "cpu", "ffmpeg", False,
                )

        vf = commands[0][commands[0].index("-vf") + 1]
        self.assertIn("drawbox=", vf)
        self.assertIn("enable='between(t,0.960,2.040)+between(t,3.960,5.040)'", vf)
        self.assertIn("subtitles=", vf)

    def test_ass_render_uses_selected_font_name(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "render.ass")
            subtitle_tool.write_ass_for_render(
                [subtitle_tool.SubtitleItem(1, "00:00:00,000", "00:00:01,000", "hello")],
                output,
                1080,
                1920,
                "replace",
                "Microsoft YaHei",
                30,
                "bottom",
                0.0,
                74.0,
                100.0,
                11.0,
            )
            content = output.read_text(encoding="utf-8-sig")

        self.assertIn("PlayResX: 1080", content)
        self.assertIn("PlayResY: 1920", content)
        self.assertIn("Style: Default,Microsoft YaHei,30", content)
        self.assertIn(r"{\an5\pos(540.0,1526.4)}hello", content)

    def test_dual_translation_writes_structured_diagnostic_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            visual = root / "visual.srt"
            audio = root / "audio.srt"
            output = root / "translated.srt"
            record_path = root / "record.json"
            subtitle_tool.write_srt(
                [subtitle_tool.SubtitleItem(1, "00:00:00,000", "00:00:02,000", "Hello 9")], visual
            )
            subtitle_tool.write_srt(
                [subtitle_tool.SubtitleItem(1, "00:00:00,100", "00:00:01,900", "Hello")], audio
            )
            review_response = {
                "review": {"bigger_problem": "visual_source", "summary": "Removed OCR noise.", "examples": []},
                "edits": [{"action": "replace", "indexes": [1], "text": "مرحبا"}],
                "subtitles": [],
            }
            glossary = subtitle_tool.load_glossary_file(
                Path(__file__).with_name("glossaries") / "chinese_history_zh_en_ar.json"
            )
            with (
                mock.patch.object(
                    subtitle_tool,
                    "translate_dual_source_texts_openai_compatible",
                    return_value=["مرحبا 9"],
                ),
                mock.patch.object(
                    subtitle_tool,
                    "chat_json_object_openai_compatible",
                    return_value=review_response,
                ),
            ):
                subtitle_tool.translate_dual_source_srts(
                    visual,
                    audio,
                    output,
                    "Arabic",
                    "English",
                    "English",
                    "deepseek-v4-flash",
                    enable_review=True,
                    review_model="deepseek-v4-flash",
                    confidence_threshold=1.0,
                    translation_record_path=record_path,
                    record_context={"input": "sample.mp4", "index": 1, "total": 1},
                    glossary=glossary,
                )

            record = json.loads(record_path.read_text(encoding="utf-8"))
            self.assertEqual(record["status"], "completed")
            self.assertEqual(record["video"]["input"], "sample.mp4")
            self.assertEqual(record["items"][0]["visual_source_raw"], "Hello 9")
            self.assertEqual(record["items"][0]["audio_asr_raw"], "Hello")
            self.assertEqual(record["items"][0]["initial_translation"], "مرحبا 9")
            self.assertEqual(record["items"][0]["final_translation"], "مرحبا")
            self.assertEqual(record["reviews"][0]["report"]["bigger_problem"], "visual_source")
            self.assertEqual(record["pipeline"]["glossary"]["id"], "chinese_history_zh_en_ar")
            self.assertNotIn("api_key", record)

    def test_bilingual_ass_is_constrained_to_manual_rectangle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory, "render.ass")
            subtitle_tool.write_ass_for_render(
                [subtitle_tool.SubtitleItem(1, "00:00:00,000", "00:00:01,000", "hello")],
                output,
                1080,
                1920,
                "bilingual",
                "Arial",
                30,
                "top",
                10.0,
                60.0,
                80.0,
                15.0,
            )
            content = output.read_text(encoding="utf-8-sig")

        self.assertIn(",8,108,108,45,1", content)
        self.assertIn(r"{\an8\pos(540.0,1167.0)}hello", content)

    def test_bilingual_render_wraps_to_manual_width_without_white_mask(self) -> None:
        commands = []
        prepared_widths = []

        def capture_prepare(input_srt, video_width, cover_width_percent, font_size):
            prepared_widths.append(cover_width_percent)
            return [subtitle_tool.SubtitleItem(1, "00:00:00,000", "00:00:01,000", "hello")]

        def capture(command, dry_run=False):
            commands.append(command)

        with tempfile.TemporaryDirectory() as directory:
            subtitle = Path(directory, "subtitle.srt")
            subtitle_tool.write_srt(
                [subtitle_tool.SubtitleItem(1, "00:00:00,000", "00:00:01,000", "hello")],
                subtitle,
            )
            with (
                mock.patch.object(subtitle_tool, "prepare_items_for_ass_render", side_effect=capture_prepare),
                mock.patch.object(subtitle_tool.video_dedup, "resolve_hardware_acceleration", return_value="cpu"),
                mock.patch.object(subtitle_tool, "run", side_effect=capture),
            ):
                subtitle_tool.render_subtitle(
                    Path("video.mp4"), subtitle, Path("output.mp4"),
                    "burn", "bilingual", "top", True, 10.0, 60.0, 35.0, 15.0, 0.82, "white", False,
                    "auto", "Arial", 28, 15, "cpu", "ffmpeg", False,
                )

        self.assertEqual(prepared_widths, [35.0])
        vf = commands[0][commands[0].index("-vf") + 1]
        self.assertNotIn("drawbox=", vf)
        self.assertIn("subtitles=", vf)

    def test_render_falls_back_to_cpu_when_gpu_encoding_fails(self) -> None:
        commands = []

        def fail_once(command, dry_run=False):
            commands.append(command)
            if len(commands) == 1:
                raise subprocess.CalledProcessError(1, command)

        with tempfile.TemporaryDirectory() as directory:
            subtitle = Path(directory, "subtitle.srt")
            subtitle_tool.write_srt([subtitle_tool.SubtitleItem(1, "00:00:00,000", "00:00:01,000", "hello world")], subtitle)
            with (
                mock.patch.object(subtitle_tool.video_dedup, "resolve_hardware_acceleration", return_value="nvidia"),
                mock.patch.object(subtitle_tool, "run", side_effect=fail_once),
            ):
                subtitle_tool.render_subtitle(
                    Path("video.mp4"), subtitle, Path("output.mp4"),
                    "burn", "replace", "bottom", False, 0.0, 74.0, 100.0, 11.0, 0.72, "white", False,
                    "auto", "Arial", 28, 15, "nvidia", "ffmpeg", False,
                )

        self.assertEqual(len(commands), 2)
        self.assertIn("h264_nvenc", commands[0])
        self.assertIn("libx264", commands[1])


if __name__ == "__main__":
    unittest.main()
