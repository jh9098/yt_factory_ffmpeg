import json
import math
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .constants import DEFAULT_BG_COLOR, DEFAULT_FADE_SEC, DEFAULT_FPS, DEFAULT_HEIGHT, DEFAULT_WIDTH
from .ffmpeg_tools import ffprobe_duration_sec, run_cmd, which_ffmpeg
from .utils import ensure_dir, log_print, normalize_motion_name, safe_float, safe_int

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
