from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).parent))
import video_dedup as MODULE


class CommandTests(unittest.TestCase):
    def test_observed_presets_and_screenshot_default(self):
        expected = {
            "custom": (3, 2, 0.20, 0.12, 0.12, 1.04, False),
            "light": (1, 10, 0.05, 0.04, 0.10, 1.03, False),
            "medium": (2, 15, 0.08, 0.06, 0.14, 1.06, False),
            "strong": (4, 20, 0.10, 0.10, 0.16, 1.10, False),
            "deep": (6, 30, 0.10, 0.12, 0.16, 1.12, True),
        }
        self.assertEqual(set(MODULE.PRESETS), set(expected))
        for name, values in expected.items():
            config = MODULE.PRESETS[name]
            self.assertEqual(
                (
                    config.crop_percent, config.zoom_percent,
                    config.color_opacity, config.effect_opacity,
                    config.brightness, config.speed, config.mirror,
                ),
                values,
            )
        custom = MODULE.PRESETS["custom"]
        self.assertEqual(custom.color, "#363636")
        self.assertEqual(custom.trim_end, 2.0)
        self.assertEqual(custom.fade_in_seconds, 0.0)
        self.assertEqual(custom.fade_out_seconds, 2.0)
        self.assertEqual(custom.music_volume, 0.0)

    def test_screenshot_default_uses_only_fade_out(self):
        command = MODULE.build_command(
            Path("input.mp4"), Path("output.mp4"),
            {"width": 1080, "height": 1920, "duration": 12.0, "has_audio": True},
            MODULE.PRESETS["custom"], "ffmpeg",
        )
        joined = " ".join(command)
        self.assertNotIn("fade=t=in", joined)
        self.assertNotIn("afade=t=in", joined)
        self.assertIn("fade=t=out", joined)
        self.assertIn("afade=t=out", joined)
        self.assertIn("color=0x363636@0.2000", joined)
        self.assertIn("aa=0.1200", joined)
        self.assertIn("trim=duration=10.000", joined)
        self.assertIn("-t 9.615", joined)

    def test_legacy_fade_remains_symmetric(self):
        config = MODULE.replace(
            MODULE.PRESETS["light"],
            fade_seconds=1.0,
            fade_in_seconds=None,
            fade_out_seconds=None,
        )
        command = MODULE.build_command(
            Path("input.mp4"), Path("output.mp4"),
            {"width": 1080, "height": 1920, "duration": 12.0, "has_audio": True},
            config, "ffmpeg",
        )
        joined = " ".join(command)
        self.assertIn("fade=t=in", joined)
        self.assertIn("fade=t=out", joined)
        self.assertIn("afade=t=in", joined)
        self.assertIn("afade=t=out", joined)

    def test_medium_preset_builds_zoom_blur_sweep_and_fixed_fps(self):
        command = MODULE.build_command(
            Path("input.mp4"), Path("output.mp4"),
            {"width": 1080, "height": 1920, "duration": 10.0, "has_audio": True},
            MODULE.PRESETS["medium"], "ffmpeg",
        )
        joined = " ".join(command)
        self.assertIn("scale=1242:2208", joined)
        self.assertIn("gblur=sigma=15.000", joined)
        self.assertIn("aa=0.0600", joined)
        self.assertIn("fps=25.000", joined)

    def test_medium_command_contains_expected_filters(self):
        config = MODULE.PRESETS["medium"]
        command = MODULE.build_command(
            Path("input.mp4"), Path("output.mp4"),
            {"width": 1920, "height": 1080, "duration": 10.0, "has_audio": True},
            config, "ffmpeg",
        )
        joined = " ".join(command)
        self.assertIn("crop=", joined)
        self.assertNotIn("hflip", joined)
        self.assertIn("atempo=", joined)
        self.assertIn("local-transform-", joined)

    def test_atempo_is_split_into_valid_ranges(self):
        filters = MODULE.atempo_filters(4.0)
        self.assertEqual(filters, ["atempo=2.000000", "atempo=2.000000"])

    def test_rejects_excessive_trim(self):
        config = MODULE.replace(MODULE.PRESETS["light"], trim_start=6, trim_end=5)
        with self.assertRaises(ValueError):
            MODULE.build_command(Path("a.mp4"), Path("b.mp4"), {"width": 10, "height": 10, "duration": 10, "has_audio": False}, config, "ffmpeg")

    def test_random_music_directory_is_reproducible(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ("a.mp3", "b.wav", "ignored.txt"):
                Path(directory, name).touch()
            config = MODULE.replace(MODULE.PRESETS["light"], background_music_dir=directory)
            first = MODULE.choose_background_music(config, 42)
            second = MODULE.choose_background_music(config, 42)
            self.assertEqual(first.background_music, second.background_music)
            self.assertIn(Path(first.background_music).suffix, {".mp3", ".wav"})

    def test_nvidia_command_uses_nvenc(self):
        config = MODULE.replace(MODULE.PRESETS["medium"], hardware_acceleration="nvidia")
        command = MODULE.build_command(Path("a.mp4"), Path("b.mp4"), {"width": 1920, "height": 1080, "duration": 10, "has_audio": True}, config, "ffmpeg")
        self.assertIn("h264_nvenc", command)

    def test_apple_command_uses_videotoolbox(self):
        config = MODULE.replace(MODULE.PRESETS["medium"], hardware_acceleration="apple")
        command = MODULE.build_command(Path("a.mp4"), Path("b.mp4"), {"width": 1920, "height": 1080, "duration": 10, "has_audio": True}, config, "ffmpeg")
        self.assertIn("h264_videotoolbox", command)

    def test_random_effect_plan_is_reproducible_and_frequency_driven(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ("fireworks.mp4", "meteor.webm", "ignored.txt"):
                Path(directory, name).touch()
            config = MODULE.replace(
                MODULE.PRESETS["medium"],
                enable_dynamic_effects=True,
                effect_dir=directory,
                effect_timing="random",
                effect_frequency=3.0,
                effect_duration=2.0,
                random_seed=42,
            )
            first = MODULE.plan_effect_events(config, 120.0)
            second = MODULE.plan_effect_events(config, 120.0)
            self.assertEqual(first, second)
            self.assertEqual(len(first), 6)
            self.assertTrue(all(path.suffix in {".mp4", ".webm"} for path, _start, _duration in first))

    def test_advanced_command_contains_blur_effect_border_and_fixed_fps(self):
        with tempfile.TemporaryDirectory() as directory:
            effect = Path(directory, "fireworks.mp4")
            border = Path(directory, "border.png")
            effect.touch()
            border.touch()
            config = MODULE.replace(
                MODULE.PRESETS["medium"],
                blur_background=True,
                output_aspect="portrait",
                target_fps=25,
                border_file=str(border),
                enable_dynamic_effects=True,
                effect_file=str(effect),
                effect_random_type=False,
                effect_frequency=1,
                effect_duration=2,
                random_seed=7,
            )
            command = MODULE.build_command(
                Path("input.mp4"), Path("output.mp4"),
                {"width": 1920, "height": 1080, "duration": 30.0, "has_audio": True},
                config, "ffmpeg",
            )
            joined = " ".join(command)
            self.assertIn("gblur=sigma=", joined)
            self.assertIn("scale=1080:1920", joined)
            self.assertIn("between(t,", joined)
            self.assertIn("fps=25.000", joined)
            self.assertIn("[border]overlay", joined)
            self.assertIn("-filter_complex", command)


if __name__ == "__main__":
    unittest.main()
