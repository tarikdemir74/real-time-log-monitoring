import re
from datetime import datetime, timedelta

WINDOW_MINUTES = 1

_EXCESS_FRACTION_RE = re.compile(r"(\.\d{6})\d+")


def parse_timestamp(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    value = _EXCESS_FRACTION_RE.sub(r"\1", value)
    return datetime.fromisoformat(value)


def compute_window(timestamp: str):
    ts = parse_timestamp(timestamp)
    window_start = ts.replace(second=0, microsecond=0)
    window_end = window_start + timedelta(minutes=WINDOW_MINUTES)
    return ts, window_start, window_end
