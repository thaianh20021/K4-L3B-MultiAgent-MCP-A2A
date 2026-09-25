from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any

import httpx2

SYSTEM_PROMPT = """You are the coordinator for an ecommerce complaint investigation.
Use only the supplied case and MCP evidence. Return exactly one JSON object with
no markdown, commentary, or hidden reasoning."""


@dataclass(frozen=True)
class WorkersAISettings:
    account_id: str
    api_token: str
    model: str

    @classmethod
    def load(cls) -> WorkersAISettings:
        values = {
            "CLOUDFLARE_ACCOUNT_ID": os.getenv("CLOUDFLARE_ACCOUNT_ID", "").strip(),
            "CLOUDFLARE_API_TOKEN": os.getenv("CLOUDFLARE_API_TOKEN", "").strip(),
            "CLOUDFLARE_AI_MODEL": os.getenv("CLOUDFLARE_AI_MODEL", "").strip(),
        }
        missing = [name for name, value in values.items() if not value]
        if missing:
            raise ValueError(f"missing Workers AI settings: {', '.join(missing)}")
        return cls(
            values["CLOUDFLARE_ACCOUNT_ID"],
            values["CLOUDFLARE_API_TOKEN"],
            values["CLOUDFLARE_AI_MODEL"],
        )


def parse_model_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        raise ValueError("Workers AI response must be a JSON object or string")
    cleaned = value.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
    start = cleaned.find("{")
    if start < 0:
        raise ValueError("Workers AI response contains no JSON object")
    parsed, _ = json.JSONDecoder().raw_decode(cleaned[start:])
    if not isinstance(parsed, dict):
        raise ValueError("Workers AI response must be a JSON object")
    return parsed


async def request_object(prompt: str) -> dict[str, Any]:
    settings = WorkersAISettings.load()
    url = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{settings.account_id}/ai/run/{settings.model}"
    )
    async with httpx2.AsyncClient(timeout=120.0) as client:
        response = await client.post(
            url,
            headers={"Authorization": f"Bearer {settings.api_token}"},
            json={
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0,
                "max_tokens": 8192,
            },
        )
        response.raise_for_status()
        payload = response.json()
    if not payload.get("success"):
        raise RuntimeError(f"Workers AI failed: {payload.get('errors', [])}")
    try:
        value = payload["result"]["response"]
    except (KeyError, TypeError) as exc:
        raise ValueError("Workers AI response has no result.response") from exc
    return parse_model_object(value)
