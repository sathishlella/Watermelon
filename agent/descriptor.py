"""Interprets a declarative API capability descriptor against the real API.

A descriptor is the synthesised artefact — a value-independent recipe for one
GitHub REST call:

    {
      "method": "POST",
      "path":   "/repos/{owner}/{repo}/issues/{issue_number}/labels",
      "query":  ["per_page"],            # arg names copied into the query string
      "body":   {"labels": "labels"},    # api_field -> arg name
      "extract": null                    # optional dotted path into the response
    }

Both the synthesiser (to test a freshly built capability) and the executor (to
run a known one) use `run_descriptor`. Keeping it declarative means we never
exec LLM-generated Python — synthesis is real but the execution surface is fixed
and safe.
"""
from __future__ import annotations

import re
from typing import Any

_TOKEN = re.compile(r"^\{([a-zA-Z0-9_]+)\}$")


def _sub_scalar(template: str, scope: dict) -> Any:
    """If the template is exactly '{name}', return the raw object (keeps lists/ints).
    Otherwise do string interpolation of any {name} tokens found."""
    m = _TOKEN.match(template)
    if m:
        return scope.get(m.group(1))
    return re.sub(r"\{([a-zA-Z0-9_]+)\}", lambda mo: str(scope.get(mo.group(1), "")),
                  template)


def fill_path(path: str, scope: dict) -> str:
    missing = [m.group(1) for m in re.finditer(r"\{([a-zA-Z0-9_]+)\}", path)
               if scope.get(m.group(1)) is None]
    if missing:
        raise KeyError(f"path {path!r} missing values for {missing}")
    return re.sub(r"\{([a-zA-Z0-9_]+)\}", lambda mo: str(scope[mo.group(1)]), path)


def build_query(query_names: list[str], args: dict) -> dict:
    return {k: args[k] for k in (query_names or []) if args.get(k) is not None}


def build_body(body_map: dict, args: dict) -> dict | None:
    """Map API body fields to argument values.

    A *string* value is, by convention, the NAME of an argument (e.g.
    {"labels": "labels"}) or a braced template ({"sha": "{sha}"}). If a named
    arg wasn't supplied, the field is OMITTED — never sent as the literal field
    name. A *non-string* value is a literal default.
    """
    if not body_map:
        return None
    body: dict[str, Any] = {}
    for api_field, spec in body_map.items():
        if isinstance(spec, str):
            if spec in args:
                val = args[spec]
            elif "{" in spec:
                val = _sub_scalar(spec, args)
            else:
                continue  # arg not provided -> omit this optional field
        else:
            val = spec  # literal default
        if val not in (None, "", [], {}):
            body[api_field] = val
    return body or None


def extract(data: Any, path: str | None) -> Any:
    if not path:
        return data
    cur = data
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


def run_descriptor(github, descriptor: dict, args: dict) -> Any:
    """Execute one descriptor. Raises GitHubError on API failure."""
    scope = {**github.context(), **args}
    path = fill_path(descriptor["path"], scope)
    query = build_query(descriptor.get("query", []), args)
    body = build_body(descriptor.get("body", {}), args)
    data = github.request(descriptor["method"], path, params=query or None, json=body)
    return extract(data, descriptor.get("extract"))
