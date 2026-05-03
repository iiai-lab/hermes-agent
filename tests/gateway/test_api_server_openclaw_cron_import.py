"""Regression tests for the OpenClaw cron import compatibility endpoint."""

import json
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter, cors_middleware

_MOD = "gateway.platforms.api_server"


def _make_adapter() -> APIServerAdapter:
    return APIServerAdapter(PlatformConfig(enabled=True, extra={}))


def _create_app(adapter: APIServerAdapter) -> web.Application:
    app = web.Application(middlewares=[cors_middleware])
    app["api_server_adapter"] = adapter
    app.router.add_get("/api/openclaw/cron-import", adapter._handle_openclaw_cron_import)
    return app


def _candidate(source_job_id: str, source_name: str) -> dict:
    return {
        "source_job_id": source_job_id,
        "source_name": source_name,
        "status": "not-created",
        "activation": "OpenClaw still owns this schedule",
        "recommended_initial_mode": "dry-run-report-only",
        "manual_review_required": True,
        "risk_tags": ["migration"],
        "create_candidate": {
            "name": source_name,
            "schedule": "0 9 * * *",
            "deliver": "origin",
            "enabled_toolsets": ["web"],
            "prompt": "secret prompt must never be returned",
        },
        "source_metadata": {
            "enabled_in_openclaw": True,
            "payload_kind": "agentTurn",
            "schedule": "0 9 * * *",
            "delivery_channel_present": True,
        },
    }


@pytest.mark.asyncio
async def test_openclaw_cron_import_matches_only_source_job_id_marker(tmp_path):
    """Same-name Hermes jobs must not hide unmigrated OpenClaw candidates."""
    definitions_path = tmp_path / "openclaw-cron-import.json"
    definitions_path.write_text(
        json.dumps(
            {
                "generated_at": "2026-04-30T00:00:00+09:00",
                "candidates": [
                    _candidate("openclaw-job-unmarked", "Daily Report"),
                    _candidate("openclaw-job-marked", "Shadowban Health Ledger"),
                ],
            }
        ),
        encoding="utf-8",
    )

    same_name_without_marker = {
        "id": "hermes-same-name",
        "name": "Daily Report",
        "schedule": "0 9 * * *",
        "enabled": True,
        "prompt": "A normal Hermes job with the same display name.",
    }
    marker_match_with_different_name = {
        "id": "hermes-marker-match",
        "name": "Migrated OpenClaw health job",
        "schedule": "*/30 * * * *",
        "enabled": True,
        "prompt": "OpenClaw-Source-Job-ID: openclaw-job-marked\nRun the migrated health check.",
    }

    adapter = _make_adapter()
    app = _create_app(adapter)
    mock_list = MagicMock(return_value=[same_name_without_marker, marker_match_with_different_name])

    async with TestClient(TestServer(app)) as cli:
        with patch(f"{_MOD}.OPENCLAW_CRON_IMPORT_DEFINITIONS_PATH", str(definitions_path)), patch(
            f"{_MOD}._CRON_AVAILABLE", True
        ), patch(f"{_MOD}._cron_list", mock_list), patch.object(
            APIServerAdapter, "_load_openclaw_enabled_by_id", return_value={}
        ):
            resp = await cli.get("/api/openclaw/cron-import")
            assert resp.status == 200
            data = await resp.json()
    assert data["mode"] == "read-only-sanitized"
    assert data["safety"] == {
        "promptsRedacted": True,
        "secretsRedacted": True,
        "readOnly": True,
    }
    mock_list.assert_called_once_with(include_disabled=True)

    candidates = {candidate["sourceJobId"]: candidate for candidate in data["candidates"]}

    unmarked = candidates["openclaw-job-unmarked"]
    assert unmarked["sourceName"] == "Daily Report"
    assert unmarked["status"] == "not-created"
    assert unmarked["manualReviewRequired"] is True
    assert "matchedHermesJob" not in unmarked

    marked = candidates["openclaw-job-marked"]
    assert marked["status"] == "migrated-active"
    assert marked["manualReviewRequired"] is False
    assert marked["recommendedInitialMode"] == "full-recurring-hermes"
    assert marked["matchedHermesJob"] == {
        "id": "hermes-marker-match",
        "name": "Migrated OpenClaw health job",
        "enabled": True,
        "schedule": None,
    }

    serialized = json.dumps(data)
    assert "secret prompt must never be returned" not in serialized
    assert "Run the migrated health check" not in serialized
