from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from .i18n import tr

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=True, separators=(",", ":"))


def json_loads_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def json_loads_dict(raw: str | None) -> dict[str, str]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(value, dict):
        return {}
    result: dict[str, str] = {}
    for key, item in value.items():
        key_str = str(key)
        item_str = str(item).strip()
        if key_str and item_str:
            result[key_str] = item_str
    return result


def format_game_name(game_name: str | None) -> str:
    if not game_name:
        return tr("value_unknown").capitalize()
    return re.sub(r"[_-]+", " ", game_name).strip().title()
