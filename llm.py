"""Thin wrapper around the LLM provider (Groq, OpenAI-compatible API).

It exists so the rest of the agent can (a) get parsed-JSON responses reliably
and (b) count how many LLM calls each run costs. That call count is one of our
learning signals: a repeated instruction should require *zero* planning/synthesis
LLM calls because the agent reuses what it already learned.

We talk to Groq's OpenAI-compatible endpoint over httpx — no extra SDK. Swapping
providers is just LLM_BASE_URL + LLM_MODEL + the API key.
"""
from __future__ import annotations

import json
import re

import httpx

from config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL


class LLM:
    def __init__(self) -> None:
        self.model = LLM_MODEL
        self.call_count = 0  # cumulative; snapshot() per run
        self._http = httpx.Client(
            base_url=LLM_BASE_URL,
            headers={"Authorization": f"Bearer {LLM_API_KEY}"},
            timeout=90.0,
        )

    def snapshot(self) -> int:
        return self.call_count

    def complete(self, system: str, user: str, max_tokens: int = 2000,
                 json_mode: bool = False) -> str:
        self.call_count += 1
        payload = {
            "model": self.model,
            "temperature": 0,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            resp = self._http.post("/chat/completions", json=payload)
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            # Some models reject json_object mode — retry once without it.
            if json_mode and e.response.status_code == 400:
                payload.pop("response_format", None)
                resp = self._http.post("/chat/completions", json=payload)
                resp.raise_for_status()
            else:
                detail = e.response.text[:300]
                raise RuntimeError(f"LLM call failed ({e.response.status_code}): "
                                   f"{detail}") from e
        return resp.json()["choices"][0]["message"]["content"]

    def complete_json(self, system: str, user: str, max_tokens: int = 2000):
        """Return parsed JSON, tolerating ```json fences and surrounding prose."""
        raw = self.complete(system, user, max_tokens, json_mode=True)
        return _extract_json(raw)


def _extract_json(text: str):
    text = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        for opener, closer in (("{", "}"), ("[", "]")):
            start = text.find(opener)
            end = text.rfind(closer)
            if start != -1 and end != -1 and end > start:
                try:
                    return json.loads(text[start : end + 1])
                except json.JSONDecodeError:
                    continue
        raise ValueError(f"LLM did not return valid JSON:\n{text}")
