"""Minimal RFC 6902 JSON Patch, as boardgame.io sends in ``patch`` events.

Spectators receive a redacted state, so a patch can remove or replace keys we
never had (the log plugin's per-move ``metadata``, for example). Those are
skipped instead of failing: consistency is checked by ``_stateID`` instead.
"""

from __future__ import annotations

import copy
from typing import Any


class JsonPatchError(ValueError):
    pass


def apply_patch(document: Any, operations: list[dict[str, Any]]) -> Any:
    """Apply *operations* to *document* in place and return it."""
    for operation in operations:
        op = operation.get("op")
        path = operation.get("path", "")
        if op in ("add", "replace"):
            document = _set(document, path, copy.deepcopy(operation.get("value")), insert=op == "add")
        elif op == "remove":
            document = _remove(document, path)
        elif op in ("move", "copy"):
            value = _get(document, operation.get("from", ""))
            if op == "move":
                document = _remove(document, operation["from"])
            document = _set(document, path, copy.deepcopy(value), insert=True)
        elif op == "test":
            continue
        else:
            raise JsonPatchError(f"Unsupported JSON Patch op: {op!r}")
    return document


def _tokens(path: str) -> list[str]:
    if path == "":
        return []
    if not path.startswith("/"):
        raise JsonPatchError(f"Invalid JSON pointer: {path!r}")
    return [token.replace("~1", "/").replace("~0", "~") for token in path[1:].split("/")]


def _parent(document: Any, tokens: list[str]) -> Any | None:
    node = document
    for token in tokens[:-1]:
        if isinstance(node, dict):
            node = node.get(token)
        elif isinstance(node, list) and token.isdigit() and int(token) < len(node):
            node = node[int(token)]
        else:
            return None
    return node


def _get(document: Any, path: str) -> Any:
    tokens = _tokens(path)
    parent = _parent(document, tokens) if tokens else None
    if not tokens:
        return document
    last = tokens[-1]
    if isinstance(parent, dict) and last in parent:
        return parent[last]
    if isinstance(parent, list) and last.isdigit() and int(last) < len(parent):
        return parent[int(last)]
    raise JsonPatchError(f"Path not found: {path!r}")


def _set(document: Any, path: str, value: Any, *, insert: bool) -> Any:
    tokens = _tokens(path)
    if not tokens:
        return value
    parent = _parent(document, tokens)
    last = tokens[-1]
    if isinstance(parent, dict):
        parent[last] = value
    elif isinstance(parent, list):
        if last == "-":
            parent.append(value)
        elif last.isdigit():
            index = int(last)
            if insert and index <= len(parent):
                parent.insert(index, value)
            elif index < len(parent):
                parent[index] = value
            elif index == len(parent):
                parent.append(value)
            else:
                raise JsonPatchError(f"Index out of range: {path!r}")
        else:
            raise JsonPatchError(f"Invalid list index: {path!r}")
    else:
        raise JsonPatchError(f"Parent not found: {path!r}")
    return document


def _remove(document: Any, path: str) -> Any:
    tokens = _tokens(path)
    parent = _parent(document, tokens)
    last = tokens[-1] if tokens else ""
    if isinstance(parent, dict):
        parent.pop(last, None)
    elif isinstance(parent, list) and last.isdigit() and int(last) < len(parent):
        del parent[int(last)]
    return document
