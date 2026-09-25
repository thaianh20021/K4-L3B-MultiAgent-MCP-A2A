from __future__ import annotations

import pytest

from student_agent.workers_ai import WorkersAISettings, parse_model_object


def test_workers_ai_settings_require_all_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "test-token")
    monkeypatch.setenv("CLOUDFLARE_AI_MODEL", "@cf/meta/test")

    with pytest.raises(ValueError, match="CLOUDFLARE_ACCOUNT_ID"):
        WorkersAISettings.load()


def test_parse_model_object_accepts_plain_json() -> None:
    assert parse_model_object('{"case_id":"CASE_001"}') == {"case_id": "CASE_001"}


def test_parse_model_object_accepts_fenced_json() -> None:
    text = '```json\n{"case_id":"CASE_001"}\n```'
    assert parse_model_object(text) == {"case_id": "CASE_001"}
