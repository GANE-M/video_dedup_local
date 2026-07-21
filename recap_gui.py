"""Tkinter workspace for structured recap projects; no Agent or chat runtime."""
from __future__ import annotations

import json
import os
import shutil
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import video_dedup
from recap.models import RecapProject, RecapSegment, now_iso
from recap.project_store import create_project, delete_segment, load_project, save_new_version, update_segment
from recap.renderer import generate_voice_preview, measure_project_loudness, render_project
from recap.timeline import validate_source_intervals
from recap.voice_library import VoiceLibrary


class RecapWorkspace(ttk.Frame):
    def __init__(self, parent) -> None:
        super().__init__(parent, padding=10)
        self.pack(fill="both", expand=True)
        self.project_path = tk.StringVar()
        self.project_name = tk.StringVar(value="未加载项目")
        self.voice_id = tk.StringVar(value="calm_female")
        self.status = tk.StringVar(value="就绪")
        self.segment_id = tk.StringVar()
        self.episode = tk.IntVar(value=1)
        self.source_start = tk.DoubleVar(value=0.0)
        self.source_end = tk.DoubleVar(value=10.0)
        self.mode = tk.StringVar(value="narration")
        self.purpose = tk.StringVar()
        self.project: RecapProject | None = None
        self.library = VoiceLibrary()
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
        ttk.Label(settings, text="固定声纹").grid(row=0, column=1, sticky="e", padx=(20, 4))
        voices = [(item.voice_id, item.display_name) for item in self.library.list()]
        self.voice_labels = {f"{name} ({voice_id})": voice_id for voice_id, name in voices}
        self.voice_display = tk.StringVar(value=next(iter(self.voice_labels), ""))
        combo = ttk.Combobox(settings, textvariable=self.voice_display, values=tuple(self.voice_labels), state="readonly", width=24)
        combo.grid(row=0, column=2, sticky="w")
        combo.bind("<<ComboboxSelected>>", lambda _event: self._set_voice())
        ttk.Button(settings, text="试听", command=self.preview_voice).grid(row=0, column=3, padx=5)
        ttk.Button(settings, text="校验项目", command=self.validate_project).grid(row=0, column=4, padx=5)
        ttk.Button(settings, text="测量响度", command=self.measure_loudness).grid(row=0, column=5, padx=5)
        ttk.Label(settings, text="本页只编辑结构化时间轴和调用本地渲染，不连接字幕Agent。", foreground="#666").grid(row=1, column=0, columnspan=6, sticky="w", pady=(5, 0))
        settings.columnconfigure(0, weight=1)

        body = ttk.Panedwindow(self, orient="horizontal")
        body.pack(fill="both", expand=True)
        left = ttk.Frame(body)
        right = ttk.Frame(body)
        body.add(left, weight=3)
        body.add(right, weight=2)

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
        payload = {
            "schema_version": 1, "project_id": project_id, "project_name": name,
            "source_root": source, "episode_pattern": pattern, "subtitle_root": "",
            "output_root": output, "target_language": "English", "target_duration_seconds": 450,
            "voice_id": self.voice_id.get(), "narration_speed": 1.0,
            "narration_target_loudness": "match_source_program", "segments": [],
            "current_version": 1, "created_at": now_iso(), "updated_at": now_iso(),
            "rendering": {"hardware_acceleration": "auto", "crf": 21, "caption_y_percent": 12.0},
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
        self.voice_id.set(self.project.voice_id)
        for label, voice_id in self.voice_labels.items():
            if voice_id == self.project.voice_id:
                self.voice_display.set(label)
        self.tree.delete(*self.tree.get_children())
        for item in self.project.segments:
            self.tree.insert("", "end", iid=item.segment_id, text=item.segment_id, values=(item.episode, f"{item.source_start:.2f}–{item.source_end:.2f}", item.mode, item.purpose, item.revision))
        self.status.set(f"已载入 {len(self.project.segments)} 个片段")

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

    def preview_voice(self) -> None:
        voice_id = self.voice_labels.get(self.voice_display.get(), self.voice_id.get())
        def work():
            result = generate_voice_preview(self.library, voice_id)
            path = Path(result["preview_audio"])
            self.after(0, lambda: os.startfile(path) if os.name == "nt" else messagebox.showinfo("试听文件", str(path)))  # type: ignore[attr-defined]
            return result
        self._background("生成/打开声纹试听", work)

    def select_segment(self, _event=None) -> None:
        if not self.project or not self.tree.selection():
            return
        selected = self.tree.selection()[0]
        item = next(segment for segment in self.project.segments if segment.segment_id == selected)
        self.segment_id.set(item.segment_id); self.episode.set(item.episode)
        self.source_start.set(item.source_start); self.source_end.set(item.source_end)
        self.mode.set(item.mode); self.purpose.set(item.purpose)
        self.narration.delete("1.0", "end"); self.narration.insert("1.0", item.narration_text)

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
