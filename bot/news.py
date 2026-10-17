"""Окна вокруг важных новостей, когда новые сделки не открываются."""
import logging
import os
from datetime import datetime, timezone

log = logging.getLogger(__name__)


def load_events(path):
    """Читает файл с событиями: строки 'ГГГГ-ММ-ДД ЧЧ:ММ описание', # — комментарий."""
    if not os.path.exists(path):
        return []
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split(maxsplit=2)
            try:
                when = datetime.strptime(f"{parts[0]} {parts[1]}", "%Y-%m-%d %H:%M")
            except (ValueError, IndexError):
                log.warning("Не разобрана строка в %s: %s", path, line)
                continue
            events.append((when.replace(tzinfo=timezone.utc), parts[2] if len(parts) > 2 else ""))
    return events


def blocking_event(events, now, hours_before, hours_after):
    """Событие, в окно которого попадает now, или None."""
    for when, title in events:
        delta_h = (when - now).total_seconds() / 3600
        if -hours_after <= delta_h <= hours_before:
            return title or when.isoformat()
    return None
