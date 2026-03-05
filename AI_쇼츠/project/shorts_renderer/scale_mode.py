from .constants import DEFAULT_SCALE_MODE, SCALE_MODE_OPTIONS


SCALE_MODE_DISPLAY_LABELS = {
    "contain": "전체 표시(잘림 없음=contain)",
    "cover": "화면 채우기(일부 잘림=cover)",
}


def normalize_scale_mode(value: object) -> str:
    mode = str(value or "").strip().lower()
    if mode not in SCALE_MODE_OPTIONS:
        return DEFAULT_SCALE_MODE
    return mode


def to_scale_mode_display(mode: object) -> str:
    normalized = normalize_scale_mode(mode)
    return SCALE_MODE_DISPLAY_LABELS.get(normalized, SCALE_MODE_DISPLAY_LABELS[DEFAULT_SCALE_MODE])


def parse_scale_mode_input(raw: object) -> str:
    value = str(raw or "").strip()
    if "=" in value:
        value = value.split("=", 1)[-1].strip().lower().rstrip(")")
    return normalize_scale_mode(value)
