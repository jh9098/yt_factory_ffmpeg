import os
import subprocess
from pathlib import Path
from typing import List, Optional

from .utils import ensure_dir, log_print

def which_ffmpeg() -> str:
    return "ffmpeg.exe" if os.name == "nt" else "ffmpeg"

def which_ffprobe() -> str:
    return "ffprobe.exe" if os.name == "nt" else "ffprobe"

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
