# -*- coding: utf-8 -*-
import argparse
import copy
import glob
import json
import math
import os
import queue
import re
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


# =========================================================
# 기본 설정
# =========================================================

DEFAULT_FPS = 30
DEFAULT_WIDTH = 1080
DEFAULT_HEIGHT = 1920
DEFAULT_BG_COLOR = "black"
DEFAULT_FADE_SEC = 0.25
DEFAULT_PREVIEW_W = 270
DEFAULT_PREVIEW_H = 480

SUPPORTED_IMG_EXTS = [".png", ".jpg", ".jpeg", ".webp"]
SUPPORTED_AUD_EXTS = [".wav", ".mp3", ".m4a", ".aac"]

DEFAULT_FONT_SIZE = 54
DEFAULT_OVERLAY_FONT_SIZE = 52


# =========================================================
# 공통 유틸
# =========================================================

def which_ffmpeg() -> str:
    return "ffmpeg.exe" if os.name == "nt" else "ffmpeg"


def which_ffprobe() -> str:
    return "ffprobe.exe" if os.name == "nt" else "ffprobe"


def log_print(msg: str) -> None:
    print(msg)


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def sanitize_folder_name(name: str) -> str:
    if not name:
        return "untitled"
    name = re.sub(r'[<>:"/\\|?*]+', "_", name.strip())
    name = name.rstrip(". ").strip()
    return name or "untitled"


def safe_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except Exception:
        return default


def safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def run_cmd(
    cmd: List[str],
    cwd: Optional[Path] = None,
    check: bool = True,
    logger=log_print,
) -> subprocess.CompletedProcess:
    logger("\n[CMD] " + " ".join(str(x) for x in cmd))
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        cwd=str(cwd) if cwd else None,
        shell=False,
        text=False,
    )
    try:
        out = proc.stdout.decode("utf-8", errors="replace")
    except Exception:
        out = proc.stdout.decode("cp949", errors="replace")
    if out:
        logger(out.rstrip())
    if check and proc.returncode != 0:
        raise RuntimeError(f"명령 실행 실패 (exit={proc.returncode})")
    proc.stdout = out  # type: ignore
    return proc


def ffprobe_duration_sec(path: Path, logger=log_print) -> float:
    cmd = [
        which_ffprobe(),
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path)
    ]
    proc = run_cmd(cmd, check=True, logger=logger)
    out = (proc.stdout or "").strip()  # type: ignore
    nums = [x for x in out.split() if x.replace(".", "", 1).isdigit()]
    if not nums:
        raise RuntimeError(f"duration 파싱 실패: {path}")
    return float(nums[-1])

def cut_wav_segment(
    master_wav: Path,
    out_wav: Path,
    start_sec: float,
    dur_sec: float,
    logger=log_print
) -> None:
    ensure_dir(out_wav.parent)

    total = ffprobe_duration_sec(master_wav, logger=logger)
    start_sec = max(0.0, float(start_sec))
    dur_sec = max(0.01, float(dur_sec))

    # 마스터 길이를 아예 넘긴 경우: 전부 무음 생성
    if start_sec >= total:
        cmd = [
            which_ffmpeg(),
            "-y",
            "-f", "lavfi",
            "-i", f"anullsrc=r=48000:cl=stereo",
            "-t", f"{dur_sec:.3f}",
            "-c:a", "pcm_s16le",
            str(out_wav)
        ]
        run_cmd(cmd, check=True, logger=logger)
        return

    # 일부만 남아있는 경우: 남은 실제 음성 + 부족분 무음 패딩
    available = max(0.0, total - start_sec)
    actual_dur = min(dur_sec, available)

    # requested duration 전체를 맞추기 위해 apad 사용
    cmd = [
        which_ffmpeg(),
        "-y",
        "-i", str(master_wav),
        "-ss", f"{start_sec:.3f}",
        "-t", f"{actual_dur:.3f}",
        "-af", f"apad=pad_dur={max(0.0, dur_sec - actual_dur):.3f}",
        "-t", f"{dur_sec:.3f}",
        "-ac", "2",
        "-ar", "48000",
        "-c:a", "pcm_s16le",
        str(out_wav)
    ]
    run_cmd(cmd, check=True, logger=logger)
     
def scene_id_candidates(scene_id: str, idx: int) -> List[str]:
    s = str(scene_id).strip()
    out: List[str] = []

    def add(x: str):
        x = str(x).strip()
        if x and x not in out:
            out.append(x)

    add(s)
    if s.isdigit():
        n = int(s)
        add(str(n))
        add(f"{n:02d}")
        add(f"{n:03d}")
        add(f"S-{n:02d}")
        add(f"S-{n:03d}")
        add(f"S{n:02d}")
        add(f"S{n:03d}")
    if s.upper().startswith("S-") and s[2:].isdigit():
        n = int(s[2:])
        add(str(n))
        add(f"{n:02d}")
        add(f"{n:03d}")
    add(str(idx))
    add(f"{idx:02d}")
    add(f"S-{idx:02d}")
    add(f"S{idx:02d}")
    return out


def find_by_scene_id(base_dir: Path, scene_id: str, exts: List[str], idx: int) -> Optional[Path]:
    cands = scene_id_candidates(scene_id, idx)

    for cid in cands:
        for ext in exts:
            p = base_dir / f"{cid}{ext}"
            if p.exists():
                return p

    if base_dir.exists():
        want = set([x.lower() for x in cands])
        for fp in base_dir.iterdir():
            if fp.is_file() and fp.suffix.lower() in exts and fp.stem.lower() in want:
                return fp

    return None


def load_json_schema_compliant(json_path: Path) -> Dict:
    try:
        raw = json_path.read_text(encoding="utf-8-sig")
    except Exception:
        raw = json_path.read_text(encoding="utf-8", errors="replace")

    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("JSON 루트는 object여야 합니다.")

    if "scenes" not in data or not isinstance(data["scenes"], list):
        raise ValueError("JSON에 scenes(list)가 필요합니다.")

    meta_required = [
        "title", "target_audience", "tone",
        "safety_notes", "cta", "estimated_total_duration_sec"
    ]

    meta_obj = data.get("meta")
    if isinstance(meta_obj, dict):
        miss = [k for k in meta_required if k not in meta_obj]
        if miss:
            raise ValueError(f"meta 누락: {miss}")
        normalized = dict(data)
        for k in meta_required:
            normalized[k] = meta_obj[k]
        data = normalized
    else:
        miss = [k for k in meta_required if k not in data]
        if miss:
            raise ValueError(f"루트 메타 누락: {miss}")

    for i, sc in enumerate(data["scenes"], start=1):
        if not isinstance(sc, dict):
            raise ValueError(f"scene #{i}가 object가 아닙니다.")
        if "scene_id" not in sc:
            raise ValueError(f"scene #{i} scene_id 누락")

    return data


def ffmpeg_escape_text(s: str) -> str:
    """
    textfile 방식으로 바꿀 예정이므로,
    여기서는 과도한 escape를 하지 않고 줄바꿈만 정리
    """
    if s is None:
        return ""
    s = str(s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    return s
def ffmpeg_escape_path_for_filter(p: Path) -> str:
    """
    FFmpeg filter(drawtext 등) 안에서 사용할 파일 경로 escape
    - Windows 드라이브 문자 D: 의 콜론도 반드시 이스케이프
    - 백슬래시는 슬래시로 통일
    """
    s = str(p).replace("\\", "/")
    s = s.replace(":", r"\:")
    s = s.replace("'", r"\'")
    s = s.replace("[", r"\[")
    s = s.replace("]", r"\]")
    s = s.replace(",", r"\,")
    s = s.replace(";", r"\;")
    return s

def seconds_to_hms(sec: float) -> str:
    if sec < 0:
        sec = 0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def normalize_motion_name(m: str) -> str:
    m = (m or "").strip().lower()
    mapping = {
        "zoom": "zoom-in",
        "zoomin": "zoom-in",
        "zoom-in": "zoom-in",
        "zoomout": "zoom-out",
        "zoom-out": "zoom-out",
        "hold": "hold",
        "none": "hold",
        "pan-left": "pan-left",
        "pan-right": "pan-right",
        "pan-up": "pan-up",
        "pan-down": "pan-down",
    }
    return mapping.get(m, "hold")


def build_zoompan_expr(
    motion: str,
    duration: float,
    fps: int,
    out_w: int,
    out_h: int,
    intensity: float = 0.06
) -> str:
    motion = normalize_motion_name(motion)
    frames = max(1, int(round(duration * fps)))

    z0 = 1.0
    z1 = 1.0 + max(0.0, intensity)

    t_expr = "0" if frames <= 1 else f"(on/{frames-1})"
    ease = f"(0.5*(1-cos(PI*{t_expr})))"

    if motion == "zoom-in":
        z_expr = f"({z0}+({z1}-{z0})*{ease})"
        x_expr = f"round(iw/2 - (iw/({z_expr}))/2)"
        y_expr = f"round(ih/2 - (ih/({z_expr}))/2)"
    elif motion == "zoom-out":
        z_expr = f"({z1}-({z1}-{z0})*{ease})"
        x_expr = f"round(iw/2 - (iw/({z_expr}))/2)"
        y_expr = f"round(ih/2 - (ih/({z_expr}))/2)"
    elif motion == "pan-left":
        z_expr = "1.03"
        x_expr = f"round((iw-iw/{z_expr})*(1-{ease}))"
        y_expr = f"round(ih/2 - (ih/({z_expr}))/2)"
    elif motion == "pan-right":
        z_expr = "1.03"
        x_expr = f"round((iw-iw/{z_expr})*{ease})"
        y_expr = f"round(ih/2 - (ih/({z_expr}))/2)"
    elif motion == "pan-up":
        z_expr = "1.03"
        x_expr = f"round(iw/2 - (iw/({z_expr}))/2)"
        y_expr = f"round((ih-ih/{z_expr})*(1-{ease}))"
    elif motion == "pan-down":
        z_expr = "1.03"
        x_expr = f"round(iw/2 - (iw/({z_expr}))/2)"
        y_expr = f"round((ih-ih/{z_expr})*{ease})"
    else:
        z_expr = "1.0"
        x_expr = f"round(iw/2 - (iw/({z_expr}))/2)"
        y_expr = f"round(ih/2 - (ih/({z_expr}))/2)"

    return f"zoompan=z='{z_expr}':d={frames}:x='{x_expr}':y='{y_expr}':s={out_w}x{out_h}:fps={fps}"


def parse_hex_color(s: str, default=(255, 255, 255)) -> Tuple[int, int, int]:
    if not s:
        return default
    s = s.strip().lstrip("#")
    if len(s) == 6:
        try:
            return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
        except Exception:
            return default
    return default


def position_to_xy(pos: str, width: int, height: int, text_w: int, text_h: int, x_offset: int = 0, y_offset: int = 0) -> Tuple[int, int]:
    pos = (pos or "").strip().lower()
    if pos == "top":
        x = (width - text_w) // 2
        y = int(height * 0.08)
    elif pos == "bottom":
        x = (width - text_w) // 2
        y = int(height * 0.82)
    elif pos == "center":
        x = (width - text_w) // 2
        y = (height - text_h) // 2
    elif pos == "left-top":
        x = int(width * 0.06)
        y = int(height * 0.08)
    elif pos == "right-top":
        x = int(width * 0.94 - text_w)
        y = int(height * 0.08)
    else:
        x = (width - text_w) // 2
        y = int(height * 0.82)

    return x + x_offset, y + y_offset


# =========================================================
# TTS 병합 / 타임라인 생성
# =========================================================

def resolve_tts_dirs(tts_selected_dir: Path, images_dir: Path) -> Tuple[Path, Path]:
    job_name = sanitize_folder_name(images_dir.name)

    candidate_job_dir = tts_selected_dir / job_name
    if candidate_job_dir.exists() and candidate_job_dir.is_dir():
        return candidate_job_dir, tts_selected_dir

    has_audio = False
    if tts_selected_dir.exists() and tts_selected_dir.is_dir():
        for fp in tts_selected_dir.iterdir():
            if fp.is_file() and fp.suffix.lower() in SUPPORTED_AUD_EXTS:
                has_audio = True
                break

    if has_audio:
        parent_dir = tts_selected_dir.parent if tts_selected_dir.parent.exists() else tts_selected_dir
        return tts_selected_dir, parent_dir

    return candidate_job_dir, tts_selected_dir


def find_tts_for_scene(tts_selected_dir: Path, images_dir: Path, scene_id: str, idx: int) -> Optional[Path]:
    job_tts_dir, common_tts_dir = resolve_tts_dirs(tts_selected_dir, images_dir)

    p = find_by_scene_id(job_tts_dir, scene_id, SUPPORTED_AUD_EXTS, idx)
    if p:
        return p
    p = find_by_scene_id(common_tts_dir, scene_id, SUPPORTED_AUD_EXTS, idx)
    if p:
        return p
    return None


def normalize_audio_to_wav(src: Path, dst: Path, logger=log_print) -> None:
    cmd = [
        which_ffmpeg(),
        "-y",
        "-i", str(src),
        "-ac", "2",
        "-ar", "48000",
        "-c:a", "pcm_s16le",
        str(dst)
    ]
    run_cmd(cmd, check=True, logger=logger)


def concat_wavs(wavs: List[Path], out_wav: Path, logger=log_print) -> None:
    concat_txt = out_wav.parent / "audio_concat_list.txt"
    concat_txt.write_text(
        "\n".join([f"file '{str(p).replace(chr(92), '/')}'" for p in wavs]),
        encoding="utf-8"
    )

    cmd = [
        which_ffmpeg(),
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_txt),
        "-c:a", "pcm_s16le",
        str(out_wav)
    ]
    run_cmd(cmd, check=True, logger=logger)
def resolve_font_path(font_path_str: str) -> Optional[Path]:
    """
    사용자가 지정한 폰트가 없을 때 Windows 기본 한글 폰트로 자동 대체
    """
    candidates: List[Path] = []

    if font_path_str:
        candidates.append(Path(font_path_str))

    if os.name == "nt":
        win_font_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        candidates += [
            win_font_dir / "malgun.ttf",          # 맑은 고딕
            win_font_dir / "malgunbd.ttf",
            win_font_dir / "gulim.ttc",
            win_font_dir / "batang.ttc",
            win_font_dir / "NanumGothic.ttf",
        ]

    for p in candidates:
        try:
            if p and p.exists() and p.is_file():
                return p
        except Exception:
            pass

    return None

def build_master_audio_and_timeline(
    project_dir: Path,
    json_path: Path,
    images_dir: Path,
    tts_dir: Path,
    out_timeline_path: Path,
    logger=log_print
) -> Path:
    data = load_json_schema_compliant(json_path)
    scenes = data["scenes"]
    title = data.get("title", "untitled")
    job_name = sanitize_folder_name(images_dir.name)

    temp_dir = project_dir / "temp" / job_name / "timeline_build"
    wav_dir = temp_dir / "wav"
    ensure_dir(wav_dir)
    ensure_dir(out_timeline_path.parent)

    audio_scene_rows = []
    norm_wavs: List[Path] = []

    cur = 0.0
    image_items: List[Dict[str, Any]] = []
    subtitle_items: List[Dict[str, Any]] = []

    # tts 폴더 안 wav 후보 미리 수집
    wav_candidates = sorted(list(tts_dir.glob("*.wav")))
    single_master_tts = wav_candidates[0] if len(wav_candidates) == 1 else None
    master_norm = wav_dir / "MASTER_48K_ST.wav"

    for i, scene in enumerate(scenes, start=1):
        scene_id = str(scene.get("scene_id", i))
        img = find_by_scene_id(images_dir, scene_id, SUPPORTED_IMG_EXTS, i)
        aud = find_tts_for_scene(tts_dir, images_dir, scene_id, i)

        if not img:
            raise FileNotFoundError(f"이미지 누락: scene_id={scene_id}")

        norm_wav = wav_dir / f"{i:03d}_{scene_id}.wav"

        # -------------------------------------------------
        # 1) 씬별 TTS가 있으면 그대로 사용
        # 2) 없고 tts_dir에 wav가 1개뿐이면 마스터 TTS를 씬별 분할
        #    (짧아도 부족분은 무음 패딩)
        # -------------------------------------------------
        if (aud is not None) and Path(aud).exists():
            normalize_audio_to_wav(Path(aud), norm_wav, logger=logger)
            source_audio_for_row = Path(aud)

        else:
            if single_master_tts is None:
                raise FileNotFoundError(f"TTS 누락: scene_id={scene_id}")

            # 마스터 TTS를 먼저 표준 wav로 1회 정규화
            if not master_norm.exists():
                normalize_audio_to_wav(single_master_tts, master_norm, logger=logger)

            # JSON의 start_time / end_time / duration_sec 기준으로 분할
            st = safe_float(scene.get("start_time", 0.0), 0.0)

            if scene.get("end_time") is not None:
                et = safe_float(scene.get("end_time"), 0.0)
                dur = max(0.01, et - st)
            else:
                dur = safe_float(scene.get("duration_sec", 0.0), 0.0)
                if dur <= 0:
                    dur = 1.0  # duration 정보가 전혀 없으면 최소 1초

            cut_wav_segment(master_norm, norm_wav, st, dur, logger=logger)
            source_audio_for_row = single_master_tts

        dur = ffprobe_duration_sec(norm_wav, logger=logger)

        start = cur
        end = start + dur
        cur = end

        audio_scene_rows.append({
            "scene_id": scene_id,
            "audio_path": str(source_audio_for_row),
            "normalized_wav_path": str(norm_wav),
            "duration_sec": round(dur, 3),
            "start_sec": round(start, 3),
            "end_sec": round(end, 3),
        })
        norm_wavs.append(norm_wav)

        overlay_text = scene.get("overlay_text") or ""
        overlay_pos = scene.get("overlay_position") or "top"
        subtitle_lines = scene.get("subtitle_lines") or []
        subtitle_text = "\n".join([str(x) for x in subtitle_lines if str(x).strip()]).strip()

        image_items.append({
            "id": f"img_{i}",
            "scene_id": scene_id,
            "path": str(img),
            "start_sec": round(start, 3),
            "end_sec": round(end, 3),
            "motion": normalize_motion_name(
                str(scene.get("camera_motion", {}).get("motion_type", "hold"))
                if isinstance(scene.get("camera_motion"), dict)
                else str(scene.get("camera_motion", "hold"))
            ),
            "motion_strength": 0.06,
            "fade_in_sec": DEFAULT_FADE_SEC,
            "fade_out_sec": DEFAULT_FADE_SEC,
            "layer": 1,
            "x": 0,
            "y": 0,
            "scale_mode": "cover",
        })

        if overlay_text:
            subtitle_items.append({
                "id": f"txt_overlay_{i}",
                "scene_id": scene_id,
                "kind": "overlay",
                "text": overlay_text,
                "start_sec": round(start, 3),
                "end_sec": round(min(end, start + 2.5), 3),
                "position": overlay_pos if overlay_pos in ["top", "bottom", "center", "left-top", "right-top"] else "top",
                "x_offset": 0,
                "y_offset": 0,
                "font_size": DEFAULT_OVERLAY_FONT_SIZE,
                "font_color": "#FFFFFF",
                "border_color": "#000000",
                "border_w": 4,
                "box": 0,
                "box_color": "#000000",
                "box_alpha": 0.0,
                "layer": 1,
            })

        if subtitle_text:
            subtitle_items.append({
                "id": f"txt_sub_{i}",
                "scene_id": scene_id,
                "kind": "subtitle",
                "text": subtitle_text,
                "start_sec": round(start, 3),
                "end_sec": round(end, 3),
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
            })

    master_audio = temp_dir / "master_audio.wav"
    concat_wavs(norm_wavs, master_audio, logger=logger)
    total_dur = ffprobe_duration_sec(master_audio, logger=logger)

    timeline = {
        "meta": {
            "title": title,
            "job_name": job_name,
            "project_dir": str(project_dir),
            "json_path": str(json_path),
            "images_dir": str(images_dir),
            "tts_dir": str(tts_dir),
            "master_audio_path": str(master_audio),
            "duration_sec": round(total_dur, 3),
            "width": DEFAULT_WIDTH,
            "height": DEFAULT_HEIGHT,
            "fps": DEFAULT_FPS,
            "bg_color": DEFAULT_BG_COLOR,
            "font_path": str(project_dir / "assets" / "fonts" / "KoddiUDOnGothic-ExtraBold.ttf"),
        },
        "audio_scenes": audio_scene_rows,
        "image_items": image_items,
        "subtitle_items": subtitle_items,
    }

    out_timeline_path.write_text(json.dumps(timeline, ensure_ascii=False, indent=2), encoding="utf-8")
    logger(f"[OK] timeline 생성 완료: {out_timeline_path}")
    return out_timeline_path
# 타임라인 렌더
# =========================================================

def render_timeline_to_video(
    timeline_path: Path,
    output_path: Path,
    logger=log_print
) -> Path:
    data = json.loads(timeline_path.read_text(encoding="utf-8"))
    meta = data["meta"]
    image_items = list(data.get("image_items", []))
    subtitle_items = list(data.get("subtitle_items", []))

    master_audio = Path(meta["master_audio_path"])
    if not master_audio.exists():
        raise FileNotFoundError(f"master_audio 없음: {master_audio}")

    # -----------------------------
    # 폰트 자동 대체
    # -----------------------------
    raw_font_path = str(meta.get("font_path", "")).strip()
    resolved_font = resolve_font_path(raw_font_path)
    if resolved_font is None:
        raise FileNotFoundError(
            "사용 가능한 폰트를 찾지 못했습니다.\n"
            f"지정 경로: {raw_font_path}\n"
            "메타 탭에서 폰트를 직접 지정하거나, Windows 기본 폰트(맑은 고딕)가 있는지 확인하세요."
        )

    width = int(meta.get("width", DEFAULT_WIDTH))
    height = int(meta.get("height", DEFAULT_HEIGHT))
    fps = int(meta.get("fps", DEFAULT_FPS))
    bg_color = meta.get("bg_color", DEFAULT_BG_COLOR)
    total_dur = safe_float(meta.get("duration_sec", 0))

    if total_dur <= 0:
        total_dur = ffprobe_duration_sec(master_audio, logger=logger)

    ensure_dir(output_path.parent)

    sorted_images = sorted(
        image_items,
        key=lambda x: (safe_int(x.get("layer", 1), 1), safe_float(x.get("start_sec", 0)))
    )

    inputs = [str(master_audio)]
    unique_image_paths: List[Path] = []
    path_to_input_idx: Dict[str, int] = {}

    for item in sorted_images:
        p = Path(item["path"])
        if not p.exists():
            raise FileNotFoundError(f"이미지 없음: {p}")
        sp = str(p)
        if sp not in path_to_input_idx:
            path_to_input_idx[sp] = len(inputs)
            inputs.append(sp)
            unique_image_paths.append(p)

    # -----------------------------
    # 자막 텍스트 파일 임시 저장 폴더
    # -----------------------------
    temp_text_dir = output_path.parent / "_drawtext_temp"
    ensure_dir(temp_text_dir)

    filter_parts: List[str] = []
    filter_parts.append(
        f"color=c={bg_color}:s={width}x{height}:r={fps}:d={total_dur}[base0]"
    )

    prev_label = "base0"

    # -------------------------------------------------
    # 이미지 레이어 구성
    # -------------------------------------------------
    for i, item in enumerate(sorted_images, start=1):
        path = str(item["path"])
        input_idx = path_to_input_idx[path]
        start = safe_float(item.get("start_sec"), 0.0)
        end = safe_float(item.get("end_sec"), 0.0)
        if end <= start:
            continue

        dur = end - start
        fade_in = max(0.0, min(safe_float(item.get("fade_in_sec"), DEFAULT_FADE_SEC), dur / 2))
        fade_out = max(0.0, min(safe_float(item.get("fade_out_sec"), DEFAULT_FADE_SEC), dur / 2))
        motion = normalize_motion_name(item.get("motion", "hold"))
        motion_strength = max(0.0, min(safe_float(item.get("motion_strength", 0.06), 0.06), 0.25))
        scale_mode = str(item.get("scale_mode", "cover")).strip().lower()
        x = safe_int(item.get("x", 0))
        y = safe_int(item.get("y", 0))

        src_label = f"imgsrc{i}"
        mov_label = f"imgmv{i}"
        fade_label = f"imgfd{i}"
        out_label = f"v{i}"

        if scale_mode == "contain":
            pre_scale = (
                f"[{input_idx}:v]"
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,"
                f"setsar=1"
                f"[{src_label}]"
            )
        else:
            up_w = int(width * 1.6)
            up_h = int(height * 1.6)
            pre_scale = (
                f"[{input_idx}:v]"
                f"scale={up_w}:{up_h}:force_original_aspect_ratio=increase,"
                f"crop={up_w}:{up_h},"
                f"setsar=1"
                f"[{src_label}]"
            )
        filter_parts.append(pre_scale)

        zp = build_zoompan_expr(
            motion=motion,
            duration=dur,
            fps=fps,
            out_w=width,
            out_h=height,
            intensity=motion_strength
        )

        filter_parts.append(
            f"[{src_label}]{zp},trim=duration={dur},setpts=PTS-STARTPTS+{start}/TB,format=rgba"
            f"[{mov_label}]"
        )

        fade_chain = f"[{mov_label}]"
        if fade_in > 0:
            fade_chain += f"fade=t=in:st=0:d={fade_in}:alpha=1,"
        if fade_out > 0 and dur > fade_out:
            fade_chain += f"fade=t=out:st={max(0.0, dur - fade_out)}:d={fade_out}:alpha=1,"
        fade_chain += f"format=rgba[{fade_label}]"
        filter_parts.append(fade_chain)

        overlay = (
            f"[{prev_label}][{fade_label}]"
            f"overlay=x={x}:y={y}:format=auto:shortest=0"
            f"[{out_label}]"
        )
        filter_parts.append(overlay)
        prev_label = out_label

    current_label = prev_label
    fontfile_escaped = ffmpeg_escape_path_for_filter(resolved_font)

    # -------------------------------------------------
    # 자막 / 오버레이 drawtext
    # text= 대신 textfile= 사용
    # -------------------------------------------------
    sorted_subs = sorted(
        subtitle_items,
        key=lambda x: (safe_int(x.get("layer", 1), 1), safe_float(x.get("start_sec", 0)))
    )

    for j, sub in enumerate(sorted_subs, start=1):
        text = ffmpeg_escape_text(str(sub.get("text", "")).strip())
        if not text:
            continue

        start = safe_float(sub.get("start_sec"), 0.0)
        end = safe_float(sub.get("end_sec"), 0.0)
        if end <= start:
            continue

        position = str(sub.get("position", "bottom"))
        x_offset = safe_int(sub.get("x_offset", 0))
        y_offset = safe_int(sub.get("y_offset", 0))
        font_size = max(10, safe_int(sub.get("font_size", DEFAULT_FONT_SIZE), DEFAULT_FONT_SIZE))
        font_color = str(sub.get("font_color", "#FFFFFF")).replace("#", "")
        border_color = str(sub.get("border_color", "#000000")).replace("#", "")
        border_w = max(0, safe_int(sub.get("border_w", 4), 4))
        box = 1 if safe_int(sub.get("box", 0), 0) else 0
        box_color = str(sub.get("box_color", "#000000")).replace("#", "")
        box_alpha = max(0.0, min(safe_float(sub.get("box_alpha", 0.35), 0.35), 1.0))

        enable = f"between(t,{start:.3f},{end:.3f})"

        if position == "top":
            x_expr = f"(w-text_w)/2+{x_offset}"
            y_expr = f"h*0.08+{y_offset}"
        elif position == "center":
            x_expr = f"(w-text_w)/2+{x_offset}"
            y_expr = f"(h-text_h)/2+{y_offset}"
        elif position == "left-top":
            x_expr = f"w*0.06+{x_offset}"
            y_expr = f"h*0.08+{y_offset}"
        elif position == "right-top":
            x_expr = f"w*0.94-text_w+{x_offset}"
            y_expr = f"h*0.08+{y_offset}"
        else:
            x_expr = f"(w-text_w)/2+{x_offset}"
            y_expr = f"h*0.82+{y_offset}"

        # 자막을 파일로 저장
        txt_file = temp_text_dir / f"subtitle_{j:03d}.txt"
        txt_file.write_text(text, encoding="utf-8")
        txt_file_escaped = ffmpeg_escape_path_for_filter(txt_file)

        draw = (
            f"[{current_label}]drawtext="
            f"fontfile='{fontfile_escaped}':"
            f"textfile='{txt_file_escaped}':"
            f"reload=0:"
            f"fontsize={font_size}:"
            f"fontcolor={font_color}:"
            f"borderw={border_w}:"
            f"bordercolor={border_color}:"
            f"x={x_expr}:"
            f"y={y_expr}:"
            f"line_spacing=8:"
            f"enable='{enable}'"
        )

        if box:
            draw += f":box=1:boxcolor={box_color}@{box_alpha}:boxborderw=18"

        out_label = f"txt{j}"
        draw += f"[{out_label}]"
        filter_parts.append(draw)
        current_label = out_label

    filter_complex = ";".join(filter_parts)

    cmd = [which_ffmpeg(), "-y"]
    cmd += ["-i", str(master_audio)]
    for p in unique_image_paths:
        cmd += ["-loop", "1", "-i", str(p)]

    cmd += [
        "-filter_complex", filter_complex,
        "-map", f"[{current_label}]",
        "-map", "0:a",
        "-r", str(fps),
        "-t", f"{total_dur:.3f}",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        str(output_path)
    ]

    run_cmd(cmd, check=True, logger=logger)
    logger(f"[OK] 렌더 완료: {output_path}")
    return output_path
# =========================================================
# GUI용 로그
# =========================================================

class TkTextLogger:
    def __init__(self, q: "queue.Queue[Tuple[str, Any]]"):
        self.q = q

    def __call__(self, msg: str) -> None:
        if msg is None:
            return
        self.q.put(("log", str(msg)))


# =========================================================
# GUI
# =========================================================

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


# =========================================================
# CLI
# =========================================================

def main():
    parser = argparse.ArgumentParser(description="Master Audio Timeline Shorts Editor")
    parser.add_argument("--base", type=str, default=r"P:\AI_shorts")
    parser.add_argument("--json", type=str, default=None)
    parser.add_argument("--images-dir", type=str, default=None)
    parser.add_argument("--tts-dir", type=str, default=None)
    parser.add_argument("--timeline", type=str, default=None)
    parser.add_argument("--output", type=str, default=None)
    parser.add_argument("--build-only", action="store_true")
    parser.add_argument("--render-only", action="store_true")
    parser.add_argument("--gui", action="store_true")
    args = parser.parse_args()

    if args.gui or len(sys.argv) == 1:
        root = tk.Tk()
        TimelineEditorGUI(root)
        root.mainloop()
        return

    base = Path(args.base)
    json_path = Path(args.json) if args.json else (base / "data" / "shorts.json")
    images_dir = Path(args.images_dir) if args.images_dir else (base / "assets" / "images" / "shorts" / "sample")
    tts_dir = Path(args.tts_dir) if args.tts_dir else (base / "assets" / "audio" / "tts" / "shorts")
    timeline_path = Path(args.timeline) if args.timeline else (base / "data" / "timeline.json")
    output_path = Path(args.output) if args.output else (base / "output" / "timeline_final.mp4")

    try:
        if not args.render_only:
            build_master_audio_and_timeline(
                project_dir=base,
                json_path=json_path,
                images_dir=images_dir,
                tts_dir=tts_dir,
                out_timeline_path=timeline_path,
                logger=log_print
            )
        if not args.build_only:
            render_timeline_to_video(
                timeline_path=timeline_path,
                output_path=output_path,
                logger=log_print
            )
        print(f"\n완료\nTimeline: {timeline_path}\nOutput: {output_path}")
    except Exception as e:
        print(f"[ERROR] {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
