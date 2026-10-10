from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx


async def test_signal_debug_and_outcomes_are_accessible_without_running_streams(monkeypatch):
    from btc_monitor import main

    signal_id = uuid4()
    row = SimpleNamespace(
        id=signal_id,
        debug_snapshot={"snapshot_origin": "live_at_decision"},
        tracking_complete=False,
        outcomes={"windows": {"15": {"status": "pending"}}},
    )
    monkeypatch.setattr(main.repository, "get_signal", AsyncMock(return_value=row))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=main.app), base_url="http://test"
    ) as client:
        debug = await client.get(f"/signals/{signal_id}/debug")
        assert debug.status_code == 200
        assert debug.json()["debug_snapshot"]["snapshot_origin"] == "live_at_decision"
        result = await client.get(f"/signals/{signal_id}/outcomes")
        assert result.json()["outcomes"]["windows"]["15"]["status"] == "pending"
        main.repository.get_signal.return_value = None
        assert (await client.get(f"/signals/{signal_id}/debug")).status_code == 404
        assert (await client.get("/signals/not-a-uuid/debug")).status_code == 422
