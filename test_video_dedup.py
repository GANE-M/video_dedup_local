from pathlib import Path
import sys
import tempfile
import unittest


sys.path.insert(0, str(Path(__file__).parent))
import video_dedup as MODULE


class CommandTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
