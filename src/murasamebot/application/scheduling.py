from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


def next_window(now: float, timezone: str, windows: tuple[tuple[str, str], ...], weekdays: tuple[int, ...]) -> float:
    if not windows or not weekdays:
        raise ValueError("没有可用浏览时段或星期")
    local = datetime.fromtimestamp(now, ZoneInfo(timezone))
    candidates = []
    # Yesterday matters for a window crossing midnight.
    for offset in range(-1, 9):
        day = local.date() + timedelta(days=offset)
        if day.weekday() not in weekdays:
            continue
        for start_text, end_text in windows:
            start = datetime.combine(day, time.fromisoformat(start_text), local.tzinfo)
            end = datetime.combine(day, time.fromisoformat(end_text), local.tzinfo)
            if end <= start:
                end += timedelta(days=1)
            if start <= local < end:
                return now
            if start > local:
                candidates.append(start.timestamp())
    if not candidates:
        raise ValueError("接下来一周没有可用时段")
    return min(candidates)


def day_start(now: float, timezone: str) -> float:
    dt = datetime.fromtimestamp(now, ZoneInfo(timezone))
    return datetime.combine(dt.date(), time.min, dt.tzinfo).timestamp()
