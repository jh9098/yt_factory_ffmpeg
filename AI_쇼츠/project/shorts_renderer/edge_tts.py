import asyncio
import importlib.util
import time
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
    max_retries: int = 3
    retry_delay_sec: float = 1.5


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

    retries = max(0, int(config.max_retries))
    base_delay = max(0.1, float(config.retry_delay_sec))
    last_error = None

    for attempt in range(1, retries + 2):
        try:
            _run_async(_save())
            logger(f"[OK] EdgeTTS 생성 완료: {out_path}")
            return
        except Exception as ex:
            last_error = ex
            is_retryable = "503" in str(ex) or "WSServerHandshakeError" in str(type(ex))

            if (not is_retryable) or attempt > retries:
                break

            wait_sec = base_delay * attempt
            logger(
                f"[WARN] EdgeTTS 일시 실패(재시도 {attempt}/{retries}): {ex} | "
                f"{wait_sec:.1f}초 후 재시도"
            )
            time.sleep(wait_sec)

    raise RuntimeError(
        "EdgeTTS 생성 실패: 네트워크/서비스(503) 문제로 음성 생성에 실패했습니다. "
        "잠시 후 다시 시도하거나 max_retries 값을 늘려주세요."
    ) from last_error
