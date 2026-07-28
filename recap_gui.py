"""Tkinter workspace for structured recap projects; no Agent or chat runtime."""
from __future__ import annotations

import json
import os
import random
import shutil
import subprocess
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import video_dedup
from recap.models import RecapProject, RecapSegment, now_iso
from recap.pacing import PRESETS, get_preset
from recap.project_store import create_project, delete_segment, load_project, save_new_version, update_segment
from recap.renderer import generate_voice_preview, inspect_sources, measure_project_loudness, render_project
from recap.timeline import validate_source_intervals
from recap.voice_library import VoiceLibrary, engines_for_language


class RecapWorkspace(ttk.Frame):
    def __init__(self, parent) -> None:
        super().__init__(parent, padding=10)
        self.pack(fill="both", expand=True)
        self.project_path = tk.StringVar()
        self.project_name = tk.StringVar(value="未加载项目")
        self.voice_id = tk.StringVar(value="calm_female")
        self.target_language = tk.StringVar(value="English")
        self.tts_engine = tk.StringVar(value="auto")
        self.narration_preset = tk.StringVar(value="standard")
        self.preset_labels = {
            "快节奏（约38%成片）": "fast",
            "标准解说（约50%成片）": "standard",
            "沉浸剧情（约65%成片）": "immersive",
        }
        self.preset_display = tk.StringVar(value="标准解说（约50%成片）")
        self.engine_labels: dict[str, str] = {}
        self.engine_display = tk.StringVar()
        self.loudness_labels = {
            "保持模型原始音量（不修改）": "keep_original",
            "匹配原片节目响度": "match_source_program",
            "统一到 -18 LUFS": "-18",
        }
        self.loudness_display = tk.StringVar(value=next(iter(self.loudness_labels)))
        self.caption_y_percent = tk.DoubleVar(value=12.0)
        self.caption_font_size = tk.IntVar(value=38)
        self.caption_preview_text = tk.StringVar(value="One decision changed everything.")
        self.caption_preview_status = tk.StringVar(value="尚未抽取随机帧")
        self.caption_preview_expanded = tk.BooleanVar(value=False)
        self.caption_preview_canvas: tk.Canvas | None = None
        self.caption_preview_photo: tk.PhotoImage | None = None
        self.caption_preview_temp: Path | None = None
        self.caption_preview_image_size = (0, 0)
        self.caption_preview_dragging = False
        self.status = tk.StringVar(value="就绪")
        self.segment_id = tk.StringVar()
        self.episode = tk.IntVar(value=1)
        self.source_start = tk.DoubleVar(value=0.0)
        self.source_end = tk.DoubleVar(value=10.0)
        self.mode = tk.StringVar(value="narration")
        self.purpose = tk.StringVar()
        self.project: RecapProject | None = None
        self.library = VoiceLibrary()
        self._refresh_engine_options()
        self._build()

    def _build(self) -> None:
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="新建项目", command=self.create_project).pack(side="left")
        ttk.Button(toolbar, text="载入项目", command=self.open_project).pack(side="left", padx=5)
        ttk.Entry(toolbar, textvariable=self.project_path, state="readonly").pack(side="left", fill="x", expand=True, padx=5)
        ttk.Label(toolbar, textvariable=self.status, foreground="#555").pack(side="right")

        settings = ttk.LabelFrame(self, text="项目与声纹", padding=8)
        settings.pack(fill="x", pady=8)
        ttk.Label(settings, textvariable=self.project_name, font=("Microsoft YaHei UI", 10, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(settings, text="解说语言").grid(row=0, column=1, sticky="e", padx=(20, 4))
        language_combo = ttk.Combobox(
            settings, textvariable=self.target_language,
            values=("English", "Arabic", "Chinese"), state="readonly", width=12,
        )
        language_combo.grid(row=0, column=2, sticky="w")
        language_combo.bind("<<ComboboxSelected>>", lambda _event: self._set_language())
        ttk.Label(settings, text="语音模型").grid(row=0, column=3, sticky="e", padx=(12, 4))
        self.engine_combo = ttk.Combobox(
            settings, textvariable=self.engine_display,
            values=tuple(self.engine_labels), state="readonly", width=24,
        )
        self.engine_combo.grid(row=0, column=4, sticky="w")
        self.engine_combo.bind("<<ComboboxSelected>>", lambda _event: self._set_engine())

        ttk.Label(settings, text="固定声纹").grid(row=1, column=0, sticky="e", padx=(0, 4), pady=(6, 0))
        self.voice_labels: dict[str, str] = {}
        self.voice_display = tk.StringVar()
        voice_control = ttk.Frame(settings)
        voice_control.grid(row=1, column=1, columnspan=3, sticky="w", pady=(6, 0))
        self.voice_combo = ttk.Combobox(voice_control, textvariable=self.voice_display, state="readonly", width=34)
        self.voice_combo.pack(side="left")
        self.voice_combo.bind("<<ComboboxSelected>>", lambda _event: self._set_voice())
        ttk.Button(voice_control, text="▶ 试听当前声纹", command=self.preview_voice).pack(side="left", padx=(6, 0))
        self.voice_count_label = ttk.Label(voice_control, foreground="#666")
        self.voice_count_label.pack(side="left", padx=(8, 0))
        ttk.Label(settings, text="解说音量").grid(row=2, column=0, sticky="e", pady=(6, 0))
        loudness_combo = ttk.Combobox(
            settings, textvariable=self.loudness_display,
            values=tuple(self.loudness_labels), state="readonly", width=28,
        )
        loudness_combo.grid(row=2, column=1, columnspan=2, sticky="w", pady=(6, 0))
        loudness_combo.bind("<<ComboboxSelected>>", lambda _event: self._set_loudness())
        ttk.Label(settings, text="节奏预设").grid(row=2, column=3, sticky="e", padx=(12, 4), pady=(6, 0))
        preset_combo = ttk.Combobox(
            settings, textvariable=self.preset_display,
            values=tuple(self.preset_labels), state="readonly", width=24,
        )
        preset_combo.grid(row=2, column=4, sticky="w", pady=(6, 0))
        preset_combo.bind("<<ComboboxSelected>>", lambda _event: self._set_preset())
        ttk.Button(settings, text="校验项目", command=self.validate_project).grid(row=3, column=3, padx=5, pady=(6, 0))
        ttk.Button(settings, text="仅测量原片响度", command=self.measure_loudness).grid(row=3, column=4, padx=5, pady=(6, 0))
        ttk.Label(
            settings,
            text="先选语言，再显示该语言支持的模型与声纹。默认保持 TTS 原始音量，不执行响度归一化。",
            foreground="#666",
        ).grid(row=4, column=0, columnspan=6, sticky="w", pady=(5, 0))
        settings.columnconfigure(0, weight=1)
        self._refresh_voice_options()

        preview_toggle = ttk.Button(self, text="展开/收起解说字幕随机帧预览", command=self.toggle_caption_preview)
        preview_toggle.pack(anchor="w", pady=(0, 5))
        self.caption_preview_frame = ttk.LabelFrame(self, text="解说字幕位置预览", padding=8)
        self._build_caption_preview(self.caption_preview_frame)

        self.body = ttk.Panedwindow(self, orient="horizontal")
        self.body.pack(fill="both", expand=True)
        left = ttk.Frame(self.body)
        right = ttk.Frame(self.body)
        self.body.add(left, weight=3)
        self.body.add(right, weight=2)

        columns = ("episode", "time", "mode", "purpose", "revision")
        self.tree = ttk.Treeview(left, columns=columns, show="tree headings", height=14)
        self.tree.heading("#0", text="segment_id")
        for key, label, width in (("episode", "集", 45), ("time", "源区间", 120), ("mode", "模式", 80), ("purpose", "用途", 150), ("revision", "修订", 50)):
            self.tree.heading(key, text=label)
            self.tree.column(key, width=width, anchor="w")
        self.tree.column("#0", width=130)
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self.select_segment)

        editor = ttk.LabelFrame(right, text="片段编辑（保存会建立新版本）", padding=8)
        editor.pack(fill="both", expand=True)
        fields = [
            ("segment_id", self.segment_id), ("集数", self.episode), ("开始秒", self.source_start),
            ("结束秒", self.source_end), ("用途", self.purpose),
        ]
        for row, (label, variable) in enumerate(fields):
            ttk.Label(editor, text=label).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Entry(editor, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=5)
        ttk.Label(editor, text="模式").grid(row=5, column=0, sticky="w", pady=3)
        ttk.Combobox(editor, textvariable=self.mode, values=("narration", "original"), state="readonly").grid(row=5, column=1, sticky="ew", padx=5)
        ttk.Label(editor, text="解说正文").grid(row=6, column=0, sticky="nw", pady=3)
        self.narration = tk.Text(editor, height=8, wrap="word")
        self.narration.grid(row=6, column=1, sticky="nsew", padx=5)
        buttons = ttk.Frame(editor)
        buttons.grid(row=7, column=0, columnspan=2, sticky="ew", pady=8)
        ttk.Button(buttons, text="新增片段", command=self.add_segment).pack(side="left")
        ttk.Button(buttons, text="保存片段", command=self.save_segment).pack(side="left", padx=5)
        ttk.Button(buttons, text="删除片段", command=self.remove_segment).pack(side="left")
        ttk.Button(buttons, text="仅渲染此片段", command=self.render_selected).pack(side="left", padx=5)
        editor.columnconfigure(1, weight=1)
        editor.rowconfigure(6, weight=1)

        actions = ttk.Frame(self)
        actions.pack(fill="x", pady=(8, 4))
        ttk.Button(actions, text="生成预览", command=lambda: self.start_render(False)).pack(side="left")
        ttk.Button(actions, text="生成最终成片", command=lambda: self.start_render(True)).pack(side="left", padx=6)
        ttk.Label(actions, text="重复报告不为空时会自动阻止最终合成；每个版本输出到独立 v#### 目录。", foreground="#666").pack(side="left", padx=8)
        self.log = tk.Text(self, height=8, wrap="word")
        self.log.pack(fill="x")

    def _append(self, value: object) -> None:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        self.log.insert("end", text + "\n")
        self.log.see("end")

    def _require(self) -> tuple[Path, RecapProject]:
        if not self.project or not self.project_path.get():
            raise RuntimeError("请先载入或新建解说项目")
        return Path(self.project_path.get()), self.project

    def create_project(self) -> None:
        path = filedialog.asksaveasfilename(title="保存解说项目", defaultextension=".json", filetypes=[("JSON", "*.json")])
        if not path:
            return
        source = filedialog.askdirectory(title="选择短剧源视频目录")
        if not source:
            return
        output = filedialog.askdirectory(title="选择解说输出目录")
        if not output:
            return
        name = simpledialog.askstring("项目名称", "请输入项目名称", initialvalue=Path(source).name) or Path(source).name
        project_id = re_safe_id(name)
        pattern = simpledialog.askstring("集数文件规则", "使用 {episode} 表示集数", initialvalue="*第{episode}集.mp4") or "*第{episode}集.mp4"
        preset = get_preset(self.narration_preset.get())
        target_percent = preset.target_ratio * 100.0
        try:
            ffprobe = video_dedup.find_binary("ffprobe")
            source_pattern = pattern.replace("{episode}", "*").replace("{number}", "*")
            source_info = inspect_sources(Path(source), ffprobe, source_pattern)
            if source_info["episode_count"] == 0 or source_info["total_duration"] <= 0:
                raise ValueError(f"没有源视频匹配规则：{pattern}")
            target_duration = round(float(source_info["total_duration"]) * target_percent / 100.0, 3)
        except Exception as exc:
            messagebox.showerror("计算成片时长失败", str(exc))
            return
        payload = {
            "schema_version": 1, "project_id": project_id, "project_name": name,
            "source_root": source, "episode_pattern": pattern, "subtitle_root": "",
            "output_root": output, "target_language": self.target_language.get(), "target_duration_seconds": target_duration,
            "narration_preset": preset.key,
            "voice_id": self.voice_id.get(), "tts_engine": self.tts_engine.get(), "narration_speed": 1.0,
            "narration_target_loudness": "keep_original", "segments": [],
            "current_version": 1, "created_at": now_iso(), "updated_at": now_iso(),
            "rendering": {
                "hardware_acceleration": "nvidia", "crf": 23,
                "caption_y_percent": self.caption_y_percent.get(),
                "caption_font_size": self.caption_font_size.get(),
                "target_duration_ratio": target_percent / 100.0,
            },
        }
        self.project = create_project(Path(path), payload)
        self.project_path.set(str(Path(path).resolve()))
        self.refresh()

    def open_project(self) -> None:
        path = filedialog.askopenfilename(title="载入解说项目", filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            self.project = load_project(Path(path))
            self.project_path.set(str(Path(path).resolve()))
            self.refresh()
        except Exception as exc:
            messagebox.showerror("项目载入失败", str(exc))

    def refresh(self) -> None:
        if not self.project:
            return
        self.project_name.set(f"{self.project.project_name} · v{self.project.current_version:04d}")
        self.target_language.set(self.project.target_language)
        selected_preset = self.project.narration_preset if self.project.narration_preset in PRESETS and self.project.narration_preset != "legacy" else "standard"
        self.narration_preset.set(selected_preset)
        self.preset_display.set(next(
            (label for label, value in self.preset_labels.items() if value == selected_preset),
            "标准解说（约50%成片）",
        ))
        self.tts_engine.set(self.project.tts_engine)
        self._refresh_engine_options(self.project.tts_engine)
        self.voice_id.set(self.project.voice_id)
        self._refresh_voice_options(self.project.voice_id)
        for label, voice_id in self.voice_labels.items():
            if voice_id == self.project.voice_id:
                self.voice_display.set(label)
        loudness = str(self.project.narration_target_loudness or "keep_original")
        self.loudness_display.set(next(
            (label for label, value in self.loudness_labels.items() if value == loudness),
            next(iter(self.loudness_labels)),
        ))
        self.caption_y_percent.set(float(self.project.rendering.get("caption_y_percent", 12.0)))
        self.caption_font_size.set(int(self.project.rendering.get("caption_font_size", 38)))
        self.redraw_caption_preview()
        self.tree.delete(*self.tree.get_children())
        for item in self.project.segments:
            self.tree.insert("", "end", iid=item.segment_id, text=item.segment_id, values=(item.episode, f"{item.source_start:.2f}–{item.source_end:.2f}", item.mode, item.purpose, item.revision))
        self.status.set(f"已载入 {len(self.project.segments)} 个片段")

    def _set_preset(self) -> None:
        preset = self.preset_labels.get(self.preset_display.get(), "standard")
        self.narration_preset.set(preset)
        if self.project and preset != self.project.narration_preset:
            self.project.narration_preset = preset
            save_new_version(Path(self.project_path.get()), self.project)
            self.refresh()

    def _set_voice(self) -> None:
        voice_id = self.voice_labels.get(self.voice_display.get())
        if voice_id:
            self.voice_id.set(voice_id)
        if not self.project:
            return
        if voice_id and voice_id != self.project.voice_id:
            self.project.voice_id = voice_id
            save_new_version(Path(self.project_path.get()), self.project)
            self.refresh()

    def _refresh_engine_options(self, preferred_engine: str = "") -> None:
        options = engines_for_language(self.target_language.get())
        self.engine_labels = {item["label"]: item["value"] for item in options}
        selected = preferred_engine or self.tts_engine.get() or "auto"
        if selected not in self.engine_labels.values():
            selected = "auto"
        label = next(name for name, value in self.engine_labels.items() if value == selected)
        self.tts_engine.set(selected)
        self.engine_display.set(label)
        if hasattr(self, "engine_combo"):
            self.engine_combo.configure(values=tuple(self.engine_labels))

    def _refresh_voice_options(self, preferred_voice_id: str = "") -> None:
        self.library = self.library.reload()
        profiles = self.library.compatible(self.target_language.get(), self.tts_engine.get())
        self.voice_labels = {
            f"{item.display_name} ({item.voice_id})": item.voice_id
            for item in profiles
        }
        self.voice_combo.configure(values=tuple(self.voice_labels))
        selected = preferred_voice_id or self.voice_id.get()
        label = next((name for name, voice_id in self.voice_labels.items() if voice_id == selected), "")
        if not label and self.voice_labels:
            label = next(iter(self.voice_labels))
            selected = self.voice_labels[label]
        self.voice_display.set(label)
        if selected:
            self.voice_id.set(selected)
        if hasattr(self, "voice_count_label"):
            self.voice_count_label.configure(text=f"已载入 {len(profiles)} 个声纹")

    def _set_language(self) -> None:
        self._refresh_engine_options()
        self._refresh_voice_options()
        self.caption_preview_text.set({
            "Arabic": "قرار واحد غيّر كل شيء.",
            "Chinese": "一个决定改变了一切。",
        }.get(self.target_language.get(), "One decision changed everything."))
        if self.project:
            self.project.target_language = self.target_language.get()
            self.project.tts_engine = self.tts_engine.get()
            self.project.voice_id = self.voice_id.get()
            save_new_version(Path(self.project_path.get()), self.project)
            self.refresh()

    def _set_engine(self) -> None:
        self.tts_engine.set(self.engine_labels.get(self.engine_display.get(), "auto"))
        self._refresh_voice_options()
        if self.project:
            self.project.tts_engine = self.tts_engine.get()
            self.project.voice_id = self.voice_id.get()
            save_new_version(Path(self.project_path.get()), self.project)
            self.refresh()

    def _set_loudness(self) -> None:
        if not self.project:
            return
        self.project.narration_target_loudness = self.loudness_labels.get(
            self.loudness_display.get(), "keep_original"
        )
        save_new_version(Path(self.project_path.get()), self.project)
        self.refresh()

    def preview_voice(self) -> None:
        voice_id = self.voice_labels.get(self.voice_display.get(), self.voice_id.get())
        def work():
            result = generate_voice_preview(self.library, voice_id)
            path = Path(result["preview_audio"])
            self.after(0, lambda: os.startfile(path) if os.name == "nt" else messagebox.showinfo("试听文件", str(path)))  # type: ignore[attr-defined]
            return result
        self._background("生成/打开声纹试听", work)

    def _build_caption_preview(self, parent: ttk.LabelFrame) -> None:
        toolbar = ttk.Frame(parent)
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ttk.Button(toolbar, text="随机换一帧", command=self.load_random_caption_frame).pack(side="left")
        ttk.Button(toolbar, text="选择其他视频", command=self.choose_caption_preview_video).pack(side="left", padx=5)
        ttk.Label(toolbar, textvariable=self.caption_preview_status, foreground="#666").pack(side="left", padx=8)
        self.caption_preview_canvas = tk.Canvas(
            parent, width=420, height=240, background="#111",
            highlightthickness=1, highlightbackground="#999",
        )
        self.caption_preview_canvas.grid(row=1, column=0, sticky="nw")
        self.caption_preview_canvas.bind("<ButtonPress-1>", self._caption_drag_start)
        self.caption_preview_canvas.bind("<B1-Motion>", self._caption_drag_move)
        self.caption_preview_canvas.bind("<ButtonRelease-1>", self._caption_drag_end)
        controls = ttk.Frame(parent)
        controls.grid(row=1, column=1, sticky="new", padx=(12, 0))
        ttk.Label(controls, text="预览字幕").pack(anchor="w")
        ttk.Entry(controls, textvariable=self.caption_preview_text, width=42).pack(fill="x", pady=(2, 8))
        ttk.Label(controls, text="字幕纵向位置 (%)").pack(anchor="w")
        tk.Scale(
            controls, variable=self.caption_y_percent, from_=0, to=92, resolution=0.5,
            orient="horizontal", command=lambda _value: self.redraw_caption_preview(),
        ).pack(fill="x")
        ttk.Label(controls, text="字幕字号").pack(anchor="w", pady=(8, 0))
        tk.Scale(
            controls, variable=self.caption_font_size, from_=16, to=90, resolution=1,
            orient="horizontal", command=lambda _value: self.redraw_caption_preview(),
        ).pack(fill="x")
        ttk.Button(controls, text="保存字幕位置与字号", command=self.save_caption_settings).pack(anchor="w", pady=(10, 0))
        ttk.Label(
            controls, text="拖动画面中的字幕也可调整纵向位置。", foreground="#666",
        ).pack(anchor="w", pady=(6, 0))
        self.caption_preview_text.trace_add("write", lambda *_args: self.redraw_caption_preview())

    def toggle_caption_preview(self) -> None:
        if self.caption_preview_expanded.get():
            self.caption_preview_frame.pack_forget()
            self.caption_preview_expanded.set(False)
            return
        self.caption_preview_frame.pack(fill="x", pady=(0, 8), before=self.body)
        self.caption_preview_expanded.set(True)
        if not self.caption_preview_photo:
            self.load_random_caption_frame()

    def _caption_preview_video(self) -> Path | None:
        if not self.project:
            return None
        if self.project.segments:
            try:
                candidate = self.project.episode_path(self.project.segments[0].episode)
                if candidate.is_file():
                    return candidate
            except (OSError, ValueError, KeyError):
                pass
        root = self.project.source_path()
        return next(
            (path for path in sorted(root.iterdir()) if path.is_file() and path.suffix.casefold() in video_dedup.VIDEO_SUFFIXES),
            None,
        )

    def choose_caption_preview_video(self) -> None:
        path = filedialog.askopenfilename(
            title="选择解说字幕预览视频",
            filetypes=[("视频文件", "*.mp4 *.mov *.mkv *.avi *.webm *.m4v"), ("所有文件", "*.*")],
        )
        if path:
            self.load_random_caption_frame(Path(path))

    def load_random_caption_frame(self, selected_video: Path | None = None) -> None:
        video = selected_video or self._caption_preview_video()
        if not video or not video.is_file():
            messagebox.showwarning("缺少视频", "请先载入解说项目，或手动选择预览视频。")
            return
        try:
            ffmpeg = video_dedup.find_binary("ffmpeg")
            ffprobe = video_dedup.find_binary("ffprobe")
            info = video_dedup.probe_video(video, ffprobe)
            duration = max(0.0, float(info.get("duration") or 0))
            second = random.uniform(0, max(0.1, duration - 0.1)) if duration > 0.2 else 0.0
            if self.caption_preview_temp:
                self.caption_preview_temp.unlink(missing_ok=True)
            fd, name = tempfile.mkstemp(prefix="recap-caption-preview-", suffix=".png")
            os.close(fd)
            frame = Path(name)
            subprocess.run([
                ffmpeg, "-hide_banner", "-y", "-ss", f"{second:.3f}", "-i", str(video),
                "-frames:v", "1", "-vf", "scale=420:-2", str(frame),
            ], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace", **video_dedup.hidden_subprocess_kwargs())
            photo = tk.PhotoImage(file=str(frame))
            self.caption_preview_temp = frame
            self.caption_preview_photo = photo
            self.caption_preview_image_size = (photo.width(), photo.height())
            if self.caption_preview_canvas:
                self.caption_preview_canvas.configure(width=photo.width(), height=photo.height())
            self.caption_preview_status.set(f"{video.name} · {second:.1f}s / {duration:.1f}s")
            self.redraw_caption_preview()
        except (OSError, ValueError, subprocess.CalledProcessError, tk.TclError) as exc:
            messagebox.showerror("抽帧失败", str(exc))

    def _preview_wrapped_lines(self, text: str, max_chars: int) -> list[str]:
        words = " ".join(text.replace("\n", " ").split()).split()
        lines: list[str] = []
        current = ""
        for word in words:
            candidate = word if not current else f"{current} {word}"
            if current and len(candidate) > max_chars:
                lines.append(current)
                current = word
                if len(lines) >= 2:
                    break
            else:
                current = candidate
        if current and len(lines) < 3:
            lines.append(current)
        return lines[:3]

    def redraw_caption_preview(self) -> None:
        canvas = self.caption_preview_canvas
        if not canvas:
            return
        canvas.delete("all")
        if not self.caption_preview_photo:
            canvas.create_text(210, 120, text="载入项目后随机抽帧", fill="#ddd")
            return
        canvas.create_image(0, 0, image=self.caption_preview_photo, anchor="nw")
        width, height = self.caption_preview_image_size
        font_size = max(8, int(self.caption_font_size.get() * width / 1080))
        max_chars = max(12, int(width * 0.88 / max(8, font_size) / 0.62))
        lines = self._preview_wrapped_lines(self.caption_preview_text.get(), max_chars)
        start_y = max(font_size, min(height - font_size, height * self.caption_y_percent.get() / 100))
        for index, line in enumerate(lines):
            y = start_y + index * (font_size + 5)
            for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
                canvas.create_text(width / 2 + dx, y + dy, text=line, fill="#000", font=("Arial", font_size, "bold"), anchor="n")
            canvas.create_text(width / 2, y, text=line, fill="#fff", font=("Arial", font_size, "bold"), anchor="n")

    def _set_caption_y_from_event(self, event) -> None:
        _width, height = self.caption_preview_image_size
        if height <= 0:
            return
        self.caption_y_percent.set(round(max(0.0, min(92.0, event.y / height * 100)), 1))
        self.redraw_caption_preview()

    def _caption_drag_start(self, event) -> None:
        self.caption_preview_dragging = True
        self._set_caption_y_from_event(event)

    def _caption_drag_move(self, event) -> None:
        if self.caption_preview_dragging:
            self._set_caption_y_from_event(event)

    def _caption_drag_end(self, _event) -> None:
        self.caption_preview_dragging = False

    def save_caption_settings(self) -> None:
        try:
            path, project = self._require()
            project.rendering["caption_y_percent"] = round(float(self.caption_y_percent.get()), 2)
            project.rendering["caption_font_size"] = int(self.caption_font_size.get())
            self.project = save_new_version(path, project)
            self.refresh()
            self.status.set("字幕位置与字号已保存")
        except Exception as exc:
            messagebox.showerror("保存失败", str(exc))

    def select_segment(self, _event=None) -> None:
        if not self.project or not self.tree.selection():
            return
        selected = self.tree.selection()[0]
        item = next(segment for segment in self.project.segments if segment.segment_id == selected)
        self.segment_id.set(item.segment_id); self.episode.set(item.episode)
        self.source_start.set(item.source_start); self.source_end.set(item.source_end)
        self.mode.set(item.mode); self.purpose.set(item.purpose)
        self.narration.delete("1.0", "end"); self.narration.insert("1.0", item.narration_text)
        if item.narration_text:
            self.caption_preview_text.set(item.narration_text)

    def add_segment(self) -> None:
        try:
            path, project = self._require()
            existing = {item.segment_id for item in project.segments}
            number = 1
            while f"seg-{number:03d}" in existing: number += 1
            project.segments.append(RecapSegment(f"seg-{number:03d}", 1, 0.0, 10.0, "narration", "", ""))
            save_new_version(path, project); self.refresh()
            self.tree.selection_set(project.segments[-1].segment_id); self.select_segment()
        except Exception as exc: messagebox.showerror("新增失败", str(exc))

    def save_segment(self) -> None:
        try:
            path, project = self._require()
            segment_id = self.segment_id.get().strip()
            project, _affected = update_segment(path, segment_id, {
                "episode": self.episode.get(), "source_start": self.source_start.get(), "source_end": self.source_end.get(),
                "mode": self.mode.get(), "purpose": self.purpose.get(), "narration_text": self.narration.get("1.0", "end").strip(),
            })
            self.project = project; self.refresh(); self.tree.selection_set(segment_id)
        except Exception as exc: messagebox.showerror("保存失败", str(exc))

    def remove_segment(self) -> None:
        try:
            path, _project = self._require(); segment_id = self.segment_id.get().strip()
            if not messagebox.askyesno("删除片段", f"删除 {segment_id} 并建立新版本？"): return
            self.project, _affected = delete_segment(path, segment_id); self.refresh()
        except Exception as exc: messagebox.showerror("删除失败", str(exc))

    def validate_project(self) -> None:
        try:
            _path, project = self._require(); ffprobe = video_dedup.find_binary("ffprobe")
            errors = validate_source_intervals(project, lambda video: video_dedup.probe_video(video, ffprobe))
            result = {"status": "ok" if not errors else "validation_failed", "validation_errors": errors}
            self._append(result)
        except Exception as exc: messagebox.showerror("校验失败", str(exc))

    def measure_loudness(self) -> None:
        self._background("测量响度", lambda: measure_project_loudness(self._require()[1], video_dedup.find_binary("ffmpeg"), video_dedup.find_binary("ffprobe")))

    def render_selected(self) -> None:
        segment_id = self.segment_id.get().strip()
        if not segment_id: messagebox.showwarning("没有片段", "请先选择片段"); return
        self._background("局部渲染", lambda: render_project(self._require()[1], only_segment_id=segment_id))

    def start_render(self, final: bool) -> None:
        self._background("最终渲染" if final else "预览渲染", lambda: render_project(self._require()[1], final=final))

    def _background(self, label: str, function) -> None:
        self.status.set(f"{label}中…")
        def worker():
            try: result = function()
            except Exception as exc: result = {"status": "error", "error_type": type(exc).__name__, "message": str(exc)}
            self.after(0, lambda: (self._append(result), self.status.set(f"{label}完成：{result.get('status')}")))
        threading.Thread(target=worker, daemon=True).start()


def re_safe_id(value: str) -> str:
    import re
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip()).strip("-").casefold()
    return cleaned or "recap-project"
