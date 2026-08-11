from __future__ import annotations

import argparse
import math
import random
import subprocess
from pathlib import Path

from PIL import Image, ImageChops


WIDTH, HEIGHT, FPS, DURATION = 540, 960, 24, 4.0


def tinted(source: Image.Image, color: tuple[int, int, int], opacity: float) -> Image.Image:
    image = source.convert("RGBA")
    alpha = image.getchannel("A")
    light = image.convert("L")
    mask = ImageChops.multiply(alpha, light).point(lambda value: int(value * opacity))
    result = Image.new("RGBA", image.size, (*color, 0))
    result.putalpha(mask)
    return result


def place(
    canvas: Image.Image,
    source: Image.Image,
    center: tuple[float, float],
    size: float,
    color: tuple[int, int, int],
    opacity: float,
    angle: float = 0.0,
) -> None:
    width = max(2, int(size))
    height = max(2, int(size * source.height / max(1, source.width)))
    sprite = source.resize((width, height), Image.Resampling.LANCZOS)
    sprite = tinted(sprite, color, max(0.0, min(1.0, opacity)))
    if angle:
        sprite = sprite.rotate(angle, resample=Image.Resampling.BICUBIC, expand=True)
    x = int(center[0] - sprite.width / 2)
    y = int(center[1] - sprite.height / 2)
    canvas.alpha_composite(sprite, (x, y))


def encode(output: Path, ffmpeg: str, renderer) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "rawvideo",
        "-pix_fmt",
        "rgb24",
        "-s",
        f"{WIDTH}x{HEIGHT}",
        "-r",
        str(FPS),
        "-i",
        "-",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "fast",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
        "-movflags",
        "+faststart",
        str(output),
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE)
    assert process.stdin is not None
    try:
        for frame_index in range(round(FPS * DURATION)):
            frame = renderer(frame_index / FPS).convert("RGB")
            process.stdin.write(frame.tobytes())
    finally:
        process.stdin.close()
    if process.wait() != 0:
        raise RuntimeError(f"FFmpeg failed while generating {output.name}")


def load(texture_dir: Path, name: str) -> Image.Image:
    return Image.open(texture_dir / name).convert("RGBA")


def make_pack(texture_dir: Path, output_dir: Path, ffmpeg: str) -> None:
    rng = random.Random(20260717)
    spark = load(texture_dir, "spark_04.png")
    star = load(texture_dir, "star_07.png")
    circle = load(texture_dir, "circle_04.png")
    magic = load(texture_dir, "magic_03.png")
    flare = load(texture_dir, "flare_01.png")
    trace = load(texture_dir, "trace_04.png")

    embers = [
        (rng.uniform(0, WIDTH), rng.uniform(0, HEIGHT), rng.uniform(45, 125), rng.uniform(8, 28), rng.random())
        for _ in range(48)
    ]

    def render_embers(t: float) -> Image.Image:
        frame = Image.new("RGBA", (WIDTH, HEIGHT), "black")
        for x0, y0, speed, size, phase in embers:
            y = (y0 - speed * t) % (HEIGHT + 80) - 40
            x = x0 + math.sin(t * 2.1 + phase * 9) * 18
            pulse = 0.45 + 0.55 * abs(math.sin(t * 3.2 + phase * 11))
            place(frame, spark if phase > 0.35 else star, (x, y), size, (255, 125 + int(90 * phase), 35), pulse)
        return frame

    orbiters = [(rng.uniform(65, 245), rng.uniform(0, math.tau), rng.uniform(0.45, 1.25), rng.uniform(12, 42)) for _ in range(36)]

    def render_magic(t: float) -> Image.Image:
        frame = Image.new("RGBA", (WIDTH, HEIGHT), "black")
        cx, cy = WIDTH / 2, HEIGHT * 0.53
        for radius, phase, speed, size in orbiters:
            angle = phase + t * speed
            squash = 0.52 + 0.12 * math.sin(t + phase)
            x = cx + math.cos(angle) * radius
            y = cy + math.sin(angle) * radius * squash
            color = (60, 215, 255) if phase % 1.0 > 0.45 else (185, 80, 255)
            place(frame, star, (x, y), size, color, 0.45 + 0.45 * abs(math.sin(angle * 2)))
        place(frame, magic, (cx, cy), 330 + 25 * math.sin(t * 2.5), (90, 170, 255), 0.42, t * 35)
        return frame

    bokeh = [(rng.uniform(-80, WIDTH + 80), rng.uniform(-100, HEIGHT + 100), rng.uniform(18, 80), rng.uniform(15, 65), rng.random()) for _ in range(28)]

    def render_bokeh(t: float) -> Image.Image:
        frame = Image.new("RGBA", (WIDTH, HEIGHT), "black")
        for x0, y0, speed, size, phase in bokeh:
            y = (y0 - speed * t * 0.45) % (HEIGHT + 180) - 90
            x = x0 + math.sin(t * 0.8 + phase * 8) * 35
            color = (255, 105, 190) if phase > 0.5 else (90, 205, 255)
            place(frame, circle, (x, y), size * (0.85 + 0.2 * math.sin(t + phase)), color, 0.14 + 0.18 * phase)
        return frame

    lines = [(rng.uniform(0, WIDTH + 500), rng.uniform(0, HEIGHT), rng.uniform(260, 560), rng.uniform(45, 150), rng.random()) for _ in range(24)]

    def render_speed(t: float) -> Image.Image:
        frame = Image.new("RGBA", (WIDTH, HEIGHT), "black")
        for x0, y, speed, size, phase in lines:
            x = (x0 - speed * t) % (WIDTH + 600) - 300
            color = (255, 255, 255) if phase > 0.35 else (60, 220, 255)
            place(frame, trace, (x, y), size, color, 0.4 + phase * 0.5, -72)
        return frame

    def render_flare(t: float) -> Image.Image:
        frame = Image.new("RGBA", (WIDTH, HEIGHT), "black")
        progress = (t % DURATION) / DURATION
        x = -180 + progress * (WIDTH + 360)
        y = HEIGHT * (0.30 + 0.35 * math.sin(progress * math.pi))
        place(frame, flare, (x, y), 620, (110, 205, 255), 0.75, t * 8)
        place(frame, circle, (x, y), 260, (255, 135, 220), 0.30)
        return frame

    def render_portal(t: float) -> Image.Image:
        frame = Image.new("RGBA", (WIDTH, HEIGHT), "black")
        pulse = 1.0 + 0.10 * math.sin(t * math.tau)
        center = (WIDTH / 2, HEIGHT * 0.54)
        place(frame, circle, center, 420 * pulse, (75, 225, 255), 0.75, t * 70)
        place(frame, magic, center, 340 / pulse, (200, 70, 255), 0.60, -t * 55)
        for index in range(10):
            angle = t * 1.9 + index * math.tau / 10
            point = (center[0] + math.cos(angle) * 205, center[1] + math.sin(angle) * 205)
            place(frame, star, point, 20 + 8 * math.sin(t * 4 + index), (255, 230, 110), 0.75)
        return frame

    jobs = [
        ("10_golden_embers_cc0.mp4", render_embers),
        ("11_magic_particles_cc0.mp4", render_magic),
        ("12_dream_bokeh_cc0.mp4", render_bokeh),
        ("13_speed_lines_cc0.mp4", render_speed),
        ("14_lens_flare_cc0.mp4", render_flare),
        ("15_energy_portal_cc0.mp4", render_portal),
    ]
    for name, renderer in jobs:
        print(f"Generating {name}")
        encode(output_dir / name, ffmpeg, renderer)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("texture_dir", type=Path)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument("--ffmpeg", default="ffmpeg")
    args = parser.parse_args()
    make_pack(args.texture_dir, args.output_dir, args.ffmpeg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
