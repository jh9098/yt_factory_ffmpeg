
import copy
import json
import os
import queue
import threading
import traceback
import wave
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import customtkinter as ctk
except Exception:
    ctk = None

try:
    import vlc
except Exception:
    vlc = None

try:
    import numpy as np
except Exception:
    np = None

try:
    from PIL import Image, ImageDraw, ImageFont, ImageTk
except ImportError:
    raise SystemExit("Pillow is required. Install with: pip install pillow")

from .constants import DEFAULT_BG_COLOR, DEFAULT_FADE_SEC, DEFAULT_FONT_SIZE, DEFAULT_FPS, DEFAULT_PREVIEW_H, DEFAULT_PREVIEW_W, DEFAULT_WIDTH, DEFAULT_HEIGHT
from .edge_tts import EdgeTTSConfig
from .renderer import render_timeline_service
from .timeline_builder import build_timeline_service
from .utils import ensure_dir, safe_float, safe_int


class TkTextLogger:
    def __init__(self, q: "queue.Queue[Tuple[str, Any]]"):
        self.q = q

    def __call__(self, msg: str) -> None:
        if msg is not None:
            self.q.put(("log", str(msg)))


class TimelineEditorGUI:
    MIN_CLIP_DUR = 0.1

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("AI Shorts Studio")
        self.root.geometry("1680x980")

        self.use_ctk = ctk is not None
        if self.use_ctk:
            ctk.set_appearance_mode("dark")
            ctk.set_default_color_theme("dark-blue")

        self.ui_queue: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self.worker_thread: Optional[threading.Thread] = None
        self.is_running = False

        self.base_var = tk.StringVar(value="")
        self.json_var = tk.StringVar(value="")
        self.images_var = tk.StringVar(value="")
        self.tts_var = tk.StringVar(value="")
        self.timeline_var = tk.StringVar(value="")
        self.output_var = tk.StringVar(value="")

        self.width_var = tk.StringVar(value=str(DEFAULT_WIDTH))
        self.height_var = tk.StringVar(value=str(DEFAULT_HEIGHT))
        self.fps_var = tk.StringVar(value=str(DEFAULT_FPS))
        self.font_var = tk.StringVar(value="")
        self.bg_var = tk.StringVar(value=DEFAULT_BG_COLOR)

        self.edge_tts_enabled_var = tk.BooleanVar(value=False)
        self.edge_tts_overwrite_var = tk.BooleanVar(value=False)
        self.edge_voice_var = tk.StringVar(value="ko-KR-SunHiNeural")

        self.playhead_var = tk.DoubleVar(value=0.0)
        self.duration_var = tk.StringVar(value="/ 0.000s")
        self.status_var = tk.StringVar(value="Ready")

        self.zoom_var = tk.DoubleVar(value=70.0)
        self.snap_enabled_var = tk.BooleanVar(value=True)

        self.timeline_data: Optional[Dict[str, Any]] = None
        self.undo_stack: List[Dict[str, Any]] = []
        self.redo_stack: List[Dict[str, Any]] = []

        self.px_per_sec = 70.0
        self.timeline_h = 250
        self.waveform_points: List[float] = []
        self.clip_geometries: Dict[str, Dict[str, Any]] = {}
        self.selected_ref: Optional[Tuple[str, str]] = None
        self.drag_state: Optional[Dict[str, Any]] = None

        self.preview_img_tk = None

        self.vlc_instance = None
        self.vlc_player = None
        self.vlc_current_path: Optional[str] = None

        self._build_ui()
        self._refresh_defaults_from_base()
        self._init_vlc()

        self.root.after(100, self._drain_ui_queue)
        self.root.after(120, self._poll_preview)

    def _frame(self, parent):
        if self.use_ctk:
            return ctk.CTkFrame(parent)
        return ttk.Frame(parent)

    def _label(self, parent, text="", textvariable=None):
        if self.use_ctk:
            return ctk.CTkLabel(parent, text=text, textvariable=textvariable)
        return ttk.Label(parent, text=text, textvariable=textvariable)

    def _entry(self, parent, var, width=220):
        if self.use_ctk:
            return ctk.CTkEntry(parent, textvariable=var, width=width)
        return ttk.Entry(parent, textvariable=var, width=max(8, int(width / 9)))

    def _button(self, parent, text, command, width=100):
        if self.use_ctk:
            return ctk.CTkButton(parent, text=text, command=command, width=width)
        return ttk.Button(parent, text=text, command=command)

    def _check(self, parent, text, var):
        if self.use_ctk:
            return ctk.CTkCheckBox(parent, text=text, variable=var)
        return ttk.Checkbutton(parent, text=text, variable=var)

    def _build_ui(self):
        root = self._frame(self.root)
        root.pack(fill="both", expand=True, padx=10, pady=10)

        top = self._frame(root)
        top.pack(fill="x")
        rows = [
            ("Project Base", self.base_var, self._pick_base, "Folder"),
            ("Script JSON", self.json_var, self._pick_json, "File"),
            ("Image Folder", self.images_var, self._pick_images, "Folder"),
            ("TTS Folder", self.tts_var, self._pick_tts, "Folder"),
            ("Timeline JSON", self.timeline_var, self._pick_timeline, "File"),
            ("Output MP4", self.output_var, self._pick_output, "Save"),
        ]
        for i, (name, var, fn, btxt) in enumerate(rows):
            self._label(top, name).grid(row=i, column=0, sticky="w", pady=2)
            self._entry(top, var, 920).grid(row=i, column=1, sticky="ew", padx=4, pady=2)
            self._button(top, btxt, fn, 80).grid(row=i, column=2, padx=2)
        top.grid_columnconfigure(1, weight=1)

        bar = self._frame(root)
        bar.pack(fill="x", pady=(8, 8))

        for txt, var, w in [("W", self.width_var, 70), ("H", self.height_var, 70), ("FPS", self.fps_var, 60), ("BG", self.bg_var, 80)]:
            self._label(bar, txt).pack(side="left")
            self._entry(bar, var, w).pack(side="left", padx=(4, 10))

        self._check(bar, "Edge TTS", self.edge_tts_enabled_var).pack(side="left", padx=4)
        self._check(bar, "Overwrite", self.edge_tts_overwrite_var).pack(side="left", padx=4)
        self._entry(bar, self.edge_voice_var, 200).pack(side="left", padx=4)

        self.btn_build = self._button(bar, "1) Build", self._start_build_timeline, 90)
        self.btn_load = self._button(bar, "2) Load", self._load_timeline_from_file, 80)
        self.btn_save = self._button(bar, "Save", self._save_timeline, 70)
        self.btn_render = self._button(bar, "3) Render", self._start_render_timeline, 90)
        for b in [self.btn_build, self.btn_load, self.btn_save, self.btn_render]:
            b.pack(side="left", padx=2)
        self._button(bar, "Undo", self._undo, 70).pack(side="left", padx=2)
        self._button(bar, "Redo", self._redo, 70).pack(side="left", padx=2)

        self._check(bar, "Snap", self.snap_enabled_var).pack(side="left", padx=(12, 2))
        self._label(bar, "Zoom").pack(side="left", padx=(8, 4))
        self.zoom_scale = tk.Scale(bar, orient="horizontal", from_=20, to=220, resolution=1, variable=self.zoom_var, command=self._on_zoom_changed, length=170)
        self.zoom_scale.pack(side="left")
        self._button(bar, "+", lambda: self._zoom_step(10), 32).pack(side="left", padx=1)
        self._button(bar, "-", lambda: self._zoom_step(-10), 32).pack(side="left", padx=1)

        self._label(bar, textvariable=self.status_var).pack(side="right")

        body = self._frame(root)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=3)
        body.columnconfigure(1, weight=2)
        body.rowconfigure(0, weight=1)

        left = self._frame(body)
        right = self._frame(body)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        right.grid(row=0, column=1, sticky="nsew")

        self._build_left(left)
        self._build_right(right)

        log_box = self._frame(root)
        log_box.pack(fill="both", expand=False, pady=(8, 0))
        self.log_text = tk.Text(log_box, height=8, bg="#0d1117", fg="#c9d1d9")
        self.log_text.pack(fill="both", expand=True)

    def _build_left(self, left):
        self.preview_stack = tk.Frame(left, bg="#111", width=DEFAULT_PREVIEW_W, height=DEFAULT_PREVIEW_H)
        self.preview_stack.pack(fill="x", pady=(4, 6))
        self.preview_stack.pack_propagate(False)

        self.preview_canvas = tk.Canvas(self.preview_stack, bg="black", width=DEFAULT_PREVIEW_W, height=DEFAULT_PREVIEW_H)
        self.preview_canvas.place(relx=0, rely=0, relwidth=1, relheight=1)

        self.vlc_panel = tk.Frame(self.preview_stack, bg="black")
        self.vlc_panel.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.vlc_panel.lower()

        seek = self._frame(left)
        seek.pack(fill="x", pady=(0, 6))
        self.seek_scale = tk.Scale(seek, orient="horizontal", resolution=0.01, from_=0, to=100, variable=self.playhead_var, command=self._on_seek_changed)
        self.seek_scale.pack(side="left", fill="x", expand=True)
        self._button(seek, "Play", self._play_preview, 60).pack(side="left", padx=2)
        self._button(seek, "Pause", self._pause_preview, 60).pack(side="left", padx=2)
        self._label(seek, textvariable=self.duration_var).pack(side="left", padx=6)

        timeline_wrap = self._frame(left)
        timeline_wrap.pack(fill="both", expand=True)

        self.timeline_canvas = tk.Canvas(
            timeline_wrap,
            bg="#0f1116",
            height=self.timeline_h,
            highlightthickness=1,
            highlightbackground="#2d3648",
        )
        self.timeline_canvas.pack(side="top", fill="both", expand=True)

        self.timeline_scroll_x = ttk.Scrollbar(timeline_wrap, orient="horizontal", command=self.timeline_canvas.xview)
        self.timeline_scroll_x.pack(side="bottom", fill="x")
        self.timeline_canvas.configure(xscrollcommand=self.timeline_scroll_x.set)

        self.timeline_canvas.bind("<Button-1>", self._on_timeline_press)
        self.timeline_canvas.bind("<B1-Motion>", self._on_timeline_drag)
        self.timeline_canvas.bind("<ButtonRelease-1>", self._on_timeline_release)

    def _build_right(self, right):
        right.rowconfigure(0, weight=2)
        right.rowconfigure(1, weight=3)
        right.rowconfigure(2, weight=2)

        clip_box = self._frame(right)
        clip_box.grid(row=0, column=0, sticky="nsew", pady=(0, 6))
        self._label(clip_box, "Clip Inspector (Video/Image)").pack(anchor="w")

        self.clip_type_var = tk.StringVar(value="image")
        self.clip_path_var = tk.StringVar(value="")
        self.clip_start_var = tk.StringVar(value="0")
        self.clip_end_var = tk.StringVar(value="0")
        self.clip_clipin_var = tk.StringVar(value="0")
        self.clip_clipout_var = tk.StringVar(value="0")
        self.clip_layer_var = tk.StringVar(value="1")
        self.clip_motion_var = tk.StringVar(value="hold")

        form = self._frame(clip_box)
        form.pack(fill="x", pady=4)
        rows = [
            ("Type", self.clip_type_var),
            ("Path", self.clip_path_var),
            ("Start", self.clip_start_var),
            ("End", self.clip_end_var),
            ("ClipIn", self.clip_clipin_var),
            ("ClipOut", self.clip_clipout_var),
            ("Layer", self.clip_layer_var),
            ("Motion", self.clip_motion_var),
        ]
        for i, (n, v) in enumerate(rows):
            self._label(form, n).grid(row=i, column=0, sticky="w", pady=1)
            self._entry(form, v, 270).grid(row=i, column=1, sticky="ew", pady=1)
        form.grid_columnconfigure(1, weight=1)

        btns = self._frame(clip_box)
        btns.pack(fill="x")
        self._button(btns, "Add Image", self._add_image_clip, 90).pack(side="left", padx=1)
        self._button(btns, "Add Video", self._add_video_clip, 90).pack(side="left", padx=1)
        self._button(btns, "Apply", self._apply_clip_form, 70).pack(side="left", padx=1)
        self._button(btns, "Delete", self._delete_selected_clip, 70).pack(side="left", padx=1)
        sub_box = self._frame(right)
        sub_box.grid(row=1, column=0, sticky="nsew", pady=(0, 6))
        self._label(sub_box, "Overlay/Subtitles Track").pack(anchor="w")

        self.subtitle_list = tk.Listbox(sub_box, bg="#151922", fg="#e6edf3", selectbackground="#2b5f9e")
        self.subtitle_list.pack(fill="both", expand=True, pady=4)
        self.subtitle_list.bind("<<ListboxSelect>>", self._on_subtitle_select)

        self.subtitle_text = tk.Text(sub_box, height=4, bg="#111722", fg="#e6edf3")
        self.subtitle_text.pack(fill="x", pady=4)

        self.sub_start_var = tk.StringVar(value="0")
        self.sub_end_var = tk.StringVar(value="0")
        self.sub_pos_var = tk.StringVar(value="bottom")

        sf = self._frame(sub_box)
        sf.pack(fill="x")
        self._label(sf, "S").pack(side="left")
        self._entry(sf, self.sub_start_var, 70).pack(side="left", padx=2)
        self._label(sf, "E").pack(side="left")
        self._entry(sf, self.sub_end_var, 70).pack(side="left", padx=2)
        self._label(sf, "Pos").pack(side="left")
        self._entry(sf, self.sub_pos_var, 90).pack(side="left", padx=2)
        self._button(sf, "Add", self._add_subtitle, 55).pack(side="left", padx=1)
        self._button(sf, "Apply", self._apply_subtitle_form, 60).pack(side="left", padx=1)
        self._button(sf, "Del", self._delete_selected_subtitle, 55).pack(side="left", padx=1)

        bgm_box = self._frame(right)
        bgm_box.grid(row=2, column=0, sticky="nsew")
        self._label(bgm_box, "BGM Track").pack(anchor="w")

        self.bgm_list = tk.Listbox(bgm_box, bg="#15221a", fg="#e6edf3", selectbackground="#2b5f9e")
        self.bgm_list.pack(fill="both", expand=True, pady=4)
        self.bgm_list.bind("<<ListboxSelect>>", self._on_bgm_select)

        self.bgm_path_var = tk.StringVar(value="")
        self.bgm_start_var = tk.StringVar(value="0")
        self.bgm_end_var = tk.StringVar(value="0")
        self.bgm_in_var = tk.StringVar(value="0")
        self.bgm_out_var = tk.StringVar(value="0")
        self.bgm_vol_var = tk.StringVar(value="0.35")

        bf = self._frame(bgm_box)
        bf.pack(fill="x")
        self._entry(bf, self.bgm_path_var, 230).pack(side="left", padx=2)
        self._button(bf, "Audio", self._add_bgm_clip, 60).pack(side="left", padx=1)
        self._button(bf, "Apply", self._apply_bgm_form, 60).pack(side="left", padx=1)
        self._button(bf, "Del", self._delete_selected_bgm, 50).pack(side="left", padx=1)

        bf2 = self._frame(bgm_box)
        bf2.pack(fill="x", pady=(2, 0))
        for label, var, width in [("S", self.bgm_start_var, 56), ("E", self.bgm_end_var, 56), ("In", self.bgm_in_var, 56), ("Out", self.bgm_out_var, 56), ("Vol", self.bgm_vol_var, 56)]:
            self._label(bf2, label).pack(side="left")
            self._entry(bf2, var, width).pack(side="left", padx=2)

    def _log(self, msg: str):
        self.log_text.insert("end", f"{msg}\n")
        self.log_text.see("end")

    def _set_running(self, running: bool):
        self.is_running = running
        state = "disabled" if running else "normal"
        for b in [self.btn_build, self.btn_load, self.btn_save, self.btn_render]:
            b.configure(state=state)
        self.status_var.set("Working..." if running else "Ready")

    def _drain_ui_queue(self):
        while True:
            try:
                kind, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self._log(str(payload))
            elif kind == "done":
                self._set_running(False)
                messagebox.showinfo(payload.get("title", "Done"), payload.get("message", ""))
            elif kind == "error":
                self._set_running(False)
                messagebox.showerror(payload.get("title", "Error"), payload.get("message", ""))
        self.root.after(100, self._drain_ui_queue)

    def _refresh_defaults_from_base(self):
        base = Path(self.base_var.get().strip() or ".")
        if not self.base_var.get().strip():
            self.base_var.set(str(base.resolve()))
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        self.json_var.set(str(base / "data" / "shorts.json"))
        self.images_var.set(str(base / "assets" / "images" / "shorts"))
        self.tts_var.set(str(base / "assets" / "tts" / "shorts"))
        self.timeline_var.set(self.timeline_var.get().strip() or str(base / "data" / "timeline.json"))
        self.output_var.set(str(base / "output" / f"{stamp}.mp4"))
        self.font_var.set(self.font_var.get().strip() or str(base / "assets" / "fonts" / "KoddiUDOnGothic-ExtraBold.ttf"))

    def _pick_base(self):
        d = filedialog.askdirectory(initialdir=self.base_var.get().strip() or os.getcwd())
        if d:
            self.base_var.set(d)
            self._refresh_defaults_from_base()

    def _pick_json(self):
        initialdir = Path(self.json_var.get().strip() or self.base_var.get().strip() or os.getcwd()).parent
        p = filedialog.askopenfilename(initialdir=str(initialdir), filetypes=[("JSON", "*.json")])
        if p:
            self.json_var.set(p)

    def _pick_images(self):
        d = filedialog.askdirectory(initialdir=self.images_var.get().strip() or os.getcwd())
        if d:
            self.images_var.set(d)

    def _pick_tts(self):
        d = filedialog.askdirectory(initialdir=self.tts_var.get().strip() or os.getcwd())
        if d:
            self.tts_var.set(d)

    def _pick_timeline(self):
        initialdir = Path(self.timeline_var.get().strip() or self.base_var.get().strip() or os.getcwd()).parent
        p = filedialog.askopenfilename(initialdir=str(initialdir), filetypes=[("JSON", "*.json")])
        if p:
            self.timeline_var.set(p)

    def _pick_output(self):
        output_path = Path(self.output_var.get().strip() or self.base_var.get().strip() or os.getcwd())
        p = filedialog.asksaveasfilename(
            initialdir=str(output_path.parent),
            initialfile=output_path.name,
            defaultextension=".mp4",
            filetypes=[("MP4", "*.mp4")],
        )
        if p:
            self.output_var.set(p)

    def _get_media(self) -> List[Dict[str, Any]]:
        if not self.timeline_data:
            return []
        items = self.timeline_data.get("media_items")
        if isinstance(items, list):
            return items
        legacy = []
        for idx, it in enumerate(self.timeline_data.get("image_items", []), 1):
            cp = dict(it)
            cp.setdefault("id", f"media_{idx}")
            cp["type"] = "image"
            cp.setdefault("clip_in_sec", 0.0)
            dur = max(0.0, safe_float(cp.get("end_sec", 0), 0) - safe_float(cp.get("start_sec", 0), 0))
            cp.setdefault("clip_out_sec", dur)
            cp.setdefault("track", "video")
            legacy.append(cp)
        self.timeline_data["media_items"] = legacy
        return legacy

    def _get_subs(self) -> List[Dict[str, Any]]:
        if not self.timeline_data:
            return []
        if "subtitle_items" not in self.timeline_data or not isinstance(self.timeline_data["subtitle_items"], list):
            self.timeline_data["subtitle_items"] = []
        return self.timeline_data["subtitle_items"]

    def _get_bgm(self) -> List[Dict[str, Any]]:
        if not self.timeline_data:
            return []
        if "bgm_items" not in self.timeline_data or not isinstance(self.timeline_data["bgm_items"], list):
            self.timeline_data["bgm_items"] = []
        return self.timeline_data["bgm_items"]

    def _is_video(self, clip: Dict[str, Any]) -> bool:
        t = str(clip.get("type", "")).lower().strip()
        if t == "video":
            return True
        ext = Path(str(clip.get("path", ""))).suffix.lower()
        return ext in {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}

    def _snapshot(self):
        if self.timeline_data is not None:
            self.undo_stack.append(copy.deepcopy(self.timeline_data))
            if len(self.undo_stack) > 60:
                self.undo_stack.pop(0)
            self.redo_stack.clear()

    def _undo(self):
        if not self.undo_stack:
            return
        if self.timeline_data is not None:
            self.redo_stack.append(copy.deepcopy(self.timeline_data))
        self.timeline_data = self.undo_stack.pop()
        self._after_load(reset_history=False)

    def _redo(self):
        if not self.redo_stack:
            return
        if self.timeline_data is not None:
            self.undo_stack.append(copy.deepcopy(self.timeline_data))
        self.timeline_data = self.redo_stack.pop()
        self._after_load(reset_history=False)

    def _start_build_timeline(self):
        if self.is_running:
            return
        base = Path(self.base_var.get().strip() or ".")
        json_path = Path(self.json_var.get().strip())
        images_dir = Path(self.images_var.get().strip())
        tts_dir = Path(self.tts_var.get().strip())
        timeline_path = Path(self.timeline_var.get().strip())
        if not json_path.exists() or not images_dir.exists() or not tts_dir.exists():
            messagebox.showerror("Error", "Check JSON/images/TTS paths")
            return

        self._set_running(True)
        logger = TkTextLogger(self.ui_queue)

        def worker():
            try:
                tl = build_timeline_service(
                    project_dir=base,
                    json_path=json_path,
                    images_dir=images_dir,
                    tts_dir=tts_dir,
                    out_timeline_path=timeline_path,
                    logger=logger,
                    edge_tts_config=EdgeTTSConfig(
                        enabled=self.edge_tts_enabled_var.get(),
                        overwrite=self.edge_tts_overwrite_var.get(),
                        voice=self.edge_voice_var.get().strip() or "ko-KR-SunHiNeural",
                    ),
                )
                self.ui_queue.put(("done", {"title": "Done", "message": f"Timeline built\n{tl}"}))
            except Exception as e:
                logger(str(e))
                logger(traceback.format_exc())
                self.ui_queue.put(("error", {"title": "Build Error", "message": str(e)}))

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()

    def _load_timeline_from_file(self):
        p = Path(self.timeline_var.get().strip())
        if not p.exists():
            messagebox.showerror("Error", f"Not found: {p}")
            return
        self.timeline_data = json.loads(p.read_text(encoding="utf-8"))
        self._after_load(reset_history=True)

    def _after_load(self, reset_history: bool):
        if not self.timeline_data:
            return
        meta = self.timeline_data.get("meta", {})
        self.width_var.set(str(meta.get("width", DEFAULT_WIDTH)))
        self.height_var.set(str(meta.get("height", DEFAULT_HEIGHT)))
        self.fps_var.set(str(meta.get("fps", DEFAULT_FPS)))
        self.bg_var.set(str(meta.get("bg_color", DEFAULT_BG_COLOR)))
        self.font_var.set(str(meta.get("font_path", "")))

        total = safe_float(meta.get("duration_sec", 0.0), 0.0)
        self.seek_scale.configure(to=max(1.0, total))
        self.duration_var.set(f"/ {total:.3f}s")

        _ = self._get_media()
        _ = self._get_subs()
        _ = self._get_bgm()

        if reset_history:
            self.undo_stack.clear()
            self.redo_stack.clear()

        self._compute_waveform()
        self._refresh_sub_list()
        self._refresh_bgm_list()
        self._draw_timeline()
        self._refresh_preview()

    def _apply_meta(self):
        if not self.timeline_data:
            return
        meta = self.timeline_data.get("meta", {})
        meta["width"] = safe_int(self.width_var.get(), DEFAULT_WIDTH)
        meta["height"] = safe_int(self.height_var.get(), DEFAULT_HEIGHT)
        meta["fps"] = safe_int(self.fps_var.get(), DEFAULT_FPS)
        meta["bg_color"] = self.bg_var.get().strip() or DEFAULT_BG_COLOR
        meta["font_path"] = self.font_var.get().strip()
        self.timeline_data["meta"] = meta

    def _save_timeline(self):
        if not self.timeline_data:
            return
        self._apply_meta()

        self.timeline_data["image_items"] = [
            {k: v for k, v in item.items() if k not in {"type", "clip_in_sec", "clip_out_sec", "track"}}
            for item in self._get_media()
            if not self._is_video(item)
        ]

        p = Path(self.timeline_var.get().strip())
        ensure_dir(p.parent)
        p.write_text(json.dumps(self.timeline_data, ensure_ascii=False, indent=2), encoding="utf-8")
        self._log(f"[OK] saved {p}")

    def _start_render_timeline(self):
        if self.is_running or not self.timeline_data:
            return
        self._save_timeline()

        p = Path(self.timeline_var.get().strip())
        output_dir = Path(self.output_var.get().strip()).parent
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        out = output_dir / f"{stamp}.mp4"
        self.output_var.set(str(out))
        ensure_dir(out.parent)

        self._set_running(True)
        logger = TkTextLogger(self.ui_queue)

        def worker():
            try:
                result = render_timeline_service(timeline_path=p, output_path=out, logger=logger)
                self.ui_queue.put(("done", {"title": "Render Done", "message": f"Rendered\n{result}"}))
            except Exception as e:
                logger(str(e))
                logger(traceback.format_exc())
                self.ui_queue.put(("error", {"title": "Render Error", "message": str(e)}))

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()
    def _find_media(self, media_id: str) -> Optional[Dict[str, Any]]:
        for item in self._get_media():
            if str(item.get("id")) == str(media_id):
                return item
        return None

    def _find_bgm(self, bgm_id: str) -> Optional[Dict[str, Any]]:
        for item in self._get_bgm():
            if str(item.get("id")) == str(bgm_id):
                return item
        return None

    def _next_start(self) -> float:
        end_t = 0.0
        for m in self._get_media():
            end_t = max(end_t, safe_float(m.get("end_sec", 0), 0))
        return end_t

    def _normalize_total(self):
        if not self.timeline_data:
            return
        end_t = 0.0
        for m in self._get_media():
            end_t = max(end_t, safe_float(m.get("end_sec", 0), 0))
        for s in self._get_subs():
            end_t = max(end_t, safe_float(s.get("end_sec", 0), 0))
        for b in self._get_bgm():
            end_t = max(end_t, safe_float(b.get("end_sec", 0), 0))

        self.timeline_data.setdefault("meta", {})["duration_sec"] = round(end_t, 3)
        self.seek_scale.configure(to=max(1.0, end_t))
        self.duration_var.set(f"/ {end_t:.3f}s")

    def _fill_clip_form(self, clip: Dict[str, Any]):
        self.clip_type_var.set(str(clip.get("type", "image")))
        self.clip_path_var.set(str(clip.get("path", "")))
        self.clip_start_var.set(f"{safe_float(clip.get('start_sec', 0), 0):.3f}")
        self.clip_end_var.set(f"{safe_float(clip.get('end_sec', 0), 0):.3f}")
        self.clip_clipin_var.set(f"{safe_float(clip.get('clip_in_sec', 0), 0):.3f}")
        self.clip_clipout_var.set(f"{safe_float(clip.get('clip_out_sec', 0), 0):.3f}")
        self.clip_layer_var.set(str(safe_int(clip.get("layer", 1), 1)))
        self.clip_motion_var.set(str(clip.get("motion", "hold")))

    def _fill_bgm_form(self, bgm: Dict[str, Any]):
        self.bgm_path_var.set(str(bgm.get("path", "")))
        self.bgm_start_var.set(f"{safe_float(bgm.get('start_sec', 0), 0):.3f}")
        self.bgm_end_var.set(f"{safe_float(bgm.get('end_sec', 0), 0):.3f}")
        self.bgm_in_var.set(f"{safe_float(bgm.get('clip_in_sec', 0), 0):.3f}")
        self.bgm_out_var.set(f"{safe_float(bgm.get('clip_out_sec', 0), 0):.3f}")
        self.bgm_vol_var.set(f"{safe_float(bgm.get('volume', 0.35), 0.35):.2f}")

    def _add_image_clip(self):
        if not self.timeline_data:
            return
        p = filedialog.askopenfilename(filetypes=[("Image", "*.png *.jpg *.jpeg *.webp")])
        if not p:
            return
        self._snapshot()
        s = self._next_start()
        items = self._get_media()
        item = {
            "id": f"media_{len(items)+1}",
            "type": "image",
            "path": p,
            "start_sec": s,
            "end_sec": s + 3.0,
            "clip_in_sec": 0.0,
            "clip_out_sec": 3.0,
            "motion": "zoom-in",
            "motion_strength": 0.06,
            "fade_in_sec": DEFAULT_FADE_SEC,
            "fade_out_sec": DEFAULT_FADE_SEC,
            "layer": 1,
            "x": 0,
            "y": 0,
            "scale_mode": "cover",
            "track": "video",
        }
        items.append(item)
        self.selected_ref = ("media", item["id"])
        self._fill_clip_form(item)
        self._normalize_total()
        self._draw_timeline()

    def _add_video_clip(self):
        if not self.timeline_data:
            return
        p = filedialog.askopenfilename(filetypes=[("Video", "*.mp4 *.mov *.mkv *.webm *.avi *.m4v")])
        if not p:
            return
        self._snapshot()
        s = self._next_start()
        items = self._get_media()
        item = {
            "id": f"media_{len(items)+1}",
            "type": "video",
            "path": p,
            "start_sec": s,
            "end_sec": s + 3.0,
            "clip_in_sec": 0.0,
            "clip_out_sec": 3.0,
            "motion": "hold",
            "motion_strength": 0.0,
            "fade_in_sec": DEFAULT_FADE_SEC,
            "fade_out_sec": DEFAULT_FADE_SEC,
            "layer": 1,
            "x": 0,
            "y": 0,
            "scale_mode": "cover",
            "track": "video",
        }
        items.append(item)
        self.selected_ref = ("media", item["id"])
        self._fill_clip_form(item)
        self._normalize_total()
        self._draw_timeline()

    def _apply_clip_form(self):
        if not self.timeline_data or not self.selected_ref or self.selected_ref[0] != "media":
            return
        clip = self._find_media(self.selected_ref[1])
        if not clip:
            return
        self._snapshot()
        clip["type"] = self.clip_type_var.get().strip() or "image"
        clip["path"] = self.clip_path_var.get().strip()
        clip["start_sec"] = max(0.0, safe_float(self.clip_start_var.get(), 0.0))
        clip["end_sec"] = max(clip["start_sec"] + self.MIN_CLIP_DUR, safe_float(self.clip_end_var.get(), clip["start_sec"] + 1.0))
        clip["clip_in_sec"] = max(0.0, safe_float(self.clip_clipin_var.get(), 0.0))
        clip["clip_out_sec"] = max(clip["clip_in_sec"] + self.MIN_CLIP_DUR, safe_float(self.clip_clipout_var.get(), clip["clip_in_sec"] + 1.0))
        clip["layer"] = max(1, safe_int(self.clip_layer_var.get(), 1))
        clip["motion"] = self.clip_motion_var.get().strip() or "hold"
        clip["track"] = "video"
        self._normalize_total()
        self._draw_timeline()
        self._refresh_preview()

    def _delete_selected_clip(self):
        if not self.timeline_data or not self.selected_ref:
            return
        kind, item_id = self.selected_ref
        self._snapshot()
        if kind == "media":
            self.timeline_data["media_items"] = [x for x in self._get_media() if str(x.get("id")) != item_id]
        elif kind == "bgm":
            self.timeline_data["bgm_items"] = [x for x in self._get_bgm() if str(x.get("id")) != item_id]
        self.selected_ref = None
        self._refresh_bgm_list()
        self._normalize_total()
        self._draw_timeline()
        self._refresh_preview()

    def _refresh_sub_list(self):
        self.subtitle_list.delete(0, "end")
        for s in self._get_subs():
            txt = str(s.get("text", "")).replace("\n", " ")
            self.subtitle_list.insert("end", f"{s.get('id','')} | {safe_float(s.get('start_sec',0),0):.2f}-{safe_float(s.get('end_sec',0),0):.2f} | {txt[:36]}")

    def _on_subtitle_select(self, _evt=None):
        sel = self.subtitle_list.curselection()
        if not sel:
            return
        sub = self._get_subs()[sel[0]]
        self.subtitle_text.delete("1.0", "end")
        self.subtitle_text.insert("1.0", str(sub.get("text", "")))
        self.sub_start_var.set(f"{safe_float(sub.get('start_sec', 0), 0):.3f}")
        self.sub_end_var.set(f"{safe_float(sub.get('end_sec', 0), 0):.3f}")
        self.sub_pos_var.set(str(sub.get("position", "bottom")))

    def _add_subtitle(self):
        if not self.timeline_data:
            return
        self._snapshot()
        t = safe_float(self.playhead_var.get(), 0.0)
        subs = self._get_subs()
        subs.append({
            "id": f"txt_{len(subs)+1}",
            "scene_id": "",
            "kind": "subtitle",
            "text": "New subtitle",
            "start_sec": t,
            "end_sec": t + 2.5,
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
            "track": "overlay",
        })
        self._refresh_sub_list()
        self._normalize_total()
        self._draw_timeline()

    def _apply_subtitle_form(self):
        sel = self.subtitle_list.curselection()
        if not sel:
            return
        self._snapshot()
        sub = self._get_subs()[sel[0]]
        sub["text"] = self.subtitle_text.get("1.0", "end").rstrip()
        sub["start_sec"] = max(0.0, safe_float(self.sub_start_var.get(), 0.0))
        sub["end_sec"] = max(sub["start_sec"] + 0.05, safe_float(self.sub_end_var.get(), sub["start_sec"] + 1.0))
        sub["position"] = self.sub_pos_var.get().strip() or "bottom"
        sub["track"] = "overlay"
        self._refresh_sub_list()
        self._normalize_total()
        self._draw_timeline()
        self._refresh_preview()

    def _delete_selected_subtitle(self):
        sel = self.subtitle_list.curselection()
        if not sel:
            return
        self._snapshot()
        self._get_subs().pop(sel[0])
        self._refresh_sub_list()
        self._normalize_total()
        self._draw_timeline()

    def _refresh_bgm_list(self):
        self.bgm_list.delete(0, "end")
        for b in self._get_bgm():
            self.bgm_list.insert(
                "end",
                f"{b.get('id','')} | {safe_float(b.get('start_sec',0),0):.2f}-{safe_float(b.get('end_sec',0),0):.2f} | vol {safe_float(b.get('volume',0.35),0.35):.2f} | {Path(str(b.get('path',''))).name}",
            )

    def _on_bgm_select(self, _evt=None):
        sel = self.bgm_list.curselection()
        if not sel:
            return
        bgm = self._get_bgm()[sel[0]]
        self.selected_ref = ("bgm", str(bgm.get("id")))
        self._fill_bgm_form(bgm)

    def _add_bgm_clip(self):
        if not self.timeline_data:
            return
        p = filedialog.askopenfilename(filetypes=[("Audio", "*.wav *.mp3 *.m4a *.aac")])
        if not p:
            return
        self._snapshot()
        items = self._get_bgm()
        s = 0.0
        e = max(3.0, safe_float(self.timeline_data.get("meta", {}).get("duration_sec", 3.0), 3.0))
        item = {
            "id": f"bgm_{len(items)+1}",
            "type": "bgm",
            "path": p,
            "start_sec": s,
            "end_sec": e,
            "clip_in_sec": 0.0,
            "clip_out_sec": e - s,
            "volume": 0.35,
            "track": "bgm",
        }
        items.append(item)
        self.selected_ref = ("bgm", item["id"])
        self._fill_bgm_form(item)
        self._refresh_bgm_list()
        self._normalize_total()
        self._draw_timeline()

    def _apply_bgm_form(self):
        if not self.timeline_data or not self.selected_ref or self.selected_ref[0] != "bgm":
            return
        bgm = self._find_bgm(self.selected_ref[1])
        if not bgm:
            return
        self._snapshot()
        bgm["path"] = self.bgm_path_var.get().strip()
        bgm["start_sec"] = max(0.0, safe_float(self.bgm_start_var.get(), 0.0))
        bgm["end_sec"] = max(bgm["start_sec"] + self.MIN_CLIP_DUR, safe_float(self.bgm_end_var.get(), bgm["start_sec"] + 1.0))
        bgm["clip_in_sec"] = max(0.0, safe_float(self.bgm_in_var.get(), 0.0))
        bgm["clip_out_sec"] = max(bgm["clip_in_sec"] + self.MIN_CLIP_DUR, safe_float(self.bgm_out_var.get(), bgm["clip_in_sec"] + 1.0))
        bgm["volume"] = max(0.0, safe_float(self.bgm_vol_var.get(), 0.35))
        bgm["track"] = "bgm"
        self._refresh_bgm_list()
        self._normalize_total()
        self._draw_timeline()

    def _delete_selected_bgm(self):
        if not self.selected_ref or self.selected_ref[0] != "bgm":
            return
        self._delete_selected_clip()

    def _init_vlc(self):
        if vlc is None:
            self._log("[INFO] python-vlc not installed")
            return
        try:
            self.vlc_instance = vlc.Instance("--quiet")
            self.vlc_player = self.vlc_instance.media_player_new()
            self.root.after(220, self._attach_vlc)
            self._log("[INFO] VLC preview ready")
        except Exception as e:
            self.vlc_instance = None
            self.vlc_player = None
            self._log(f"[WARN] VLC init failed: {e}")

    def _attach_vlc(self):
        if not self.vlc_player:
            return
        wid = self.vlc_panel.winfo_id()
        try:
            if os.name == "nt":
                self.vlc_player.set_hwnd(wid)
            elif os.name == "posix":
                self.vlc_player.set_xwindow(wid)
        except Exception:
            pass

    def _play_preview(self):
        t = safe_float(self.playhead_var.get(), 0.0)
        active = self._find_active_clip(t)
        if active and self._is_video(active) and self.vlc_player:
            self._show_vlc(active, t, autoplay=True)

    def _pause_preview(self):
        if self.vlc_player:
            try:
                self.vlc_player.pause()
            except Exception:
                pass

    def _show_vlc(self, clip: Dict[str, Any], timeline_t: float, autoplay: bool):
        if not self.vlc_player:
            return
        path = str(clip.get("path", ""))
        if self.vlc_current_path != path:
            media = self.vlc_instance.media_new(path)
            self.vlc_player.set_media(media)
            self.vlc_current_path = path
        self.vlc_panel.lift()
        local = max(0.0, timeline_t - safe_float(clip.get("start_sec", 0), 0))
        target_ms = int((safe_float(clip.get("clip_in_sec", 0), 0) + local) * 1000)
        self.vlc_player.play()
        self.root.after(110, lambda: self._seek_vlc(target_ms, autoplay))

    def _seek_vlc(self, ms: int, autoplay: bool):
        if not self.vlc_player:
            return
        try:
            self.vlc_player.set_time(ms)
            if not autoplay:
                self.vlc_player.pause()
        except Exception:
            pass

    def _on_seek_changed(self, _evt=None):
        self._refresh_preview()
        self._draw_timeline()

    def _poll_preview(self):
        if self.vlc_player and self.vlc_player.is_playing():
            t = safe_float(self.playhead_var.get(), 0.0)
            clip = self._find_active_clip(t)
            if clip and self._is_video(clip):
                cur = self.vlc_player.get_time() / 1000.0
                in_t = safe_float(clip.get("clip_in_sec", 0), 0)
                timeline_t = safe_float(clip.get("start_sec", 0), 0) + max(0.0, cur - in_t)
                if timeline_t <= safe_float(clip.get("end_sec", 0), 0) + 0.05:
                    self.playhead_var.set(timeline_t)
                else:
                    self.vlc_player.pause()
        self._draw_timeline()
        self.root.after(120, self._poll_preview)

    def _find_active_clip(self, t: float) -> Optional[Dict[str, Any]]:
        active = []
        for m in self._get_media():
            if safe_float(m.get("start_sec", 0), 0) <= t <= safe_float(m.get("end_sec", 0), 0):
                active.append(m)
        if not active:
            return None
        active.sort(key=lambda x: safe_int(x.get("layer", 1), 1))
        return active[-1]

    def _find_active_subs(self, t: float) -> List[Dict[str, Any]]:
        out = []
        for s in self._get_subs():
            if safe_float(s.get("start_sec", 0), 0) <= t <= safe_float(s.get("end_sec", 0), 0):
                out.append(s)
        out.sort(key=lambda x: safe_int(x.get("layer", 1), 1))
        return out

    def _refresh_preview(self):
        self.preview_canvas.delete("all")
        if not self.timeline_data:
            self.preview_canvas.create_text(DEFAULT_PREVIEW_W // 2, DEFAULT_PREVIEW_H // 2, fill="white", text="No timeline")
            return

        t = safe_float(self.playhead_var.get(), 0)
        total = safe_float(self.timeline_data.get("meta", {}).get("duration_sec", 0), 0)
        clip = self._find_active_clip(t)
        subs = self._find_active_subs(t)

        if clip and self._is_video(clip) and self.vlc_player:
            self._show_vlc(clip, t, autoplay=False)
            self.status_var.set(f"{t:.2f}s / {total:.2f}s | video")
            return

        self.vlc_panel.lower()
        bg = self.bg_var.get().strip().lower()
        self.preview_canvas.configure(bg=bg if bg in {"black", "white", "gray"} else "black")

        if clip and Path(str(clip.get("path", ""))).exists() and not self._is_video(clip):
            try:
                img = Image.open(str(clip.get("path", ""))).convert("RGB")
                img.thumbnail((DEFAULT_PREVIEW_W, DEFAULT_PREVIEW_H))
                canvas_img = Image.new("RGB", (DEFAULT_PREVIEW_W, DEFAULT_PREVIEW_H), "black")
                x = (DEFAULT_PREVIEW_W - img.width) // 2
                y = (DEFAULT_PREVIEW_H - img.height) // 2
                canvas_img.paste(img, (x, y))

                draw = ImageDraw.Draw(canvas_img)
                font_path = self.font_var.get().strip()
                try:
                    font = ImageFont.truetype(font_path, 18) if font_path and Path(font_path).exists() else ImageFont.load_default()
                except Exception:
                    font = ImageFont.load_default()

                for s in subs:
                    txt = str(s.get("text", "")).replace("\\n", "\n").strip()
                    if txt:
                        y0 = DEFAULT_PREVIEW_H - 120 if str(s.get("position", "bottom")) == "bottom" else 30
                        draw.multiline_text((14, y0), txt, fill=(255, 255, 255), font=font, spacing=4)

                self.preview_img_tk = ImageTk.PhotoImage(canvas_img)
                self.preview_canvas.create_image(DEFAULT_PREVIEW_W // 2, DEFAULT_PREVIEW_H // 2, image=self.preview_img_tk)
            except Exception:
                self.preview_canvas.create_text(DEFAULT_PREVIEW_W // 2, DEFAULT_PREVIEW_H // 2, fill="white", text="Preview failed")
        elif clip and self._is_video(clip):
            self.preview_canvas.create_text(DEFAULT_PREVIEW_W // 2, DEFAULT_PREVIEW_H // 2, fill="white", text="Install python-vlc for video preview")
        else:
            self.preview_canvas.create_text(DEFAULT_PREVIEW_W // 2, DEFAULT_PREVIEW_H // 2, fill="white", text="No active clip")

        self.preview_canvas.create_rectangle(4, 4, DEFAULT_PREVIEW_W - 4, DEFAULT_PREVIEW_H - 4, outline="#8b949e")
        self.preview_canvas.create_text(8, 8, anchor="nw", fill="#f7c948", text=f"{t:.2f}s / {total:.2f}s")
        self.status_var.set(f"{t:.2f}s / {total:.2f}s")
    def _on_zoom_changed(self, _evt=None):
        self.px_per_sec = max(20.0, float(self.zoom_var.get()))
        self._draw_timeline()

    def _zoom_step(self, delta: int):
        self.zoom_var.set(max(20.0, min(220.0, self.zoom_var.get() + delta)))
        self._on_zoom_changed()

    def _snap_time(self, t: float, moving_kind: str, moving_id: str) -> float:
        if not self.snap_enabled_var.get():
            return t

        threshold = 12.0 / max(1.0, self.px_per_sec)
        candidates = [0.0]

        if moving_kind == "media":
            for m in self._get_media():
                if str(m.get("id")) == moving_id:
                    continue
                candidates.append(safe_float(m.get("start_sec", 0), 0))
                candidates.append(safe_float(m.get("end_sec", 0), 0))
        elif moving_kind == "bgm":
            for b in self._get_bgm():
                if str(b.get("id")) == moving_id:
                    continue
                candidates.append(safe_float(b.get("start_sec", 0), 0))
                candidates.append(safe_float(b.get("end_sec", 0), 0))

        best = t
        best_d = 9999.0
        for c in candidates:
            d = abs(c - t)
            if d < best_d and d <= threshold:
                best = c
                best_d = d
        return best

    def _draw_timeline(self):
        c = self.timeline_canvas
        c.delete("all")
        if not self.timeline_data:
            c.create_text(20, 20, anchor="w", fill="#c9d1d9", text="Load timeline to edit")
            return

        total = max(1.0, safe_float(self.timeline_data.get("meta", {}).get("duration_sec", 1), 1))
        width = max(1200, int(total * self.px_per_sec) + 80)
        c.config(scrollregion=(0, 0, width, self.timeline_h))

        c.create_rectangle(0, 22, width, 66, fill="#10203d", outline="#2d3648")
        c.create_rectangle(0, 74, width, 110, fill="#2a1947", outline="#2d3648")
        c.create_rectangle(0, 118, width, 154, fill="#183124", outline="#2d3648")
        c.create_rectangle(0, 162, width, 236, fill="#0d141f", outline="#2d3648")

        c.create_text(8, 8, anchor="w", fill="#8aa4d1", text="VIDEO TRACK")
        c.create_text(8, 78, anchor="w", fill="#c4a7e7", text="OVERLAY TRACK")
        c.create_text(8, 122, anchor="w", fill="#7fd48f", text="BGM TRACK")
        c.create_text(8, 166, anchor="w", fill="#58a6ff", text="WAVEFORM")

        step = 1 if total <= 20 else 2 if total <= 60 else 5
        for sec in range(0, int(total) + 1, step):
            x = sec * self.px_per_sec
            c.create_line(x, 0, x, self.timeline_h, fill="#1f2a3a")
            c.create_text(x + 2, 2, anchor="nw", fill="#9fb4d0", text=f"{sec}s")

        self.clip_geometries.clear()

        for m in sorted(self._get_media(), key=lambda x: (safe_int(x.get("layer", 1), 1), safe_float(x.get("start_sec", 0), 0))):
            clip_id = str(m.get("id", ""))
            s = safe_float(m.get("start_sec", 0), 0)
            e = safe_float(m.get("end_sec", 0), 0)
            if e <= s:
                continue
            x1, x2 = s * self.px_per_sec, e * self.px_per_sec
            y1, y2 = 28, 60
            color = "#1f6feb" if self._is_video(m) else "#2ea043"
            selected = self.selected_ref == ("media", clip_id)
            c.create_rectangle(x1, y1, x2, y2, fill=color, outline="#ffd166" if selected else "#304760", width=2)
            c.create_text(x1 + 4, (y1 + y2) / 2, anchor="w", fill="#f4f7fb", text=f"[{m.get('type','image')}] {Path(str(m.get('path',''))).name}")
            c.create_line(x1 + 5, y1 + 2, x1 + 5, y2 - 2, fill="#dbe5f2", width=2)
            c.create_line(x2 - 5, y1 + 2, x2 - 5, y2 - 2, fill="#dbe5f2", width=2)
            self.clip_geometries[f"media::{clip_id}"] = {"kind": "media", "id": clip_id, "x1": x1, "x2": x2, "y1": y1, "y2": y2}

        for s in self._get_subs():
            st = safe_float(s.get("start_sec", 0), 0)
            et = safe_float(s.get("end_sec", 0), 0)
            if et <= st:
                continue
            x1, x2 = st * self.px_per_sec, et * self.px_per_sec
            c.create_rectangle(x1, 80, x2, 104, fill="#8250df", outline="#30363d")

        for b in self._get_bgm():
            bgm_id = str(b.get("id", ""))
            st = safe_float(b.get("start_sec", 0), 0)
            et = safe_float(b.get("end_sec", 0), 0)
            if et <= st:
                continue
            x1, x2 = st * self.px_per_sec, et * self.px_per_sec
            selected = self.selected_ref == ("bgm", bgm_id)
            c.create_rectangle(x1, 122, x2, 150, fill="#238636", outline="#ffd166" if selected else "#2d5a3c", width=2)
            c.create_text(x1 + 4, 136, anchor="w", fill="#f4f7fb", text=f"[bgm] {Path(str(b.get('path',''))).name} ({safe_float(b.get('volume',0.35),0.35):.2f})")
            c.create_line(x1 + 5, 124, x1 + 5, 148, fill="#dbe5f2", width=2)
            c.create_line(x2 - 5, 124, x2 - 5, 148, fill="#dbe5f2", width=2)
            self.clip_geometries[f"bgm::{bgm_id}"] = {"kind": "bgm", "id": bgm_id, "x1": x1, "x2": x2, "y1": 122, "y2": 150}

        if self.waveform_points:
            base_y, amp = 198, 26
            stride = max(1, int(len(self.waveform_points) / max(1, width)))
            pts = []
            for x in range(width):
                idx = min(len(self.waveform_points) - 1, x * stride)
                pts.extend((x, base_y - (self.waveform_points[idx] * amp)))
            if len(pts) >= 4:
                c.create_line(*pts, fill="#58a6ff", smooth=True)

        px = safe_float(self.playhead_var.get(), 0) * self.px_per_sec
        c.create_line(px, 0, px, self.timeline_h, fill="#ff6b6b", width=2)

    def _on_timeline_press(self, evt):
        if not self.timeline_data:
            return
        x = self.timeline_canvas.canvasx(evt.x)
        y = self.timeline_canvas.canvasy(evt.y)

        found = None
        for _, g in self.clip_geometries.items():
            if g["x1"] <= x <= g["x2"] and g["y1"] <= y <= g["y2"]:
                found = g
                break

        if not found:
            self.selected_ref = None
            self.playhead_var.set(max(0.0, x / self.px_per_sec))
            self._refresh_preview()
            self._draw_timeline()
            return

        kind = found["kind"]
        item_id = found["id"]
        self.selected_ref = (kind, item_id)

        if kind == "media":
            clip = self._find_media(item_id)
            if not clip:
                return
            self._fill_clip_form(clip)
            s0 = safe_float(clip.get("start_sec", 0), 0)
            e0 = safe_float(clip.get("end_sec", 0), 0)
            in0 = safe_float(clip.get("clip_in_sec", 0), 0)
            out0 = safe_float(clip.get("clip_out_sec", 0), 0)
        else:
            clip = self._find_bgm(item_id)
            if not clip:
                return
            self._fill_bgm_form(clip)
            s0 = safe_float(clip.get("start_sec", 0), 0)
            e0 = safe_float(clip.get("end_sec", 0), 0)
            in0 = safe_float(clip.get("clip_in_sec", 0), 0)
            out0 = safe_float(clip.get("clip_out_sec", 0), 0)

        mode = "drag"
        if abs(x - found["x1"]) <= 8:
            mode = "trim_start"
        elif abs(x - found["x2"]) <= 8:
            mode = "trim_end"

        self.drag_state = {
            "kind": kind,
            "id": item_id,
            "mode": mode,
            "press_x": x,
            "s0": s0,
            "e0": e0,
            "in0": in0,
            "out0": out0,
        }

        self.playhead_var.set(s0)
        self._refresh_preview()
        self._draw_timeline()

    def _on_timeline_drag(self, evt):
        if not self.drag_state:
            return

        kind = self.drag_state["kind"]
        item_id = self.drag_state["id"]

        item = self._find_media(item_id) if kind == "media" else self._find_bgm(item_id)
        if not item:
            return

        x = self.timeline_canvas.canvasx(evt.x)
        delta = (x - self.drag_state["press_x"]) / self.px_per_sec
        s0 = self.drag_state["s0"]
        e0 = self.drag_state["e0"]
        in0 = self.drag_state["in0"]
        out0 = self.drag_state["out0"]

        mode = self.drag_state["mode"]
        if mode == "drag":
            dur = e0 - s0
            ns = max(0.0, s0 + delta)
            ns = self._snap_time(ns, kind, item_id)
            item["start_sec"] = round(ns, 3)
            item["end_sec"] = round(ns + dur, 3)
        elif mode == "trim_start":
            ns = min(e0 - self.MIN_CLIP_DUR, max(0.0, s0 + delta))
            ns = self._snap_time(ns, kind, item_id)
            item["start_sec"] = round(ns, 3)
            item["clip_in_sec"] = round(max(0.0, in0 + (ns - s0)), 3)
        else:
            ne = max(s0 + self.MIN_CLIP_DUR, e0 + delta)
            ne = self._snap_time(ne, kind, item_id)
            item["end_sec"] = round(ne, 3)
            item["clip_out_sec"] = round(max(in0 + self.MIN_CLIP_DUR, out0 + (ne - e0)), 3)

        self._normalize_total()
        if kind == "media":
            self._fill_clip_form(item)
        else:
            self._fill_bgm_form(item)
        self.playhead_var.set(safe_float(item.get("start_sec", 0), 0))
        self._refresh_preview()
        self._draw_timeline()

    def _on_timeline_release(self, _evt):
        if self.drag_state:
            self._snapshot()
        self.drag_state = None

    def _compute_waveform(self):
        self.waveform_points = []
        if not self.timeline_data:
            return

        ap = Path(str(self.timeline_data.get("meta", {}).get("master_audio_path", "")))
        if not ap.exists():
            return

        try:
            with wave.open(str(ap), "rb") as wf:
                channels = wf.getnchannels()
                frames = wf.readframes(wf.getnframes())
                samp = wf.getsampwidth()
            if samp != 2:
                return

            if np is not None:
                arr = np.frombuffer(frames, dtype=np.int16).astype(np.float32)
                if channels > 1:
                    arr = arr.reshape(-1, channels).mean(axis=1)
                arr = np.abs(arr)
                m = float(np.max(arr)) or 1.0
                self.waveform_points = (arr / m).tolist()
            else:
                step = 2 * channels * 100
                vals = []
                for i in range(0, len(frames), step):
                    b = frames[i:i+2]
                    if len(b) < 2:
                        break
                    vals.append(abs(int.from_bytes(b, "little", signed=True)) / 32768.0)
                self.waveform_points = vals
        except Exception as e:
            self._log(f"[WARN] waveform failed: {e}")
