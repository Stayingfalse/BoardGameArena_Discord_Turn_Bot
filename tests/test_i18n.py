"""Keep locale files and the code in sync."""

from __future__ import annotations

import ast
import json
import string
from importlib.resources import files
from pathlib import Path

import pytest

from bga_turn import i18n

SRC = Path(i18n.__file__).parent
LOCALES = SRC / "locales"


def _messages(lang: str) -> dict[str, str]:
    return json.loads((LOCALES / f"{lang}.json").read_text(encoding="utf-8"))


def _placeholders(template: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(template) if field}


def _source() -> str:
    return "".join(path.read_text(encoding="utf-8-sig") for path in SRC.rglob("*.py"))


def _literal_tr_keys() -> set[str]:
    keys: set[str] = set()
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "tr"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                keys.add(node.args[0].value)
    return keys


def test_every_key_used_in_code_exists_in_english():
    missing = sorted(_literal_tr_keys() - set(_messages("en")))
    assert missing == []


def test_english_has_no_unused_keys():
    source = _source()
    unused = sorted(key for key in _messages("en") if f'"{key}"' not in source and f"'{key}'" not in source)
    assert unused == []


def test_english_templates_are_valid_format_strings():
    for key, template in _messages("en").items():
        _placeholders(template)  # raises ValueError on a malformed template
        assert template.strip(), key


@pytest.mark.parametrize("lang", [lang for lang in i18n.available_languages() if lang != "en"])
def test_other_languages_match_english(lang):
    english, other = _messages("en"), _messages(lang)
    assert sorted(set(other) - set(english)) == [], "keys not in en.json"
    for key, template in other.items():
        assert _placeholders(template) == _placeholders(english[key]), key


def test_unknown_language_falls_back_to_english(monkeypatch):
    monkeypatch.setenv("BOT_LANG", "xx")
    assert i18n.get_bot_lang() == "en"
    assert i18n.tr("link_status_detecting", table_id="1").startswith("🔎")


def test_language_code_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("BOT_LANG", "EN")  # value used by older .env files
    assert i18n.get_bot_lang() == "en"


def test_unknown_key_returns_the_key():
    assert i18n.tr("no_such_key") == "no_such_key"


def test_locales_are_packaged():
    assert files("bga_turn").joinpath("locales", "en.json").is_file()
