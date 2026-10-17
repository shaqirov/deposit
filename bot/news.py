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


def add_event(path, when_text, title):
    """Добавить событие. when_text — 'ГГГГ-ММ-ДД ЧЧ:ММ' (UTC). Ошибка формата — ValueError."""
    datetime.strptime(when_text, "%Y-%m-%d %H:%M")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{when_text}  {title}\n")


def remove_event(path, index):
    """Удалить событие по номеру (с 1) в порядке файла. Возвращает удалённую строку или None."""
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    n = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        n += 1
        if n == index:
            del lines[i]
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines)
            return stripped
    return None
