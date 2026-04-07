"""Tests for app.web.broadcaster.SseBroadcaster."""

import asyncio
import json

import pytest

from app.web.broadcaster import SseBroadcaster


class TestSseBroadcaster:
    @pytest.mark.asyncio
    async def test_connect_returns_uuid_and_queue(self):
        b = SseBroadcaster()
        cid, q = await b.connect()
        assert len(cid) == 36  # UUID4 format "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
        assert isinstance(q, asyncio.Queue)

    @pytest.mark.asyncio
    async def test_broadcast_delivers_json_to_client(self):
        b = SseBroadcaster()
        _, q = await b.connect()
        await b.broadcast({"type": "ping", "value": 42})
        data = q.get_nowait()
        event = json.loads(data)
        assert event["type"] == "ping"
        assert event["value"] == 42

    @pytest.mark.asyncio
    async def test_broadcast_delivers_to_all_connected_clients(self):
        b = SseBroadcaster()
        _, q1 = await b.connect()
        _, q2 = await b.connect()
        _, q3 = await b.connect()
        await b.broadcast({"type": "multi"})
        assert not q1.empty()
        assert not q2.empty()
        assert not q3.empty()

    @pytest.mark.asyncio
    async def test_disconnect_sends_none_sentinel(self):
        b = SseBroadcaster()
        cid, q = await b.connect()
        await b.disconnect(cid)
        sentinel = await q.get()
        assert sentinel is None

    @pytest.mark.asyncio
    async def test_disconnected_client_receives_no_further_broadcasts(self):
        b = SseBroadcaster()
        cid, q = await b.connect()
        await b.disconnect(cid)
        await q.get()  # consume sentinel
        await b.broadcast({"type": "after-disconnect"})
        assert q.empty()

    @pytest.mark.asyncio
    async def test_broadcast_with_no_clients_does_not_raise(self):
        b = SseBroadcaster()
        await b.broadcast({"type": "no-clients"})  # should not raise

    @pytest.mark.asyncio
    async def test_broadcast_serialises_nested_dict(self):
        b = SseBroadcaster()
        _, q = await b.connect()
        payload = {"type": "nested", "data": {"a": [1, 2, 3]}}
        await b.broadcast(payload)
        received = json.loads(q.get_nowait())
        assert received["data"]["a"] == [1, 2, 3]
