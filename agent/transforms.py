"""A small, safe, in-process transform engine.

Data-shaping steps (filter / group / count / extract / format) run here with no
API cost and no `exec` of generated code. The set is fixed but composable, which
is how the agent handles "group these by label and summarise" without an API
for it.
"""
from __future__ import annotations

import json
import re
from typing import Any


def get_field(item: Any, path: str):
    """Resolve a dotted path. A list-valued hop fans out, so e.g. 'labels.name'
    on a GitHub issue returns ['bug', 'p1']."""
    cur: Any = item
    for part in path.split("."):
        if isinstance(cur, list):
            cur = [(_get(x, part)) for x in cur]
            cur = _flatten(cur)
        else:
            cur = _get(cur, part)
    return cur


def _get(obj, key):
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _flatten(values):
    out = []
    for v in values:
        if isinstance(v, list):
            out.extend(v)
        elif v is not None:
            out.append(v)
    return out


def _as_keys(value) -> list:
    if value is None:
        return ["(none)"]
    if isinstance(value, list):
        return value or ["(none)"]
    return [value]


def run_transform(name: str, args: dict) -> Any:
    if name == "filter":
        return _filter(args["items"], args["where"])
    if name == "group_by":
        return _group_by(args["items"], args["field"])
    if name == "count_by":
        groups = _group_by(args["items"], args["field"])
        return {k: len(v) for k, v in groups.items()}
    if name == "extract_field":
        return [get_field(it, args["field"]) for it in args["items"]]
    if name == "format_template":
        return _format(args["template"], args.get("data", {}))
    raise ValueError(f"Unknown transform {name!r}")


def _filter(items: list, where: dict) -> list:
    field, op, value = where.get("field"), where.get("op"), where.get("value")
    out = []
    for it in items:
        actual = get_field(it, field) if field else it
        if _match(actual, op, value):
            out.append(it)
    return out


def _match(actual, op, value) -> bool:
    if op == "eq":
        return actual == value
    if op == "ne":
        return actual != value
    if op == "in":
        return actual in (value or [])
    if op == "contains":
        try:
            return value in actual
        except TypeError:
            return False
    if op in ("empty",):
        return actual in (None, "", [], {})
    if op in ("not_empty", "exists"):
        return actual not in (None, "", [], {})
    raise ValueError(f"Unknown filter op {op!r}")


def _group_by(items: list, field: str) -> dict:
    groups: dict[str, list] = {}
    for it in items:
        for key in _as_keys(get_field(it, field)):
            groups.setdefault(str(key), []).append(it)
    return groups


def _format(template: str, data: dict) -> str:
    """Substitute only simple {name} placeholders that exist in `data`.

    We do NOT use str.format_map: real templates often contain JSON braces or
    colons (e.g. "{count}: ...") which the strict formatter rejects with
    "Invalid format specifier". A targeted regex replace is robust to that.
    """
    rendered = {
        k: (json.dumps(v, indent=2) if isinstance(v, (dict, list)) else str(v))
        for k, v in data.items()
    }
    return re.sub(
        r"\{([A-Za-z_][A-Za-z0-9_]*)\}",
        lambda m: rendered.get(m.group(1), m.group(0)),  # leave unknown as-is
        template,
    )
