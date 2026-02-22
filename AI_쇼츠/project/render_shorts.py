import argparse
import json
import os
import re
import subprocess
import sys
import threading
import queue
import traceback
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Callable, Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


# =========================================================
# Config (Windows-friendly)
# =========================================================

DEFAULT_FPS = 30

DEFAULT_FADE_SEC = 0.35
DEFAULT_BGM_VOLUME = 0.12
DEFAULT_TTS_VOLUME = 1.0
DEFAULT_TTS_END_PAD_SEC = 0.12  # TTS 끝나고 아주 살짝 여유 (자연스러운 전환용)
SUPPORTED_IMG_EXTS = [".png", ".jpg", ".jpeg", ".webp"]
SUPPORTED_AUD_EXTS = [".wav", ".mp3", ".m4a", ".aac"]


# =========================================================
# Logging / Events
# =========================================================

def default_logger(msg: str) -> None:
    print(msg)


ProgressCallback = Optional[Callable[[str, Dict[str, Any]], None]]
# event examples:
#   ("validate_progress", {"current": 3, "total": 10, "label": "영상A / scene 2"})
#   ("render_progress", {"current": 4, "total": 12, "label": "영상A / scene 4"})
#   ("batch_progress", {"current": 1, "total": 5, "label": "영상A"})
#   ("status", {"message": "..."})
#   ("job_done", {"job_name": "...", "output": "..."})
#   ("job_error", {"job_name": "...", "error": "..."})


# =========================================================
# Helpers
# =========================================================

def which_ffmpeg() -> str:
    return "ffmpeg.exe" if os.name == "nt" else "ffmpeg"


def which_ffprobe() -> str:
    return "ffprobe.exe" if os.name == "nt" else "ffprobe"


def run_cmd(
    cmd: List[str],
    check: bool = True,
    cwd: Optional[Path] = None,
    log_func: Optional[Callable[[str], None]] = None
) -> subprocess.CompletedProcess:
    log = log_func or default_logger
    log("\n[CMD] " + " ".join(cmd))

    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=False,
        cwd=str(cwd) if cwd else None,
        shell=False
    )

    try:
        out = proc.stdout.decode("utf-8", errors="replace")
    except Exception:
        out = proc.stdout.decode("cp949", errors="replace")

    if out:
        log(out.rstrip())

    if check and proc.returncode != 0:
        raise RuntimeError(f"Command failed with exit code {proc.returncode}")

    proc.stdout = out  # type: ignore
    return proc


def ffprobe_duration_sec(
    media_path: Path,
    cwd: Optional[Path] = None,
    log_func: Optional[Callable[[str], None]] = None
) -> float:
    cmd = [
        which_ffprobe(),
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(media_path)
    ]
    proc = run_cmd(cmd, check=True, cwd=cwd, log_func=log_func)
    out = (proc.stdout or "").strip()  # type: ignore
    tokens = [t for t in out.split() if t.replace(".", "", 1).isdigit()]
    if not tokens:
        raise RuntimeError(f"Could not parse duration from ffprobe output for {media_path}")
    return float(tokens[-1])


def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def sanitize_folder_name(name: str) -> str:
    if not name:
        return "untitled"
    name = name.strip()
    name = re.sub(r'[<>:"/\\|?*]+', "_", name)
    name = name.rstrip(". ").strip()
    return name or "untitled"


def scene_id_candidates(scene_id: str, idx: int) -> List[str]:
    s = str(scene_id).strip()
    cands: List[str] = []

    def add(x: str):
        x = str(x).strip()
        if x and x not in cands:
            cands.append(x)

    add(s)

    if s.isdigit():
        n = int(s)
        add(f"{n:02d}")
        add(f"{n:03d}")
        add(f"S-{n:02d}")
        add(f"S-{n:03d}")
        add(f"S{n:02d}")
        add(f"S{n:03d}")

    if s.upper().startswith("S-") and s[2:].isdigit():
        n = int(s[2:])
        add(f"{n}")
        add(f"{n:02d}")
        add(f"{n:03d}")

    add(f"S-{idx:02d}")
    add(f"{idx:02d}")
    add(f"{idx}")
    return cands


def find_by_scene_id(base_dir: Path, scene_id: str, exts: List[str], idx: int) -> Optional[Path]:
    candidates = scene_id_candidates(scene_id, idx)

    for cid in candidates:
        for ext in exts:
            candidate = base_dir / f"{cid}{ext}"
            if candidate.exists():
                return candidate

    if base_dir.exists():
        want = set([c.lower() for c in candidates])
        for fp in base_dir.iterdir():
            if not fp.is_file():
                continue
            if fp.suffix.lower() not in exts:
                continue
            stem = fp.stem.lower()
            if stem in want:
                return fp

    return None


def dir_has_audio_files(p: Path) -> bool:
    if not p.exists() or not p.is_dir():
        return False
    for fp in p.iterdir():
        if fp.is_file() and fp.suffix.lower() in SUPPORTED_AUD_EXTS:
            return True
    return False


def normalize_aspect(aspect_ratio: str) -> Tuple[int, int]:
    ar = (aspect_ratio or "").strip()
    if ar == "9:16":
        return 1080, 1920
    if ar == "16:9":
        return 1920, 1080
    return 1080, 1920


def safe_text_drawtext(s: str) -> str:
    if s is None:
        return ""
    s = str(s)
    s = s.replace("\r", " ").replace("\n", " ")
    s = s.replace("\\", "\\\\")
    s = s.replace(":", "\\:")
    s = s.replace("'", "\\'")
    s = s.replace("%", "\\%")
    return s


def fontfile_relative(project_dir: Path, font_path: Path) -> str:
    rel = font_path.relative_to(project_dir)
    return str(rel).replace("\\", "/")


def build_drawtext_filters(
    subtitle_lines: List[str],
    overlay_text: Optional[str],
    overlay_position: Optional[str],
    font_path: Path,
    w: int,
    h: int
) -> str:
    filters: List[str] = []

    if not font_path.exists():
        raise FileNotFoundError(f"Font not found: {font_path}")

    # font_path = <project>/assets/fonts/xxx.ttf  -> parents[2] == <project>
    ff_font = fontfile_relative(project_dir=font_path.parents[2], font_path=font_path)
    fontfile_part = f":fontfile='{ff_font}'"

    if overlay_text:
        t = safe_text_drawtext(overlay_text)
        pos = (overlay_position or "").lower().strip()

        if pos in ["top", "상단"]:
            x_expr, y_expr = "(w-text_w)/2", "h*0.08"
        elif pos in ["bottom", "하단"]:
            x_expr, y_expr = "(w-text_w)/2", "h*0.78"
        elif pos in ["left", "좌측", "left_top", "좌상"]:
            x_expr, y_expr = "w*0.06", "h*0.08"
        elif pos in ["right", "우측", "right_top", "우상"]:
            x_expr, y_expr = "w*0.94-text_w", "h*0.08"
        elif pos in ["center", "중앙"]:
            x_expr, y_expr = "(w-text_w)/2", "(h-text_h)/2"
        else:
            x_expr, y_expr = "(w-text_w)/2", "h*0.08"

        filters.append(
            "drawtext="
            f"text='{t}'"
            f"{fontfile_part}"
            ":fontsize=52"
            ":fontcolor=white"
            ":borderw=4"
            ":bordercolor=black@0.65"
            ":shadowx=0:shadowy=0"
            f":x={x_expr}:y={y_expr}"
        )

    lines = [ln for ln in (subtitle_lines or []) if (ln or "").strip()]
    if lines:
        lines = lines[:2]  # 최대 2줄
        base_y = int(h * 0.82)
        line_gap = 72
        ys = [int(h * 0.84)] if len(lines) == 1 else [base_y, base_y + line_gap]

        for i, ln in enumerate(lines):
            t = safe_text_drawtext(ln)
            filters.append(
                "drawtext="
                f"text='{t}'"
                f"{fontfile_part}"
                ":fontsize=54"
                ":fontcolor=white"
                ":borderw=4"
                ":bordercolor=black@0.70"
                ":box=1"
                ":boxcolor=black@0.35"
                ":boxborderw=18"
                f":x=(w-text_w)/2:y={ys[i]}"
            )

    return ",".join(filters)


def normalize_camera_motion(camera_motion) -> Dict:
    if isinstance(camera_motion, dict):
        return camera_motion

    if isinstance(camera_motion, str):
        s = camera_motion.strip().lower().replace("_", "-").replace(" ", "-")
        mapping = {
            "zoom": "zoom-in",
            "zoomin": "zoom-in",
            "zoom-in": "zoom-in",
            "zoomout": "zoom-out",
            "zoom-out": "zoom-out",
            "pan": "hold",  # 문자열 복합 모션 미지원 -> hold fallback
            "hold": "hold",
            "none": "hold",
            "정지": "hold",
            "스틸": "hold",
            "줌인": "zoom-in",
            "줌아웃": "zoom-out",
        }
        motion = mapping.get(s, "hold")
        return {"motion_type": motion, "intensity": "약"}

    return {"motion_type": "hold", "intensity": "약"}


def motion_to_zoompan(camera_motion, duration_sec: float, fps: int, w: int, h: int) -> str:
    cm = normalize_camera_motion(camera_motion)
    motion_type = str(cm.get("motion_type") or cm.get("type") or cm.get("motion") or "hold").lower().strip()
    intensity = str(cm.get("intensity") or cm.get("strength") or "약").lower().strip()
    frames = max(1, int(round(duration_sec * fps)))

    if intensity in ["강", "strong", "high"]:
        z0, z1 = 1.00, 1.10
    elif intensity in ["중", "medium", "mid"]:
        z0, z1 = 1.00, 1.08
    else:
        z0, z1 = 1.00, 1.05

    t_expr = "0" if frames <= 1 else f"(on/{frames-1})"
    ease = f"(0.5*(1-cos(PI*{t_expr})))"

    if motion_type in ["hold", "정지", "스틸", "none"]:
        z_expr = f"{z0}"
    elif motion_type in ["zoom-out", "zoomout", "줌아웃"]:
        z_expr = f"({z1} - ({z1}-{z0})*{ease})"
    else:
        z_expr = f"({z0} + ({z1}-{z0})*{ease})"

    x_expr = f"round(iw/2 - (iw/({z_expr}))/2)"
    y_expr = f"round(ih/2 - (ih/({z_expr}))/2)"
    return f"zoompan=z='{z_expr}':d={frames}:x='{x_expr}':y='{y_expr}':s={w}x{h}:fps={fps}"


def load_json_schema_compliant(json_path: Path) -> Dict:
    data = json.loads(json_path.read_text(encoding="utf-8"))

    if not isinstance(data, dict):
        raise ValueError("JSON root must be an object/dict (contains meta keys + scenes).")
    if "scenes" not in data or not isinstance(data["scenes"], list):
        raise ValueError("JSON must contain 'scenes' as a list.")

    meta_required = ["title", "target_audience", "tone", "safety_notes", "cta", "estimated_total_duration_sec"]
    missing_meta = [k for k in meta_required if k not in data]
    if missing_meta:
        raise ValueError(f"Missing meta keys in JSON: {missing_meta}")

    scene_required = [
        "scene_id", "purpose", "duration_sec", "start_time", "end_time", "tts_text", "subtitle_lines",
        "keywords", "visual_type", "image_prompt_ko", "image_prompt_en", "negative_prompt",
        "aspect_ratio", "camera_motion", "transition_to_next", "overlay_text", "overlay_position",
        "sfx_optional", "bgm_mood_optional"
    ]

    for i, sc in enumerate(data["scenes"], start=1):
        if not isinstance(sc, dict):
            raise ValueError(f"Scene #{i} must be an object/dict.")
        missing = [k for k in scene_required if k not in sc]
        if missing:
            raise ValueError(f"Scene #{i} missing required keys: {missing}")

    return data


# =========================================================
# Asset Path Resolvers (TTS selectable)
# =========================================================

def resolve_tts_dirs(tts_selected_dir: Path, images_dir: Path) -> Tuple[Path, Path]:
    """
    선택한 TTS 폴더를 기준으로 우선순위 결정.

    지원 케이스
    1) 배치/루트 선택:
       tts_selected_dir = .../assets/audio/tts/shorts
       -> 우선: .../shorts/<영상폴더명>
       -> fallback: .../shorts

    2) 단일/영상별 폴더 직접 선택:
       tts_selected_dir = .../assets/audio/tts/shorts/영상A
       -> 우선: .../영상A
       -> fallback: .../shorts (부모)
    """
    job_name = sanitize_folder_name(images_dir.name)

    # 1) 선택 폴더 하위에 영상명 폴더가 있으면 루트로 간주
    candidate_job_dir = tts_selected_dir / job_name
    if candidate_job_dir.exists() and candidate_job_dir.is_dir():
        return candidate_job_dir, tts_selected_dir

    # 2) 선택 폴더 자체에 오디오가 있으면 "영상별 폴더 직접 선택"으로 간주
    if dir_has_audio_files(tts_selected_dir):
        parent_dir = tts_selected_dir.parent if tts_selected_dir.parent.exists() else tts_selected_dir
        return tts_selected_dir, parent_dir

    # 3) 기본값: 루트처럼 취급 (아직 파일이 없을 수도 있음)
    return candidate_job_dir, tts_selected_dir


def find_tts_for_scene(
    tts_selected_dir: Path,
    images_dir: Path,
    scene_id: str,
    idx: int
) -> Tuple[Optional[Path], str]:
    """
    returns: (path, source_type)
    source_type: "job", "common", "none"
    """
    job_tts_dir, common_tts_dir = resolve_tts_dirs(tts_selected_dir, images_dir)

    p = find_by_scene_id(job_tts_dir, scene_id, SUPPORTED_AUD_EXTS, idx)
    if p:
        return p, "job"

    p = find_by_scene_id(common_tts_dir, scene_id, SUPPORTED_AUD_EXTS, idx)
    if p:
        return p, "common"

    return None, "none"


# =========================================================
# Validation
# =========================================================

def validate_assets_for_job(
    project_dir: Path,
    json_path: Path,
    images_dir: Path,
    tts_selected_dir: Path,
    log_func: Optional[Callable[[str], None]] = None,
    progress_cb: ProgressCallback = None
) -> Dict[str, Any]:
    """
    씬별 이미지/TTS 누락 검증.
    TTS는 선택한 TTS 폴더를 기준으로:
      - 영상별 폴더 우선
      - 선택 폴더 직접 파일 fallback
    """
    log = log_func or default_logger

    data = load_json_schema_compliant(json_path)
    scenes = data["scenes"]
    job_name = sanitize_folder_name(images_dir.name)

    result = {
        "ok": True,
        "job_name": job_name,
        "total_scenes": len(scenes),
        "missing_images": [],
        "missing_tts": [],
        "details": []
    }

    job_tts_dir, common_tts_dir = resolve_tts_dirs(tts_selected_dir, images_dir)

    log("\n=== 씬별 자산 검증 시작 ===")
    log(f"Job: {job_name}")
    log(f"Images Dir: {images_dir}")
    log(f"TTS Selected Dir: {tts_selected_dir}")
    log(f"TTS Job Dir (우선): {job_tts_dir}")
    log(f"TTS Common Dir (fallback): {common_tts_dir}")

    total = len(scenes)
    for i, scene in enumerate(scenes, start=1):
        scene_id = str(scene["scene_id"])
        image_path = find_by_scene_id(images_dir, scene_id, SUPPORTED_IMG_EXTS, i)
        tts_path, tts_source = find_tts_for_scene(tts_selected_dir, images_dir, scene_id, i)

        if progress_cb:
            progress_cb("validate_progress", {
                "current": i,
                "total": total,
                "label": f"{job_name} / scene {scene_id}"
            })

        row = {
            "scene_index": i,
            "scene_id": scene_id,
            "image_ok": bool(image_path),
            "image_path": str(image_path) if image_path else None,
            "tts_ok": bool(tts_path),
            "tts_path": str(tts_path) if tts_path else None,
            "tts_source": tts_source,
        }
        result["details"].append(row)

        if not image_path:
            result["missing_images"].append({"scene_index": i, "scene_id": scene_id})
        if not tts_path:
            result["missing_tts"].append({"scene_index": i, "scene_id": scene_id})

        status_img = "OK" if image_path else "MISSING"
        status_tts = f"OK({tts_source})" if tts_path else "MISSING"
        log(f"[검증] Scene {i}/{total} ({scene_id}) | IMG={status_img} | TTS={status_tts}")

    if result["missing_images"] or result["missing_tts"]:
        result["ok"] = False

    if result["ok"]:
        log(f"✅ 검증 통과: {job_name} ({total} scenes)")
    else:
        log(f"❌ 검증 실패: {job_name}")
        if result["missing_images"]:
            log(f" - 이미지 누락: {len(result['missing_images'])}개")
            for m in result["missing_images"]:
                cands = ", ".join(scene_id_candidates(str(m['scene_id']), int(m['scene_index'])))
                log(f"   · scene {m['scene_index']} ({m['scene_id']}) → {cands} + {SUPPORTED_IMG_EXTS}")
        if result["missing_tts"]:
            log(f" - TTS 누락: {len(result['missing_tts'])}개")
            for m in result["missing_tts"]:
                cands = ", ".join(scene_id_candidates(str(m['scene_id']), int(m['scene_index'])))
                log(f"   · scene {m['scene_index']} ({m['scene_id']}) → {cands} + {SUPPORTED_AUD_EXTS}")

    return result


# =========================================================
# Rendering
# =========================================================

def render_scene_clip(
    scene: Dict,
    idx: int,
    project_dir: Path,
    temp_clips_dir: Path,
    fps: int,
    images_dir: Path,
    tts_selected_dir: Optional[Path] = None,
    log_func: Optional[Callable[[str], None]] = None
) -> Tuple[Path, float]:
    log = log_func or default_logger

    scene_id = str(scene["scene_id"])
    json_duration_sec = float(scene.get("duration_sec") or 0.0)

    w, h = normalize_aspect(scene.get("aspect_ratio", "9:16"))

    font_path = project_dir / "assets" / "fonts" / "KoddiUDOnGothic-ExtraBold.ttf"
    if not font_path.exists():
        raise FileNotFoundError(f"Font not found: {font_path}")

    image_path = find_by_scene_id(images_dir, scene_id, SUPPORTED_IMG_EXTS, idx)
    if not image_path:
        cands = ", ".join(scene_id_candidates(scene_id, idx))
        raise FileNotFoundError(f"[{scene_id}] Image not found in {images_dir}. Tried: {cands} (+ .png/.jpg/...)")

    # -----------------------------------------------------
    # TTS 선택 우선순위
    # 1) GUI에서 선택한 TTS 폴더(tts_selected_dir)
    # 2) assets/audio/tts/shorts/<영상폴더명> (job dir)
    # 3) assets/audio/tts/shorts (common dir)
    # -----------------------------------------------------
    audio_path = None
    tts_source = "none"

    if tts_selected_dir and tts_selected_dir.exists():
        p = find_by_scene_id(tts_selected_dir, scene_id, SUPPORTED_AUD_EXTS, idx)
        if p:
            audio_path = p
            tts_source = "selected"

    if audio_path is None:
        # 기존 fallback 로직 유지
        p, src = find_tts_for_scene(project_dir, images_dir, scene_id, idx)
        if p:
            audio_path = p
            tts_source = src

    if not audio_path:
        cands = ", ".join(scene_id_candidates(scene_id, idx))
        job_tts_dir, common_tts_dir = resolve_tts_dirs(project_dir, images_dir)
        raise FileNotFoundError(
            f"[{scene_id}] TTS audio not found.\n"
            f" - selected dir: {tts_selected_dir}\n"
            f" - job dir: {job_tts_dir}\n"
            f" - common dir: {common_tts_dir}\n"
            f"Tried: {cands} (+ .wav/.mp3/.m4a/.aac)"
        )

    # 오디오 길이 측정
    aud_dur = ffprobe_duration_sec(audio_path, cwd=project_dir, log_func=log)
    log(f"[INFO] TTS source for scene {scene_id}: {tts_source} -> {audio_path}")
    log(f"[INFO] json duration_sec={json_duration_sec:.3f}, audio_duration={aud_dur:.3f}")

    # TTS 끊김 방지: 씬 길이 = max(JSON, TTS+여유)
    tts_end_pad = 0.12
    tts_based_min_duration = aud_dur + tts_end_pad

    if json_duration_sec <= 0:
        duration_sec = max(1.0, tts_based_min_duration)
        log(f"[INFO] duration_sec auto (tts-based) -> {duration_sec:.3f}")
    else:
        duration_sec = max(json_duration_sec, tts_based_min_duration)
        if duration_sec > json_duration_sec:
            log(f"[INFO] duration_sec extended to avoid TTS cut: {json_duration_sec:.3f} -> {duration_sec:.3f}")
        else:
            log(f"[INFO] duration_sec kept from JSON: {duration_sec:.3f}")

    subtitle_lines = scene.get("subtitle_lines") or []
    overlay_text = scene.get("overlay_text")
    overlay_position = scene.get("overlay_position")

    zoompan = motion_to_zoompan(
        scene.get("camera_motion") or {},
        duration_sec=duration_sec,
        fps=fps,
        w=w,
        h=h
    )

    up_w, up_h = int(w * 1.6), int(h * 1.6)

    vf_parts = [
        f"scale={up_w}:{up_h}:force_original_aspect_ratio=increase",
        f"crop={up_w}:{up_h}",
        zoompan
    ]

    draw = build_drawtext_filters(
        subtitle_lines=subtitle_lines,
        overlay_text=overlay_text,
        overlay_position=overlay_position,
        font_path=font_path,
        w=w, h=h
    )
    if draw:
        vf_parts.append(draw)

    vf = ",".join(vf_parts)

    out_clip = temp_clips_dir / f"{scene_id}.mp4"

    # -t: 최종 길이 고정, -af apad: 오디오 짧을 때 패딩
    cmd = [
        which_ffmpeg(),
        "-y",
        "-loop", "1",
        "-i", str(image_path),
        "-i", str(audio_path),
        "-t", f"{duration_sec:.3f}",
        "-vf", vf,
        "-af", "apad",
        "-r", str(fps),
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "192k",
        str(out_clip)
    ]

    run_cmd(cmd, check=True, cwd=project_dir, log_func=log)

    clip_dur = ffprobe_duration_sec(out_clip, cwd=project_dir, log_func=log)
    log(f"[INFO] rendered clip duration={clip_dur:.3f}s (target={duration_sec:.3f}s)")
    return out_clip, clip_dur
def concat_clips_cut(
    clips: List[Path],
    out_path: Path,
    cwd: Optional[Path] = None,
    log_func: Optional[Callable[[str], None]] = None
) -> None:
    log = log_func or default_logger

    concat_file = out_path.parent / "concat_list.txt"
    lines = []
    for c in clips:
        p = str(c).replace("\\", "/")
        lines.append(f"file '{p}'")
    concat_file.write_text("\n".join(lines), encoding="utf-8")

    cmd = [
        which_ffmpeg(),
        "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", str(concat_file),
        "-c", "copy",
        str(out_path)
    ]
    run_cmd(cmd, check=True, cwd=cwd, log_func=log)


def apply_bgm_mix(
    input_video: Path,
    bgm_path: Path,
    out_path: Path,
    bgm_volume: float,
    cwd: Optional[Path] = None,
    log_func: Optional[Callable[[str], None]] = None
) -> None:
    log = log_func or default_logger
    dur = ffprobe_duration_sec(input_video, cwd=cwd, log_func=log)

    cmd = [
        which_ffmpeg(),
        "-y",
        "-i", str(input_video),
        "-stream_loop", "-1",
        "-i", str(bgm_path),
        "-t", f"{dur:.3f}",
        "-filter_complex",
        f"[0:a]volume={DEFAULT_TTS_VOLUME}[a0];"
        f"[1:a]volume={bgm_volume}[a1];"
        f"[a0][a1]amix=inputs=2:duration=first:dropout_transition=0[aout]",
        "-map", "0:v",
        "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac",
        "-b:a", "192k",
        str(out_path)
    ]
    run_cmd(cmd, check=True, cwd=cwd, log_func=log)


def render_shorts_job(
    project_dir: Path,
    json_path: Path,
    images_dir: Path,
    tts_selected_dir: Path,
    fps: int = DEFAULT_FPS,
    no_bgm: bool = False,
    log_func: Optional[Callable[[str], None]] = None,
    progress_cb: ProgressCallback = None
) -> Path:
    log = log_func or default_logger

    if not json_path.exists():
        raise FileNotFoundError(f"JSON not found: {json_path}")
    if not images_dir.exists() or not images_dir.is_dir():
        raise FileNotFoundError(f"Image folder not found: {images_dir}")
    if not tts_selected_dir.exists() or not tts_selected_dir.is_dir():
        raise FileNotFoundError(f"TTS folder not found: {tts_selected_dir}")

    images_root = project_dir / "assets" / "images" / "shorts"
    font_path = project_dir / "assets" / "fonts" / "KoddiUDOnGothic-ExtraBold.ttf"

    if not font_path.exists():
        raise FileNotFoundError(f"Font not found: {font_path}")

    job_name = sanitize_folder_name(images_dir.name)

    try:
        images_dir.relative_to(images_root)
    except Exception:
        log(f"[WARN] 선택 이미지 폴더가 기본 경로({images_root}) 하위가 아닙니다: {images_dir}")

    temp_dir = project_dir / "temp" / job_name
    temp_clips_dir = temp_dir / "clips_shorts"
    rendered_dir = temp_dir / "rendered"
    output_dir = project_dir / "output" / job_name

    ensure_dir(temp_clips_dir)
    ensure_dir(rendered_dir)
    ensure_dir(output_dir)

    data = load_json_schema_compliant(json_path)
    title = data["title"]
    scenes = data["scenes"]

    if progress_cb:
        progress_cb("status", {"message": f"렌더 준비: {job_name}"})

    job_tts_dir, common_tts_dir = resolve_tts_dirs(tts_selected_dir, images_dir)

    log("\n=== Rendering Shorts (schema-compliant) ===")
    log(f"Project:         {project_dir}")
    log(f"JSON:            {json_path}")
    log(f"Scenes:          {len(scenes)}")
    log(f"Title:           {title}")
    log(f"Images Dir:      {images_dir}")
    log(f"TTS Selected:    {tts_selected_dir}")
    log(f"TTS Job Dir:     {job_tts_dir}")
    log(f"TTS Common Dir:  {common_tts_dir}")
    log(f"Job Name:        {job_name}")
    log(f"Output Dir:      {output_dir}")

    rendered_clips: List[Path] = []
    total_scenes = len(scenes)

    for i, scene in enumerate(scenes, start=1):
        scene_id = str(scene["scene_id"])

        if progress_cb:
            progress_cb("render_progress", {
                "current": i - 1,
                "total": total_scenes,
                "label": f"{job_name} / scene {scene_id} 준비중"
            })

        log(f"\n--- Scene {i}/{len(scenes)}: {scene_id} ---")
        clip_path, clip_dur = render_scene_clip(
            scene=scene,
            idx=i,
            project_dir=project_dir,
            temp_clips_dir=temp_clips_dir,
            fps=fps,
            images_dir=images_dir,
            tts_selected_dir=tts_selected_dir,
            log_func=log
        )
        rendered_clips.append(clip_path)
        log(f"[OK] Rendered {clip_path.name} ({clip_dur:.2f}s)")

        if progress_cb:
            progress_cb("render_progress", {
                "current": i,
                "total": total_scenes,
                "label": f"{job_name} / scene {scene_id} 완료"
            })

    concat_out = rendered_dir / "shorts_concat.mp4"
    log("\n=== Concatenating clips (cut) ===")
    concat_clips_cut(rendered_clips, concat_out, cwd=project_dir, log_func=log)

    final_out = output_dir / "shorts_final.mp4"
    bgm_path = project_dir / "assets" / "audio" / "bgm" / "shorts_bgm.mp3"

    if (not no_bgm) and bgm_path.exists():
        log("\n=== Mixing BGM ===")
        apply_bgm_mix(concat_out, bgm_path, final_out, bgm_volume=DEFAULT_BGM_VOLUME, cwd=project_dir, log_func=log)
        log(f"\n✅ Done! Output: {final_out}")
    else:
        if no_bgm:
            log("\n=== BGM disabled by user ===")
        else:
            log(f"\n=== BGM file not found, skipping === ({bgm_path})")
        cmd = [which_ffmpeg(), "-y", "-i", str(concat_out), "-c", "copy", str(final_out)]
        run_cmd(cmd, check=True, cwd=project_dir, log_func=log)
        log(f"\n✅ Done! Output: {final_out}")

    total = ffprobe_duration_sec(final_out, cwd=project_dir, log_func=log)
    log(f"Estimated final duration: {total:.2f}s")

    if progress_cb:
        progress_cb("job_done", {"job_name": job_name, "output": str(final_out)})

    return final_out


# =========================================================
# GUI Queue Logger
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

class RenderShortsGUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Shorts Renderer (Batch GUI)")
        self.root.geometry("1150x860")

        self.ui_queue: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self.worker_thread: Optional[threading.Thread] = None
        self.is_running = False

        self.base_var = tk.StringVar(value=r"D:\AI_쇼츠\project")
        self.json_var = tk.StringVar(value="")
        self.images_var = tk.StringVar(value="")  # 단일 렌더용 이미지 폴더 or 루트
        self.tts_var = tk.StringVar(value="")     # 단일 렌더용 TTS 폴더 or 루트 (NEW)
        self.fps_var = tk.StringVar(value=str(DEFAULT_FPS))
        self.no_bgm_var = tk.BooleanVar(value=False)
        self.batch_subfolders_var = tk.BooleanVar(value=True)
        self.stop_on_error_var = tk.BooleanVar(value=False)

        # Progress vars
        self.validate_prog_var = tk.DoubleVar(value=0)
        self.render_prog_var = tk.DoubleVar(value=0)
        self.batch_prog_var = tk.DoubleVar(value=0)
        self.validate_label_var = tk.StringVar(value="검증 진행률: 대기")
        self.render_label_var = tk.StringVar(value="렌더 진행률: 대기")
        self.batch_label_var = tk.StringVar(value="배치 진행률: 대기")

        self._build_ui()
        self._refresh_defaults_from_base()
        self.root.after(100, self._drain_ui_queue)

    # ---------------- UI ----------------

    def _build_ui(self) -> None:
        top = ttk.Frame(self.root, padding=10)
        top.pack(fill="x")

        # row 0: base
        ttk.Label(top, text="Project Base").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(top, textvariable=self.base_var, width=100).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Button(top, text="폴더 선택", command=self._pick_base).grid(row=0, column=2, padx=(8, 0), pady=4)

        # row 1: json
        ttk.Label(top, text="JSON 파일").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(top, textvariable=self.json_var, width=100).grid(row=1, column=1, sticky="ew", pady=4)
        ttk.Button(top, text="파일 선택", command=self._pick_json).grid(row=1, column=2, padx=(8, 0), pady=4)

        # row 2: images
        ttk.Label(top, text="이미지 폴더 (단일/루트)").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(top, textvariable=self.images_var, width=100).grid(row=2, column=1, sticky="ew", pady=4)
        ttk.Button(top, text="폴더 선택", command=self._pick_images).grid(row=2, column=2, padx=(8, 0), pady=4)

        # row 3: tts (NEW)
        ttk.Label(top, text="TTS 폴더 (단일/루트)").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(top, textvariable=self.tts_var, width=100).grid(row=3, column=1, sticky="ew", pady=4)
        ttk.Button(top, text="폴더 선택", command=self._pick_tts).grid(row=3, column=2, padx=(8, 0), pady=4)

        # options
        opt = ttk.Frame(top)
        opt.grid(row=4, column=1, sticky="w", pady=(8, 4))

        ttk.Label(opt, text="FPS").pack(side="left")
        ttk.Entry(opt, textvariable=self.fps_var, width=6).pack(side="left", padx=(6, 12))
        ttk.Checkbutton(opt, text="BGM 끄기", variable=self.no_bgm_var).pack(side="left", padx=(0, 10))
        ttk.Checkbutton(opt, text="이미지 루트 하위 폴더 전체 일괄 렌더", variable=self.batch_subfolders_var).pack(side="left", padx=(0, 10))
        ttk.Checkbutton(opt, text="배치 중 오류 시 중단", variable=self.stop_on_error_var).pack(side="left")

        # buttons
        btn = ttk.Frame(top)
        btn.grid(row=5, column=1, sticky="w", pady=(8, 4))

        self.btn_validate = ttk.Button(btn, text="씬별 검증", command=self._start_validate)
        self.btn_validate.pack(side="left")

        self.btn_render = ttk.Button(btn, text="렌더 시작 (단일)", command=self._start_render_single)
        self.btn_render.pack(side="left", padx=(8, 0))

        self.btn_batch = ttk.Button(btn, text="일괄 렌더 시작", command=self._start_render_batch)
        self.btn_batch.pack(side="left", padx=(8, 0))

        self.btn_open_output = ttk.Button(btn, text="결과 폴더 열기", command=self._open_output_folder)
        self.btn_open_output.pack(side="left", padx=(8, 0))

        ttk.Button(btn, text="로그 지우기", command=self._clear_log).pack(side="left", padx=(8, 0))
        ttk.Button(btn, text="기본경로 다시채움", command=self._refresh_defaults_from_base).pack(side="left", padx=(8, 0))

        top.columnconfigure(1, weight=1)

        # Progress panel
        prog_frame = ttk.LabelFrame(self.root, text="진행률", padding=8)
        prog_frame.pack(fill="x", padx=10, pady=(0, 8))

        ttk.Label(prog_frame, textvariable=self.validate_label_var).pack(anchor="w")
        self.validate_bar = ttk.Progressbar(prog_frame, maximum=100, variable=self.validate_prog_var)
        self.validate_bar.pack(fill="x", pady=(2, 8))

        ttk.Label(prog_frame, textvariable=self.render_label_var).pack(anchor="w")
        self.render_bar = ttk.Progressbar(prog_frame, maximum=100, variable=self.render_prog_var)
        self.render_bar.pack(fill="x", pady=(2, 8))

        ttk.Label(prog_frame, textvariable=self.batch_label_var).pack(anchor="w")
        self.batch_bar = ttk.Progressbar(prog_frame, maximum=100, variable=self.batch_prog_var)
        self.batch_bar.pack(fill="x", pady=(2, 2))

        # Log area
        log_frame = ttk.LabelFrame(self.root, text="로그", padding=8)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        self.log_text = tk.Text(log_frame, wrap="word", height=30)
        self.log_text.pack(side="left", fill="both", expand=True)

        scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        scroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=scroll.set)

        self._log("GUI 준비 완료.")

    # ---------------- basic utils ----------------

    def _log(self, msg: str) -> None:
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")
        self.root.update_idletasks()

    def _reset_progress(self) -> None:
        self.validate_prog_var.set(0)
        self.render_prog_var.set(0)
        self.batch_prog_var.set(0)
        self.validate_label_var.set("검증 진행률: 대기")
        self.render_label_var.set("렌더 진행률: 대기")
        self.batch_label_var.set("배치 진행률: 대기")

    def _set_progress(self, kind: str, current: int, total: int, label: str = "") -> None:
        pct = 0.0 if total <= 0 else (current / total) * 100.0
        if kind == "validate":
            self.validate_prog_var.set(pct)
            self.validate_label_var.set(f"검증 진행률: {current}/{total} ({pct:.1f}%) | {label}")
        elif kind == "render":
            self.render_prog_var.set(pct)
            self.render_label_var.set(f"렌더 진행률: {current}/{total} ({pct:.1f}%) | {label}")
        elif kind == "batch":
            self.batch_prog_var.set(pct)
            self.batch_label_var.set(f"배치 진행률: {current}/{total} ({pct:.1f}%) | {label}")

    def _drain_ui_queue(self) -> None:
        try:
            while True:
                typ, payload = self.ui_queue.get_nowait()

                if typ == "log":
                    self._log(str(payload))

                elif typ == "progress":
                    event_type = payload.get("event_type")
                    data = payload.get("data", {})

                    if event_type == "validate_progress":
                        self._set_progress("validate", int(data.get("current", 0)), int(data.get("total", 0)), str(data.get("label", "")))
                    elif event_type == "render_progress":
                        self._set_progress("render", int(data.get("current", 0)), int(data.get("total", 0)), str(data.get("label", "")))
                    elif event_type == "batch_progress":
                        self._set_progress("batch", int(data.get("current", 0)), int(data.get("total", 0)), str(data.get("label", "")))
                    elif event_type == "status":
                        msg = str(data.get("message", ""))
                        self._log(f"[STATUS] {msg}")

                elif typ == "done":
                    self._set_running_state(False)
                    title = payload.get("title", "완료")
                    msg = payload.get("message", "작업 완료")
                    messagebox.showinfo(title, msg)

                elif typ == "error":
                    self._set_running_state(False)
                    title = payload.get("title", "오류")
                    msg = payload.get("message", "작업 실패")
                    messagebox.showerror(title, msg)

        except queue.Empty:
            pass
        finally:
            self.root.after(100, self._drain_ui_queue)

    def _progress_cb(self, event_type: str, data: Dict[str, Any]) -> None:
        self.ui_queue.put(("progress", {"event_type": event_type, "data": data}))

    def _pick_base(self) -> None:
        start_dir = self.base_var.get().strip() or os.getcwd()
        selected = filedialog.askdirectory(title="프로젝트 베이스 폴더 선택", initialdir=start_dir)
        if selected:
            self.base_var.set(selected)
            self._refresh_defaults_from_base()

    def _pick_json(self) -> None:
        base = Path(self.base_var.get().strip() or ".")
        initial = base / "data"
        file_path = filedialog.askopenfilename(
            title="shorts JSON 파일 선택",
            initialdir=str(initial if initial.exists() else base),
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
        )
        if file_path:
            self.json_var.set(file_path)

    def _pick_images(self) -> None:
        base = Path(self.base_var.get().strip() or ".")
        initial = base / "assets" / "images" / "shorts"
        selected = filedialog.askdirectory(
            title="이미지 폴더 선택 (예: assets/images/shorts/영상명 또는 shorts 루트)",
            initialdir=str(initial if initial.exists() else base)
        )
        if selected:
            self.images_var.set(selected)

    def _pick_tts(self) -> None:
        base = Path(self.base_var.get().strip() or ".")
        initial = base / "assets" / "audio" / "tts" / "shorts"
        selected = filedialog.askdirectory(
            title="TTS 폴더 선택 (예: assets/audio/tts/shorts/영상명 또는 shorts 루트)",
            initialdir=str(initial if initial.exists() else base)
        )
        if selected:
            self.tts_var.set(selected)

    def _refresh_defaults_from_base(self) -> None:
        base = Path(self.base_var.get().strip() or ".")
        default_json = base / "data" / "shorts.json"
        default_images_root = base / "assets" / "images" / "shorts"
        default_tts_root = base / "assets" / "audio" / "tts" / "shorts"

        if default_json.exists():
            self.json_var.set(str(default_json))
        if not self.images_var.get().strip() and default_images_root.exists():
            self.images_var.set(str(default_images_root))
        if not self.tts_var.get().strip() and default_tts_root.exists():
            self.tts_var.set(str(default_tts_root))

        self._log(f"[INFO] 기본 이미지 루트: {default_images_root}")
        self._log(f"[INFO] 기본 TTS 루트: {default_tts_root}")

    def _set_running_state(self, running: bool) -> None:
        self.is_running = running
        state = "disabled" if running else "normal"
        self.btn_validate.configure(state=state)
        self.btn_render.configure(state=state)
        self.btn_batch.configure(state=state)

    def _validate_common_inputs(self, require_single_images_folder: bool = False) -> Tuple[bool, Optional[Path], Optional[Path], Optional[Path], Optional[Path], int]:
        base_text = self.base_var.get().strip()
        json_text = self.json_var.get().strip()
        images_text = self.images_var.get().strip()
        tts_text = self.tts_var.get().strip()
        fps_text = self.fps_var.get().strip()

        if not base_text:
            messagebox.showerror("오류", "Project Base 경로를 입력하세요.")
            return False, None, None, None, None, 0
        if not json_text:
            messagebox.showerror("오류", "JSON 파일을 선택하세요.")
            return False, None, None, None, None, 0
        if not images_text:
            messagebox.showerror("오류", "이미지 폴더 경로를 입력/선택하세요.")
            return False, None, None, None, None, 0
        if not tts_text:
            messagebox.showerror("오류", "TTS 폴더 경로를 입력/선택하세요.")
            return False, None, None, None, None, 0

        base = Path(base_text)
        json_path = Path(json_text)
        images_path = Path(images_text)
        tts_path = Path(tts_text)

        if not base.exists():
            messagebox.showerror("오류", f"Project Base가 존재하지 않습니다.\n{base}")
            return False, None, None, None, None, 0
        if not json_path.exists():
            messagebox.showerror("오류", f"JSON 파일이 존재하지 않습니다.\n{json_path}")
            return False, None, None, None, None, 0
        if not images_path.exists() or not images_path.is_dir():
            messagebox.showerror("오류", f"이미지 경로가 존재하지 않거나 폴더가 아닙니다.\n{images_path}")
            return False, None, None, None, None, 0
        if not tts_path.exists() or not tts_path.is_dir():
            messagebox.showerror("오류", f"TTS 경로가 존재하지 않거나 폴더가 아닙니다.\n{tts_path}")
            return False, None, None, None, None, 0

        if require_single_images_folder:
            # 필요 시 추가 규칙 적용 가능
            pass

        try:
            fps = int(fps_text)
            if fps <= 0:
                raise ValueError
        except Exception:
            messagebox.showerror("오류", "FPS는 1 이상의 정수여야 합니다.")
            return False, None, None, None, None, 0

        return True, base, json_path, images_path, tts_path, fps

    def _resolve_batch_targets(self, images_path: Path) -> List[Path]:
        """
        batch_subfolders_var=True:
            images_path를 루트로 보고 하위 '폴더들'을 대상 처리
        False:
            images_path 단일 폴더만 처리
        """
        if not self.batch_subfolders_var.get():
            return [images_path]

        children = [p for p in images_path.iterdir() if p.is_dir()]
        children.sort(key=lambda p: p.name.lower())

        filtered = []
        for p in children:
            n = p.name.strip()
            if not n:
                continue
            if n.startswith("."):
                continue
            filtered.append(p)
        return filtered

    # ---------------- Buttons ----------------

    def _start_validate(self) -> None:
        if self.is_running:
            messagebox.showinfo("안내", "작업이 진행 중입니다.")
            return

        ok, base, json_path, images_path, tts_path, _fps = self._validate_common_inputs()
        if not ok:
            return

        targets = self._resolve_batch_targets(images_path)  # type: ignore[arg-type]
        if not targets:
            messagebox.showerror("오류", "검증할 하위 영상 폴더가 없습니다.")
            return

        self._set_running_state(True)
        self._reset_progress()
        self._log("\n" + "=" * 100)
        self._log("[START] 씬별 검증 시작")
        self._log(f"대상 폴더 수: {len(targets)}")
        self._log(f"TTS 기준 폴더: {tts_path}")

        logger = TkTextLogger(self.ui_queue)

        def worker():
            try:
                total_jobs = len(targets)
                failed_jobs = []
                passed_jobs = []

                for j, images_dir in enumerate(targets, start=1):
                    job_name = sanitize_folder_name(images_dir.name)
                    self._progress_cb("batch_progress", {"current": j - 1, "total": total_jobs, "label": f"{job_name} 검증 시작"})
                    logger(f"\n{'-'*80}\n[검증 대상] {images_dir}")

                    result = validate_assets_for_job(
                        project_dir=base,         # type: ignore[arg-type]
                        json_path=json_path,      # type: ignore[arg-type]
                        images_dir=images_dir,
                        tts_selected_dir=tts_path, # type: ignore[arg-type]
                        log_func=logger,
                        progress_cb=self._progress_cb
                    )

                    if result["ok"]:
                        passed_jobs.append(job_name)
                    else:
                        failed_jobs.append(job_name)

                    self._progress_cb("batch_progress", {"current": j, "total": total_jobs, "label": f"{job_name} 검증 완료"})

                logger("\n=== 검증 요약 ===")
                logger(f"통과: {len(passed_jobs)}개")
                if passed_jobs:
                    logger(" - " + ", ".join(passed_jobs))
                logger(f"실패: {len(failed_jobs)}개")
                if failed_jobs:
                    logger(" - " + ", ".join(failed_jobs))

                self.ui_queue.put(("done", {
                    "title": "검증 완료",
                    "message": f"검증 완료\n통과: {len(passed_jobs)}개 / 실패: {len(failed_jobs)}개"
                }))
            except Exception as e:
                logger("\n[ERROR] 검증 실패")
                logger(str(e))
                logger(traceback.format_exc())
                self.ui_queue.put(("error", {
                    "title": "검증 오류",
                    "message": str(e)
                }))

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()

    def _start_render_single(self) -> None:
        if self.is_running:
            messagebox.showinfo("안내", "작업이 진행 중입니다.")
            return

        ok, base, json_path, images_path, tts_path, fps = self._validate_common_inputs(require_single_images_folder=True)
        if not ok:
            return

        target = images_path  # type: ignore[assignment]
        self._set_running_state(True)
        self._reset_progress()
        self._log("\n" + "=" * 100)
        self._log("[START] 단일 렌더 시작")
        self._log(f"Target Images: {target}")
        self._log(f"TTS Selected : {tts_path}")

        logger = TkTextLogger(self.ui_queue)

        def worker():
            try:
                # 먼저 검증
                self._progress_cb("batch_progress", {"current": 0, "total": 1, "label": "사전 검증"})
                result = validate_assets_for_job(
                    project_dir=base,          # type: ignore[arg-type]
                    json_path=json_path,       # type: ignore[arg-type]
                    images_dir=target,         # type: ignore[arg-type]
                    tts_selected_dir=tts_path, # type: ignore[arg-type]
                    log_func=logger,
                    progress_cb=self._progress_cb
                )
                if not result["ok"]:
                    raise RuntimeError(f"자산 검증 실패: {result['job_name']} (로그 확인)")

                self._progress_cb("batch_progress", {"current": 0, "total": 1, "label": "렌더링 중"})
                final_out = render_shorts_job(
                    project_dir=base,          # type: ignore[arg-type]
                    json_path=json_path,       # type: ignore[arg-type]
                    images_dir=target,         # type: ignore[arg-type]
                    tts_selected_dir=tts_path, # type: ignore[arg-type]
                    fps=fps,
                    no_bgm=self.no_bgm_var.get(),
                    log_func=logger,
                    progress_cb=self._progress_cb
                )
                self._progress_cb("batch_progress", {"current": 1, "total": 1, "label": "완료"})

                self.ui_queue.put(("done", {
                    "title": "렌더 완료",
                    "message": f"단일 렌더 완료\n{final_out}"
                }))
            except Exception as e:
                logger("\n[ERROR] 단일 렌더 실패")
                logger(str(e))
                logger(traceback.format_exc())
                self.ui_queue.put(("error", {
                    "title": "렌더 오류",
                    "message": str(e)
                }))

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()

    def _start_render_batch(self) -> None:
        if self.is_running:
            messagebox.showinfo("안내", "작업이 진행 중입니다.")
            return

        ok, base, json_path, images_path, tts_path, fps = self._validate_common_inputs()
        if not ok:
            return

        targets = self._resolve_batch_targets(images_path)  # type: ignore[arg-type]
        if not targets:
            messagebox.showerror("오류", "일괄 렌더할 하위 영상 폴더가 없습니다.")
            return

        self._set_running_state(True)
        self._reset_progress()
        self._log("\n" + "=" * 100)
        self._log("[START] 일괄 렌더 시작")
        self._log(f"대상 폴더 수: {len(targets)}")
        self._log(f"TTS 기준 폴더: {tts_path}")

        logger = TkTextLogger(self.ui_queue)

        def worker():
            try:
                total_jobs = len(targets)
                success_jobs = []
                failed_jobs = []

                for j, images_dir in enumerate(targets, start=1):
                    job_name = sanitize_folder_name(images_dir.name)
                    self._progress_cb("batch_progress", {"current": j - 1, "total": total_jobs, "label": f"{job_name} 시작"})
                    logger(f"\n{'='*90}\n[배치 {j}/{total_jobs}] {job_name}")

                    try:
                        # 1) 사전 검증
                        self._progress_cb("status", {"message": f"{job_name} 검증 중"})
                        result = validate_assets_for_job(
                            project_dir=base,          # type: ignore[arg-type]
                            json_path=json_path,       # type: ignore[arg-type]
                            images_dir=images_dir,
                            tts_selected_dir=tts_path, # type: ignore[arg-type]
                            log_func=logger,
                            progress_cb=self._progress_cb
                        )
                        if not result["ok"]:
                            raise RuntimeError(f"자산 검증 실패: {job_name}")

                        # 2) 렌더
                        self._progress_cb("status", {"message": f"{job_name} 렌더 중"})
                        final_out = render_shorts_job(
                            project_dir=base,          # type: ignore[arg-type]
                            json_path=json_path,       # type: ignore[arg-type]
                            images_dir=images_dir,
                            tts_selected_dir=tts_path, # type: ignore[arg-type]
                            fps=fps,
                            no_bgm=self.no_bgm_var.get(),
                            log_func=logger,
                            progress_cb=self._progress_cb
                        )
                        success_jobs.append((job_name, str(final_out)))
                        logger(f"[BATCH OK] {job_name} -> {final_out}")

                    except Exception as job_e:
                        failed_jobs.append((job_name, str(job_e)))
                        logger(f"[BATCH ERROR] {job_name}: {job_e}")
                        logger(traceback.format_exc())

                        if self.stop_on_error_var.get():
                            raise RuntimeError(f"배치 중단(오류 발생): {job_name} / {job_e}")

                    self._progress_cb("batch_progress", {"current": j, "total": total_jobs, "label": f"{job_name} 완료"})

                logger("\n" + "=" * 90)
                logger("=== 일괄 렌더 요약 ===")
                logger(f"성공: {len(success_jobs)}개")
                for n, outp in success_jobs:
                    logger(f" - {n}: {outp}")
                logger(f"실패: {len(failed_jobs)}개")
                for n, err in failed_jobs:
                    logger(f" - {n}: {err}")

                self.ui_queue.put(("done", {
                    "title": "일괄 렌더 완료",
                    "message": f"일괄 렌더 완료\n성공: {len(success_jobs)}개 / 실패: {len(failed_jobs)}개"
                }))

            except Exception as e:
                logger("\n[ERROR] 일괄 렌더 실패/중단")
                logger(str(e))
                logger(traceback.format_exc())
                self.ui_queue.put(("error", {
                    "title": "일괄 렌더 오류",
                    "message": str(e)
                }))

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()

    def _open_output_folder(self) -> None:
        base_text = self.base_var.get().strip()
        images_text = self.images_var.get().strip()

        if not base_text:
            messagebox.showerror("오류", "Project Base 경로가 비어 있습니다.")
            return

        base = Path(base_text)
        if images_text:
            img_path = Path(images_text)
            if img_path.exists() and img_path.is_dir():
                shorts_root = base / "assets" / "images" / "shorts"
                if img_path == shorts_root:
                    out_dir = base / "output"
                else:
                    job_name = sanitize_folder_name(img_path.name)
                    out_dir = base / "output" / job_name
            else:
                out_dir = base / "output"
        else:
            out_dir = base / "output"

        ensure_dir(out_dir)

        try:
            if os.name == "nt":
                os.startfile(str(out_dir))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(out_dir)])
            else:
                subprocess.Popen(["xdg-open", str(out_dir)])
        except Exception as e:
            messagebox.showerror("오류", f"폴더 열기 실패:\n{e}")

    def _clear_log(self) -> None:
        self.log_text.delete("1.0", "end")
        self._log("로그 초기화됨.")


# =========================================================
# CLI / GUI Entry
# =========================================================

def main_cli():
    parser = argparse.ArgumentParser(description="Auto-render shorts with tkinter GUI / batch support.")
    parser.add_argument("--base", type=str, default=r"D:\AI_쇼츠\project", help="Project base directory")
    parser.add_argument("--json", type=str, default=None, help="Path to shorts.json")
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS, help=f"FPS (default: {DEFAULT_FPS})")
    parser.add_argument("--no-bgm", action="store_true", help="Disable BGM")
    parser.add_argument("--images-dir", type=str, default=None, help="Single image folder path")
    parser.add_argument("--tts-dir", type=str, default=None, help="TTS folder path (root or per-video folder)")  # NEW
    parser.add_argument("--gui", action="store_true", help="Launch GUI")
    args = parser.parse_args()

    if args.gui or len(sys.argv) == 1:
        app = tk.Tk()
        RenderShortsGUI(app)
        app.mainloop()
        return

    # CLI fallback (단일만 지원)
    project_dir = Path(args.base)
    json_path = Path(args.json) if args.json else (project_dir / "data" / "shorts.json")
    if not args.images_dir:
        print("[ERROR] CLI 모드에서는 --images-dir 지정 필요")
        sys.exit(1)
    if not args.tts_dir:
        print("[ERROR] CLI 모드에서는 --tts-dir 지정 필요")
        sys.exit(1)

    images_dir = Path(args.images_dir)
    tts_dir = Path(args.tts_dir)

    try:
        validate_assets_for_job(
            project_dir=project_dir,
            json_path=json_path,
            images_dir=images_dir,
            tts_selected_dir=tts_dir,
            log_func=default_logger
        )
        final_out = render_shorts_job(
            project_dir=project_dir,
            json_path=json_path,
            images_dir=images_dir,
            tts_selected_dir=tts_dir,
            fps=args.fps,
            no_bgm=args.no_bgm,
            log_func=default_logger
        )
        print(f"\n✅ Final Output: {final_out}")
    except Exception as e:
        print(f"\n[ERROR] {e}")
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main_cli()