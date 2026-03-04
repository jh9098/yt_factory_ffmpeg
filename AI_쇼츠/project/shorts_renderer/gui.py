import copy
import glob
import json
import os
import queue
import subprocess
import sys
import threading
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    from PIL import Image, ImageTk, ImageDraw, ImageFont
except ImportError:
    raise SystemExit("Pillow가 필요합니다. 설치: pip install pillow")

from .constants import (
    DEFAULT_FADE_SEC,
    DEFAULT_FONT_SIZE,
    DEFAULT_FPS,
    DEFAULT_HEIGHT,
    DEFAULT_OVERLAY_FONT_SIZE,
    DEFAULT_PREVIEW_H,
    DEFAULT_PREVIEW_W,
    DEFAULT_WIDTH,
)
from .renderer import parse_hex_color, position_to_xy, render_timeline_to_video
from .timeline_builder import build_master_audio_and_timeline
from .utils import ensure_dir, safe_float, safe_int

class TkTextLogger:
    def __init__(self, q: "queue.Queue[Tuple[str, Any]]"):
        self.q = q

    def __call__(self, msg: str) -> None:
        if msg is None:
            return
        self.q.put(("log", str(msg)))

class TimelineEditorGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Master Audio Timeline Shorts Editor")
        self.root.geometry("1580x940")

        self.ui_queue: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self.worker_thread: Optional[threading.Thread] = None
        self.is_running = False

        self.base_var = tk.StringVar(value=r"P:\AI_shorts")
        self.json_var = tk.StringVar(value="")
        self.images_var = tk.StringVar(value="")
        self.tts_var = tk.StringVar(value="")
        self.timeline_var = tk.StringVar(value="")
        self.output_var = tk.StringVar(value="")
        self.width_var = tk.StringVar(value=str(DEFAULT_WIDTH))
        self.height_var = tk.StringVar(value=str(DEFAULT_HEIGHT))
        self.fps_var = tk.StringVar(value=str(DEFAULT_FPS))
        self.font_var = tk.StringVar(value="")
        self.bg_var = tk.StringVar(value="black")

        self.playhead_var = tk.DoubleVar(value=0.0)
        self.duration_var = tk.StringVar(value="0.000")
        self.selected_type_var = tk.StringVar(value="image")
        self.preview_info_var = tk.StringVar(value="프리뷰 준비됨")

        self.timeline_data: Optional[Dict[str, Any]] = None
        self.preview_img_tk = None
        self.undo_stack: List[Dict[str, Any]] = []
        self.redo_stack: List[Dict[str, Any]] = []

        self.image_form_vars = {
            "id": tk.StringVar(),
            "scene_id": tk.StringVar(),
            "path": tk.StringVar(),
            "start_sec": tk.StringVar(),
            "end_sec": tk.StringVar(),
            "motion": tk.StringVar(value="hold"),
            "motion_strength": tk.StringVar(value="0.06"),
            "fade_in_sec": tk.StringVar(value=str(DEFAULT_FADE_SEC)),
            "fade_out_sec": tk.StringVar(value=str(DEFAULT_FADE_SEC)),
            "layer": tk.StringVar(value="1"),
            "x": tk.StringVar(value="0"),
            "y": tk.StringVar(value="0"),
            "scale_mode": tk.StringVar(value="cover"),
        }

        self.subtitle_form_vars = {
            "id": tk.StringVar(),
            "scene_id": tk.StringVar(),
            "kind": tk.StringVar(value="subtitle"),
            "text": tk.StringVar(),
            "start_sec": tk.StringVar(),
            "end_sec": tk.StringVar(),
            "position": tk.StringVar(value="bottom"),
            "x_offset": tk.StringVar(value="0"),
            "y_offset": tk.StringVar(value="0"),
            "font_size": tk.StringVar(value=str(DEFAULT_FONT_SIZE)),
            "font_color": tk.StringVar(value="#FFFFFF"),
            "border_color": tk.StringVar(value="#000000"),
            "border_w": tk.StringVar(value="4"),
            "box": tk.StringVar(value="1"),
            "box_color": tk.StringVar(value="#000000"),
            "box_alpha": tk.StringVar(value="0.35"),
            "layer": tk.StringVar(value="2"),
        }

        self._build_ui()
        self._refresh_defaults_from_base()
        self.root.after(100, self._drain_ui_queue)

    # -------------------------------------------------
    # UI
    # -------------------------------------------------

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")

        ttk.Label(top, text="Project Base").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.base_var, width=88).grid(row=0, column=1, sticky="ew", padx=4, pady=2)
        ttk.Button(top, text="폴더", command=self._pick_base).grid(row=0, column=2, padx=2)

        ttk.Label(top, text="JSON").grid(row=1, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.json_var, width=88).grid(row=1, column=1, sticky="ew", padx=4, pady=2)
        ttk.Button(top, text="파일", command=self._pick_json).grid(row=1, column=2, padx=2)

        ttk.Label(top, text="이미지 폴더").grid(row=2, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.images_var, width=88).grid(row=2, column=1, sticky="ew", padx=4, pady=2)
        ttk.Button(top, text="폴더", command=self._pick_images).grid(row=2, column=2, padx=2)

        ttk.Label(top, text="TTS 폴더").grid(row=3, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.tts_var, width=88).grid(row=3, column=1, sticky="ew", padx=4, pady=2)
        ttk.Button(top, text="폴더", command=self._pick_tts).grid(row=3, column=2, padx=2)

        ttk.Label(top, text="Timeline JSON").grid(row=4, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.timeline_var, width=88).grid(row=4, column=1, sticky="ew", padx=4, pady=2)
        ttk.Button(top, text="파일", command=self._pick_timeline).grid(row=4, column=2, padx=2)

        ttk.Label(top, text="출력 mp4").grid(row=5, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.output_var, width=88).grid(row=5, column=1, sticky="ew", padx=4, pady=2)
        ttk.Button(top, text="저장", command=self._pick_output).grid(row=5, column=2, padx=2)

        opt = ttk.Frame(top)
        opt.grid(row=6, column=1, sticky="w", pady=6)
        ttk.Label(opt, text="W").pack(side="left")
        ttk.Entry(opt, textvariable=self.width_var, width=7).pack(side="left", padx=(4, 12))
        ttk.Label(opt, text="H").pack(side="left")
        ttk.Entry(opt, textvariable=self.height_var, width=7).pack(side="left", padx=(4, 12))
        ttk.Label(opt, text="FPS").pack(side="left")
        ttk.Entry(opt, textvariable=self.fps_var, width=6).pack(side="left", padx=(4, 12))
        ttk.Label(opt, text="BG").pack(side="left")
        ttk.Entry(opt, textvariable=self.bg_var, width=10).pack(side="left", padx=(4, 12))
        ttk.Button(opt, text="기본경로", command=self._refresh_defaults_from_base).pack(side="left", padx=4)

        btn = ttk.Frame(top)
        btn.grid(row=7, column=1, sticky="w", pady=(4, 6))
        self.btn_build = ttk.Button(btn, text="1) 타임라인 생성", command=self._start_build_timeline)
        self.btn_build.pack(side="left")
        self.btn_load = ttk.Button(btn, text="2) 타임라인 불러오기", command=self._load_timeline_from_file)
        self.btn_load.pack(side="left", padx=4)
        self.btn_save = ttk.Button(btn, text="저장", command=self._save_timeline)
        self.btn_save.pack(side="left", padx=4)
        self.btn_render = ttk.Button(btn, text="3) 최종 렌더", command=self._start_render_timeline)
        self.btn_render.pack(side="left", padx=4)
        self.btn_undo = ttk.Button(btn, text="Undo", command=self._undo)
        self.btn_undo.pack(side="left", padx=4)
        self.btn_redo = ttk.Button(btn, text="Redo", command=self._redo)
        self.btn_redo.pack(side="left", padx=4)
        ttk.Button(btn, text="결과 폴더", command=self._open_output_folder).pack(side="left", padx=4)

        top.columnconfigure(1, weight=1)

        body = ttk.Panedwindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=8, pady=4)

        left = ttk.Frame(body, padding=4)
        center = ttk.Frame(body, padding=4)
        right = ttk.Frame(body, padding=4)

        body.add(left, weight=4)
        body.add(center, weight=3)
        body.add(right, weight=4)

        # 좌측 - 리스트
        left_top = ttk.LabelFrame(left, text="이미지 타임라인", padding=4)
        left_top.pack(fill="both", expand=True)

        self.image_tree = ttk.Treeview(
            left_top,
            columns=("id", "scene", "start", "end", "motion", "path"),
            show="headings",
            height=12
        )
        for c, w in [("id", 90), ("scene", 60), ("start", 70), ("end", 70), ("motion", 90), ("path", 260)]:
            self.image_tree.heading(c, text=c)
            self.image_tree.column(c, width=w, anchor="w")
        self.image_tree.pack(fill="both", expand=True)
        self.image_tree.bind("<<TreeviewSelect>>", self._on_image_select)

        left_mid = ttk.Frame(left)
        left_mid.pack(fill="x", pady=4)
        ttk.Button(left_mid, text="이미지 추가", command=self._add_image_item).pack(side="left")
        ttk.Button(left_mid, text="이미지 삭제", command=self._delete_selected_image_item).pack(side="left", padx=4)

        left_bottom = ttk.LabelFrame(left, text="자막 타임라인", padding=4)
        left_bottom.pack(fill="both", expand=True)

        self.subtitle_tree = ttk.Treeview(
            left_bottom,
            columns=("id", "kind", "start", "end", "position", "text"),
            show="headings",
            height=12
        )
        for c, w in [("id", 100), ("kind", 70), ("start", 70), ("end", 70), ("position", 90), ("text", 260)]:
            self.subtitle_tree.heading(c, text=c)
            self.subtitle_tree.column(c, width=w, anchor="w")
        self.subtitle_tree.pack(fill="both", expand=True)
        self.subtitle_tree.bind("<<TreeviewSelect>>", self._on_subtitle_select)

        left_btn2 = ttk.Frame(left)
        left_btn2.pack(fill="x", pady=4)
        ttk.Button(left_btn2, text="자막 추가", command=self._add_subtitle_item).pack(side="left")
        ttk.Button(left_btn2, text="자막 삭제", command=self._delete_selected_subtitle_item).pack(side="left", padx=4)

        # 중앙 - 프리뷰
        preview_frame = ttk.LabelFrame(center, text="프리뷰", padding=6)
        preview_frame.pack(fill="both", expand=True)

        self.preview_canvas = tk.Canvas(
            preview_frame,
            width=DEFAULT_PREVIEW_W,
            height=DEFAULT_PREVIEW_H,
            bg="black",
            highlightthickness=1,
            highlightbackground="#888"
        )
        self.preview_canvas.pack(pady=4)

        ttk.Label(preview_frame, textvariable=self.preview_info_var).pack(anchor="w", pady=(4, 4))

        seek = ttk.Frame(preview_frame)
        seek.pack(fill="x", pady=6)
        ttk.Label(seek, text="현재시간").pack(side="left")
        self.seek_scale = tk.Scale(
            seek,
            orient="horizontal",
            resolution=0.01,
            from_=0,
            to=100,
            variable=self.playhead_var,
            command=self._on_seek_changed,
            length=420
        )
        self.seek_scale.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Label(seek, textvariable=self.duration_var).pack(side="left")

        ttk.Button(preview_frame, text="현재시간 프리뷰 갱신", command=self._refresh_preview).pack(anchor="w")

        # 우측 - 상세 편집
        form_notebook = ttk.Notebook(right)
        form_notebook.pack(fill="both", expand=True)

        image_tab = ttk.Frame(form_notebook, padding=6)
        subtitle_tab = ttk.Frame(form_notebook, padding=6)
        meta_tab = ttk.Frame(form_notebook, padding=6)

        form_notebook.add(image_tab, text="이미지 편집")
        form_notebook.add(subtitle_tab, text="자막 편집")
        form_notebook.add(meta_tab, text="메타")

        self._build_image_form(image_tab)
        self._build_subtitle_form(subtitle_tab)
        self._build_meta_form(meta_tab)

        log_frame = ttk.LabelFrame(self.root, text="로그", padding=6)
        log_frame.pack(fill="both", expand=False, padx=8, pady=(0, 8))

        self.log_text = tk.Text(log_frame, wrap="word", height=10)
        self.log_text.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        sb.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=sb.set)

        self._log("GUI 준비 완료")

    def _build_image_form(self, parent):
        r = 0
        for label, key in [
            ("id", "id"),
            ("scene_id", "scene_id"),
            ("path", "path"),
            ("start_sec", "start_sec"),
            ("end_sec", "end_sec"),
            ("motion", "motion"),
            ("motion_strength", "motion_strength"),
            ("fade_in_sec", "fade_in_sec"),
            ("fade_out_sec", "fade_out_sec"),
            ("layer", "layer"),
            ("x", "x"),
            ("y", "y"),
            ("scale_mode", "scale_mode"),
        ]:
            ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", pady=2)
            if key == "motion":
                cb = ttk.Combobox(parent, textvariable=self.image_form_vars[key], state="readonly",
                                  values=["hold", "zoom-in", "zoom-out", "pan-left", "pan-right", "pan-up", "pan-down"])
                cb.grid(row=r, column=1, sticky="ew", pady=2)
            elif key == "scale_mode":
                cb = ttk.Combobox(parent, textvariable=self.image_form_vars[key], state="readonly",
                                  values=["cover", "contain"])
                cb.grid(row=r, column=1, sticky="ew", pady=2)
            else:
                ttk.Entry(parent, textvariable=self.image_form_vars[key], width=42).grid(row=r, column=1, sticky="ew", pady=2)
                if key == "path":
                    ttk.Button(parent, text="...", command=self._pick_image_item_path).grid(row=r, column=2, padx=4)
            r += 1

        ttk.Button(parent, text="이미지 항목 반영", command=self._apply_image_form).grid(row=r, column=1, sticky="w", pady=8)
        parent.columnconfigure(1, weight=1)

    def _build_subtitle_form(self, parent):
        r = 0
        for label, key in [
            ("id", "id"),
            ("scene_id", "scene_id"),
            ("kind", "kind"),
            ("text", "text"),
            ("start_sec", "start_sec"),
            ("end_sec", "end_sec"),
            ("position", "position"),
            ("x_offset", "x_offset"),
            ("y_offset", "y_offset"),
            ("font_size", "font_size"),
            ("font_color", "font_color"),
            ("border_color", "border_color"),
            ("border_w", "border_w"),
            ("box", "box"),
            ("box_color", "box_color"),
            ("box_alpha", "box_alpha"),
            ("layer", "layer"),
        ]:
            ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", pady=2)
            if key == "kind":
                cb = ttk.Combobox(parent, textvariable=self.subtitle_form_vars[key], state="readonly",
                                  values=["subtitle", "overlay"])
                cb.grid(row=r, column=1, sticky="ew", pady=2)
            elif key == "position":
                cb = ttk.Combobox(parent, textvariable=self.subtitle_form_vars[key], state="readonly",
                                  values=["top", "bottom", "center", "left-top", "right-top"])
                cb.grid(row=r, column=1, sticky="ew", pady=2)
            elif key == "text":
                txt = tk.Text(parent, height=5, width=40)
                txt.grid(row=r, column=1, sticky="ew", pady=2)
                self.subtitle_text_widget = txt
            else:
                ttk.Entry(parent, textvariable=self.subtitle_form_vars[key], width=42).grid(row=r, column=1, sticky="ew", pady=2)
            r += 1

        ttk.Button(parent, text="자막 항목 반영", command=self._apply_subtitle_form).grid(row=r, column=1, sticky="w", pady=8)
        parent.columnconfigure(1, weight=1)

    def _build_meta_form(self, parent):
        ttk.Label(parent, text="폰트 경로").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(parent, textvariable=self.font_var, width=60).grid(row=0, column=1, sticky="ew", pady=2)
        ttk.Button(parent, text="...", command=self._pick_font).grid(row=0, column=2, padx=4)

        ttk.Label(parent, text="배경색").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(parent, textvariable=self.bg_var, width=30).grid(row=1, column=1, sticky="w", pady=2)

        ttk.Label(parent, text="해상도 W/H").grid(row=2, column=0, sticky="w", pady=2)
        whf = ttk.Frame(parent)
        whf.grid(row=2, column=1, sticky="w")
        ttk.Entry(whf, textvariable=self.width_var, width=10).pack(side="left")
        ttk.Entry(whf, textvariable=self.height_var, width=10).pack(side="left", padx=6)

        ttk.Label(parent, text="FPS").grid(row=3, column=0, sticky="w", pady=2)
        ttk.Entry(parent, textvariable=self.fps_var, width=10).grid(row=3, column=1, sticky="w", pady=2)

        ttk.Button(parent, text="메타 반영", command=self._apply_meta_form).grid(row=4, column=1, sticky="w", pady=10)
        parent.columnconfigure(1, weight=1)

    # -------------------------------------------------
    # 로그 / 큐
    # -------------------------------------------------

    def _log(self, msg: str):
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.root.update_idletasks()

    def _drain_ui_queue(self):
        try:
            while True:
                typ, payload = self.ui_queue.get_nowait()
                if typ == "log":
                    self._log(str(payload))
                elif typ == "done":
                    self._set_running(False)
                    messagebox.showinfo(payload.get("title", "완료"), payload.get("message", "작업 완료"))
                elif typ == "error":
                    self._set_running(False)
                    messagebox.showerror(payload.get("title", "오류"), payload.get("message", "작업 실패"))
        except queue.Empty:
            pass
        finally:
            self.root.after(100, self._drain_ui_queue)

    def _set_running(self, flag: bool):
        self.is_running = flag
        state = "disabled" if flag else "normal"
        for b in [self.btn_build, self.btn_load, self.btn_save, self.btn_render, self.btn_undo, self.btn_redo]:
            b.configure(state=state)

    # -------------------------------------------------
    # 기본 경로
    # -------------------------------------------------

    def _refresh_defaults_from_base(self):
        base = Path(self.base_var.get().strip() or ".")
        default_json = base / "data" / "shorts.json"
        default_images = base / "assets" / "images" / "shorts"
        default_tts = base / "assets" / "audio" / "tts" / "shorts"
        default_font = base / "assets" / "fonts" / "KoddiUDOnGothic-ExtraBold.ttf"
        default_timeline = base / "data" / "timeline.json"
        default_output = base / "output" / "timeline_final.mp4"

        if default_json.exists():
            self.json_var.set(str(default_json))
        if default_images.exists():
            self.images_var.set(str(default_images))
        if default_tts.exists():
            self.tts_var.set(str(default_tts))
        if default_font.exists():
            self.font_var.set(str(default_font))
        self.timeline_var.set(str(default_timeline))
        self.output_var.set(str(default_output))

        self._log(f"[INFO] 기본경로 갱신: {base}")

    # -------------------------------------------------
    # 파일 선택
    # -------------------------------------------------

    def _pick_base(self):
        p = filedialog.askdirectory(title="프로젝트 베이스 선택", initialdir=self.base_var.get() or os.getcwd())
        if p:
            self.base_var.set(p)
            self._refresh_defaults_from_base()

    def _pick_json(self):
        base = Path(self.base_var.get().strip() or ".")
        p = filedialog.askopenfilename(title="JSON 선택", initialdir=str(base), filetypes=[("JSON", "*.json"), ("All", "*.*")])
        if p:
            self.json_var.set(p)

    def _pick_images(self):
        base = Path(self.base_var.get().strip() or ".")
        p = filedialog.askdirectory(title="이미지 폴더 선택", initialdir=str(base))
        if p:
            self.images_var.set(p)

    def _pick_tts(self):
        base = Path(self.base_var.get().strip() or ".")
        p = filedialog.askdirectory(title="TTS 폴더 선택", initialdir=str(base))
        if p:
            self.tts_var.set(p)

    def _pick_timeline(self):
        base = Path(self.base_var.get().strip() or ".")
        p = filedialog.askopenfilename(title="Timeline JSON 선택", initialdir=str(base), filetypes=[("JSON", "*.json"), ("All", "*.*")])
        if p:
            self.timeline_var.set(p)

    def _pick_output(self):
        base = Path(self.base_var.get().strip() or ".")
        p = filedialog.asksaveasfilename(title="출력 mp4 저장", initialdir=str(base), defaultextension=".mp4", filetypes=[("MP4", "*.mp4")])
        if p:
            self.output_var.set(p)

    def _pick_font(self):
        base = Path(self.base_var.get().strip() or ".")
        p = filedialog.askopenfilename(title="폰트 선택", initialdir=str(base), filetypes=[("Font", "*.ttf *.otf"), ("All", "*.*")])
        if p:
            self.font_var.set(p)

    def _pick_image_item_path(self):
        base = Path(self.base_var.get().strip() or ".")
        p = filedialog.askopenfilename(title="이미지 파일 선택", initialdir=str(base), filetypes=[("Images", "*.png *.jpg *.jpeg *.webp"), ("All", "*.*")])
        if p:
            self.image_form_vars["path"].set(p)

    # -------------------------------------------------
    # 타임라인 관리
    # -------------------------------------------------

    def _validate_build_inputs(self) -> Tuple[bool, Optional[Path], Optional[Path], Optional[Path], Optional[Path], Optional[Path]]:
        try:
            base = Path(self.base_var.get().strip())
            json_path = Path(self.json_var.get().strip())
            images = Path(self.images_var.get().strip())
            tts = Path(self.tts_var.get().strip())
            timeline = Path(self.timeline_var.get().strip())
        except Exception:
            messagebox.showerror("오류", "경로 확인 필요")
            return False, None, None, None, None, None

        if not base.exists():
            messagebox.showerror("오류", f"Project Base 없음\n{base}")
            return False, None, None, None, None, None
        if not json_path.exists():
            messagebox.showerror("오류", f"JSON 없음\n{json_path}")
            return False, None, None, None, None, None
        if not images.exists():
            messagebox.showerror("오류", f"이미지 폴더 없음\n{images}")
            return False, None, None, None, None, None
        if not tts.exists():
            messagebox.showerror("오류", f"TTS 폴더 없음\n{tts}")
            return False, None, None, None, None, None

        return True, base, json_path, images, tts, timeline

    def _start_build_timeline(self):
        if self.is_running:
            return
        ok, base, json_path, images, tts, timeline = self._validate_build_inputs()
        if not ok:
            return

        self._set_running(True)
        logger = TkTextLogger(self.ui_queue)

        def worker():
            try:
                tl = build_master_audio_and_timeline(
                    project_dir=base,  # type: ignore[arg-type]
                    json_path=json_path,  # type: ignore[arg-type]
                    images_dir=images,  # type: ignore[arg-type]
                    tts_dir=tts,  # type: ignore[arg-type]
                    out_timeline_path=timeline,  # type: ignore[arg-type]
                    logger=logger
                )
                self.ui_queue.put(("log", f"[OK] 타임라인 생성: {tl}"))
                self.ui_queue.put(("done", {"title": "완료", "message": f"타임라인 생성 완료\n{tl}"}))
            except Exception as e:
                logger(str(e))
                logger(traceback.format_exc())
                self.ui_queue.put(("error", {"title": "타임라인 생성 오류", "message": str(e)}))

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()

    def _load_timeline_from_file(self):
        timeline_path = Path(self.timeline_var.get().strip())
        if not timeline_path.exists():
            messagebox.showerror("오류", f"timeline.json 없음\n{timeline_path}")
            return

        try:
            data = json.loads(timeline_path.read_text(encoding="utf-8"))
            self.timeline_data = data
            meta = data.get("meta", {})
            self.width_var.set(str(meta.get("width", DEFAULT_WIDTH)))
            self.height_var.set(str(meta.get("height", DEFAULT_HEIGHT)))
            self.fps_var.set(str(meta.get("fps", DEFAULT_FPS)))
            self.bg_var.set(str(meta.get("bg_color", DEFAULT_BG_COLOR)))
            self.font_var.set(str(meta.get("font_path", "")))

            total = safe_float(meta.get("duration_sec", 0))
            self.seek_scale.configure(to=max(1.0, total))
            self.duration_var.set(f"/ {total:.3f}s")
            self.playhead_var.set(0.0)

            self.undo_stack.clear()
            self.redo_stack.clear()
            self._refresh_trees()
            self._refresh_preview()
            self._log(f"[OK] 타임라인 로드: {timeline_path}")
        except Exception as e:
            messagebox.showerror("오류", str(e))

    def _save_timeline(self):
        if not self.timeline_data:
            messagebox.showerror("오류", "로드된 타임라인이 없습니다.")
            return
        try:
            self._apply_meta_form(silent=True)
            timeline_path = Path(self.timeline_var.get().strip())
            ensure_dir(timeline_path.parent)
            timeline_path.write_text(json.dumps(self.timeline_data, ensure_ascii=False, indent=2), encoding="utf-8")
            self._log(f"[OK] 저장 완료: {timeline_path}")
            messagebox.showinfo("완료", f"저장 완료\n{timeline_path}")
        except Exception as e:
            messagebox.showerror("오류", str(e))

    def _snapshot(self):
        if self.timeline_data is not None:
            self.undo_stack.append(copy.deepcopy(self.timeline_data))
            if len(self.undo_stack) > 50:
                self.undo_stack.pop(0)
            self.redo_stack.clear()

    def _undo(self):
        if not self.undo_stack:
            return
        if self.timeline_data is not None:
            self.redo_stack.append(copy.deepcopy(self.timeline_data))
        self.timeline_data = self.undo_stack.pop()
        self._refresh_trees()
        self._refresh_preview()
        self._log("[UNDO] 복원됨")

    def _redo(self):
        if not self.redo_stack:
            return
        if self.timeline_data is not None:
            self.undo_stack.append(copy.deepcopy(self.timeline_data))
        self.timeline_data = self.redo_stack.pop()
        self._refresh_trees()
        self._refresh_preview()
        self._log("[REDO] 재적용됨")

    # -------------------------------------------------
    # Tree 갱신
    # -------------------------------------------------

    def _refresh_trees(self):
        for x in self.image_tree.get_children():
            self.image_tree.delete(x)
        for x in self.subtitle_tree.get_children():
            self.subtitle_tree.delete(x)

        if not self.timeline_data:
            return

        for item in self.timeline_data.get("image_items", []):
            self.image_tree.insert("", "end", iid=item["id"], values=(
                item.get("id", ""),
                item.get("scene_id", ""),
                item.get("start_sec", ""),
                item.get("end_sec", ""),
                item.get("motion", ""),
                item.get("path", ""),
            ))

        for item in self.timeline_data.get("subtitle_items", []):
            text_preview = str(item.get("text", "")).replace("\n", " / ")
            self.subtitle_tree.insert("", "end", iid=item["id"], values=(
                item.get("id", ""),
                item.get("kind", ""),
                item.get("start_sec", ""),
                item.get("end_sec", ""),
                item.get("position", ""),
                text_preview[:80],
            ))

    # -------------------------------------------------
    # 선택 이벤트
    # -------------------------------------------------

    def _on_image_select(self, _evt=None):
        if not self.timeline_data:
            return
        sel = self.image_tree.selection()
        if not sel:
            return
        item_id = sel[0]
        for item in self.timeline_data.get("image_items", []):
            if item.get("id") == item_id:
                for k, var in self.image_form_vars.items():
                    var.set(str(item.get(k, "")))
                self.selected_type_var.set("image")
                try:
                    self.playhead_var.set(safe_float(item.get("start_sec", 0)))
                except Exception:
                    pass
                self._refresh_preview()
                break

    def _on_subtitle_select(self, _evt=None):
        if not self.timeline_data:
            return
        sel = self.subtitle_tree.selection()
        if not sel:
            return
        item_id = sel[0]
        for item in self.timeline_data.get("subtitle_items", []):
            if item.get("id") == item_id:
                for k, var in self.subtitle_form_vars.items():
                    if k != "text":
                        var.set(str(item.get(k, "")))
                self.subtitle_text_widget.delete("1.0", "end")
                self.subtitle_text_widget.insert("1.0", str(item.get("text", "")))
                self.selected_type_var.set("subtitle")
                try:
                    self.playhead_var.set(safe_float(item.get("start_sec", 0)))
                except Exception:
                    pass
                self._refresh_preview()
                break

    # -------------------------------------------------
    # 항목 수정
    # -------------------------------------------------

    def _apply_meta_form(self, silent: bool = False):
        if not self.timeline_data:
            if not silent:
                messagebox.showerror("오류", "타임라인이 없습니다.")
            return

        self._snapshot()
        meta = self.timeline_data["meta"]
        meta["width"] = safe_int(self.width_var.get(), DEFAULT_WIDTH)
        meta["height"] = safe_int(self.height_var.get(), DEFAULT_HEIGHT)
        meta["fps"] = safe_int(self.fps_var.get(), DEFAULT_FPS)
        meta["bg_color"] = self.bg_var.get().strip() or "black"
        meta["font_path"] = self.font_var.get().strip()
        self._refresh_preview()

    def _apply_image_form(self):
        if not self.timeline_data:
            messagebox.showerror("오류", "타임라인이 없습니다.")
            return

        sel = self.image_tree.selection()
        if not sel:
            messagebox.showerror("오류", "이미지 항목을 선택하세요.")
            return
        item_id = sel[0]

        self._snapshot()

        for item in self.timeline_data.get("image_items", []):
            if item.get("id") == item_id:
                for k, var in self.image_form_vars.items():
                    v = var.get()
                    if k in ["start_sec", "end_sec", "motion_strength", "fade_in_sec", "fade_out_sec"]:
                        item[k] = safe_float(v, 0.0)
                    elif k in ["layer", "x", "y"]:
                        item[k] = safe_int(v, 0)
                    else:
                        item[k] = v
                break

        self._refresh_trees()
        self._refresh_preview()
        self._log(f"[OK] 이미지 항목 반영: {item_id}")

    def _apply_subtitle_form(self):
        if not self.timeline_data:
            messagebox.showerror("오류", "타임라인이 없습니다.")
            return

        sel = self.subtitle_tree.selection()
        if not sel:
            messagebox.showerror("오류", "자막 항목을 선택하세요.")
            return
        item_id = sel[0]

        self._snapshot()

        for item in self.timeline_data.get("subtitle_items", []):
            if item.get("id") == item_id:
                for k, var in self.subtitle_form_vars.items():
                    if k == "text":
                        continue
                    v = var.get()
                    if k in ["start_sec", "end_sec", "box_alpha"]:
                        item[k] = safe_float(v, 0.0)
                    elif k in ["x_offset", "y_offset", "font_size", "border_w", "box", "layer"]:
                        item[k] = safe_int(v, 0)
                    else:
                        item[k] = v
                item["text"] = self.subtitle_text_widget.get("1.0", "end").rstrip()
                break

        self._refresh_trees()
        self._refresh_preview()
        self._log(f"[OK] 자막 항목 반영: {item_id}")

    def _add_image_item(self):
        if not self.timeline_data:
            messagebox.showerror("오류", "타임라인이 없습니다.")
            return
        self._snapshot()
        idx = len(self.timeline_data.get("image_items", [])) + 1
        new_item = {
            "id": f"img_new_{idx}",
            "scene_id": "",
            "path": "",
            "start_sec": 0.0,
            "end_sec": 2.0,
            "motion": "hold",
            "motion_strength": 0.06,
            "fade_in_sec": DEFAULT_FADE_SEC,
            "fade_out_sec": DEFAULT_FADE_SEC,
            "layer": 1,
            "x": 0,
            "y": 0,
            "scale_mode": "cover",
        }
        self.timeline_data["image_items"].append(new_item)
        self._refresh_trees()

    def _delete_selected_image_item(self):
        if not self.timeline_data:
            return
        sel = self.image_tree.selection()
        if not sel:
            return
        item_id = sel[0]
        self._snapshot()
        self.timeline_data["image_items"] = [x for x in self.timeline_data.get("image_items", []) if x.get("id") != item_id]
        self._refresh_trees()
        self._refresh_preview()

    def _add_subtitle_item(self):
        if not self.timeline_data:
            messagebox.showerror("오류", "타임라인이 없습니다.")
            return
        self._snapshot()
        idx = len(self.timeline_data.get("subtitle_items", [])) + 1
        new_item = {
            "id": f"txt_new_{idx}",
            "scene_id": "",
            "kind": "subtitle",
            "text": "새 자막",
            "start_sec": 0.0,
            "end_sec": 2.0,
            "position": "bottom",
            "x_offset": 0,
            "y_offset": 0,
            "font_size": DEFAULT_FONT_SIZE,
            "font_color": "#FFFFFF",
            "border_color": "#000000",
            "border_w": 4,
            "box": 1,
            "box_color": "#000000",
            "box_alpha": 0.35,
            "layer": 2,
        }
        self.timeline_data["subtitle_items"].append(new_item)
        self._refresh_trees()

    def _delete_selected_subtitle_item(self):
        if not self.timeline_data:
            return
        sel = self.subtitle_tree.selection()
        if not sel:
            return
        item_id = sel[0]
        self._snapshot()
        self.timeline_data["subtitle_items"] = [x for x in self.timeline_data.get("subtitle_items", []) if x.get("id") != item_id]
        self._refresh_trees()
        self._refresh_preview()

    # -------------------------------------------------
    # 프리뷰
    # -------------------------------------------------

    def _on_seek_changed(self, _evt=None):
        self._refresh_preview()

    def _find_active_image(self, t: float) -> Optional[Dict[str, Any]]:
        if not self.timeline_data:
            return None
        active = []
        for item in self.timeline_data.get("image_items", []):
            s = safe_float(item.get("start_sec"), 0.0)
            e = safe_float(item.get("end_sec"), 0.0)
            if s <= t <= e:
                active.append(item)
        if not active:
            return None
        active.sort(key=lambda x: safe_int(x.get("layer", 1), 1))
        return active[-1]

    def _find_active_subtitles(self, t: float) -> List[Dict[str, Any]]:
        if not self.timeline_data:
            return []
        out = []
        for item in self.timeline_data.get("subtitle_items", []):
            s = safe_float(item.get("start_sec"), 0.0)
            e = safe_float(item.get("end_sec"), 0.0)
            if s <= t <= e:
                out.append(item)
        out.sort(key=lambda x: safe_int(x.get("layer", 1), 1))
        return out

    def _refresh_preview(self):
        self.preview_canvas.delete("all")

        if not self.timeline_data:
            self.preview_canvas.create_text(DEFAULT_PREVIEW_W // 2, DEFAULT_PREVIEW_H // 2, fill="white", text="timeline 미로드")
            return

        meta = self.timeline_data.get("meta", {})
        total = safe_float(meta.get("duration_sec", 0), 0.0)
        t = safe_float(self.playhead_var.get(), 0.0)

        bg = self.bg_var.get().strip() or "black"
        self.preview_canvas.configure(bg=bg if bg in ["black", "white", "gray"] else "black")

        active_image = self._find_active_image(t)
        active_subs = self._find_active_subtitles(t)

        preview_w = DEFAULT_PREVIEW_W
        preview_h = DEFAULT_PREVIEW_H

        if active_image and active_image.get("path"):
            img_path = Path(active_image["path"])
            if img_path.exists():
                try:
                    img = Image.open(img_path).convert("RGB")
                    img.thumbnail((preview_w, preview_h))
                    canvas_img = Image.new("RGB", (preview_w, preview_h), color="black")
                    x = (preview_w - img.width) // 2
                    y = (preview_h - img.height) // 2
                    canvas_img.paste(img, (x, y))

                    draw = ImageDraw.Draw(canvas_img)

                    try:
                        font_path = self.font_var.get().strip()
                        font1 = ImageFont.truetype(font_path, 18) if font_path and Path(font_path).exists() else ImageFont.load_default()
                        font2 = ImageFont.truetype(font_path, 16) if font_path and Path(font_path).exists() else ImageFont.load_default()
                    except Exception:
                        font1 = ImageFont.load_default()
                        font2 = ImageFont.load_default()

                    for sub in active_subs:
                        txt = str(sub.get("text", "")).strip()
                        if not txt:
                            continue
                        pos = str(sub.get("position", "bottom"))
                        x_off = safe_int(sub.get("x_offset", 0), 0)
                        y_off = safe_int(sub.get("y_offset", 0), 0)
                        color = parse_hex_color(str(sub.get("font_color", "#FFFFFF")), (255, 255, 255))
                        box = safe_int(sub.get("box", 0), 0)
                        box_color_rgb = parse_hex_color(str(sub.get("box_color", "#000000")), (0, 0, 0))

                        bbox = draw.multiline_textbbox((0, 0), txt, font=font2, spacing=4)
                        tw = bbox[2] - bbox[0]
                        th = bbox[3] - bbox[1]
                        tx, ty = position_to_xy(pos, preview_w, preview_h, tw, th, x_off, y_off)

                        if box:
                            draw.rounded_rectangle([tx - 10, ty - 6, tx + tw + 10, ty + th + 6], radius=8, fill=box_color_rgb)
                        draw.multiline_text((tx, ty), txt, fill=color, font=font2, spacing=4, align="center")

                    self.preview_img_tk = ImageTk.PhotoImage(canvas_img)
                    self.preview_canvas.create_image(preview_w // 2, preview_h // 2, image=self.preview_img_tk)
                except Exception:
                    self.preview_canvas.create_text(preview_w // 2, preview_h // 2, fill="white", text="이미지 프리뷰 실패")
            else:
                self.preview_canvas.create_text(preview_w // 2, preview_h // 2, fill="white", text="이미지 파일 없음")
        else:
            self.preview_canvas.create_text(preview_w // 2, preview_h // 2, fill="white", text="현재 시간 활성 이미지 없음")

        self.preview_canvas.create_rectangle(4, 4, preview_w - 4, preview_h - 4, outline="#aaaaaa")
        self.preview_canvas.create_text(8, 8, anchor="nw", fill="yellow", text=f"{t:.2f}s / {total:.2f}s")
        self.preview_info_var.set(f"현재시간 {t:.3f}s | 활성 이미지: {active_image.get('id') if active_image else '없음'} | 자막 {len(active_subs)}개")

    # -------------------------------------------------
    # 렌더
    # -------------------------------------------------

    def _start_render_timeline(self):
        if self.is_running:
            return
        if not self.timeline_data:
            messagebox.showerror("오류", "타임라인을 먼저 로드하세요.")
            return

        self._apply_meta_form(silent=True)
        self._save_timeline()

        timeline_path = Path(self.timeline_var.get().strip())
        output_path = Path(self.output_var.get().strip())
        if not timeline_path.exists():
            messagebox.showerror("오류", "timeline.json이 없습니다.")
            return
        if not output_path.parent.exists():
            ensure_dir(output_path.parent)

        self._set_running(True)
        logger = TkTextLogger(self.ui_queue)

        def worker():
            try:
                out = render_timeline_to_video(
                    timeline_path=timeline_path,
                    output_path=output_path,
                    logger=logger
                )
                self.ui_queue.put(("done", {"title": "렌더 완료", "message": f"최종 렌더 완료\n{out}"}))
            except Exception as e:
                logger(str(e))
                logger(traceback.format_exc())
                self.ui_queue.put(("error", {"title": "렌더 오류", "message": str(e)}))

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()

    # -------------------------------------------------
    # 기타
    # -------------------------------------------------

    def _open_output_folder(self):
        p = Path(self.output_var.get().strip()).parent
        ensure_dir(p)
        try:
            if os.name == "nt":
                os.startfile(str(p))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p)])
        except Exception as e:
            messagebox.showerror("오류", str(e))
