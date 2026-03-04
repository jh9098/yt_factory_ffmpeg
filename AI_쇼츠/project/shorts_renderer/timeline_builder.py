import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .constants import (
    DEFAULT_BG_COLOR,
    DEFAULT_FADE_SEC,
    DEFAULT_FPS,
    DEFAULT_FONT_SIZE,
    DEFAULT_HEIGHT,
    DEFAULT_OVERLAY_FONT_SIZE,
    DEFAULT_WIDTH,
    SUPPORTED_AUD_EXTS,
    SUPPORTED_IMG_EXTS,
)
from .ffmpeg_tools import (
    concat_wavs,
    cut_wav_segment,
    ffprobe_duration_sec,
    normalize_audio_to_wav,
)
from .utils import ensure_dir, log_print, normalize_motion_name, safe_float, sanitize_folder_name

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


def build_timeline_service(
    project_dir: Path,
    json_path: Path,
    images_dir: Path,
    tts_dir: Path,
    out_timeline_path: Path,
    logger=log_print
) -> Path:
    """GUI/CLI에서 공통으로 호출하는 타임라인 빌드 서비스 함수"""
    return build_master_audio_and_timeline(
        project_dir=project_dir,
        json_path=json_path,
        images_dir=images_dir,
        tts_dir=tts_dir,
        out_timeline_path=out_timeline_path,
        logger=logger
    )
