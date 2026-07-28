#!/usr/bin/env python3
"""Tkinter desktop UI for the local FFmpeg video transformation tool."""

from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk

import video_dedup
import agent_bridge
from recap_gui import RecapWorkspace


PRESET_LABELS = {
    "light": "初级",
    "medium": "中级",
    "strong": "高级",
    "deep": "深度",
}
PRESET_KEYS_BY_LABEL = {label: key for key, label in PRESET_LABELS.items()}


@dataclass
class QueuedTask:
    task_id: int
    title: str
    command: list[str]
    cleanup_paths: list[Path]
    env: dict[str, str] | None = None
    log_path: Path | None = None


class VideoToolApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("本地视频处理工具")
        self.geometry("860x760")
        self.minsize(760, 680)
        self.process: subprocess.Popen[str] | None = None
        self.config_file: Path | None = None
        self.input_list_file: Path | None = None
        self.selected_inputs: list[str] = []
        self.task_counter = 0
        self.pending_tasks: list[QueuedTask] = []
        self.active_processes: dict[int, subprocess.Popen[str]] = {}
        self.starting_tasks: set[int] = set()
        self.task_cleanup: dict[int, list[Path]] = {}
        self.task_windows: dict[int, tk.Toplevel] = {}
        self.task_logs: dict[int, tk.Text] = {}
        self.task_log_paths: dict[int, Path] = {}
        self.task_lock = threading.Lock()
        self.state_file = Path(__file__).with_name(".video_tool_state.json")
        self.glossary_dir = Path(__file__).with_name("glossaries")
        self.glossary_files_by_label: dict[str, Path | None] = {}
        self.subtitle_glossary_combo: ttk.Combobox | None = None
        self.refresh_glossary_choices(update_widget=False)
        self._state_save_after_id: str | None = None
        self._auto_state_enabled = False
        self.subtitle_preview_frame: ttk.Frame | None = None
        self.llm_review_frame: ttk.Frame | None = None
        self.subtitle_preview_canvas: tk.Canvas | None = None
        self.subtitle_preview_photo: tk.PhotoImage | None = None
        self.subtitle_preview_composite_photo: tk.PhotoImage | None = None
        self.subtitle_preview_mask_stipple: str | None = None
        self.subtitle_preview_temp: Path | None = None
        self.subtitle_preview_image_size = (0, 0)
        self.subtitle_preview_drag: str | None = None
        self.subtitle_preview_drag_offset = (0, 0)
        self._make_variables()
        self._build_ui()
        self.load_preset()
        self.load_last_state()
        self.ensure_bundled_effect_defaults()
        self.refresh_translation_mode_panel()
        self.enable_auto_state_save()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def ensure_bundled_effect_defaults(self) -> None:
        """Use the bundled black-background effect pack when no asset was selected."""
        if self.effect_file.get().strip() or self.effect_dir.get().strip():
            return
        bundled_effects = Path(__file__).resolve().parent / "effects"
        if not bundled_effects.is_dir():
            return
        self.effect_dir.set(str(bundled_effects))
        # Every bundled clip uses a black background.  Alpha mode would keep
        # the black canvas and make the effect look as if it did not work.
        self.effect_key_mode.set("黑色背景")
        # The previous 0.12 default was nearly invisible on ordinary footage.
        self.effect_opacity.set(0.30)

    def _make_variables(self) -> None:
        self.input_path = tk.StringVar()
        self.output_path = tk.StringVar()
        self.preset = tk.StringVar(value="medium")
        self.preset_display = tk.StringVar(value=PRESET_LABELS["medium"])
        self.seed = tk.StringVar(value="2026")
        self.crop = tk.DoubleVar()
        self.zoom = tk.DoubleVar()
        self.mirror = tk.BooleanVar()
        self.speed = tk.DoubleVar()
        self.brightness = tk.DoubleVar()
        self.contrast = tk.DoubleVar()
        self.saturation = tk.DoubleVar()
        self.color = tk.StringVar(value="#8bc34a")
        self.color_opacity = tk.DoubleVar()
        self.fade = tk.DoubleVar()
        self.trim_start = tk.DoubleVar()
        self.trim_end = tk.DoubleVar()
        self.music = tk.StringVar()
        self.music_dir = tk.StringVar()
        self.music_volume = tk.DoubleVar()
        self.keep_audio = tk.BooleanVar(value=True)
        self.crf = tk.IntVar(value=23)
        self.encoder_preset = tk.StringVar(value="medium")
        self.hardware_acceleration = tk.StringVar(value="nvidia")
        self.blur_background = tk.BooleanVar(value=False)
        self.blur_sigma = tk.DoubleVar(value=18.0)
        self.foreground_scale = tk.DoubleVar(value=0.96)
        self.output_aspect = tk.StringVar(value="保持原画幅")
        self.target_fps = tk.StringVar(value="保持源帧率")
        self.border_file = tk.StringVar()
        self.enable_dynamic_effects = tk.BooleanVar(value=False)
        self.effect_file = tk.StringVar()
        self.effect_dir = tk.StringVar()
        self.effect_random_type = tk.BooleanVar(value=True)
        self.effect_timing = tk.StringVar(value="随机时间出现")
        self.effect_frequency = tk.DoubleVar(value=3.0)
        self.effect_duration = tk.DoubleVar(value=2.5)
        self.effect_opacity = tk.DoubleVar(value=0.30)
        self.effect_key_mode = tk.StringVar(value="黑色背景")
        self.effect_position = tk.StringVar(value="全屏")
        self.enable_dedup_stage = tk.BooleanVar(value=True)
        self.enable_subtitle_pipeline = tk.BooleanVar(value=True)
        self.enable_recap_stage = tk.BooleanVar(value=False)
        self.reuse_final_subtitle_cache = tk.BooleanVar(value=True)
        self.subtitle_source = tk.StringVar(value="自动：软字幕优先，否则硬字幕OCR+音频ASR")
        self.subtitle_mode = tk.StringVar(value="烧录到画面")
        self.subtitle_layout = tk.StringVar(value="覆盖原字幕")
        self.subtitle_position = tk.StringVar(value="自动")
        self.subtitle_cover = tk.BooleanVar(value=True)
        self.subtitle_cover_auto_detect = tk.BooleanVar(value=True)
        self.subtitle_cover_x = tk.DoubleVar(value=0.0)
        self.subtitle_cover_y = tk.DoubleVar(value=74.0)
        self.subtitle_cover_width = tk.DoubleVar(value=100.0)
        self.subtitle_cover_height = tk.DoubleVar(value=11.0)
        self.subtitle_cover_opacity = tk.DoubleVar(value=0.82)
        self.subtitle_cover_mode = tk.StringVar(value="局部高斯模糊")
        self.subtitle_cover_blur_sigma = tk.DoubleVar(value=22.0)
        self.subtitle_font_name = tk.StringVar(value="Arial")
        self.subtitle_font_size = tk.IntVar(value=28)
        self.subtitle_ocr_language = tk.StringVar(value="自动")
        self.subtitle_ocr_device = tk.StringVar(value="cuda")
        self.subtitle_source_language = tk.StringVar(value="自动")
        self.subtitle_target_language = tk.StringVar(value="English")
        self.subtitle_glossary = tk.StringVar(value="不使用术语表")
        self.subtitle_file = tk.StringVar()
        self.subtitle_output = tk.StringVar()
        self.subtitle_provider = tk.StringVar(value="openai-compatible")
        self.translation_backend = tk.StringVar(value="API 模式")
        self.translation_quality = tk.StringVar(value="快速翻译")
        self.localization_strategy = tk.StringVar(value="标准影视本地化")
        self.agent_bridge_status = tk.StringVar(value="Agent 未初始化")
        self.llm_api_key = tk.StringVar(value=os.environ.get("OPENAI_API_KEY", ""))
        self.llm_base_url = tk.StringVar(value=os.environ.get("OPENAI_BASE_URL", "https://theruta.ai/api/v1/chat/completions"))
        self.llm_model = tk.StringVar(value=os.environ.get("OPENAI_MODEL", "deepseek-v4-flash"))
        self.enable_llm_review = tk.BooleanVar(value=True)
        # Retained for backward-compatible config loading; the new adaptive
        # pipeline no longer sends a second independent translation.
        self.llm_model_b = tk.StringVar(value=os.environ.get("OPENAI_MODEL_B", "deepseek-v4-flash"))
        self.llm_review_model = tk.StringVar(value=os.environ.get("OPENAI_REVIEW_MODEL", "deepseek-v4-flash"))
        self.review_confidence_threshold = tk.DoubleVar(value=0.82)
        self.llm_review_expanded = tk.BooleanVar(value=False)
        self.whisper_model = tk.StringVar(value="medium")
        self.whisper_device = tk.StringVar(value="cuda")
        self.subtitle_backend = tk.StringVar(value="本机 Python")
        self.docker_image = tk.StringVar(value="video-dedup-local:ocr")
        self.max_parallel_tasks = tk.IntVar(value=5)
        self.subtitle_preview_expanded = tk.BooleanVar(value=False)
        self.subtitle_preview_video = tk.StringVar()
        self.subtitle_preview_time = tk.StringVar(value="未加载")
        self.subtitle_preview_text = tk.StringVar(value="Are you sure he's here?")
        self.status = tk.StringVar(value="就绪")

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Section.TLabelframe.Label", font=("Microsoft YaHei UI", 10, "bold"))
        root = ttk.Frame(self, padding=16)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text="本地视频处理工具", style="Title.TLabel").pack(anchor="w")
        ttk.Label(root, text="全部处理在本机完成；请仅处理自己拥有或获准修改的视频。", foreground="#666").pack(anchor="w", pady=(3, 12))

        paths = ttk.LabelFrame(root, text="文件", style="Section.TLabelframe", padding=10)
        paths.pack(fill="x")
        ttk.Label(paths, text="输入视频").grid(row=0, column=0, sticky="w", pady=5)
        ttk.Entry(paths, textvariable=self.input_path, state="readonly").grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(paths, text="多选文件", command=self.choose_input).grid(row=0, column=2, padx=(0, 5))
        ttk.Button(paths, text="选择目录", command=self.choose_input_dir).grid(row=0, column=3)
        ttk.Label(paths, text="输出位置").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(paths, textvariable=self.output_path).grid(row=1, column=1, sticky="ew", padx=8)
        ttk.Button(paths, text="选择", command=self.choose_output).grid(row=1, column=2, columnspan=2, sticky="ew")
        paths.columnconfigure(1, weight=1)

        stages = ttk.LabelFrame(root, text="处理阶段（可独立或组合）", style="Section.TLabelframe", padding=8)
        stages.pack(fill="x", pady=(10, 0))
        ttk.Checkbutton(stages, text="1. 字幕获取与翻译", variable=self.enable_subtitle_pipeline).pack(side="left", padx=(0, 18))
        ttk.Checkbutton(stages, text="2. 解说视频编排与剪辑", variable=self.enable_recap_stage).pack(side="left", padx=(0, 18))
        ttk.Checkbutton(stages, text="3. 二次去重", variable=self.enable_dedup_stage).pack(side="left")
        ttk.Label(stages, text="组合时固定顺序：字幕 → 解说 → 去重", foreground="#666").pack(side="right")

        preset_bar = ttk.Frame(root)
        preset_bar.pack(fill="x", pady=10)
        ttk.Label(preset_bar, text="处理强度").pack(side="left")
        combo = ttk.Combobox(
            preset_bar,
            textvariable=self.preset_display,
            values=tuple(PRESET_LABELS.values()),
            state="readonly",
            width=12,
        )
        combo.pack(side="left", padx=(8, 16))
        combo.bind("<<ComboboxSelected>>", self.on_preset_selected)
        ttk.Button(preset_bar, text="载入预设", command=lambda: self.load_preset(save_state=True)).pack(side="left")
        ttk.Label(preset_bar, text="随机种子").pack(side="left", padx=(24, 6))
        ttk.Entry(preset_bar, textvariable=self.seed, width=10).pack(side="left")
        ttk.Label(preset_bar, text="并行任务").pack(side="left", padx=(24, 6))
        ttk.Spinbox(preset_bar, from_=1, to=5, textvariable=self.max_parallel_tasks, width=5).pack(side="left")

        actions = ttk.Frame(root)
        actions.pack(fill="x", pady=(0, 10))
        self.start_button = ttk.Button(actions, text="开始处理", command=self.start, width=16)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="停止全部任务", command=self.stop, state="disabled", width=14)
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(actions, text="保存配置", command=self.save_config).pack(side="left", padx=(8, 0))
        ttk.Button(actions, text="载入配置", command=self.open_config).pack(side="left", padx=8)
        ttk.Label(actions, textvariable=self.status).pack(side="right")

        notebook = ttk.Notebook(root)
        notebook.pack(fill="both", expand=True)
        video_tab = ttk.Frame(notebook, padding=12)
        time_tab = ttk.Frame(notebook, padding=12)
        audio_tab = ttk.Frame(notebook, padding=12)
        output_tab = ttk.Frame(notebook, padding=12)
        effects_container = ttk.Frame(notebook)
        effects_tab = self._make_scrollable_frame(effects_container)
        subtitle_container = ttk.Frame(notebook)
        subtitle_tab = self._make_scrollable_frame(subtitle_container)
        notebook.add(video_tab, text="画面")
        notebook.add(time_tab, text="时间")
        notebook.add(audio_tab, text="声音")
        notebook.add(output_tab, text="输出质量")
        notebook.add(effects_container, text="动态特效")
        notebook.add(subtitle_container, text="字幕")
        recap_tab = ttk.Frame(notebook)
        notebook.add(recap_tab, text="解说剪辑")
        self.recap_workspace = RecapWorkspace(recap_tab)

        self._scale_row(video_tab, 0, "裁边比例 (%)", self.crop, 0, 15, 0.1)
        self._scale_row(video_tab, 1, "视频放大 (%)", self.zoom, 0, 50, 1)
        ttk.Checkbutton(video_tab, text="水平镜像", variable=self.mirror).grid(row=2, column=0, columnspan=3, sticky="w", pady=6)
        self._scale_row(video_tab, 3, "亮度", self.brightness, -0.2, 0.2, 0.005)
        self._scale_row(video_tab, 4, "对比度", self.contrast, 0.5, 2.0, 0.01)
        self._scale_row(video_tab, 5, "饱和度", self.saturation, 0, 3.0, 0.01)
        self._scale_row(video_tab, 6, "色彩叠加透明度", self.color_opacity, 0, 0.3, 0.005)
        ttk.Label(video_tab, text="叠加颜色").grid(row=7, column=0, sticky="w", pady=6)
        ttk.Entry(video_tab, textvariable=self.color, width=14).grid(row=7, column=1, sticky="w")
        ttk.Button(video_tab, text="选择颜色", command=self.choose_color).grid(row=7, column=2, sticky="w")
        video_tab.columnconfigure(1, weight=1)

        self._scale_row(time_tab, 0, "播放速度", self.speed, 0.5, 2.0, 0.005)
        self._scale_row(time_tab, 1, "淡入淡出 (秒)", self.fade, 0, 5, 0.05)
        self._scale_row(time_tab, 2, "裁掉开头 (秒)", self.trim_start, 0, 30, 0.1)
        self._scale_row(time_tab, 3, "裁掉结尾 (秒)", self.trim_end, 0, 30, 0.1)
        time_tab.columnconfigure(1, weight=1)

        ttk.Checkbutton(audio_tab, text="保留原视频声音", variable=self.keep_audio).grid(row=0, column=0, columnspan=3, sticky="w", pady=6)
        ttk.Label(audio_tab, text="背景音乐").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Entry(audio_tab, textvariable=self.music).grid(row=1, column=1, sticky="ew", padx=8)
        ttk.Button(audio_tab, text="选择文件", command=self.choose_music).grid(row=1, column=2)
        ttk.Label(audio_tab, text="随机音乐目录").grid(row=2, column=0, sticky="w", pady=6)
        ttk.Entry(audio_tab, textvariable=self.music_dir).grid(row=2, column=1, sticky="ew", padx=8)
        ttk.Button(audio_tab, text="选择目录", command=self.choose_music_dir).grid(row=2, column=2)
        ttk.Label(audio_tab, text="指定单曲时优先使用单曲；单曲为空时，批量任务会从目录中逐个随机选择。", foreground="#666").grid(row=3, column=0, columnspan=3, sticky="w", pady=(0, 5))
        self._scale_row(audio_tab, 4, "背景音乐音量", self.music_volume, 0, 1, 0.01)
        audio_tab.columnconfigure(1, weight=1)

        self._scale_row(output_tab, 0, "CRF（越低越清晰）", self.crf, 14, 32, 1)
        ttk.Label(output_tab, text="编码速度").grid(row=1, column=0, sticky="w", pady=8)
        ttk.Combobox(output_tab, textvariable=self.encoder_preset, values=("ultrafast", "veryfast", "fast", "medium", "slow"), state="readonly", width=15).grid(row=1, column=1, sticky="w")
        ttk.Label(output_tab, text="显卡编码").grid(row=2, column=0, sticky="w", pady=8)
        ttk.Combobox(output_tab, textvariable=self.hardware_acceleration, values=("auto", "nvidia", "amd", "intel", "apple", "cpu"), state="readonly", width=15).grid(row=2, column=1, sticky="w")
        ttk.Label(output_tab, text="Windows 选 nvidia/amd/intel；Mac 选 apple=VideoToolbox；auto 会自动探测。", foreground="#666").grid(row=3, column=0, columnspan=3, sticky="w")
        output_tab.columnconfigure(1, weight=1)

        ttk.Label(effects_tab, text="画面增强与动态特效", font=("Microsoft YaHei UI", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        ttk.Checkbutton(effects_tab, text="模糊背景＋主体画中画", variable=self.blur_background).grid(row=1, column=0, columnspan=3, sticky="w", pady=5)
        self._scale_row(effects_tab, 2, "背景模糊强度", self.blur_sigma, 0, 40, 1)
        self._scale_row(effects_tab, 3, "主体画面比例", self.foreground_scale, 0.5, 1.0, 0.01)
        ttk.Label(effects_tab, text="输出画幅").grid(row=4, column=0, sticky="w", pady=5)
        ttk.Combobox(effects_tab, textvariable=self.output_aspect, values=("保持原画幅", "竖屏 9:16", "横屏 16:9", "方形 1:1"), state="readonly", width=18).grid(row=4, column=1, sticky="w", padx=8)
        ttk.Label(effects_tab, text="固定帧率").grid(row=5, column=0, sticky="w", pady=5)
        ttk.Combobox(effects_tab, textvariable=self.target_fps, values=("保持源帧率", "24", "25", "30", "50", "60"), state="readonly", width=18).grid(row=5, column=1, sticky="w", padx=8)
        ttk.Label(effects_tab, text="边框素材").grid(row=6, column=0, sticky="w", pady=5)
        ttk.Entry(effects_tab, textvariable=self.border_file).grid(row=6, column=1, sticky="ew", padx=8)
        ttk.Button(effects_tab, text="选择", command=self.choose_border_file).grid(row=6, column=2, sticky="w")
        ttk.Label(effects_tab, text="边框支持透明 PNG/WebM/MOV，也可使用循环视频边框。", foreground="#666").grid(row=7, column=1, columnspan=2, sticky="w", padx=8)

        ttk.Separator(effects_tab, orient="horizontal").grid(row=8, column=0, columnspan=3, sticky="ew", pady=10)
        ttk.Checkbutton(effects_tab, text="启用随机动态特效", variable=self.enable_dynamic_effects).grid(row=9, column=0, columnspan=3, sticky="w", pady=5)
        ttk.Checkbutton(effects_tab, text="每次随机选择不同特效", variable=self.effect_random_type).grid(row=10, column=0, columnspan=3, sticky="w", pady=5)
        ttk.Label(effects_tab, text="固定特效文件").grid(row=11, column=0, sticky="w", pady=5)
        ttk.Entry(effects_tab, textvariable=self.effect_file).grid(row=11, column=1, sticky="ew", padx=8)
        ttk.Button(effects_tab, text="选择", command=self.choose_effect_file).grid(row=11, column=2, sticky="w")
        ttk.Label(effects_tab, text="随机特效目录").grid(row=12, column=0, sticky="w", pady=5)
        ttk.Entry(effects_tab, textvariable=self.effect_dir).grid(row=12, column=1, sticky="ew", padx=8)
        ttk.Button(effects_tab, text="选择目录", command=self.choose_effect_dir).grid(row=12, column=2, sticky="w")
        ttk.Label(effects_tab, text="出现方式").grid(row=13, column=0, sticky="w", pady=5)
        ttk.Combobox(effects_tab, textvariable=self.effect_timing, values=("随机时间出现", "全程循环"), state="readonly", width=18).grid(row=13, column=1, sticky="w", padx=8)
        self._scale_row(effects_tab, 14, "每分钟出现次数", self.effect_frequency, 0.5, 12, 0.5)
        self._scale_row(effects_tab, 15, "单次持续时间（秒）", self.effect_duration, 0.5, 15, 0.5)
        self._scale_row(effects_tab, 16, "特效透明度", self.effect_opacity, 0.01, 1.0, 0.01)
        ttk.Label(effects_tab, text="素材背景").grid(row=17, column=0, sticky="w", pady=5)
        ttk.Combobox(effects_tab, textvariable=self.effect_key_mode, values=("黑色背景", "绿色幕布", "透明通道"), state="readonly", width=18).grid(row=17, column=1, sticky="w", padx=8)
        ttk.Label(effects_tab, text="显示区域").grid(row=18, column=0, sticky="w", pady=5)
        ttk.Combobox(effects_tab, textvariable=self.effect_position, values=("全屏", "顶部", "底部"), state="readonly", width=18).grid(row=18, column=1, sticky="w", padx=8)
        ttk.Label(effects_tab, text="可把烟花、流星雨、飘雪、光斑和扫光素材放在同一目录；随机种子可复现选择和出现时间。", foreground="#666", wraplength=620).grid(row=19, column=0, columnspan=3, sticky="w", pady=(5, 0))
        effects_tab.columnconfigure(1, weight=1)

        ttk.Label(subtitle_tab, text="字幕阶段设置：识别/读取 → 翻译审核 → 保存字幕终稿；是否编码视频由顶部阶段选择决定。", foreground="#555").grid(row=0, column=0, columnspan=3, sticky="w", pady=6)
        ttk.Label(subtitle_tab, text="字幕来源").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Combobox(
            subtitle_tab,
            textvariable=self.subtitle_source,
            values=(
                "自动：软字幕优先，否则硬字幕OCR+音频ASR",
                "自动：软字幕优先，否则硬字幕OCR",
                "软字幕+音频ASR交叉审核",
                "硬字幕OCR+音频ASR交叉审核",
                "只用软字幕轨道",
                "只用硬字幕OCR",
                "只用音频ASR",
            ),
            state="readonly",
            width=44,
        ).grid(row=1, column=1, sticky="w", padx=8)
        ttk.Label(subtitle_tab, text="原字幕语言（OCR）").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Combobox(subtitle_tab, textvariable=self.subtitle_ocr_language, values=("自动", "中文", "英语", "阿拉伯语"), state="readonly", width=18).grid(row=2, column=1, sticky="w", padx=8)
        ocr_device_frame = ttk.Frame(subtitle_tab)
        ocr_device_frame.grid(row=2, column=2, sticky="w")
        ttk.Label(ocr_device_frame, text="OCR设备").pack(side="left")
        ttk.Combobox(
            ocr_device_frame,
            textvariable=self.subtitle_ocr_device,
            values=("自动", "cuda", "cpu"),
            state="readonly",
            width=8,
        ).pack(side="left", padx=(6, 0))
        ttk.Label(subtitle_tab, text="翻译源语言").grid(row=3, column=0, sticky="w", pady=5)
        ttk.Combobox(subtitle_tab, textvariable=self.subtitle_source_language, values=("自动", "中文", "英语", "阿拉伯语"), state="readonly", width=18).grid(row=3, column=1, sticky="w", padx=8)
        ttk.Label(subtitle_tab, text="目标语言").grid(row=4, column=0, sticky="w", pady=5)
        ttk.Combobox(
            subtitle_tab,
            textvariable=self.subtitle_target_language,
            values=("English", "Arabic", "Chinese", "Spanish", "French", "German", "Portuguese", "Japanese", "Korean", "Russian", "Turkish", "Indonesian", "Vietnamese", "Thai"),
            state="readonly",
            width=18,
        ).grid(row=4, column=1, sticky="w", padx=8)
        glossary_frame = ttk.Frame(subtitle_tab)
        glossary_frame.grid(row=4, column=2, sticky="w")
        ttk.Label(glossary_frame, text="术语表").pack(side="left")
        self.subtitle_glossary_combo = ttk.Combobox(
            glossary_frame,
            textvariable=self.subtitle_glossary,
            values=tuple(self.glossary_files_by_label),
            state="readonly",
            width=25,
        )
        self.subtitle_glossary_combo.pack(side="left", padx=(6, 3))
        ttk.Button(glossary_frame, text="刷新", command=self.refresh_glossary_choices).pack(side="left")
        ttk.Label(subtitle_tab, text="API Key").grid(row=5, column=0, sticky="w", pady=5)
        ttk.Entry(subtitle_tab, textvariable=self.llm_api_key, show="*").grid(row=5, column=1, sticky="ew", padx=8)
        ttk.Label(subtitle_tab, text="接口地址").grid(row=6, column=0, sticky="w", pady=5)
        ttk.Entry(subtitle_tab, textvariable=self.llm_base_url).grid(row=6, column=1, sticky="ew", padx=8)
        ttk.Label(subtitle_tab, text="模型名").grid(row=7, column=0, sticky="w", pady=5)
        ttk.Entry(subtitle_tab, textvariable=self.llm_model, width=24).grid(row=7, column=1, sticky="w", padx=8)
        ttk.Label(subtitle_tab, text="每个视频的全部字幕一次发送；总并发由顶部“并行任务”控制，最多 5。", foreground="#666").grid(row=7, column=2, sticky="w")
        ttk.Label(subtitle_tab, text="DeepSeek/OpenAI-compatible 均可；模型名填供应商后台显示的准确 ID。", foreground="#666").grid(row=8, column=0, columnspan=3, sticky="w", pady=(0, 8))

        ttk.Label(subtitle_tab, text="添加方式").grid(row=9, column=0, sticky="w", pady=5)
        ttk.Combobox(subtitle_tab, textvariable=self.subtitle_mode, values=("烧录到画面", "封装软字幕"), state="readonly", width=20).grid(row=9, column=1, sticky="w", padx=8)
        ttk.Label(subtitle_tab, text="覆盖形式").grid(row=10, column=0, sticky="w", pady=5)
        ttk.Combobox(subtitle_tab, textvariable=self.subtitle_layout, values=("覆盖原字幕", "双语字幕"), state="readonly", width=20).grid(row=10, column=1, sticky="w", padx=8)
        ttk.Label(subtitle_tab, text="字幕位置").grid(row=11, column=0, sticky="w", pady=5)
        ttk.Combobox(subtitle_tab, textvariable=self.subtitle_position, values=("自动", "底部", "顶部"), state="readonly", width=20).grid(row=11, column=1, sticky="w", padx=8)
        ttk.Label(subtitle_tab, text="提示：双语字幕的“自动”位置会放顶部，避免和原字幕换行重叠。", foreground="#666").grid(row=12, column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(subtitle_tab, text="覆盖原字幕时遮住旧字幕区域", variable=self.subtitle_cover).grid(row=13, column=0, columnspan=3, sticky="w", pady=6)
        ttk.Checkbutton(subtitle_tab, text="自动识别原字幕区域（OCR，失败则用下方手动参数）", variable=self.subtitle_cover_auto_detect).grid(row=14, column=0, columnspan=3, sticky="w", pady=6)
        self._scale_row(subtitle_tab, 15, "手动字幕区域左侧 (%)", self.subtitle_cover_x, 0, 90, 1)
        self._scale_row(subtitle_tab, 16, "手动字幕区域宽度 (%)", self.subtitle_cover_width, 10, 100, 1)
        self._scale_row(subtitle_tab, 17, "手动字幕区域起点高度 (%)", self.subtitle_cover_y, 50, 95, 1)
        self._scale_row(subtitle_tab, 18, "手动字幕区域高度 (%)", self.subtitle_cover_height, 4, 30, 1)
        cover_style = ttk.LabelFrame(subtitle_tab, text="字幕遮挡效果", padding=6)
        cover_style.grid(row=19, column=0, columnspan=3, sticky="ew", pady=5)
        ttk.Label(cover_style, text="方式").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Combobox(
            cover_style,
            textvariable=self.subtitle_cover_mode,
            values=("局部高斯模糊", "白色半透明蒙版"),
            state="readonly",
            width=20,
        ).grid(row=0, column=1, sticky="w", padx=8)
        self._scale_row(cover_style, 1, "局部模糊强度", self.subtitle_cover_blur_sigma, 0.5, 50, 0.5)
        self._scale_row(cover_style, 2, "白色蒙版透明度（兼容）", self.subtitle_cover_opacity, 0, 1, 0.02)
        cover_style.columnconfigure(1, weight=1)
        self._scale_row(subtitle_tab, 20, "字幕字号", self.subtitle_font_size, 16, 64, 1)
        ttk.Label(subtitle_tab, text="字幕字体").grid(row=21, column=0, sticky="w", pady=5)
        ttk.Combobox(subtitle_tab, textvariable=self.subtitle_font_name, values=self._subtitle_font_choices(), width=28).grid(row=21, column=1, sticky="w", padx=8)

        ttk.Button(subtitle_tab, text="展开/收起字幕区域预览", command=self.toggle_subtitle_preview).grid(row=22, column=0, sticky="w", pady=(8, 4))
        ttk.Label(subtitle_tab, text="可随机抽取视频帧，用滑块或拖动画面方框校准字幕/遮罩区域。", foreground="#666").grid(row=22, column=1, columnspan=2, sticky="w", padx=8)
        self.subtitle_preview_frame = ttk.Frame(subtitle_tab)
        self._build_subtitle_preview(self.subtitle_preview_frame)

        ttk.Label(subtitle_tab, text="Whisper 模型").grid(row=24, column=0, sticky="w", pady=5)
        ttk.Entry(subtitle_tab, textvariable=self.whisper_model, width=18).grid(row=24, column=1, sticky="w", padx=8)
        ttk.Label(subtitle_tab, text="Whisper 设备").grid(row=25, column=0, sticky="w", pady=5)
        ttk.Combobox(subtitle_tab, textvariable=self.whisper_device, values=("auto", "cuda", "cpu"), state="readonly", width=18).grid(row=25, column=1, sticky="w", padx=8)
        ttk.Label(subtitle_tab, text="字幕处理后端").grid(row=26, column=0, sticky="w", pady=5)
        ttk.Combobox(subtitle_tab, textvariable=self.subtitle_backend, values=("Docker OCR", "本机 Python"), state="readonly", width=18).grid(row=26, column=1, sticky="w", padx=8)
        ttk.Label(subtitle_tab, text="Docker 镜像").grid(row=27, column=0, sticky="w", pady=5)
        ttk.Entry(subtitle_tab, textvariable=self.docker_image, width=28).grid(row=27, column=1, sticky="w", padx=8)
        ttk.Label(subtitle_tab, text="Docker Desktop 需要处于运行状态；容器只在任务期间临时启动。", foreground="#666").grid(row=28, column=0, columnspan=3, sticky="w")
        ttk.Button(subtitle_tab, text="展开/收起审核模型", command=self.toggle_llm_review_panel).grid(row=29, column=0, sticky="w", pady=(10, 4))
        ttk.Label(subtitle_tab, text="可选：整集结合上下文审核，全部视频完成后再统一全剧实体。", foreground="#666").grid(row=29, column=1, columnspan=2, sticky="w", padx=8)
        self.llm_review_frame = ttk.Frame(subtitle_tab)
        self._build_llm_review_panel(self.llm_review_frame)
        self.subtitle_tab = subtitle_tab
        # Reorder the page without rebuilding the established controls:
        # languages/glossary (0-4) -> translation mode (5) -> Agent bridge (6)
        # -> API settings (7-10) -> render settings (13+) -> API review (33+).
        # Moving the already-created widgets also preserves their bindings and
        # backward-compatible config behavior.
        for old_row in range(29, 8, -1):
            for widget in subtitle_tab.grid_slaves(row=old_row):
                widget.grid_configure(row=old_row + 4)
        for old_row in range(8, 4, -1):
            for widget in subtitle_tab.grid_slaves(row=old_row):
                widget.grid_configure(row=old_row + 2)
        self.api_mode_widgets = [
            widget
            for row in (7, 8, 9, 10, 33)
            for widget in subtitle_tab.grid_slaves(row=row)
        ]
        translation_mode_frame = ttk.LabelFrame(subtitle_tab, text="翻译执行方式", padding=8)
        translation_mode_frame.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(10, 4))
        ttk.Label(translation_mode_frame, text="模式").grid(row=0, column=0, sticky="w")
        mode_combo = ttk.Combobox(
            translation_mode_frame,
            textvariable=self.translation_backend,
            values=("API 模式", "Agent 模式"),
            state="readonly",
            width=16,
        )
        mode_combo.grid(row=0, column=1, sticky="w", padx=8)
        mode_combo.bind("<<ComboboxSelected>>", lambda _event: self.refresh_translation_mode_panel())
        ttk.Label(translation_mode_frame, text="翻译质量").grid(row=1, column=0, sticky="w", pady=(7, 0))
        ttk.Combobox(
            translation_mode_frame,
            textvariable=self.translation_quality,
            values=("快速翻译", "高级翻译"),
            state="readonly",
            width=16,
        ).grid(row=1, column=1, sticky="w", padx=8, pady=(7, 0))
        ttk.Label(translation_mode_frame, text="本地化策略").grid(row=2, column=0, sticky="w", pady=(7, 0))
        ttk.Combobox(
            translation_mode_frame,
            textvariable=self.localization_strategy,
            values=("标准影视本地化", "自然口语", "忠实正式", "海湾中性口语（阿拉伯语）"),
            state="readonly",
            width=24,
        ).grid(row=2, column=1, sticky="w", padx=8, pady=(7, 0))
        ttk.Label(
            translation_mode_frame,
            text="高级翻译由 Agent 模式执行母语化精修与独立全剧终审；海湾口语仅建议用于阿拉伯语。",
            foreground="#666",
            wraplength=580,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))
        ttk.Checkbutton(
            translation_mode_frame,
            text="快速翻译优先复用“字幕终稿”（高级翻译会作为只读初稿重新精修）",
            variable=self.reuse_final_subtitle_cache,
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))

        self.agent_mode_frame = ttk.LabelFrame(subtitle_tab, text="Agent 文件桥接", padding=8)
        ttk.Button(self.agent_mode_frame, text="生成 Agent 初始化命令", command=self.generate_agent_initialization).grid(row=0, column=0, sticky="w")
        ttk.Button(self.agent_mode_frame, text="测试通信", command=self.test_agent_communication).grid(row=0, column=1, sticky="w", padx=8)
        ttk.Button(self.agent_mode_frame, text="停止 Agent 监听", command=self.stop_agent_listener).grid(row=0, column=2, sticky="w")
        ttk.Label(self.agent_mode_frame, textvariable=self.agent_bridge_status, foreground="#555").grid(row=1, column=0, columnspan=3, sticky="w", pady=(7, 0))
        ttk.Label(
            self.agent_mode_frame,
            text="空闲时单次监听20分钟、约每分钟检查；领取任务后必须持续到提交。每集保存检查点，意外结束后同一注册可恢复未提交任务。",
            foreground="#666",
            wraplength=650,
        ).grid(row=2, column=0, columnspan=3, sticky="w", pady=(5, 0))
        subtitle_tab.columnconfigure(1, weight=1)

        log_frame = ttk.LabelFrame(root, text="运行日志", padding=6)
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(log_frame, height=9, wrap="word", state="disabled", font=("Consolas", 9))
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _make_scrollable_frame(self, parent: ttk.Frame) -> ttk.Frame:
        canvas = tk.Canvas(parent, highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        frame = ttk.Frame(canvas, padding=12)
        window_id = canvas.create_window((0, 0), window=frame, anchor="nw")

        def update_scroll_region(_event=None) -> None:
            canvas.configure(scrollregion=canvas.bbox("all"))

        def update_width(event) -> None:
            canvas.itemconfigure(window_id, width=event.width)

        def on_mousewheel(event) -> None:
            delta = -1 * int(event.delta / 120) if event.delta else 0
            canvas.yview_scroll(delta, "units")

        frame.bind("<Configure>", update_scroll_region)
        canvas.bind("<Configure>", update_width)
        canvas.bind("<Enter>", lambda _event: canvas.bind_all("<MouseWheel>", on_mousewheel))
        canvas.bind("<Leave>", lambda _event: canvas.unbind_all("<MouseWheel>"))
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        return frame

    def _path_row(self, parent: ttk.Widget, row: int, label: str, variable: tk.StringVar, command) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=8)
        ttk.Button(parent, text="选择", command=command).grid(row=row, column=2)

    def refresh_glossary_choices(self, update_widget: bool = True) -> None:
        choices: dict[str, Path | None] = {"不使用术语表": None}
        self.glossary_dir.mkdir(parents=True, exist_ok=True)
        for path in sorted(self.glossary_dir.glob("*.json")):
            if path.name.casefold().startswith("template_"):
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
                label = str(payload.get("name") or path.stem).strip()
            except (OSError, json.JSONDecodeError, AttributeError):
                label = f"无效文件：{path.name}"
            if label in choices:
                label = f"{label} [{path.stem}]"
            choices[label] = path
        self.glossary_files_by_label = choices
        if update_widget and self.subtitle_glossary_combo:
            current = self.subtitle_glossary.get()
            self.subtitle_glossary_combo.configure(values=tuple(choices))
            if current not in choices:
                self.subtitle_glossary.set("不使用术语表")

    def selected_glossary_path(self) -> Path | None:
        return self.glossary_files_by_label.get(self.subtitle_glossary.get())

    def _subtitle_font_choices(self) -> tuple[str, ...]:
        preferred = [
            "Arial",
            "Microsoft YaHei",
            "Microsoft YaHei UI",
            "SimHei",
            "SimSun",
            "Noto Sans",
            "Noto Sans Arabic",
            "Segoe UI",
            "Tahoma",
        ]
        try:
            installed = set(tkfont.families(self))
        except tk.TclError:
            installed = set()
        choices = [font for font in preferred if not installed or font in installed]
        if not choices:
            choices = ["Arial"]
        return tuple(dict.fromkeys(choices))

    def _scale_row(self, parent: ttk.Widget, row: int, label: str, variable, start: float, end: float, resolution: float) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=5)
        scale = tk.Scale(parent, variable=variable, from_=start, to=end, resolution=resolution, orient="horizontal", showvalue=False, highlightthickness=0, command=lambda _value: self.redraw_subtitle_preview_box())
        scale.grid(row=row, column=1, sticky="ew", padx=8)
        ttk.Entry(parent, textvariable=variable, width=9).grid(row=row, column=2)

    def _build_subtitle_preview(self, parent: ttk.Frame) -> None:
        toolbar = ttk.Frame(parent)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(4, 8))
        ttk.Button(toolbar, text="使用当前第一个视频", command=self.use_current_video_for_preview).pack(side="left")
        ttk.Button(toolbar, text="手动选择视频", command=self.choose_preview_video).pack(side="left", padx=6)
        ttk.Button(toolbar, text="随机换一帧", command=self.load_random_preview_frame).pack(side="left")
        ttk.Label(toolbar, textvariable=self.subtitle_preview_time, foreground="#666").pack(side="left", padx=12)
        ttk.Label(parent, textvariable=self.subtitle_preview_video, foreground="#666").grid(row=1, column=0, sticky="w")
        text_row = ttk.Frame(parent)
        text_row.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(text_row, text="预览字幕").pack(side="left")
        ttk.Entry(text_row, textvariable=self.subtitle_preview_text).pack(side="left", fill="x", expand=True, padx=8)
        canvas = tk.Canvas(parent, width=420, height=640, background="#111", highlightthickness=1, highlightbackground="#999")
        canvas.grid(row=3, column=0, sticky="w", pady=(8, 4))
        canvas.bind("<ButtonPress-1>", self._preview_press)
        canvas.bind("<B1-Motion>", self._preview_drag)
        canvas.bind("<ButtonRelease-1>", self._preview_release)
        self.subtitle_preview_canvas = canvas
        ttk.Label(parent, text="拖动方框内部可移动；拖动四角可调整范围。没有字幕的随机帧，点“随机换一帧”。", foreground="#666").grid(row=4, column=0, sticky="w")
        parent.columnconfigure(0, weight=1)
        for variable in (self.subtitle_cover_x, self.subtitle_cover_y, self.subtitle_cover_width, self.subtitle_cover_height, self.subtitle_cover_opacity, self.subtitle_cover_mode, self.subtitle_cover_blur_sigma, self.subtitle_font_name, self.subtitle_font_size, self.subtitle_position, self.subtitle_layout, self.subtitle_preview_text):
            variable.trace_add("write", lambda *_args: self.redraw_subtitle_preview_box())

    def _build_llm_review_panel(self, parent: ttk.Frame) -> None:
        ttk.Checkbutton(parent, text="启用整集语义审核 + 全剧一致性审核", variable=self.enable_llm_review).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        ttk.Label(parent, text="审核模型").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(parent, textvariable=self.llm_review_model, width=28).grid(row=1, column=1, sticky="w", padx=8)
        ttk.Label(parent, text="Ruta 暂用 deepseek-v4-flash；以后可改 deepseek-v4-pro。", foreground="#666").grid(row=1, column=2, sticky="w")
        ttk.Label(parent, text="高可信阈值").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Spinbox(parent, from_=0.50, to=0.99, increment=0.01, textvariable=self.review_confidence_threshold, width=8).grid(row=2, column=1, sticky="w", padx=8)
        ttk.Label(parent, text="默认 0.82；仅用于日志标记风险，不再跳过高置信字幕。", foreground="#666").grid(row=2, column=2, sticky="w")
        ttk.Label(parent, text="流程：OCR/ASR对齐 → Flash初译 → 整集审核并合并语义碎片 → 全文件夹统一人物/家族/地点 → 编码。", foreground="#666").grid(row=3, column=0, columnspan=3, sticky="w", pady=(2, 0))
        parent.columnconfigure(1, weight=1)

    def toggle_llm_review_panel(self) -> None:
        if not self.llm_review_frame:
            return
        if self.llm_review_expanded.get():
            self.llm_review_frame.grid_remove()
            self.llm_review_expanded.set(False)
        else:
            self.llm_review_frame.grid(row=34, column=0, columnspan=3, sticky="ew", pady=(0, 10))
            self.llm_review_expanded.set(True)

    def agent_bridge_root(self) -> Path:
        return Path(__file__).resolve().with_name("agent-bridge")

    def refresh_translation_mode_panel(self) -> None:
        if not hasattr(self, "subtitle_tab") or not hasattr(self, "agent_mode_frame"):
            return
        agent_mode = self.translation_backend.get() == "Agent 模式"
        # Existing API inputs occupy these rows. Keeping their original grid
        # positions preserves old layouts/configs while the mode selector acts
        # as their containing fold.
        for widget in self.api_mode_widgets:
            if agent_mode:
                widget.grid_remove()
            else:
                widget.grid()
        if agent_mode:
            if self.llm_review_frame:
                self.llm_review_frame.grid_remove()
            self.agent_mode_frame.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(0, 10))
            status = agent_bridge.bridge_status(self.agent_bridge_root())
            if status.get("connected"):
                self.agent_bridge_status.set("Agent 已连接，最近心跳正常")
            elif status.get("state", {}).get("agent_enabled"):
                self.agent_bridge_status.set("已生成初始化文件，等待 Agent 对话注册或恢复监听")
            else:
                self.agent_bridge_status.set("Agent 未初始化")
        else:
            self.agent_mode_frame.grid_remove()
            if self.llm_review_expanded.get() and self.llm_review_frame:
                self.llm_review_frame.grid(row=34, column=0, columnspan=3, sticky="ew", pady=(0, 10))

    def generate_agent_initialization(self) -> None:
        try:
            result = agent_bridge.generate_initialization(
                self.agent_bridge_root(),
                Path(__file__).resolve().parent,
                python_executable=sys.executable,
                max_parallel=self._parallel_limit(),
            )
            self.clipboard_clear()
            self.clipboard_append(result["command"])
            self.update_idletasks()
            self.agent_bridge_status.set(f"初始化命令已复制；注册文件：{result['init_path']}")
            self.append_log(f"Agent 初始化命令已复制到剪贴板。\n规则：{result['rules_path']}\n注册：{result['init_path']}\n")
            messagebox.showinfo("Agent 初始化", "初始化命令已复制到剪贴板。\n请粘贴到你希望作为翻译 Agent 的 Codex 对话中。")
        except Exception as exc:
            messagebox.showerror("Agent 初始化失败", str(exc))

    def test_agent_communication(self) -> None:
        try:
            status = agent_bridge.bridge_status(self.agent_bridge_root())
            if status.get("connected"):
                age = status.get("heartbeat_age_seconds")
                self.agent_bridge_status.set(f"通信正常；心跳约 {age:.0f} 秒前")
                messagebox.showinfo("Agent 通信", "文件协议、注册代次和 Agent 心跳均正常。")
            elif status.get("state", {}).get("agent_enabled"):
                self.agent_bridge_status.set("协议目录正常，但没有新鲜的 Agent 心跳")
                messagebox.showwarning("Agent 通信", "初始化文件存在，但 Agent 对话尚未注册，或已经停止轮询。\n请在对应 Codex 对话运行初始化命令并保持监听。")
            else:
                self.agent_bridge_status.set("Agent 未初始化")
                messagebox.showwarning("Agent 通信", "请先生成初始化命令，并粘贴到一个 Codex 对话。")
        except Exception as exc:
            messagebox.showerror("Agent 通信测试失败", str(exc))

    def stop_agent_listener(self) -> None:
        if not messagebox.askyesno("停止 Agent 监听", "这会使当前翻译 Agent 对话的注册失效。确定继续吗？"):
            return
        try:
            agent_bridge.stop_agent(self.agent_bridge_root())
            self.agent_bridge_status.set("Agent 监听已停止；再次使用需重新初始化")
        except Exception as exc:
            messagebox.showerror("停止 Agent 失败", str(exc))

    def toggle_subtitle_preview(self) -> None:
        if not self.subtitle_preview_frame:
            return
        if self.subtitle_preview_expanded.get():
            self.subtitle_preview_frame.grid_remove()
            self.subtitle_preview_expanded.set(False)
        else:
            self.subtitle_preview_frame.grid(row=27, column=0, columnspan=3, sticky="ew", pady=(0, 10))
            self.subtitle_preview_expanded.set(True)
            if not self.subtitle_preview_photo:
                self.use_current_video_for_preview()

    def _first_input_video(self) -> Path | None:
        if self.selected_inputs:
            path = Path(self.selected_inputs[0])
            return path if path.is_file() else None
        raw = self.input_path.get().strip()
        if not raw:
            return None
        path = Path(raw)
        if path.is_file():
            return path
        if path.is_dir():
            try:
                inputs = video_dedup.collect_inputs(path)
            except (OSError, ValueError):
                return None
            return inputs[0] if inputs else None
        return None

    def use_current_video_for_preview(self) -> None:
        video = self._first_input_video()
        if not video:
            messagebox.showwarning("缺少视频", "请先选择输入视频/目录，或手动选择一个预览视频。")
            return
        self.subtitle_preview_video.set(str(video))
        self.load_random_preview_frame()

    def choose_preview_video(self) -> None:
        path = filedialog.askopenfilename(title="选择字幕区域预览视频", filetypes=[("视频文件", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v"), ("所有文件", "*.*")])
        if path:
            self.subtitle_preview_video.set(path)
            self.load_random_preview_frame()

    def load_random_preview_frame(self) -> None:
        raw = self.subtitle_preview_video.get().strip()
        if not raw:
            self.use_current_video_for_preview()
            return
        video = Path(raw)
        if not video.is_file():
            messagebox.showwarning("视频不存在", f"找不到预览视频：{video}")
            return
        try:
            ffmpeg = video_dedup.find_binary("ffmpeg", None)
            ffprobe = video_dedup.find_binary("ffprobe", None)
            info = video_dedup.probe_video(video, ffprobe)
            duration = max(0.0, float(info.get("duration") or 0))
            second = random.uniform(0, max(0.1, duration - 0.1)) if duration > 0.2 else 0
            if self.subtitle_preview_temp:
                self.subtitle_preview_temp.unlink(missing_ok=True)
            fd, name = tempfile.mkstemp(prefix="subtitle-preview-", suffix=".png")
            os.close(fd)
            frame = Path(name)
            command = [
                ffmpeg,
                "-hide_banner",
                "-y",
                "-ss",
                f"{second:.3f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-vf",
                "scale=420:-2",
                str(frame),
            ]
            subprocess.run(command, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", **video_dedup.hidden_subprocess_kwargs())
            photo = tk.PhotoImage(file=str(frame))
            self.subtitle_preview_temp = frame
            self.subtitle_preview_photo = photo
            self.subtitle_preview_image_size = (photo.width(), photo.height())
            if self.subtitle_preview_canvas:
                self.subtitle_preview_canvas.configure(width=photo.width(), height=photo.height())
            self.subtitle_preview_time.set(f"随机帧：{second:.1f}s / {duration:.1f}s")
            self.redraw_subtitle_preview_box()
        except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, tk.TclError) as exc:
            messagebox.showerror("抽帧失败", str(exc))

    def _cover_box_pixels(self) -> tuple[float, float, float, float]:
        width, height = self.subtitle_preview_image_size
        if width <= 0 or height <= 0:
            return (0, 0, 0, 0)
        cover_x, cover_y, cover_width, cover_height = self._cover_values()
        x = width * cover_x / 100
        y = height * cover_y / 100
        w = width * cover_width / 100
        h = height * cover_height / 100
        return x, y, x + w, y + h

    def _cover_values(self) -> tuple[float, float, float, float]:
        cover_x = max(0.0, min(99.0, float(self.subtitle_cover_x.get())))
        cover_y = max(0.0, min(99.0, float(self.subtitle_cover_y.get())))
        cover_width = max(1.0, min(100.0 - cover_x, float(self.subtitle_cover_width.get())))
        cover_height = max(1.0, min(100.0 - cover_y, float(self.subtitle_cover_height.get())))
        return cover_x, cover_y, cover_width, cover_height

    def _set_cover_box_from_pixels(self, x1: float, y1: float, x2: float, y2: float) -> None:
        width, height = self.subtitle_preview_image_size
        if width <= 0 or height <= 0:
            return
        min_w = width * 0.05
        min_h = height * 0.03
        x1, x2 = sorted((max(0, min(width, x1)), max(0, min(width, x2))))
        y1, y2 = sorted((max(0, min(height, y1)), max(0, min(height, y2))))
        if x2 - x1 < min_w:
            x2 = min(width, x1 + min_w)
        if y2 - y1 < min_h:
            y2 = min(height, y1 + min_h)
        self.subtitle_cover_x.set(round(x1 / width * 100, 1))
        self.subtitle_cover_width.set(round((x2 - x1) / width * 100, 1))
        self.subtitle_cover_y.set(round(y1 / height * 100, 1))
        self.subtitle_cover_height.set(round((y2 - y1) / height * 100, 1))

    def redraw_subtitle_preview_box(self) -> None:
        canvas = self.subtitle_preview_canvas
        if not canvas:
            return
        canvas.delete("all")
        if not self.subtitle_preview_photo:
            canvas.create_text(210, 320, text="展开后选择视频并随机抽帧", fill="#ddd")
            return
        x1, y1, x2, y2 = self._cover_box_pixels()
        preview_image = self._preview_image_with_mask(x1, y1, x2, y2)
        canvas.create_image(0, 0, image=preview_image, anchor="nw")
        if self.subtitle_preview_mask_stipple:
            canvas.create_rectangle(x1, y1, x2, y2, fill="#ffffff", stipple=self.subtitle_preview_mask_stipple, outline="")
        canvas.create_rectangle(x1, y1, x2, y2, outline="#7b61ff", width=2)
        self._draw_preview_subtitle(canvas, x1, y1, x2, y2)
        handle_size = 8
        for x, y in ((x1, y1), (x2, y1), (x1, y2), (x2, y2)):
            canvas.create_rectangle(x - handle_size / 2, y - handle_size / 2, x + handle_size / 2, y + handle_size / 2, fill="#7b61ff", outline="white")

    def _preview_image_with_mask(self, x1: float, y1: float, x2: float, y2: float) -> tk.PhotoImage:
        opacity = max(0.0, min(1.0, float(self.subtitle_cover_opacity.get())))
        blur_sigma = max(0.5, min(100.0, float(self.subtitle_cover_blur_sigma.get())))
        cover_mode = self._subtitle_cover_mode_value()
        self.subtitle_preview_mask_stipple = None
        # In bilingual mode the rectangle constrains text placement only. The
        # cover effect is rendered exclusively when replacing the old subtitle.
        show_mask = self._subtitle_layout_value() == "replace" and self.subtitle_cover.get()
        if not self.subtitle_preview_temp or not show_mask or (cover_mode == "color" and opacity <= 0):
            return self.subtitle_preview_photo
        try:
            from PIL import Image, ImageFilter, ImageTk

            base = Image.open(self.subtitle_preview_temp).convert("RGBA")
            box = (
                int(round(x1)),
                int(round(y1)),
                int(round(x2)),
                int(round(y2)),
            )
            if cover_mode == "blur":
                region = base.crop(box)
                # Convert the full-resolution FFmpeg sigma into the preview's
                # smaller coordinate system so the GUI resembles final output.
                preview_scale = max(0.1, base.width / 1080.0)
                region = region.filter(ImageFilter.GaussianBlur(radius=blur_sigma * preview_scale))
                composite = base.copy()
                composite.paste(region, box)
            else:
                overlay = Image.new("RGBA", base.size, (255, 255, 255, 0))
                mask_alpha = int(round(opacity * 255))
                overlay.paste((255, 255, 255, mask_alpha), box)
                composite = Image.alpha_composite(base, overlay)
            self.subtitle_preview_composite_photo = ImageTk.PhotoImage(composite)
            return self.subtitle_preview_composite_photo
        except Exception:
            # Pillow is optional for the bare GUI. A color mask can still use
            # Tk stipple; blur falls back to the outlined region only.
            if not self.subtitle_preview_photo:
                raise
            if cover_mode == "blur":
                return self.subtitle_preview_photo
            stipple = ""
            if opacity < 0.2:
                stipple = "gray12"
            elif opacity < 0.4:
                stipple = "gray25"
            elif opacity < 0.7:
                stipple = "gray50"
            else:
                stipple = "gray75"
            self.subtitle_preview_mask_stipple = stipple
            return self.subtitle_preview_photo

    def _preview_font_size(self) -> int:
        canvas_width, _height = self.subtitle_preview_image_size
        try:
            configured = int(round(float(self.subtitle_font_size.get())))
        except (tk.TclError, TypeError, ValueError):
            configured = 28
        if canvas_width <= 0:
            return configured
        # The preview frame is scaled to 420px wide while real videos are
        # commonly 1080px wide. Scale the real burn-in font down for preview.
        return max(8, int(configured * canvas_width / 1080))

    def _preview_font_name(self) -> str:
        return self.subtitle_font_name.get().strip() or "Arial"

    def _preview_position_value(self) -> str:
        position = self._subtitle_position_value()
        if position == "auto":
            return "top" if self._subtitle_layout_value() == "bilingual" else "bottom"
        return position

    def _wrap_preview_text(self, text: str, box_width: float, font_size: int) -> str:
        value = " ".join(text.replace("\n", " ").split()).strip()
        if not value:
            return ""
        max_chars = max(10, min(56, int(box_width / max(8, font_size) / 0.72)))
        words = value.split(" ")
        lines: list[str] = []
        current = ""
        for word in words:
            candidate = word if not current else f"{current} {word}"
            if len(candidate) <= max_chars or not current:
                current = candidate
                continue
            lines.append(current)
            current = word
            if len(lines) >= 1:
                break
        used = sum(len(line.split(" ")) for line in lines)
        remaining = words[used:]
        if remaining:
            current = " ".join(remaining)
        if current:
            lines.append(current)
        return "\n".join(lines[:2])

    def _draw_preview_subtitle(self, canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float) -> None:
        text = self._wrap_preview_text(self.subtitle_preview_text.get(), x2 - x1, self._preview_font_size())
        if not text:
            return
        font_size = self._preview_font_size()
        font_name = self._preview_font_name()
        lines = text.splitlines()
        line_height = font_size + 4
        total_height = line_height * len(lines)
        if self._subtitle_layout_value() == "replace":
            base_y = (y1 + y2) / 2 - total_height / 2 + line_height / 2
        else:
            position = self._preview_position_value()
            if position == "top":
                base_y = y1 + total_height / 2 + 10
            else:
                base_y = y2 - total_height / 2 - 10
            base_y = min(y2 - total_height / 2 - 6, max(y1 + total_height / 2 + 6, base_y))
        center_x = (x1 + x2) / 2
        for index, line in enumerate(lines):
            y = base_y + index * line_height
            for dx, dy in ((-1, -1), (1, -1), (-1, 1), (1, 1), (0, 1)):
                canvas.create_text(center_x + dx, y + dy, text=line, fill="#000000", font=(font_name, font_size, "bold"), anchor="center")
            canvas.create_text(center_x, y, text=line, fill="#ffffff", font=(font_name, font_size, "bold"), anchor="center")

    def _preview_hit_test(self, x: float, y: float) -> str | None:
        x1, y1, x2, y2 = self._cover_box_pixels()
        handles = {"nw": (x1, y1), "ne": (x2, y1), "sw": (x1, y2), "se": (x2, y2)}
        for name, (hx, hy) in handles.items():
            if abs(x - hx) <= 12 and abs(y - hy) <= 12:
                return name
        if x1 <= x <= x2 and y1 <= y <= y2:
            self.subtitle_preview_drag_offset = (x - x1, y - y1)
            return "move"
        return None

    def _preview_press(self, event) -> None:
        self.subtitle_preview_drag = self._preview_hit_test(event.x, event.y)

    def _preview_drag(self, event) -> None:
        if not self.subtitle_preview_drag:
            return
        x1, y1, x2, y2 = self._cover_box_pixels()
        mode = self.subtitle_preview_drag
        if mode == "move":
            offset_x, offset_y = self.subtitle_preview_drag_offset
            box_w, box_h = x2 - x1, y2 - y1
            width, height = self.subtitle_preview_image_size
            nx1 = max(0, min(width - box_w, event.x - offset_x))
            ny1 = max(0, min(height - box_h, event.y - offset_y))
            self._set_cover_box_from_pixels(nx1, ny1, nx1 + box_w, ny1 + box_h)
        elif mode == "nw":
            self._set_cover_box_from_pixels(event.x, event.y, x2, y2)
        elif mode == "ne":
            self._set_cover_box_from_pixels(x1, event.y, event.x, y2)
        elif mode == "sw":
            self._set_cover_box_from_pixels(event.x, y1, x2, event.y)
        elif mode == "se":
            self._set_cover_box_from_pixels(x1, y1, event.x, event.y)

    def _preview_release(self, _event) -> None:
        self.subtitle_preview_drag = None

    def choose_input(self) -> None:
        paths = list(filedialog.askopenfilenames(title="选择一个或多个输入视频", filetypes=[("视频文件", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v"), ("所有文件", "*.*")]))
        if paths:
            self.selected_inputs = paths
            self.input_path.set(paths[0] if len(paths) == 1 else f"已选择 {len(paths)} 个视频")
            if len(paths) == 1 and not self.output_path.get():
                source = Path(paths[0])
                self.output_path.set(str(source.with_name(f"{source.stem}_local{source.suffix}")))
            elif len(paths) > 1:
                self.output_path.set(str(Path(paths[0]).parent / "processed"))

    def choose_input_dir(self) -> None:
        path = filedialog.askdirectory(title="选择包含视频的目录")
        if path:
            self.selected_inputs = []
            count = len(video_dedup.collect_inputs(Path(path)))
            self.input_path.set(path)
            self.output_path.set(str(Path(path) / "processed"))
            self.status.set(f"目录内发现 {count} 个视频")

    def choose_output(self) -> None:
        source = Path(self.selected_inputs[0]) if len(self.selected_inputs) == 1 else (Path(self.input_path.get()) if self.input_path.get() else None)
        if source and source.is_file() and len(self.selected_inputs) <= 1:
            path = filedialog.asksaveasfilename(title="保存输出视频", defaultextension=source.suffix, initialfile=f"{source.stem}_local{source.suffix}", filetypes=[("MP4 视频", "*.mp4"), ("所有文件", "*.*")])
        else:
            path = filedialog.askdirectory(title="选择输出目录")
        if path:
            self.output_path.set(path)

    def choose_music(self) -> None:
        path = filedialog.askopenfilename(title="选择背景音乐", filetypes=[("音频文件", "*.mp3 *.wav *.m4a *.aac *.flac"), ("所有文件", "*.*")])
        if path:
            self.music.set(path)

    def choose_music_dir(self) -> None:
        path = filedialog.askdirectory(title="选择随机背景音乐目录")
        if path:
            self.music_dir.set(path)

    def choose_effect_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择动态特效素材",
            filetypes=[
                ("特效素材", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v *.png *.jpg *.jpeg *.webp"),
                ("所有文件", "*.*"),
            ],
        )
        if path:
            self.effect_file.set(path)

    def choose_effect_dir(self) -> None:
        path = filedialog.askdirectory(title="选择随机动态特效目录")
        if path:
            self.effect_dir.set(path)

    def choose_border_file(self) -> None:
        path = filedialog.askopenfilename(
            title="选择边框素材",
            filetypes=[
                ("边框素材", "*.png *.webp *.mp4 *.mov *.webm *.mkv"),
                ("所有文件", "*.*"),
            ],
        )
        if path:
            self.border_file.set(path)

    def choose_color(self) -> None:
        chosen = colorchooser.askcolor(self.color.get(), title="选择叠加颜色")[1]
        if chosen:
            self.color.set(chosen)

    def choose_subtitle(self) -> None:
        path = filedialog.askopenfilename(title="选择字幕文件", filetypes=[("字幕文件", "*.srt *.ass *.vtt"), ("所有文件", "*.*")])
        if path:
            self.subtitle_file.set(path)

    def choose_subtitle_output(self) -> None:
        source = self._current_video_path()
        if self._subtitle_mode_value() in {"soft", "burn"}:
            initial = f"{source.stem}_subtitle.mp4" if source else "output_subtitle.mp4"
            path = filedialog.asksaveasfilename(title="保存字幕处理后的视频", defaultextension=".mp4", initialfile=initial, filetypes=[("MP4 视频", "*.mp4"), ("MKV 视频", "*.mkv"), ("所有文件", "*.*")])
        else:
            path = filedialog.asksaveasfilename(title="保存字幕文件", defaultextension=".srt", filetypes=[("SRT 字幕", "*.srt"), ("ASS 字幕", "*.ass"), ("所有文件", "*.*")])
        if path:
            self.subtitle_output.set(path)

    def _current_video_path(self) -> Path | None:
        if self.selected_inputs:
            path = Path(self.selected_inputs[0])
            return path if path.is_file() else None
        raw = self.input_path.get().strip()
        if raw:
            path = Path(raw)
            if path.is_file():
                return path
        return None

    def _start_external_command(self, command: list[str], status: str) -> None:
        self.enqueue_task(status.replace("…", ""), command, env=self._llm_env())

    def enqueue_task(self, title: str, command: list[str], cleanup_paths: list[Path] | None = None, env: dict[str, str] | None = None) -> int:
        cleanup_paths = cleanup_paths or []
        with self.task_lock:
            self.task_counter += 1
            log_dir = Path(__file__).with_name("logs")
            log_dir.mkdir(parents=True, exist_ok=True)
            safe_title = "".join(char if char.isalnum() or char in "-_" else "_" for char in title).strip("_")[:60]
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            log_path = log_dir / f"{timestamp}-task-{self.task_counter}-{safe_title or 'video'}.log"
            task = QueuedTask(self.task_counter, title, command, cleanup_paths, env, log_path)
            self.task_log_paths[task.task_id] = log_path
            self.pending_tasks.append(task)
            pending = len(self.pending_tasks)
            active = len(self.active_processes)
        self._create_task_window(task)
        self.append_task_log(task.task_id, f"\n[任务 {task.task_id}] 已加入队列：{title}\n[任务 {task.task_id}] 日志文件：{log_path}\n> {subprocess.list2cmdline(command)}\n")
        self.stop_button.configure(state="normal")
        self.status.set(f"排队 {pending} / 运行 {active}")
        self._maybe_start_tasks()
        return task.task_id

    def _create_task_window(self, task: QueuedTask) -> None:
        window = tk.Toplevel(self)
        window.title(f"任务 {task.task_id}: {task.title}")
        window.geometry("760x420")
        frame = ttk.Frame(window, padding=10)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=f"任务 {task.task_id}: {task.title}", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
        text = tk.Text(frame, height=18, wrap="word", state="disabled", font=("Consolas", 9))
        scroll = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side="left", fill="both", expand=True, pady=(8, 0))
        scroll.pack(side="right", fill="y", pady=(8, 0))
        window.protocol("WM_DELETE_WINDOW", lambda task_id=task.task_id: self._hide_task_window(task_id))
        self.task_windows[task.task_id] = window
        self.task_logs[task.task_id] = text

    def _hide_task_window(self, task_id: int) -> None:
        window = self.task_windows.get(task_id)
        if window and window.winfo_exists():
            window.withdraw()

    def append_task_log(self, task_id: int, text: str) -> None:
        self.append_log(text)
        log_path = self.task_log_paths.get(task_id)
        if log_path:
            try:
                with log_path.open("a", encoding="utf-8", newline="") as stream:
                    stream.write(text)
            except OSError as exc:
                self.append_log(f"[任务 {task_id}] 写入日志文件失败: {exc}\n")
        task_log = self.task_logs.get(task_id)
        if task_log and task_log.winfo_exists():
            task_log.configure(state="normal")
            task_log.insert("end", text)
            task_log.see("end")
            task_log.configure(state="disabled")

    def _parallel_limit(self) -> int:
        try:
            return max(1, min(5, int(self.max_parallel_tasks.get())))
        except (TypeError, ValueError, tk.TclError):
            return 5

    def _subtitle_source_value(self) -> str:
        return {
            "自动：软字幕优先，否则硬字幕OCR+音频ASR": "auto",
            "自动：软字幕优先，否则OCR+音频ASR交叉审核": "auto",
            "自动：软字幕→硬字幕OCR+音频ASR交叉审核": "auto",
            "自动：优先软字幕，否则语音识别": "auto",
            "自动：软字幕优先，否则硬字幕OCR": "auto-ocr",
            "自动：软字幕→硬字幕OCR": "auto-ocr",
            "自动：软字幕→硬字幕OCR→语音识别": "auto",
            "软字幕+音频ASR交叉审核": "soft-asr",
            "硬字幕OCR+音频ASR交叉审核": "ocr-asr",
            "只用软字幕轨道": "soft",
            "只用硬字幕OCR": "hard-ocr",
            "只用音频ASR": "asr",
            "只用语音识别": "asr",
        }.get(self.subtitle_source.get(), "hard-ocr")

    def _subtitle_mode_value(self) -> str:
        return {"烧录到画面": "burn", "封装软字幕": "soft"}.get(self.subtitle_mode.get(), "burn")

    def _subtitle_layout_value(self) -> str:
        return {"覆盖原字幕": "replace", "双语字幕": "bilingual"}.get(self.subtitle_layout.get(), "replace")

    def _subtitle_cover_mode_value(self) -> str:
        return {"局部高斯模糊": "blur", "白色半透明蒙版": "color"}.get(
            self.subtitle_cover_mode.get(), "blur"
        )

    def _subtitle_position_value(self) -> str:
        return {
            "自动": "auto",
            "底部": "bottom",
            "顶部": "top",
        }.get(self.subtitle_position.get(), "auto")

    def _ocr_language_value(self) -> str:
        value = {
            "自动": "auto",
            "中文": "ch",
            "英语": "en",
            "阿拉伯语": "arabic",
        }.get(self.subtitle_ocr_language.get(), "auto")
        if value == "auto" and self._source_language_value() == "Arabic":
            return "arabic"
        return value

    def _ocr_device_value(self) -> str:
        return {"自动": "auto", "auto": "auto", "cuda": "cuda", "cpu": "cpu"}.get(
            self.subtitle_ocr_device.get(), "auto"
        )

    def _source_language_value(self) -> str:
        return {
            "自动": "auto",
            "中文": "Chinese",
            "英语": "English",
            "阿拉伯语": "Arabic",
        }.get(self.subtitle_source_language.get(), "auto")

    def _llm_env(self) -> dict[str, str]:
        env: dict[str, str] = {
            "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True",
            "VIDEO_DEDUP_GLOBAL_LLM_WORKERS": "5",
        }
        if self.llm_api_key.get().strip():
            env["OPENAI_API_KEY"] = self.llm_api_key.get().strip()
        if self.llm_base_url.get().strip():
            env["OPENAI_BASE_URL"] = self.llm_base_url.get().strip()
        if self.llm_model.get().strip():
            env["OPENAI_MODEL"] = self.llm_model.get().strip()
        if self.llm_model_b.get().strip():
            env["OPENAI_MODEL_B"] = self.llm_model_b.get().strip()
        if self.llm_review_model.get().strip():
            env["OPENAI_REVIEW_MODEL"] = self.llm_review_model.get().strip()
        return env

    def _docker_path(self, path: Path, host_root: Path, container_root: str) -> str:
        rel = path.resolve().relative_to(host_root.resolve())
        value = rel.as_posix()
        return container_root if not value or value == "." else f"{container_root}/{value}"

    def _build_docker_command(
        self,
        input_arg: Path,
        output: Path,
        config_file: Path,
        input_list_file: Path | None,
        inputs: list[Path],
        output_is_file: bool,
        seed: int | None,
    ) -> list[str]:
        if shutil.which("docker") is None:
            raise RuntimeError("未找到 docker 命令。请先安装并启动 Docker Desktop。")
        image = self.docker_image.get().strip() or "video-dedup-local:ocr"
        input_mount = input_arg if input_arg.is_dir() else input_arg.parent
        if input_list_file:
            parents = {item.parent.resolve() for item in inputs}
            if len(parents) != 1:
                raise RuntimeError("Docker 模式下多选文件需要位于同一个目录。建议直接选择目录。")
            input_mount = next(iter(parents))
        output_mount = output.parent if output_is_file else output
        temp_mount = config_file.parent

        container_input = self._docker_path(input_arg, input_mount, "/input") if not input_list_file else "/input"
        container_output = "/output/" + output.name if output_is_file else "/output"
        container_config = self._docker_path(config_file, temp_mount, "/tmpcfg")
        config_values = json.loads(config_file.read_text(encoding="utf-8-sig"))
        asset_mounts: list[tuple[Path, str]] = []
        for key in ("background_music", "effect_file", "border_file"):
            raw = config_values.get(key)
            if not raw:
                continue
            host_file = Path(raw).expanduser().resolve()
            container_dir = f"/assets/{key}"
            asset_mounts.append((host_file.parent, container_dir))
            config_values[key] = f"{container_dir}/{host_file.name}"
        for key in ("background_music_dir", "effect_dir"):
            raw = config_values.get(key)
            if not raw:
                continue
            host_dir = Path(raw).expanduser().resolve()
            container_dir = f"/assets/{key}"
            asset_mounts.append((host_dir, container_dir))
            config_values[key] = container_dir
        config_file.write_text(json.dumps(config_values, ensure_ascii=False), encoding="utf-8")
        container_list = None
        if input_list_file:
            input_list_file.write_text(json.dumps([self._docker_path(item, input_mount, "/input") for item in inputs], ensure_ascii=False), encoding="utf-8")
            container_list = self._docker_path(input_list_file, temp_mount, "/tmpcfg")

        docker_hardware = self.hardware_acceleration.get()
        if docker_hardware == "apple":
            docker_hardware = "cpu"

        command = [
            "docker",
            "run",
            "--rm",
        ]
        if docker_hardware == "nvidia":
            command += ["--gpus", "all"]
        for host_path, container_path in asset_mounts:
            command += ["-v", f"{host_path}:{container_path}:ro"]
        command += [
            "-v",
            f"{input_mount}:/input",
            "-v",
            f"{output_mount}:/output",
            "-v",
            f"{temp_mount}:/tmpcfg",
            "-v",
            "video_dedup_paddlex:/root/.paddlex",
            "-e",
            "OPENAI_API_KEY",
            "-e",
            "OPENAI_BASE_URL",
            "-e",
            "OPENAI_MODEL",
            "-e",
            "OPENAI_MODEL_B",
            "-e",
            "OPENAI_REVIEW_MODEL",
            "-e",
            "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True",
            "-e",
            "VIDEO_DEDUP_GLOBAL_LLM_WORKERS=5",
            "-e",
            "VIDEO_DEDUP_GLOBAL_SLOT_DIR=/tmpcfg/video-dedup-locks",
            "--entrypoint",
            "python",
            image,
            "/app/batch_pipeline.py",
            container_input,
            container_output,
            "--preset",
            self.preset.get(),
            "--config",
            container_config,
            "--hardware-acceleration",
            docker_hardware,
        ]
        if container_list:
            command += ["--input-list", container_list]
        if seed is not None:
            command += ["--seed", str(seed)]
        return command

    def _maybe_start_tasks(self) -> None:
        with self.task_lock:
            limit = self._parallel_limit()
            to_start: list[QueuedTask] = []
            while self.pending_tasks and len(self.active_processes) + len(self.starting_tasks) + len(to_start) < limit:
                task = self.pending_tasks.pop(0)
                self.starting_tasks.add(task.task_id)
                to_start.append(task)
            pending = len(self.pending_tasks)
            active = len(self.active_processes) + len(self.starting_tasks)
        for task in to_start:
            threading.Thread(target=self._run_task, args=(task,), daemon=True).start()
        self.status.set(f"排队 {pending} / 运行 {active}" if active or pending else "就绪")

    def subtitle_detect(self) -> None:
        source = self._current_video_path()
        if not source:
            messagebox.showwarning("缺少视频", "请先选择一个输入视频。目录批量字幕处理稍后再做。")
            return
        command = [sys.executable, "-u", str(Path(__file__).with_name("subtitle_tool.py")), "detect", str(source)]
        self._start_external_command(command, "检测字幕中…")

    def subtitle_extract(self) -> None:
        source = self._current_video_path()
        if not source:
            messagebox.showwarning("缺少视频", "请先选择一个输入视频。")
            return
        output = self.subtitle_output.get().strip() or str(source.with_name(f"{source.stem}_subtitle.srt"))
        self.subtitle_output.set(output)
        self.subtitle_file.set(output)
        command = [sys.executable, "-u", str(Path(__file__).with_name("subtitle_tool.py")), "extract", str(source), output]
        self._start_external_command(command, "提取字幕中…")

    def subtitle_transcribe(self) -> None:
        source = self._current_video_path()
        if not source:
            messagebox.showwarning("缺少视频", "请先选择一个输入视频。")
            return
        output = str(source.with_name(f"{source.stem}_asr.srt"))
        self.subtitle_output.set(output)
        self.subtitle_file.set(output)
        command = [
            sys.executable,
            "-u",
            str(Path(__file__).with_name("subtitle_tool.py")),
            "transcribe",
            str(source),
            output,
            "--model-size",
            self.whisper_model.get().strip() or "medium",
            "--language",
            "auto",
            "--device",
            self.whisper_device.get(),
        ]
        self._start_external_command(command, "语音识别字幕中…")

    def subtitle_detect_region(self) -> None:
        source = self._current_video_path()
        if not source:
            messagebox.showwarning("缺少视频", "请先选择一个输入视频。")
            return
        command = [
            sys.executable,
            "-u",
            str(Path(__file__).with_name("subtitle_tool.py")),
            "detect-region",
            str(source),
            "--ocr-language",
            self._ocr_language_value(),
            "--device",
            self._ocr_device_value(),
        ]
        self._start_external_command(command, "OCR 检测字幕区域中…")

    def subtitle_translate(self) -> None:
        subtitle = self.subtitle_file.get().strip()
        if not subtitle or not Path(subtitle).is_file():
            messagebox.showwarning("缺少字幕", "请先选择或提取一个 SRT/ASS 字幕文件。")
            return
        path = Path(subtitle)
        output = self.subtitle_output.get().strip() or str(path.with_name(f"{path.stem}_{self.subtitle_target_language.get().strip() or 'translated'}{path.suffix}"))
        if Path(output).resolve() == path.resolve() or Path(output).suffix.lower() not in {".srt", ".ass", ".vtt"}:
            output = str(path.with_name(f"{path.stem}_{self.subtitle_target_language.get().strip() or 'translated'}{path.suffix}"))
        self.subtitle_output.set(output)
        command = [
            sys.executable,
            "-u",
            str(Path(__file__).with_name("subtitle_tool.py")),
            "translate",
            subtitle,
            output,
            "--target-language",
            self.subtitle_target_language.get().strip() or "English",
            "--source-language",
            self._source_language_value(),
            "--provider",
            self.subtitle_provider.get(),
            "--model",
            self.llm_model.get().strip() or "deepseek-v4-flash",
            "--source-kind",
            "asr" if self._subtitle_source_value() == "asr" else ("soft" if self._subtitle_source_value() == "soft" else "ocr"),
            "--parallel-batches",
            "1",
        ]
        if self.enable_llm_review.get():
            command += [
                "--enable-llm-review",
                "--llm-review-model",
                self.llm_review_model.get().strip() or self.llm_model.get().strip() or "deepseek-v4-flash",
            ]
        self._start_external_command(command, "翻译字幕中…")

    def subtitle_render(self) -> None:
        source = self._current_video_path()
        subtitle = self.subtitle_file.get().strip()
        if not source:
            messagebox.showwarning("缺少视频", "请先选择一个输入视频。")
            return
        if not subtitle or not Path(subtitle).is_file():
            messagebox.showwarning("缺少字幕", "请先选择或生成一个字幕文件。")
            return
        output = self.subtitle_output.get().strip() or str(source.with_name(f"{source.stem}_subtitle.mp4"))
        if Path(output).suffix.lower() in {".srt", ".ass", ".vtt"}:
            output = str(source.with_name(f"{source.stem}_subtitle.mp4"))
        self.subtitle_output.set(output)
        command = [
            sys.executable,
            "-u",
            str(Path(__file__).with_name("subtitle_tool.py")),
            "render",
            str(source),
            subtitle,
            output,
            "--mode",
            self._subtitle_mode_value(),
            "--layout",
            self._subtitle_layout_value(),
            "--position",
            self._subtitle_position_value(),
            "--font-name",
            self.subtitle_font_name.get().strip() or "Arial",
            "--font-size",
            str(self.subtitle_font_size.get()),
            "--quality",
            str(self.crf.get()),
            "--hardware-acceleration",
            self.hardware_acceleration.get(),
        ]
        if self.subtitle_cover.get():
            cover_x, cover_y, cover_width, cover_height = self._cover_values()
            command += [
                "--cover",
                "--cover-x-percent",
                str(cover_x),
                "--cover-y-percent",
                str(cover_y),
                "--cover-width-percent",
                str(cover_width),
                "--cover-height-percent",
                str(cover_height),
                "--cover-opacity",
                str(self.subtitle_cover_opacity.get()),
                "--cover-color",
                "white",
                "--cover-mode",
                self._subtitle_cover_mode_value(),
                "--cover-blur-sigma",
                str(self.subtitle_cover_blur_sigma.get()),
                "--cover-ocr-language",
                self._ocr_language_value(),
            ]
            if self.subtitle_cover_auto_detect.get():
                command += ["--cover-auto-detect"]
        self._start_external_command(command, "处理字幕视频中…")

    def config_dict(self) -> dict:
        output_aspect = {
            "保持原画幅": "source",
            "竖屏 9:16": "portrait",
            "横屏 16:9": "landscape",
            "方形 1:1": "square",
        }.get(self.output_aspect.get(), "source")
        target_fps = 0.0 if self.target_fps.get() == "保持源帧率" else float(self.target_fps.get())
        effect_timing = {"随机时间出现": "random", "全程循环": "continuous"}.get(self.effect_timing.get(), "random")
        effect_key_mode = {"黑色背景": "black", "绿色幕布": "green", "透明通道": "alpha"}.get(self.effect_key_mode.get(), "black")
        effect_position = {"全屏": "full", "顶部": "top", "底部": "bottom"}.get(self.effect_position.get(), "full")
        return {
            "state_version": 8,
            "transform_preset": self.preset.get(),
            "crop_percent": self.crop.get(), "zoom_percent": self.zoom.get(),
            "mirror": self.mirror.get(), "speed": self.speed.get(),
            "brightness": self.brightness.get(), "contrast": self.contrast.get(), "saturation": self.saturation.get(),
            "color": self.color.get() or None, "color_opacity": self.color_opacity.get(), "fade_seconds": self.fade.get(),
            "trim_start": self.trim_start.get(), "trim_end": self.trim_end.get(), "background_music": self.music.get() or None,
            "background_music_dir": self.music_dir.get() or None,
            "music_volume": self.music_volume.get(), "keep_audio": self.keep_audio.get(), "crf": self.crf.get(),
            "preset": self.encoder_preset.get(), "audio_bitrate": "192k",
            "hardware_acceleration": self.hardware_acceleration.get(),
            "blur_background": self.blur_background.get(),
            "blur_sigma": self.blur_sigma.get(),
            "foreground_scale": self.foreground_scale.get(),
            "output_aspect": output_aspect,
            "target_fps": target_fps,
            "border_file": self.border_file.get().strip() or None,
            "enable_dynamic_effects": self.enable_dynamic_effects.get(),
            "effect_file": self.effect_file.get().strip() or None,
            "effect_dir": self.effect_dir.get().strip() or None,
            "effect_random_type": self.effect_random_type.get(),
            "effect_timing": effect_timing,
            "effect_frequency": self.effect_frequency.get(),
            "effect_duration": self.effect_duration.get(),
            "effect_opacity": self.effect_opacity.get(),
            "effect_key_mode": effect_key_mode,
            "effect_position": effect_position,
            "random_seed": None,
            "enable_dedup_stage": self.enable_dedup_stage.get(),
            "enable_subtitle_pipeline": self.enable_subtitle_pipeline.get(),
            "enable_recap_stage": self.enable_recap_stage.get(),
            "reuse_final_subtitle_cache": self.reuse_final_subtitle_cache.get(),
            "subtitle_source": self.subtitle_source.get(),
            "subtitle_mode": self.subtitle_mode.get(),
            "subtitle_layout": self.subtitle_layout.get(),
            "subtitle_position": self.subtitle_position.get(),
            "subtitle_cover": self.subtitle_cover.get(),
            "subtitle_cover_auto_detect": self.subtitle_cover_auto_detect.get(),
            "subtitle_cover_x": self.subtitle_cover_x.get(),
            "subtitle_cover_y": self.subtitle_cover_y.get(),
            "subtitle_cover_width": self.subtitle_cover_width.get(),
            "subtitle_cover_height": self.subtitle_cover_height.get(),
            "subtitle_cover_opacity": self.subtitle_cover_opacity.get(),
            "subtitle_cover_mode": self.subtitle_cover_mode.get(),
            "subtitle_cover_blur_sigma": self.subtitle_cover_blur_sigma.get(),
            "subtitle_font_name": self.subtitle_font_name.get().strip() or "Arial",
            "subtitle_font_size": self.subtitle_font_size.get(),
            "subtitle_ocr_language": self.subtitle_ocr_language.get(),
            "subtitle_ocr_device": self.subtitle_ocr_device.get(),
            "subtitle_source_language": self.subtitle_source_language.get(),
            "subtitle_target_language": self.subtitle_target_language.get(),
            "subtitle_glossary": self.subtitle_glossary.get(),
            "translation_backend": self.translation_backend.get(),
            "translation_quality": self.translation_quality.get(),
            "localization_strategy": self.localization_strategy.get(),
            "llm_base_url": self.llm_base_url.get().strip(),
            "llm_model": self.llm_model.get().strip(),
            "enable_llm_review": self.enable_llm_review.get(),
            "llm_model_b": self.llm_model_b.get().strip(),
            "llm_review_model": self.llm_review_model.get().strip(),
            "review_confidence_threshold": self.review_confidence_threshold.get(),
            "whisper_model": self.whisper_model.get().strip(),
            "whisper_device": self.whisper_device.get(),
            "subtitle_backend": self.subtitle_backend.get(),
            "docker_image": self.docker_image.get().strip(),
        }

    def video_config_dict(self) -> dict:
        config = self.config_dict()
        allowed = set(video_dedup.asdict(video_dedup.PRESETS[self.preset.get()]))
        return {key: value for key, value in config.items() if key in allowed}

    def apply_config(self, config: dict) -> None:
        config = dict(config)
        transform_preset = str(config.get("transform_preset", "")).strip()
        if transform_preset in video_dedup.PRESETS:
            self.preset.set(transform_preset)
            self.preset_display.set(PRESET_LABELS.get(transform_preset, transform_preset))
        try:
            state_version = int(config.get("state_version", 0) or 0)
        except (TypeError, ValueError):
            state_version = 0
        if state_version < 2:
            if str(config.get("llm_model_b", "")).casefold().startswith("qwen3.6"):
                config["llm_model_b"] = "deepseek-v4-flash"
            if str(config.get("llm_review_model", "")).casefold().startswith("qwen3.6"):
                config["llm_review_model"] = "deepseek-v4-flash"
            config.setdefault("review_confidence_threshold", 0.82)
        if state_version < 8:
            config["crf"] = 23
            if config.get("hardware_acceleration") == "auto":
                config["hardware_acceleration"] = "nvidia"
            if config.get("subtitle_ocr_device") == "自动":
                config["subtitle_ocr_device"] = "cuda"
            if config.get("whisper_device") == "auto":
                config["whisper_device"] = "cuda"
        if config.get("subtitle_source") in {
            "自动：优先软字幕，否则语音识别",
            "自动：软字幕→硬字幕OCR→语音识别",
            "自动：软字幕→硬字幕OCR+音频ASR交叉审核",
            "自动：软字幕优先，否则OCR+音频ASR交叉审核",
        }:
            config["subtitle_source"] = "自动：软字幕优先，否则硬字幕OCR+音频ASR"
        elif config.get("subtitle_source") == "自动：软字幕→硬字幕OCR":
            config["subtitle_source"] = "自动：软字幕优先，否则硬字幕OCR"
        elif config.get("subtitle_source") == "只用语音识别":
            config["subtitle_source"] = "只用音频ASR"
        config["output_aspect"] = {
            "source": "保持原画幅",
            "portrait": "竖屏 9:16",
            "landscape": "横屏 16:9",
            "square": "方形 1:1",
        }.get(config.get("output_aspect"), config.get("output_aspect", "保持原画幅"))
        fps = config.get("target_fps")
        config["target_fps"] = "保持源帧率" if not fps else str(int(float(fps)))
        config["effect_timing"] = {"random": "随机时间出现", "continuous": "全程循环"}.get(
            config.get("effect_timing"), config.get("effect_timing", "随机时间出现")
        )
        config["effect_key_mode"] = {"black": "黑色背景", "green": "绿色幕布", "alpha": "透明通道"}.get(
            config.get("effect_key_mode"), config.get("effect_key_mode", "黑色背景")
        )
        config["effect_position"] = {"full": "全屏", "top": "顶部", "bottom": "底部"}.get(
            config.get("effect_position"), config.get("effect_position", "全屏")
        )
        mapping = {
            "crop_percent": self.crop, "zoom_percent": self.zoom,
            "mirror": self.mirror, "speed": self.speed, "brightness": self.brightness,
            "contrast": self.contrast, "saturation": self.saturation, "color": self.color, "color_opacity": self.color_opacity,
            "fade_seconds": self.fade, "trim_start": self.trim_start, "trim_end": self.trim_end,
            "background_music": self.music, "music_volume": self.music_volume, "keep_audio": self.keep_audio,
            "background_music_dir": self.music_dir,
            "crf": self.crf, "preset": self.encoder_preset,
            "hardware_acceleration": self.hardware_acceleration,
            "blur_background": self.blur_background,
            "blur_sigma": self.blur_sigma,
            "foreground_scale": self.foreground_scale,
            "output_aspect": self.output_aspect,
            "target_fps": self.target_fps,
            "border_file": self.border_file,
            "enable_dynamic_effects": self.enable_dynamic_effects,
            "effect_file": self.effect_file,
            "effect_dir": self.effect_dir,
            "effect_random_type": self.effect_random_type,
            "effect_timing": self.effect_timing,
            "effect_frequency": self.effect_frequency,
            "effect_duration": self.effect_duration,
            "effect_opacity": self.effect_opacity,
            "effect_key_mode": self.effect_key_mode,
            "effect_position": self.effect_position,
            "enable_dedup_stage": self.enable_dedup_stage,
            "enable_subtitle_pipeline": self.enable_subtitle_pipeline,
            "enable_recap_stage": self.enable_recap_stage,
            "reuse_final_subtitle_cache": self.reuse_final_subtitle_cache,
            "subtitle_source": self.subtitle_source,
            "subtitle_mode": self.subtitle_mode,
            "subtitle_layout": self.subtitle_layout,
            "subtitle_position": self.subtitle_position,
            "subtitle_cover": self.subtitle_cover,
            "subtitle_cover_auto_detect": self.subtitle_cover_auto_detect,
            "subtitle_cover_x": self.subtitle_cover_x,
            "subtitle_cover_y": self.subtitle_cover_y,
            "subtitle_cover_width": self.subtitle_cover_width,
            "subtitle_cover_height": self.subtitle_cover_height,
            "subtitle_cover_opacity": self.subtitle_cover_opacity,
            "subtitle_cover_mode": self.subtitle_cover_mode,
            "subtitle_cover_blur_sigma": self.subtitle_cover_blur_sigma,
            "subtitle_font_name": self.subtitle_font_name,
            "subtitle_font_size": self.subtitle_font_size,
            "subtitle_ocr_language": self.subtitle_ocr_language,
            "subtitle_ocr_device": self.subtitle_ocr_device,
            "subtitle_source_language": self.subtitle_source_language,
            "subtitle_target_language": self.subtitle_target_language,
            "subtitle_glossary": self.subtitle_glossary,
            "translation_backend": self.translation_backend,
            "translation_quality": self.translation_quality,
            "localization_strategy": self.localization_strategy,
            "llm_base_url": self.llm_base_url,
            "llm_model": self.llm_model,
            "enable_llm_review": self.enable_llm_review,
            "llm_model_b": self.llm_model_b,
            "llm_review_model": self.llm_review_model,
            "review_confidence_threshold": self.review_confidence_threshold,
            "whisper_model": self.whisper_model,
            "whisper_device": self.whisper_device,
            "subtitle_backend": self.subtitle_backend,
            "docker_image": self.docker_image,
        }
        for key, variable in mapping.items():
            if key in config:
                variable.set(config[key] if config[key] is not None else "")
        self.refresh_translation_mode_panel()

    def load_preset(self, save_state: bool = False) -> None:
        preset = self.preset.get()
        if preset not in video_dedup.PRESETS:
            preset = "medium"
            self.preset.set(preset)
        self.preset_display.set(PRESET_LABELS.get(preset, preset))
        self.apply_config(video_dedup.asdict(video_dedup.PRESETS[preset]))
        if save_state:
            self.schedule_state_save()

    def on_preset_selected(self, _event=None) -> None:
        preset = PRESET_KEYS_BY_LABEL.get(self.preset_display.get(), "medium")
        self.preset.set(preset)
        self.load_preset(save_state=True)

    def state_variables(self) -> tuple[tk.Variable, ...]:
        return (
            self.preset,
            self.seed,
            self.crop,
            self.zoom,
            self.mirror,
            self.speed,
            self.brightness,
            self.contrast,
            self.saturation,
            self.color,
            self.color_opacity,
            self.fade,
            self.trim_start,
            self.trim_end,
            self.music,
            self.music_dir,
            self.music_volume,
            self.keep_audio,
            self.crf,
            self.encoder_preset,
            self.hardware_acceleration,
            self.blur_background,
            self.blur_sigma,
            self.foreground_scale,
            self.output_aspect,
            self.target_fps,
            self.border_file,
            self.enable_dynamic_effects,
            self.effect_file,
            self.effect_dir,
            self.effect_random_type,
            self.effect_timing,
            self.effect_frequency,
            self.effect_duration,
            self.effect_opacity,
            self.effect_key_mode,
            self.effect_position,
            self.enable_subtitle_pipeline,
            self.reuse_final_subtitle_cache,
            self.subtitle_source,
            self.subtitle_mode,
            self.subtitle_layout,
            self.subtitle_position,
            self.subtitle_cover,
            self.subtitle_cover_auto_detect,
            self.subtitle_cover_x,
            self.subtitle_cover_y,
            self.subtitle_cover_width,
            self.subtitle_cover_height,
            self.subtitle_cover_opacity,
            self.subtitle_cover_mode,
            self.subtitle_cover_blur_sigma,
            self.subtitle_font_name,
            self.subtitle_font_size,
            self.subtitle_ocr_language,
            self.subtitle_ocr_device,
            self.subtitle_source_language,
            self.subtitle_target_language,
            self.subtitle_glossary,
            self.translation_backend,
            self.translation_quality,
            self.localization_strategy,
            self.llm_base_url,
            self.llm_model,
            self.enable_llm_review,
            self.llm_model_b,
            self.llm_review_model,
            self.review_confidence_threshold,
            self.whisper_model,
            self.whisper_device,
            self.subtitle_backend,
            self.docker_image,
        )

    def enable_auto_state_save(self) -> None:
        if self._auto_state_enabled:
            return
        self._auto_state_enabled = True
        for variable in self.state_variables():
            variable.trace_add("write", lambda *_args: self.schedule_state_save())

    def schedule_state_save(self) -> None:
        if not self._auto_state_enabled:
            return
        if self._state_save_after_id:
            self.after_cancel(self._state_save_after_id)
        self._state_save_after_id = self.after(600, self.save_last_state)

    def save_last_state(self) -> None:
        self._state_save_after_id = None
        try:
            self.state_file.write_text(json.dumps(self.config_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError as exc:
            self.append_log(f"[配置] 自动保存上次参数失败: {exc}\n")

    def load_last_state(self) -> None:
        if not self.state_file.is_file():
            return
        try:
            self.apply_config(json.loads(self.state_file.read_text(encoding="utf-8-sig")))
            self.status.set("已恢复上次配置")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.append_log(f"[配置] 自动恢复上次参数失败: {exc}\n")

    def save_config(self) -> None:
        path = filedialog.asksaveasfilename(title="保存配置", defaultextension=".json", filetypes=[("JSON 配置", "*.json")])
        if path:
            Path(path).write_text(json.dumps(self.config_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            self.save_last_state()
            self.status.set("配置已保存")

    def open_config(self) -> None:
        path = filedialog.askopenfilename(title="载入配置", filetypes=[("JSON 配置", "*.json")])
        if not path:
            return
        try:
            self.apply_config(json.loads(Path(path).read_text(encoding="utf-8-sig")))
            self.save_last_state()
            self.status.set("配置已载入")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            messagebox.showerror("配置错误", str(exc))

    def append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def start(self) -> None:
        source, target = self.input_path.get().strip(), self.output_path.get().strip()
        if not any((self.enable_subtitle_pipeline.get(), self.enable_recap_stage.get(), self.enable_dedup_stage.get())):
            messagebox.showwarning("未选择处理阶段", "请至少勾选字幕、解说或去重中的一个阶段。")
            return
        if self.enable_recap_stage.get():
            messagebox.showinfo(
                "解说阶段使用独立项目",
                "解说编排需要先由 Agent 生成并校验结构化项目。请在“解说剪辑”页新建或载入项目，"
                "完成编排后使用该页的“生成最终成片”。\n\n"
                "主开始按钮当前负责字幕与去重；不会假装已经执行解说阶段。",
            )
            return
        if not self.selected_inputs and (not source or not Path(source).exists()):
            messagebox.showwarning("缺少输入", "请选择有效的输入视频或目录。")
            return
        if not target and self.enable_dedup_stage.get():
            messagebox.showwarning("缺少输出", "请选择输出视频或目录。")
            return
        if not target:
            raw_source = Path(self.selected_inputs[0]) if self.selected_inputs else Path(source)
            target = str((raw_source if raw_source.is_dir() else raw_source.parent) / "processed")
        if (
            self.enable_subtitle_pipeline.get()
            and self.translation_quality.get() == "高级翻译"
            and self.translation_backend.get() != "Agent 模式"
        ):
            messagebox.showwarning("高级翻译需要 Agent", "高级翻译的独立终审由 Agent 文件桥接执行，请把翻译执行方式切换为“Agent 模式”。")
            return
        if self.enable_subtitle_pipeline.get() and self.translation_backend.get() == "API 模式" and not self.llm_api_key.get().strip():
            messagebox.showwarning("缺少 API Key", "启用自动字幕流水线时需要填写 API Key。")
            return
        if self.enable_subtitle_pipeline.get() and self.translation_backend.get() == "Agent 模式":
            bridge_state = agent_bridge.bridge_status(self.agent_bridge_root())
            if not bridge_state.get("connected"):
                messagebox.showwarning(
                    "Agent 尚未连接",
                    "当前没有可用的 Agent 心跳。请先生成初始化命令，在指定 Codex 对话完成注册并保持监听，然后再开始任务。",
                )
                return
            if self.subtitle_backend.get() == "Docker OCR":
                messagebox.showwarning("Agent 模式不支持 Docker", "Agent 文件桥接首版仅支持“本机 Python”字幕后端，请切换后再开始。")
                return
        if self.enable_subtitle_pipeline.get() and self._subtitle_mode_value() == "soft" and self.subtitle_cover.get():
            messagebox.showwarning(
                "字幕模式冲突",
                "当前选择的是“封装软字幕”，这种模式不会把字幕或白色蒙版烧进画面。\n\n"
                "如果你需要白色蒙版和新字幕直接出现在视频画面里，请把“添加方式”改为“烧录到画面”。",
            )
            return
        try:
            seed = int(self.seed.get()) if self.seed.get().strip() else None
            config = self.video_config_dict()
            checked_config = video_dedup.TransformConfig(**config)
            if self.enable_dedup_stage.get():
                if checked_config.enable_dynamic_effects:
                    video_dedup.effect_candidates(checked_config)
                if checked_config.border_file and not Path(checked_config.border_file).expanduser().is_file():
                    raise FileNotFoundError(f"找不到边框素材: {checked_config.border_file}")
        except (OSError, TypeError, ValueError) as exc:
            messagebox.showerror("参数错误", str(exc))
            return
        self.save_last_state()
        try:
            if self.selected_inputs:
                inputs = [Path(item).resolve() for item in self.selected_inputs]
                input_arg = inputs[0]
                input_list_file: Path | None = None
            else:
                input_arg = Path(source).resolve()
                input_list_file = None
                inputs = video_dedup.collect_inputs(input_arg)
        except (OSError, ValueError) as exc:
            messagebox.showerror("输入错误", str(exc))
            return
        if not inputs:
            messagebox.showwarning("缺少输入", "没有找到可处理的视频。")
            return

        output = Path(target).resolve()
        source_is_directory = not self.selected_inputs and Path(source).resolve().is_dir()
        output_is_file = len(inputs) == 1 and not source_is_directory and output.suffix.lower() in video_dedup.VIDEO_SUFFIXES
        subtitle_only = self.enable_subtitle_pipeline.get() and not self.enable_dedup_stage.get()
        if not subtitle_only:
            if not output_is_file:
                output.mkdir(parents=True, exist_ok=True)
            else:
                output.parent.mkdir(parents=True, exist_ok=True)

        for input_file in inputs:
            output_file = output if output_is_file else output / f"{input_file.stem}_local{input_file.suffix}"
            if input_file.resolve() == output_file.resolve():
                messagebox.showerror("输出错误", f"输出不能覆盖输入：{input_file}")
                return

        fd, name = tempfile.mkstemp(prefix="video-tool-", suffix=".json")
        os.close(fd)
        config_file = Path(name)
        config_file.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
        cleanup_paths = [config_file]

        if self.selected_inputs and len(inputs) > 1:
            fd, list_name = tempfile.mkstemp(prefix="video-inputs-", suffix=".json")
            os.close(fd)
            input_list_file = Path(list_name)
            input_list_file.write_text(json.dumps([str(item) for item in inputs], ensure_ascii=False), encoding="utf-8")
            cleanup_paths.append(input_list_file)

        use_docker = self.enable_subtitle_pipeline.get() and self.subtitle_backend.get() == "Docker OCR"
        glossary_command_path: str | None = None
        selected_glossary = self.selected_glossary_path() if self.enable_subtitle_pipeline.get() else None
        if selected_glossary:
            if use_docker:
                fd, glossary_name = tempfile.mkstemp(
                    prefix="video-glossary-", suffix=".json", dir=config_file.parent
                )
                os.close(fd)
                glossary_copy = Path(glossary_name)
                shutil.copyfile(selected_glossary, glossary_copy)
                cleanup_paths.append(glossary_copy)
                glossary_command_path = self._docker_path(glossary_copy, config_file.parent, "/tmpcfg")
            else:
                glossary_command_path = str(selected_glossary.resolve())
        try:
            if use_docker:
                command = self._build_docker_command(input_arg, output, config_file, input_list_file, inputs, output_is_file, seed)
            else:
                command = [
                    sys.executable,
                    "-u",
                    str(Path(__file__).with_name("batch_pipeline.py")),
                    str(input_arg),
                    str(output),
                    "--preset",
                    self.preset.get(),
                    "--config",
                    str(config_file),
                    "--hardware-acceleration",
                    self.hardware_acceleration.get(),
                    "--video-workers",
                    str(self._parallel_limit()),
                ]
                if input_list_file:
                    command += ["--input-list", str(input_list_file)]
                if seed is not None:
                    command += ["--seed", str(seed)]
        except RuntimeError as exc:
            messagebox.showerror("Docker 后端错误", str(exc))
            for path in cleanup_paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            return
        if self.enable_subtitle_pipeline.get():
            cover_x, cover_y, cover_width, cover_height = self._cover_values()
            command += [
                "--enable-subtitles",
                "--translation-backend",
                "agent" if self.translation_backend.get() == "Agent 模式" else "api",
                "--translation-quality",
                "advanced" if self.translation_quality.get() == "高级翻译" else "fast",
                "--localization-strategy",
                {
                    "标准影视本地化": "cinematic_standard",
                    "自然口语": "conversational",
                    "忠实正式": "formal_faithful",
                    "海湾中性口语（阿拉伯语）": "gulf_neutral",
                }.get(self.localization_strategy.get(), "cinematic_standard"),
                "--agent-bridge-root",
                str(self.agent_bridge_root()),
                "--agent-task-title",
                Path(source).name if source else inputs[0].stem,
                "--subtitle-source",
                self._subtitle_source_value(),
                "--target-language",
                self.subtitle_target_language.get().strip() or "English",
                "--source-language",
                self._source_language_value(),
                "--ocr-language",
                self._ocr_language_value(),
                "--llm-model",
                    self.llm_model.get().strip() or "deepseek-v4-flash",
                "--parallel-batches",
                "1",
                "--whisper-model",
                self.whisper_model.get().strip() or "medium",
                "--whisper-device",
                "cpu" if use_docker else self.whisper_device.get(),
                "--subtitle-mode",
                self._subtitle_mode_value(),
                "--subtitle-layout",
                self._subtitle_layout_value(),
                "--subtitle-position",
                self._subtitle_position_value(),
                "--cover-y-percent",
                str(cover_y),
                "--cover-height-percent",
                str(cover_height),
                "--cover-opacity",
                str(self.subtitle_cover_opacity.get()),
                "--cover-color",
                "white",
                "--cover-mode",
                self._subtitle_cover_mode_value(),
                "--cover-blur-sigma",
                str(self.subtitle_cover_blur_sigma.get()),
                "--font-name",
                self.subtitle_font_name.get().strip() or "Arial",
                "--font-size",
                str(self.subtitle_font_size.get()),
            ]
            if not use_docker:
                command += ["--ocr-device", self._ocr_device_value()]
            if self.translation_backend.get() == "API 模式" and self.enable_llm_review.get():
                command += [
                    "--enable-llm-review",
                    "--llm-review-model",
                    self.llm_review_model.get().strip() or self.llm_model.get().strip() or "deepseek-v4-flash",
                    "--review-confidence-threshold",
                    str(self.review_confidence_threshold.get()),
                ]
            if glossary_command_path:
                command += ["--glossary-file", glossary_command_path]
            if not use_docker:
                command += [
                    "--cover-x-percent",
                    str(cover_x),
                    "--cover-width-percent",
                    str(cover_width),
                ]
            if self.subtitle_cover.get():
                command += ["--subtitle-cover"]
                if self.subtitle_cover_auto_detect.get():
                    command += ["--cover-auto-detect"]
            if not self.reuse_final_subtitle_cache.get():
                command += ["--force-subtitle-translation"]
            if not self.enable_dedup_stage.get():
                command += ["--subtitle-only"]

        if not self.enable_subtitle_pipeline.get() and not self.enable_dedup_stage.get():
            messagebox.showwarning("没有可执行阶段", "当前主流水线没有选中字幕或去重阶段。")
            return

        if source_is_directory:
            title = f"目录任务 {Path(source).name}（{len(inputs)} 个视频）"
        elif len(inputs) > 1:
            title = f"文件组任务（{len(inputs)} 个视频）"
        else:
            title = f"完整流水线 {inputs[0].name}"
        if self.enable_subtitle_pipeline.get() and not self.enable_dedup_stage.get():
            title = title.replace("完整流水线", "仅字幕").replace("目录任务", "仅字幕目录任务")
        task_env = self._llm_env() if self.translation_backend.get() == "API 模式" else {"PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK": "True"}
        self.enqueue_task(title, command, cleanup_paths, task_env)

    def _run_task(self, task: QueuedTask) -> None:
        try:
            process_env = os.environ.copy()
            if task.env:
                process_env.update(task.env)
            process = subprocess.Popen(task.command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", env=process_env, **video_dedup.hidden_subprocess_kwargs())
            with self.task_lock:
                cancelled_before_start = task.task_id not in self.starting_tasks
                self.starting_tasks.discard(task.task_id)
                if not cancelled_before_start:
                    self.active_processes[task.task_id] = process
                    self.task_cleanup[task.task_id] = task.cleanup_paths
                active = len(self.active_processes) + len(self.starting_tasks)
                pending = len(self.pending_tasks)
            if cancelled_before_start:
                if process.poll() is None:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, **video_dedup.hidden_subprocess_kwargs())
                    else:
                        process.terminate()
                for path in task.cleanup_paths:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                self.after(0, self.append_task_log, task.task_id, f"[任务 {task.task_id}] 已在启动前取消\n")
                self.after(0, self._task_finished, task.task_id, 1)
                return
            self.after(0, self.append_task_log, task.task_id, f"[任务 {task.task_id}] 开始：{task.title}\n")
            self.after(0, self.status.set, f"排队 {pending} / 运行 {active}")
            assert process.stdout
            for line in process.stdout:
                self.after(0, self.append_task_log, task.task_id, f"[任务 {task.task_id}] {line}")
            code = process.wait()
            self.after(0, self._task_finished, task.task_id, code)
        except OSError as exc:
            for path in task.cleanup_paths:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            self.after(0, self.append_task_log, task.task_id, f"[任务 {task.task_id}] 启动失败: {exc}\n")
            self.after(0, self._task_finished, task.task_id, 1)

    def _task_finished(self, task_id: int, code: int) -> None:
        with self.task_lock:
            self.starting_tasks.discard(task_id)
            self.active_processes.pop(task_id, None)
            cleanup_paths = self.task_cleanup.pop(task_id, [])
            active = len(self.active_processes) + len(self.starting_tasks)
            pending = len(self.pending_tasks)
        for path in cleanup_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        state = "完成" if code == 0 else "失败或已停止"
        self.append_task_log(task_id, f"[任务 {task_id}] {state}，退出码 {code}\n")
        window = self.task_windows.get(task_id)
        if window and window.winfo_exists():
            window.title(f"任务 {task_id}: {state}")
        self.stop_button.configure(state="normal" if active or pending else "disabled")
        self.status.set("全部任务完成" if not active and not pending else f"排队 {pending} / 运行 {active}")
        self._maybe_start_tasks()

    def stop(self) -> None:
        with self.task_lock:
            has_tasks = bool(self.active_processes or self.pending_tasks or self.starting_tasks)
        if not has_tasks:
            return
        if messagebox.askyesno("停止全部任务", "确定要停止所有运行中任务并清空等待队列吗？"):
            with self.task_lock:
                pending_cleanup = [path for task in self.pending_tasks for path in task.cleanup_paths]
                self.pending_tasks.clear()
                self.starting_tasks.clear()
                processes = list(self.active_processes.items())
            for path in pending_cleanup:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            # Invalidate outstanding Agent replies before terminating local
            # workers so a late conversation response cannot revive a stopped
            # task.
            agent_bridge.request_stop_all(self.agent_bridge_root())
            for task_id, process in processes:
                if process.poll() is not None:
                    continue
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, **video_dedup.hidden_subprocess_kwargs())
                else:
                    process.terminate()
                self.append_log(f"[任务 {task_id}] 已请求停止\n")
            self.status.set("正在停止…")

    def on_close(self) -> None:
        if self._state_save_after_id:
            self.after_cancel(self._state_save_after_id)
            self._state_save_after_id = None
        with self.task_lock:
            has_tasks = bool(self.active_processes or self.pending_tasks or self.starting_tasks)
        if has_tasks:
            if not messagebox.askyesno("退出", "仍有任务在运行或排队，确定停止并退出吗？"):
                return
            with self.task_lock:
                pending_cleanup = [path for task in self.pending_tasks for path in task.cleanup_paths]
                self.pending_tasks.clear()
                self.starting_tasks.clear()
                processes = list(self.active_processes.items())
            for path in pending_cleanup:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            agent_bridge.request_stop_all(self.agent_bridge_root())
            for _task_id, process in processes:
                if process.poll() is None:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, **video_dedup.hidden_subprocess_kwargs())
                    else:
                        process.terminate()
        self.save_last_state()
        self.destroy()


if __name__ == "__main__":
    video_dedup.install_hidden_subprocess_policy()
    VideoToolApp().mainloop()
