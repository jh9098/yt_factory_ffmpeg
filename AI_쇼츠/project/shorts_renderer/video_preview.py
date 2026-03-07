from io import BytesIO
from pathlib import Path
from typing import Dict, Optional, Tuple

from PIL import Image

from .ffmpeg_tools import which_ffmpeg


class VideoFramePreviewer:
    def __init__(self, cache_limit: int = 40):
        self.cache_limit = max(1, cache_limit)
        self._cache: Dict[Tuple[str, int], Image.Image] = {}

    def extract_frame(self, video_path: Path, time_sec: float) -> Optional[Image.Image]:
        key = (str(video_path), int(max(0.0, time_sec) * 10))
        cached = self._cache.get(key)
        if cached is not None:
            return cached.copy()

        frame = self._extract_frame_uncached(video_path=video_path, time_sec=time_sec)
        if frame is None:
            return None

        if len(self._cache) >= self.cache_limit:
            first_key = next(iter(self._cache.keys()))
            self._cache.pop(first_key, None)
        self._cache[key] = frame.copy()
        return frame

    def _extract_frame_uncached(self, video_path: Path, time_sec: float) -> Optional[Image.Image]:
        import subprocess

        cmd = [
            which_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{max(0.0, time_sec):.3f}",
            "-i",
            str(video_path),
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "png",
            "-",
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if proc.returncode != 0 or not proc.stdout:
            return None
        try:
            return Image.open(BytesIO(proc.stdout)).convert("RGB")
        except Exception:
            return None

