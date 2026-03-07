import json
import os
import shutil
import tempfile
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import constants as _constants

DEFAULT_BG_COLOR = getattr(_constants, "DEFAULT_BG_COLOR", "black")
DEFAULT_FADE_SEC = getattr(_constants, "DEFAULT_FADE_SEC", 0.25)
DEFAULT_FONT_SIZE = getattr(_constants, "DEFAULT_FONT_SIZE", 54)
DEFAULT_FPS = getattr(_constants, "DEFAULT_FPS", 30)
DEFAULT_HEIGHT = getattr(_constants, "DEFAULT_HEIGHT", 1920)
DEFAULT_SCALE_MODE = getattr(_constants, "DEFAULT_SCALE_MODE", "contain")
DEFAULT_WIDTH = getattr(_constants, "DEFAULT_WIDTH", 1080)
from .ffmpeg_tools import ffprobe_duration_sec, run_cmd, which_ffmpeg
from .media_transform import normalized_crop
from .scale_mode import normalize_scale_mode
from .utils import ensure_dir, log_print, normalize_motion_name, safe_float, safe_int


def ffmpeg_escape_text(s: str) -> str:
    if s is None:
        return ""
    s = str(s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace("\ufeff", "").replace("\x00", "")
    return s


def normalize_subtitle_text(s: str) -> str:
    if s is None:
        return ""
    text = str(s)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u2028", "\n").replace("\u2029", "\n")
    # Handle timeline entries that saved escaped newline text.
    text = text.replace("\\n", "\n")
    text = text.replace("\ufeff", "").replace("\x00", "")

    sanitized_chars: List[str] = []
    for ch in text:
        if ch == "\n":
            sanitized_chars.append(ch)
            continue

        # drawtext may render unsupported control chars as "□".
        # Remove hidden/control characters so rendered output matches preview.
        if unicodedata.category(ch).startswith("C"):
            continue
        sanitized_chars.append(ch)

    normalized = "".join(sanitized_chars)
    normalized = "\n".join(line.rstrip() for line in normalized.split("\n"))
    return normalized.strip()


def ffmpeg_escape_path_for_filter(p: Path) -> str:
    s = str(p).replace("\\", "/")
    s = s.replace(":", r"\:")
    s = s.replace("'", r"\'")
    s = s.replace("[", r"\[")
    s = s.replace("]", r"\]")
    s = s.replace(",", r"\,")
    s = s.replace(";", r"\;")
    return s


def build_zoompan_expr(motion: str, duration: float, fps: int, out_w: int, out_h: int, intensity: float = 0.06) -> str:
    motion = normalize_motion_name(motion)
    frames = max(1, int(round(duration * fps)))

    z0 = 1.0
    z1 = 1.0 + max(0.0, intensity)
    t_expr = "0" if frames <= 1 else f"(on/{frames - 1})"
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


def resolve_font_path(font_path_str: str) -> Optional[Path]:
    candidates: List[Path] = []
    if font_path_str:
        candidates.append(Path(font_path_str))

    if os.name == "nt":
        win_font_dir = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
        candidates += [
            win_font_dir / "malgun.ttf",
            win_font_dir / "malgunbd.ttf",
            win_font_dir / "gulim.ttc",
            win_font_dir / "batang.ttc",
            win_font_dir / "NanumGothic.ttf",
        ]

    for p in candidates:
        try:
            if p.exists() and p.is_file():
                return p
        except Exception:
            pass
    return None


def prepare_filter_asset_dir(output_path: Path) -> Path:
    candidates: List[Path] = []

    if os.name == "nt":
        system_root = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
        candidates.append(system_root / "Temp" / "shorts_renderer_drawtext")
        candidates.append(Path(r"C:\Temp\shorts_renderer_drawtext"))

    candidates.append(output_path.parent / "_drawtext_temp")
    candidates.append(Path(tempfile.gettempdir()) / "shorts_renderer_drawtext")

    for base in candidates:
        try:
            base.mkdir(parents=True, exist_ok=True)
            if all(ord(ch) < 128 for ch in str(base)):
                return base
        except Exception:
            continue

    fallback = output_path.parent / "_drawtext_temp"
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _is_video_item(item: Dict[str, Any]) -> bool:
    kind = str(item.get("type", "")).strip().lower()
    if kind == "video":
        return True
    if kind == "image":
        return False
    ext = Path(str(item.get("path", ""))).suffix.lower()
    return ext in {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}


def _to_media_items(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    media_items = list(data.get("media_items", []))
    if media_items:
        return media_items

    # Backward compatibility with old schema.
    legacy = []
    for idx, item in enumerate(data.get("image_items", []), start=1):
        copied = dict(item)
        copied["id"] = copied.get("id") or f"media_{idx}"
        copied["type"] = "image"
        copied["clip_in_sec"] = safe_float(copied.get("clip_in_sec", 0.0), 0.0)
        duration = max(0.0, safe_float(copied.get("end_sec", 0.0), 0.0) - safe_float(copied.get("start_sec", 0.0), 0.0))
        copied["clip_out_sec"] = safe_float(copied.get("clip_out_sec", duration), duration)
        copied["scale_mode"] = normalize_scale_mode(copied.get("scale_mode", DEFAULT_SCALE_MODE))
        legacy.append(copied)
    return legacy


def _build_source_scaler(input_label: str, out_label: str, width: int, height: int, scale_mode: str, item: Dict[str, Any]) -> str:
    scale_mode = normalize_scale_mode(scale_mode)
    crop_x, crop_y, crop_w, crop_h = normalized_crop(item)
    crop_expr = (
        f"crop=w='iw*{crop_w:.6f}':h='ih*{crop_h:.6f}':"
        f"x='iw*{crop_x:.6f}':y='ih*{crop_y:.6f}',"
    )
    if scale_mode == "contain":
        return (
            f"[{input_label}]{crop_expr}scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,setsar=1[{out_label}]"
        )

    up_w = int(width * 1.6)
    up_h = int(height * 1.6)
    return (
        f"[{input_label}]{crop_expr}scale={up_w}:{up_h}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1[{out_label}]"
    )


def render_timeline_to_video(timeline_path: Path, output_path: Path, logger=log_print) -> Path:
    data = json.loads(timeline_path.read_text(encoding="utf-8"))
    meta = data["meta"]
    subtitle_items = list(data.get("subtitle_items", []))
    media_items = _to_media_items(data)
    bgm_items = list(data.get("bgm_items", []))

    master_audio = Path(meta["master_audio_path"])
    if not master_audio.exists():
        raise FileNotFoundError(f"master_audio not found: {master_audio}")

    raw_font_path = str(meta.get("font_path", "")).strip()
    resolved_font = resolve_font_path(raw_font_path)
    if resolved_font is None:
        raise FileNotFoundError(f"font not found: {raw_font_path}")

    width = int(meta.get("width", DEFAULT_WIDTH))
    height = int(meta.get("height", DEFAULT_HEIGHT))
    fps = int(meta.get("fps", DEFAULT_FPS))
    bg_color = meta.get("bg_color", DEFAULT_BG_COLOR)
    total_dur = safe_float(meta.get("duration_sec", 0))
    if total_dur <= 0:
        total_dur = ffprobe_duration_sec(master_audio, logger=logger)

    ensure_dir(output_path.parent)

    sorted_media = sorted(media_items, key=lambda x: (safe_int(x.get("layer", 1), 1), safe_float(x.get("start_sec", 0))))

    source_map: Dict[Tuple[str, str], int] = {}
    source_list: List[Tuple[str, str]] = []
    next_input_idx = 1

    for item in sorted_media:
        p = Path(str(item.get("path", "")))
        if not p.exists():
            raise FileNotFoundError(f"media source not found: {p}")
        key = (str(p), "video" if _is_video_item(item) else "image")
        if key not in source_map:
            source_map[key] = next_input_idx
            source_list.append(key)
            next_input_idx += 1

    bgm_source_map: Dict[str, int] = {}
    bgm_source_list: List[str] = []
    for item in bgm_items:
        p = Path(str(item.get("path", "")))
        if not p.exists():
            raise FileNotFoundError(f"bgm source not found: {p}")
        sp = str(p)
        if sp not in bgm_source_map:
            bgm_source_map[sp] = next_input_idx
            bgm_source_list.append(sp)
            next_input_idx += 1

    temp_text_dir = prepare_filter_asset_dir(output_path)
    ensure_dir(temp_text_dir)

    temp_font_path = temp_text_dir / f"font_{os.getpid()}.ttf"
    shutil.copy2(resolved_font, temp_font_path)

    filter_parts: List[str] = [f"color=c={bg_color}:s={width}x{height}:r={fps}:d={total_dur}[base0]"]
    prev_label = "base0"

    for i, item in enumerate(sorted_media, start=1):
        path = str(item.get("path", ""))
        kind = "video" if _is_video_item(item) else "image"
        input_idx = source_map[(path, kind)]

        start = safe_float(item.get("start_sec"), 0.0)
        end = safe_float(item.get("end_sec"), 0.0)
        if end <= start:
            continue

        dur = end - start
        fade_in = max(0.0, min(safe_float(item.get("fade_in_sec"), DEFAULT_FADE_SEC), dur / 2))
        fade_out = max(0.0, min(safe_float(item.get("fade_out_sec"), DEFAULT_FADE_SEC), dur / 2))
        scale_mode = normalize_scale_mode(item.get("scale_mode", DEFAULT_SCALE_MODE))
        x = safe_int(item.get("x", 0))
        y = safe_int(item.get("y", 0))
        crop_x, crop_y, crop_w, crop_h = normalized_crop(item)

        logger(
            "[DEBUG] media_item "
            f"id={item.get('id', i)} type={kind} scale_mode={scale_mode} "
            f"crop=({crop_x:.3f},{crop_y:.3f},{crop_w:.3f},{crop_h:.3f})"
        )

        src_label = f"src{i}"
        scaled_label = f"scaled{i}"
        mov_label = f"mov{i}"
        fade_label = f"fade{i}"
        timed_label = f"timed{i}"
        out_label = f"v{i}"

        if kind == "image":
            filter_parts.append(f"[{input_idx}:v]format=rgba[{src_label}]")
            filter_parts.append(_build_source_scaler(src_label, scaled_label, width, height, scale_mode, item))
            motion = normalize_motion_name(item.get("motion", "hold"))
            motion_strength = max(0.0, min(safe_float(item.get("motion_strength", 0.06), 0.06), 0.25))
            zp = build_zoompan_expr(motion=motion, duration=dur, fps=fps, out_w=width, out_h=height, intensity=motion_strength)
            filter_parts.append(
                f"[{scaled_label}]{zp},trim=duration={dur},setpts=PTS-STARTPTS,format=rgba[{mov_label}]"
            )
        else:
            clip_in = max(0.0, safe_float(item.get("clip_in_sec", 0.0), 0.0))
            clip_out = safe_float(item.get("clip_out_sec", clip_in + dur), clip_in + dur)
            clip_out = max(clip_in + 0.05, clip_out)
            if (clip_out - clip_in) < dur:
                clip_out = clip_in + dur

            filter_parts.append(
                f"[{input_idx}:v]trim=start={clip_in:.3f}:end={clip_out:.3f},setpts=PTS-STARTPTS,fps={fps}[{src_label}]"
            )
            filter_parts.append(_build_source_scaler(src_label, scaled_label, width, height, scale_mode, item))
            filter_parts.append(
                f"[{scaled_label}]trim=duration={dur:.3f},setpts=PTS-STARTPTS,format=rgba[{mov_label}]"
            )

        fade_chain = f"[{mov_label}]"
        if fade_in > 0:
            fade_chain += f"fade=t=in:st=0:d={fade_in}:alpha=1,"
        if fade_out > 0 and dur > fade_out:
            fade_chain += f"fade=t=out:st={max(0.0, dur - fade_out)}:d={fade_out}:alpha=1,"
        fade_chain += f"format=rgba[{fade_label}]"
        filter_parts.append(fade_chain)

        filter_parts.append(f"[{fade_label}]setpts=PTS+{start}/TB[{timed_label}]")
        overlay_enable = f"between(t,{start:.3f},{end:.3f})"
        filter_parts.append(
            f"[{prev_label}][{timed_label}]overlay=x={x}:y={y}:format=auto:shortest=0:eof_action=pass:repeatlast=0:enable='{overlay_enable}'[{out_label}]"
        )
        prev_label = out_label

    current_label = prev_label
    fontfile_escaped = ffmpeg_escape_path_for_filter(temp_font_path)

    sorted_subs = sorted(subtitle_items, key=lambda x: (safe_int(x.get("layer", 1), 1), safe_float(x.get("start_sec", 0))))

    for j, sub in enumerate(sorted_subs, start=1):
        text = normalize_subtitle_text(str(sub.get("text", "")).strip())
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

        txt_file = temp_text_dir / f"subtitle_{j:03d}.txt"
        with open(txt_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        txt_file_escaped = ffmpeg_escape_path_for_filter(txt_file)

        draw = (
            f"[{current_label}]drawtext="
            f"fontfile='{fontfile_escaped}':"
            f"textfile='{txt_file_escaped}':"
            f"reload=0:"
            f"expansion=none:"
            f"fontsize={font_size}:"
            f"fontcolor={font_color}:"
            f"borderw={border_w}:"
            f"bordercolor={border_color}:"
            f"x={x_expr}:"
            f"y={y_expr}:"
            f"line_spacing=14:"
            f"enable='{enable}'"
        )

        if box:
            draw += f":box=1:boxcolor={box_color}@{box_alpha}:boxborderw=18"

        out_label = f"txt{j}"
        draw += f"[{out_label}]"
        filter_parts.append(draw)
        current_label = out_label

    audio_mix_labels: List[str] = []
    sorted_bgm = sorted(bgm_items, key=lambda x: safe_float(x.get("start_sec", 0.0), 0.0))
    for k, bgm in enumerate(sorted_bgm, start=1):
        path = str(bgm.get("path", ""))
        if not path:
            continue
        input_idx = bgm_source_map[path]
        start = max(0.0, safe_float(bgm.get("start_sec", 0.0), 0.0))
        end = max(start + 0.01, safe_float(bgm.get("end_sec", start + 0.01), start + 0.01))
        dur = end - start
        clip_in = max(0.0, safe_float(bgm.get("clip_in_sec", 0.0), 0.0))
        clip_out = max(clip_in + 0.01, safe_float(bgm.get("clip_out_sec", clip_in + dur), clip_in + dur))
        if (clip_out - clip_in) < dur:
            clip_out = clip_in + dur
        volume = max(0.0, safe_float(bgm.get("volume", 0.35), 0.35))
        delay_ms = int(round(start * 1000))
        label = f"bgma{k}"

        filter_parts.append(
            f"[{input_idx}:a]atrim=start={clip_in:.3f}:end={clip_out:.3f},"
            f"asetpts=PTS-STARTPTS,volume={volume:.4f},adelay={delay_ms}|{delay_ms},"
            f"atrim=duration={total_dur:.3f},aresample=async=1[{label}]"
        )
        audio_mix_labels.append(label)

    audio_map_ref = "0:a"
    if audio_mix_labels:
        mix_input = "[0:a]" + "".join([f"[{x}]" for x in audio_mix_labels])
        filter_parts.append(
            f"{mix_input}amix=inputs={1 + len(audio_mix_labels)}:duration=longest:dropout_transition=2[aout]"
        )
        audio_map_ref = "[aout]"

    filter_complex = ";".join(filter_parts)

    cmd = [which_ffmpeg(), "-y", "-i", str(master_audio)]
    for p, kind in source_list:
        if kind == "image":
            cmd += ["-loop", "1", "-i", p]
        else:
            cmd += ["-i", p]
    for p in bgm_source_list:
        cmd += ["-i", p]

    cmd += [
        "-filter_complex",
        filter_complex,
        "-map",
        f"[{current_label}]",
        "-map",
        audio_map_ref,
        "-r",
        str(fps),
        "-t",
        f"{total_dur:.3f}",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        str(output_path),
    ]

    run_cmd(cmd, check=True, logger=logger)
    logger(f"[OK] Render done: {output_path}")
    return output_path


def render_timeline_service(timeline_path: Path, output_path: Path, logger=log_print) -> Path:
    return render_timeline_to_video(timeline_path=timeline_path, output_path=output_path, logger=logger)
