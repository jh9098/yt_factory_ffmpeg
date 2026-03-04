from typing import Any, Dict, Tuple

from .utils import safe_float


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def normalized_crop(item: Dict[str, Any]) -> Tuple[float, float, float, float]:
    """Return crop rect as normalized values (x, y, w, h)."""
    cx = _clamp(safe_float(item.get("crop_x", 0.0), 0.0), 0.0, 1.0)
    cy = _clamp(safe_float(item.get("crop_y", 0.0), 0.0), 0.0, 1.0)
    cw = _clamp(safe_float(item.get("crop_w", 1.0), 1.0), 0.05, 1.0)
    ch = _clamp(safe_float(item.get("crop_h", 1.0), 1.0), 0.05, 1.0)

    if cx + cw > 1.0:
        cx = 1.0 - cw
    if cy + ch > 1.0:
        cy = 1.0 - ch
    return cx, cy, cw, ch
