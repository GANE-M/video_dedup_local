#!/usr/bin/env python3
"""Tkinter desktop UI for the local FFmpeg video transformation tool."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, ttk

import video_dedup


@dataclass
class QueuedTask:
    task_id: int
    title: str
    command: list[str]
    cleanup_paths: list[Path]
    env: dict[str, str] | None = None


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
        self.task_lock = threading.Lock()
        self._make_variables()
        self._build_ui()
        self.load_preset()
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    def _make_variables(self) -> None:
        self.input_path = tk.StringVar()
        self.output_path = tk.StringVar()
        self.preset = tk.StringVar(value="medium")
        self.seed = tk.StringVar(value="2026")
        self.crop = tk.DoubleVar()
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
        self.crf = tk.IntVar(value=20)
        self.encoder_preset = tk.StringVar(value="medium")
        self.hardware_acceleration = tk.StringVar(value="nvidia")
        self.enable_subtitle_pipeline = tk.BooleanVar(value=True)
        self.subtitle_source = tk.StringVar(value="只用硬字幕OCR")
        self.subtitle_mode = tk.StringVar(value="烧录到画面")
        self.subtitle_layout = tk.StringVar(value="覆盖原字幕")
        self.subtitle_position = tk.StringVar(value="自动")
        self.subtitle_cover = tk.BooleanVar(value=True)
        self.subtitle_cover_auto_detect = tk.BooleanVar(value=True)
        self.subtitle_cover_y = tk.DoubleVar(value=74.0)
        self.subtitle_cover_height = tk.DoubleVar(value=11.0)
        self.subtitle_cover_opacity = tk.DoubleVar(value=0.72)
        self.subtitle_font_size = tk.IntVar(value=28)
        self.subtitle_ocr_language = tk.StringVar(value="自动")
        self.subtitle_source_language = tk.StringVar(value="自动")
        self.subtitle_target_language = tk.StringVar(value="English")
        self.llm_api_key = tk.StringVar(value=os.environ.get("OPENAI_API_KEY", ""))
        self.llm_base_url = tk.StringVar(value=os.environ.get("OPENAI_BASE_URL", "https://theruta.ai/api/v1/chat/completions"))
        self.llm_model = tk.StringVar(value=os.environ.get("OPENAI_MODEL", "deepseek-v4-flash"))
        self.llm_parallel_batches = tk.IntVar(value=2)
        self.whisper_model = tk.StringVar(value="medium")
        self.whisper_device = tk.StringVar(value="cuda")
        self.subtitle_backend = tk.StringVar(value="Docker OCR")
        self.docker_image = tk.StringVar(value="video-dedup-local:ocr")
        self.max_parallel_tasks = tk.IntVar(value=2)
        self.status = tk.StringVar(value="就绪")

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 16, "bold"))
        style.configure("Section.TLabelframe.Label", font=("Microsoft YaHei UI", 10, "bold"))
        root = ttk.Frame(self, padding=16)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text="本地视频处理工具", style="Title.TLabel").pack(anchor="w")
        ttk.Label(root, text="全部处理在本机完成，不连接易剪媒服务器。请仅处理自己拥有或获准修改的视频。", foreground="#666").pack(anchor="w", pady=(3, 12))

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

        preset_bar = ttk.Frame(root)
        preset_bar.pack(fill="x", pady=10)
        ttk.Label(preset_bar, text="处理强度").pack(side="left")
        combo = ttk.Combobox(preset_bar, textvariable=self.preset, values=("light", "medium", "strong"), state="readonly", width=12)
        combo.pack(side="left", padx=(8, 16))
        combo.bind("<<ComboboxSelected>>", lambda _e: self.load_preset())
        ttk.Button(preset_bar, text="载入预设", command=self.load_preset).pack(side="left")
        ttk.Label(preset_bar, text="随机种子").pack(side="left", padx=(24, 6))
        ttk.Entry(preset_bar, textvariable=self.seed, width=10).pack(side="left")
        ttk.Label(preset_bar, text="并行任务").pack(side="left", padx=(24, 6))
        ttk.Spinbox(preset_bar, from_=1, to=6, textvariable=self.max_parallel_tasks, width=5).pack(side="left")

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
        subtitle_tab = ttk.Frame(notebook, padding=12)
        notebook.add(video_tab, text="画面")
        notebook.add(time_tab, text="时间")
        notebook.add(audio_tab, text="声音")
        notebook.add(output_tab, text="输出质量")
        notebook.add(subtitle_tab, text="字幕")

        self._scale_row(video_tab, 0, "裁边比例 (%)", self.crop, 0, 15, 0.1)
        ttk.Checkbutton(video_tab, text="水平镜像", variable=self.mirror).grid(row=1, column=0, columnspan=3, sticky="w", pady=6)
        self._scale_row(video_tab, 2, "亮度", self.brightness, -0.2, 0.2, 0.005)
        self._scale_row(video_tab, 3, "对比度", self.contrast, 0.5, 2.0, 0.01)
        self._scale_row(video_tab, 4, "饱和度", self.saturation, 0, 3.0, 0.01)
        self._scale_row(video_tab, 5, "色彩叠加透明度", self.color_opacity, 0, 0.3, 0.005)
        ttk.Label(video_tab, text="叠加颜色").grid(row=6, column=0, sticky="w", pady=6)
        ttk.Entry(video_tab, textvariable=self.color, width=14).grid(row=6, column=1, sticky="w")
        ttk.Button(video_tab, text="选择颜色", command=self.choose_color).grid(row=6, column=2, sticky="w")
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

        ttk.Checkbutton(subtitle_tab, text="启用自动字幕流水线：自动取字幕/识别语音 → LLM 翻译 → 写入成片", variable=self.enable_subtitle_pipeline).grid(row=0, column=0, columnspan=3, sticky="w", pady=6)
        ttk.Label(subtitle_tab, text="字幕来源").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Combobox(subtitle_tab, textvariable=self.subtitle_source, values=("只用硬字幕OCR", "自动：软字幕→硬字幕OCR", "自动：软字幕→硬字幕OCR→语音识别", "只用软字幕轨道", "只用语音识别"), state="readonly", width=34).grid(row=1, column=1, sticky="w", padx=8)
        ttk.Label(subtitle_tab, text="原字幕语言（OCR）").grid(row=2, column=0, sticky="w", pady=5)
        ttk.Combobox(subtitle_tab, textvariable=self.subtitle_ocr_language, values=("自动", "中文", "英语", "阿拉伯语"), state="readonly", width=18).grid(row=2, column=1, sticky="w", padx=8)
        ttk.Label(subtitle_tab, text="翻译源语言").grid(row=3, column=0, sticky="w", pady=5)
        ttk.Combobox(subtitle_tab, textvariable=self.subtitle_source_language, values=("自动", "中文", "英语", "阿拉伯语"), state="readonly", width=18).grid(row=3, column=1, sticky="w", padx=8)
        ttk.Label(subtitle_tab, text="目标语言").grid(row=4, column=0, sticky="w", pady=5)
        ttk.Entry(subtitle_tab, textvariable=self.subtitle_target_language, width=18).grid(row=4, column=1, sticky="w", padx=8)
        ttk.Label(subtitle_tab, text="API Key").grid(row=5, column=0, sticky="w", pady=5)
        ttk.Entry(subtitle_tab, textvariable=self.llm_api_key, show="*").grid(row=5, column=1, sticky="ew", padx=8)
        ttk.Label(subtitle_tab, text="接口地址").grid(row=6, column=0, sticky="w", pady=5)
        ttk.Entry(subtitle_tab, textvariable=self.llm_base_url).grid(row=6, column=1, sticky="ew", padx=8)
        ttk.Label(subtitle_tab, text="模型名").grid(row=7, column=0, sticky="w", pady=5)
        ttk.Entry(subtitle_tab, textvariable=self.llm_model, width=24).grid(row=7, column=1, sticky="w", padx=8)
        llm_parallel = ttk.Frame(subtitle_tab)
        llm_parallel.grid(row=7, column=2, sticky="w")
        ttk.Label(llm_parallel, text="LLM并发").pack(side="left")
        ttk.Spinbox(llm_parallel, from_=1, to=4, textvariable=self.llm_parallel_batches, width=4).pack(side="left", padx=(5, 0))
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
        self._scale_row(subtitle_tab, 15, "手动遮盖起点高度 (%)", self.subtitle_cover_y, 50, 95, 1)
        self._scale_row(subtitle_tab, 16, "手动遮盖高度 (%)", self.subtitle_cover_height, 4, 30, 1)
        self._scale_row(subtitle_tab, 17, "白色蒙版透明度", self.subtitle_cover_opacity, 0, 1, 0.02)
        self._scale_row(subtitle_tab, 18, "字幕字号", self.subtitle_font_size, 16, 64, 1)
        ttk.Label(subtitle_tab, text="Whisper 模型").grid(row=19, column=0, sticky="w", pady=5)
        ttk.Entry(subtitle_tab, textvariable=self.whisper_model, width=18).grid(row=19, column=1, sticky="w", padx=8)
        ttk.Label(subtitle_tab, text="Whisper 设备").grid(row=20, column=0, sticky="w", pady=5)
        ttk.Combobox(subtitle_tab, textvariable=self.whisper_device, values=("cuda", "cpu"), state="readonly", width=18).grid(row=20, column=1, sticky="w", padx=8)
        ttk.Label(subtitle_tab, text="字幕处理后端").grid(row=21, column=0, sticky="w", pady=5)
        ttk.Combobox(subtitle_tab, textvariable=self.subtitle_backend, values=("Docker OCR", "本机 Python"), state="readonly", width=18).grid(row=21, column=1, sticky="w", padx=8)
        ttk.Label(subtitle_tab, text="Docker 镜像").grid(row=22, column=0, sticky="w", pady=5)
        ttk.Entry(subtitle_tab, textvariable=self.docker_image, width=28).grid(row=22, column=1, sticky="w", padx=8)
        ttk.Label(subtitle_tab, text="Docker Desktop 需要处于运行状态；容器只在任务期间临时启动。", foreground="#666").grid(row=23, column=0, columnspan=3, sticky="w")
        subtitle_tab.columnconfigure(1, weight=1)

        log_frame = ttk.LabelFrame(root, text="运行日志", padding=6)
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(log_frame, height=9, wrap="word", state="disabled", font=("Consolas", 9))
        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _path_row(self, parent: ttk.Widget, row: int, label: str, variable: tk.StringVar, command) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=5)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=8)
        ttk.Button(parent, text="选择", command=command).grid(row=row, column=2)

    def _scale_row(self, parent: ttk.Widget, row: int, label: str, variable, start: float, end: float, resolution: float) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=5)
        scale = tk.Scale(parent, variable=variable, from_=start, to=end, resolution=resolution, orient="horizontal", showvalue=False, highlightthickness=0)
        scale.grid(row=row, column=1, sticky="ew", padx=8)
        ttk.Entry(parent, textvariable=variable, width=9).grid(row=row, column=2)

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
        if self.subtitle_mode.get() == "soft" or self.subtitle_mode.get() == "burn":
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
        self.enqueue_task(status.replace("…", ""), command)

    def enqueue_task(self, title: str, command: list[str], cleanup_paths: list[Path] | None = None, env: dict[str, str] | None = None) -> int:
        cleanup_paths = cleanup_paths or []
        with self.task_lock:
            self.task_counter += 1
            task = QueuedTask(self.task_counter, title, command, cleanup_paths, env)
            self.pending_tasks.append(task)
            pending = len(self.pending_tasks)
            active = len(self.active_processes)
        self._create_task_window(task)
        self.append_task_log(task.task_id, f"\n[任务 {task.task_id}] 已加入队列：{title}\n> {subprocess.list2cmdline(command)}\n")
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
        task_log = self.task_logs.get(task_id)
        if task_log and task_log.winfo_exists():
            task_log.configure(state="normal")
            task_log.insert("end", text)
            task_log.see("end")
            task_log.configure(state="disabled")

    def _parallel_limit(self) -> int:
        try:
            return max(1, min(6, int(self.max_parallel_tasks.get())))
        except (TypeError, ValueError, tk.TclError):
            return 2

    def _llm_parallel_limit(self) -> int:
        try:
            return max(1, min(4, int(self.llm_parallel_batches.get())))
        except (TypeError, ValueError, tk.TclError):
            return 2

    def _subtitle_source_value(self) -> str:
        return {
            "自动：优先软字幕，否则语音识别": "auto",
            "自动：软字幕→硬字幕OCR": "auto-ocr",
            "自动：软字幕→硬字幕OCR→语音识别": "auto",
            "只用软字幕轨道": "soft",
            "只用硬字幕OCR": "hard-ocr",
            "只用语音识别": "asr",
        }.get(self.subtitle_source.get(), "hard-ocr")

    def _subtitle_mode_value(self) -> str:
        return {"烧录到画面": "burn", "封装软字幕": "soft"}.get(self.subtitle_mode.get(), "burn")

    def _subtitle_layout_value(self) -> str:
        return {"覆盖原字幕": "replace", "双语字幕": "bilingual"}.get(self.subtitle_layout.get(), "replace")

    def _subtitle_position_value(self) -> str:
        return {
            "自动": "auto",
            "底部": "bottom",
            "顶部": "top",
        }.get(self.subtitle_position.get(), "auto")

    def _ocr_language_value(self) -> str:
        return {
            "自动": "auto",
            "中文": "ch",
            "英语": "en",
            "阿拉伯语": "arabic",
        }.get(self.subtitle_ocr_language.get(), "auto")

    def _source_language_value(self) -> str:
        return {
            "自动": "auto",
            "中文": "Chinese",
            "英语": "English",
            "阿拉伯语": "Arabic",
        }.get(self.subtitle_source_language.get(), "auto")

    def _llm_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        if self.llm_api_key.get().strip():
            env["OPENAI_API_KEY"] = self.llm_api_key.get().strip()
        if self.llm_base_url.get().strip():
            env["OPENAI_BASE_URL"] = self.llm_base_url.get().strip()
        if self.llm_model.get().strip():
            env["OPENAI_MODEL"] = self.llm_model.get().strip()
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
        container_list = None
        if input_list_file:
            input_list_file.write_text(json.dumps([self._docker_path(item, input_mount, "/input") for item in inputs], ensure_ascii=False), encoding="utf-8")
            container_list = self._docker_path(input_list_file, temp_mount, "/tmpcfg")

        command = [
            "docker",
            "run",
            "--rm",
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
            "PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True",
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
            "cpu",
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
            "medium",
            "--language",
            "auto",
            "--device",
            "cuda",
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
            "--parallel-batches",
            str(self._llm_parallel_limit()),
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
            self.subtitle_mode.get(),
            "--layout",
            self.subtitle_layout.get(),
            "--position",
            self.subtitle_position.get(),
            "--font-size",
            str(self.subtitle_font_size.get()),
            "--hardware-acceleration",
            self.hardware_acceleration.get(),
        ]
        if self.subtitle_cover.get():
            command += [
                "--cover",
                "--cover-y-percent",
                str(self.subtitle_cover_y.get()),
                "--cover-height-percent",
                str(self.subtitle_cover_height.get()),
                "--cover-opacity",
                str(self.subtitle_cover_opacity.get()),
                "--cover-color",
                "white",
                "--cover-ocr-language",
                self._ocr_language_value(),
            ]
            if self.subtitle_cover_auto_detect.get():
                command += ["--cover-auto-detect"]
        self._start_external_command(command, "处理字幕视频中…")

    def config_dict(self) -> dict:
        return {
            "crop_percent": self.crop.get(), "mirror": self.mirror.get(), "speed": self.speed.get(),
            "brightness": self.brightness.get(), "contrast": self.contrast.get(), "saturation": self.saturation.get(),
            "color": self.color.get() or None, "color_opacity": self.color_opacity.get(), "fade_seconds": self.fade.get(),
            "trim_start": self.trim_start.get(), "trim_end": self.trim_end.get(), "background_music": self.music.get() or None,
            "background_music_dir": self.music_dir.get() or None,
            "music_volume": self.music_volume.get(), "keep_audio": self.keep_audio.get(), "crf": self.crf.get(),
            "preset": self.encoder_preset.get(), "audio_bitrate": "192k",
            "hardware_acceleration": self.hardware_acceleration.get(),
        }

    def apply_config(self, config: dict) -> None:
        mapping = {
            "crop_percent": self.crop, "mirror": self.mirror, "speed": self.speed, "brightness": self.brightness,
            "contrast": self.contrast, "saturation": self.saturation, "color": self.color, "color_opacity": self.color_opacity,
            "fade_seconds": self.fade, "trim_start": self.trim_start, "trim_end": self.trim_end,
            "background_music": self.music, "music_volume": self.music_volume, "keep_audio": self.keep_audio,
            "background_music_dir": self.music_dir,
            "crf": self.crf, "preset": self.encoder_preset,
            "hardware_acceleration": self.hardware_acceleration,
        }
        for key, variable in mapping.items():
            if key in config:
                variable.set(config[key] if config[key] is not None else "")

    def load_preset(self) -> None:
        self.apply_config(video_dedup.asdict(video_dedup.PRESETS[self.preset.get()]))

    def save_config(self) -> None:
        path = filedialog.asksaveasfilename(title="保存配置", defaultextension=".json", filetypes=[("JSON 配置", "*.json")])
        if path:
            Path(path).write_text(json.dumps(self.config_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            self.status.set("配置已保存")

    def open_config(self) -> None:
        path = filedialog.askopenfilename(title="载入配置", filetypes=[("JSON 配置", "*.json")])
        if not path:
            return
        try:
            self.apply_config(json.loads(Path(path).read_text(encoding="utf-8-sig")))
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
        if not self.selected_inputs and (not source or not Path(source).exists()):
            messagebox.showwarning("缺少输入", "请选择有效的输入视频或目录。")
            return
        if not target:
            messagebox.showwarning("缺少输出", "请选择输出视频或目录。")
            return
        if self.enable_subtitle_pipeline.get() and not self.llm_api_key.get().strip():
            messagebox.showwarning("缺少 API Key", "启用自动字幕流水线时需要填写 API Key。")
            return
        try:
            seed = int(self.seed.get()) if self.seed.get().strip() else None
            config = self.config_dict()
            video_dedup.load_config(self.preset.get(), None, seed)
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc))
            return
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
            command += [
                "--enable-subtitles",
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
                str(self._llm_parallel_limit()),
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
                str(self.subtitle_cover_y.get()),
                "--cover-height-percent",
                str(self.subtitle_cover_height.get()),
                "--cover-opacity",
                str(self.subtitle_cover_opacity.get()),
                "--cover-color",
                "white",
                "--font-size",
                str(self.subtitle_font_size.get()),
            ]
            if self.subtitle_cover.get():
                command += ["--subtitle-cover"]
                if self.subtitle_cover_auto_detect.get():
                    command += ["--cover-auto-detect"]

        if source_is_directory:
            title = f"目录任务 {Path(source).name}（{len(inputs)} 个视频）"
        elif len(inputs) > 1:
            title = f"文件组任务（{len(inputs)} 个视频）"
        else:
            title = f"完整流水线 {inputs[0].name}"
        self.enqueue_task(title, command, cleanup_paths, self._llm_env())

    def _run_task(self, task: QueuedTask) -> None:
        startup = None
        if os.name == "nt":
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        try:
            process_env = os.environ.copy()
            if task.env:
                process_env.update(task.env)
            process = subprocess.Popen(task.command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", startupinfo=startup, env=process_env)
            with self.task_lock:
                self.starting_tasks.discard(task.task_id)
                self.active_processes[task.task_id] = process
                self.task_cleanup[task.task_id] = task.cleanup_paths
                active = len(self.active_processes) + len(self.starting_tasks)
                pending = len(self.pending_tasks)
            self.after(0, self.append_task_log, task.task_id, f"[任务 {task.task_id}] 开始：{task.title}\n")
            self.after(0, self.status.set, f"排队 {pending} / 运行 {active}")
            assert process.stdout
            for line in process.stdout:
                self.after(0, self.append_task_log, task.task_id, f"[任务 {task.task_id}] {line}")
            code = process.wait()
            self.after(0, self._task_finished, task.task_id, code)
        except OSError as exc:
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
            has_tasks = bool(self.active_processes or self.pending_tasks)
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
            for task_id, process in processes:
                if process.poll() is not None:
                    continue
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
                else:
                    process.terminate()
                self.append_log(f"[任务 {task_id}] 已请求停止\n")
            self.status.set("正在停止…")

    def on_close(self) -> None:
        with self.task_lock:
            has_tasks = bool(self.active_processes or self.pending_tasks)
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
            for _task_id, process in processes:
                if process.poll() is None:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
                    else:
                        process.terminate()
        self.destroy()


if __name__ == "__main__":
    VideoToolApp().mainloop()
