from datetime import datetime, timedelta, timezone

from gateway.agent_activity import (
    AGENT_ACTIVITY_SCHEMA,
    build_agent_activity_event,
    compact_display_text,
    normalize_activity_phase,
)


def test_normalize_activity_phase_accepts_legacy_aliases():
    assert normalize_activity_phase("started") == "start"
    assert normalize_activity_phase("call") == "start"
    assert normalize_activity_phase("result") == "completed"
    assert normalize_activity_phase("end") == "completed"
    assert normalize_activity_phase("finished") == "completed"
    assert normalize_activity_phase("error") == "failed"


def test_compact_display_text_trims_and_limits_text():
    text = "  first line  \nsecond line " + ("x" * 240)
    compacted = compact_display_text(text, limit=32)
    assert compacted == "first line second line xxxxxx..."
    assert len(compacted) == 32


def test_build_agent_activity_event_uses_contract_shape():
    event = build_agent_activity_event(
        session_key="agent:main:main",
        run_id="run-1",
        stream="thinking",
        phase="started",
        timestamp=1777783739.461,
        data={"statusLine": "Checking files"},
    )

    assert event == {
        "schema": AGENT_ACTIVITY_SCHEMA,
        "sessionKey": "agent:main:main",
        "runId": "run-1",
        "stream": "thinking",
        "phase": "start",
        "timestamp": "2026-05-03T04:48:59.461000+00:00",
        "data": {"statusLine": "Checking files"},
    }


def test_build_agent_activity_event_normalizes_aware_datetime_to_utc():
    event = build_agent_activity_event(
        session_key="agent:main:main",
        stream="thinking",
        phase="update",
        timestamp=datetime(
            2026,
            5,
            3,
            13,
            48,
            59,
            461000,
            tzinfo=timezone(timedelta(hours=9)),
        ),
    )

    assert event["timestamp"] == "2026-05-03T04:48:59.461000+00:00"


def test_build_agent_activity_event_treats_naive_datetime_as_utc():
    event = build_agent_activity_event(
        session_key="agent:main:main",
        stream="thinking",
        phase="update",
        timestamp=datetime(2026, 5, 3, 4, 48, 59, 461000),
    )

    assert event["timestamp"] == "2026-05-03T04:48:59.461000+00:00"


def test_build_agent_activity_event_rejects_unknown_stream_and_phase():
    try:
        build_agent_activity_event(
            session_key="agent:main:main",
            stream="mystery",
            phase="update",
        )
    except ValueError as exc:
        assert "Unsupported activity stream" in str(exc)
    else:
        raise AssertionError("expected ValueError for unsupported stream")

    try:
        build_agent_activity_event(
            session_key="agent:main:main",
            stream="thinking",
            phase="mystery",
        )
    except ValueError as exc:
        assert "Unsupported activity phase" in str(exc)
    else:
        raise AssertionError("expected ValueError for unsupported phase")
