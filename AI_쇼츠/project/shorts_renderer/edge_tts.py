import asyncio
import importlib.util
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass
class EdgeTTSConfig:
    enabled: bool = False
    voice: str = "ko-KR-SunHiNeural"
    rate: str = "+0%"
    volume: str = "+0%"
    overwrite: bool = False


def is_edge_tts_available() -> bool:
    return importlib.util.find_spec("edge_tts") is not None


def _run_async(coro):
    try:
        asyncio.run(coro)
        return
    except RuntimeError as ex:
        if "asyncio.run() cannot be called" not in str(ex):
            raise

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()


def synthesize_text_to_file(
    text: str,
    out_path: Path,
    config: EdgeTTSConfig,
    logger: Callable[[str], None],
) -> None:
    if not text.strip():
        raise ValueError("EdgeTTS 입력 텍스트가 비어 있습니다.")
    if not is_edge_tts_available():
        raise ModuleNotFoundError("edge-tts 패키지가 설치되어 있지 않습니다. (pip install edge-tts)")

    import edge_tts

    async def _save():
        communicate = edge_tts.Communicate(
            text=text,
            voice=config.voice,
            rate=config.rate,
            volume=config.volume,
        )
        await communicate.save(str(out_path))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    logger(f"[INFO] EdgeTTS 생성 시작: {out_path.name} | voice={config.voice}")
    _run_async(_save())
    logger(f"[OK] EdgeTTS 생성 완료: {out_path}")
