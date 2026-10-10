"""Translated bot messages, loaded from ``locales/<lang>.json``.

English (``en.json``) is the source of truth and the fallback for any missing
language or key. To add a language, copy ``en.json`` to ``<code>.json`` (for
example ``fr.json``), translate the values and keep every ``{placeholder}``;
then set ``BOT_LANG=<code>``. ``tests/test_i18n.py`` checks new files for
missing keys and mismatched placeholders.
"""

from __future__ import annotations

import json
import logging
import os
from functools import cache
from importlib.resources import files

LOGGER = logging.getLogger(__name__)

DEFAULT_LANG = "en"


def available_languages() -> list[str]:
    return sorted(
        item.name.removesuffix(".json")
        for item in files("bga_turn").joinpath("locales").iterdir()
        if item.name.endswith(".json")
    )


def get_bot_lang() -> str:
    candidate = os.getenv("BOT_LANG", DEFAULT_LANG).strip().lower() or DEFAULT_LANG
    if candidate not in available_languages():
        _warn_unknown_language(candidate)
        return DEFAULT_LANG
    return candidate


@cache
def _warn_unknown_language(lang: str) -> None:
    LOGGER.warning(
        "BOT_LANG=%s has no locales/%s.json; using %s. Available: %s",
        lang,
        lang,
        DEFAULT_LANG,
        ", ".join(available_languages()),
    )


@cache
def load_messages(lang: str) -> dict[str, str]:
    raw = files("bga_turn").joinpath("locales", f"{lang}.json").read_text(encoding="utf-8")
    messages = json.loads(raw)
    if not isinstance(messages, dict):
        raise ValueError(f"locales/{lang}.json must contain a JSON object")
    return {str(key): str(value) for key, value in messages.items()}


def tr(key: str, **kwargs: object) -> str:
    lang = get_bot_lang()
    template = load_messages(lang).get(key)
    if template is None and lang != DEFAULT_LANG:
        template = load_messages(DEFAULT_LANG).get(key)
    if template is None:
        return key
    return template.format(**kwargs)
