import re
from pathlib import Path
from typing import Any


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


def seconds_to_hms(sec: float) -> str:
    if sec < 0:
        sec = 0
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"
