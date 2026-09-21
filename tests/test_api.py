"""
External Fuse API — local test suite (TEST_MODE=true, in-memory DB via monkeypatching).

Scenarios:
 1.  GET  /           → 200
 2.  GET  /health     → 200, {status: ok}
 3.  POST /provision  no payment → 402
 4.  POST /provision  TEST_MODE  → 200, {fuse_id, state: INTACT}
 5.  GET  /fuse/{id}  INTACT     → 200, {fuse_id, state: INTACT}
 6.  GET  /fuse/{id}  not found  → 404
 7.  POST /fuse/{id}/trip  INTACT, no payment → 402
 8.  POST /fuse/{id}/trip  INTACT, TEST_MODE  → 200, {state: BLOWN}
 9.  POST /fuse/{id}/trip  BLOWN,  no payment → 200, BLOWN (idempotent, free)
10.  GET  /fuse/{id}  BLOWN      → 200, {state: BLOWN}
11.  POST /fuse/{id}/trip  not found → 404 (free, no payment)
"""

import os
import uuid
from typing import Optional, Tuple
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("TEST_MODE", "true")
os.environ.setdefault("DATABASE_URL", "postgresql://unused/unused")

from main import app  # noqa: E402  (import after env setup)


# ── In-memory fuse store stub ─────────────────────────────────────────────────

class _MemoryStore:
    def __init__(self):
        self._db: dict[str, dict] = {}
        self.transition_count: int = 0

    async def initialize(self):
        pass

    async def provision(self) -> dict:
        fuse_id = str(uuid.uuid4())
        self._db[fuse_id] = {"fuse_id": fuse_id, "state": "INTACT"}
        return {"fuse_id": fuse_id, "state": "INTACT"}

    async def read(self, fuse_id: str) -> Optional[dict]:
        row = self._db.get(fuse_id)
        if row is None:
            return None
        return {"fuse_id": row["fuse_id"], "state": row["state"]}

    async def trip(self, fuse_id: str) -> Tuple[Optional[dict], bool]:
        row = self._db.get(fuse_id)
        if row is None:
            return None, False
        if row["state"] == "BLOWN":
            return {"fuse_id": row["fuse_id"], "state": "BLOWN", "tripped_at": None}, False
        row["state"] = "BLOWN"
        self.transition_count += 1
        return {"fuse_id": row["fuse_id"], "state": "BLOWN", "tripped_at": "2026-01-01T00:00:00+00:00"}, True


_store = _MemoryStore()


@pytest_asyncio.fixture(autouse=True)
async def patch_store():
    """Replace fuse_store module functions with in-memory stub for every test."""
    _store._db.clear()
    _store.transition_count = 0
    with (
        patch("fuse_store.initialize", _store.initialize),
        patch("fuse_store.provision",  _store.provision),
        patch("fuse_store.read",        _store.read),
        patch("fuse_store.trip",        _store.trip),
    ):
        yield


@pytest_asyncio.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def intact_fuse_id(client):
    """Provision a fuse in TEST_MODE and return its fuse_id."""
    resp = await client.post("/provision", headers={"PAYMENT-SIGNATURE": "test-token"})
    assert resp.status_code == 200
    return resp.json()["fuse_id"]


@pytest_asyncio.fixture
async def blown_fuse_id(client, intact_fuse_id):
    """Trip an INTACT fuse in TEST_MODE and return its fuse_id."""
    resp = await client.post(
        f"/fuse/{intact_fuse_id}/trip",
        headers={"PAYMENT-SIGNATURE": "test-token"},
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "BLOWN"
    return intact_fuse_id


# ── Test cases ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_01_root(client):
    resp = await client.get("/")
    assert resp.status_code == 200
    assert resp.json()["service"] == "external-fuse"


@pytest.mark.asyncio
async def test_02_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_03_provision_no_payment(client):
    resp = await client.post("/provision")
    assert resp.status_code == 402
    assert "payment-required" in resp.headers
    body = resp.json()
    assert body.get("x402Version") == 2
    assert body.get("error") == "Payment required"
    assert "accepts" in body
    assert isinstance(body["accepts"][0].get("resource"), dict)
    assert "resource" in body


@pytest.mark.asyncio
async def test_04_provision_with_payment(client):
    resp = await client.post("/provision", headers={"PAYMENT-SIGNATURE": "test-token"})
    assert resp.status_code == 200
    body = resp.json()
    assert "fuse_id" in body
    assert body["state"] == "INTACT"


@pytest.mark.asyncio
async def test_05_read_intact_fuse(client, intact_fuse_id):
    resp = await client.get(f"/fuse/{intact_fuse_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["fuse_id"] == intact_fuse_id
    assert body["state"] == "INTACT"


@pytest.mark.asyncio
async def test_06_read_nonexistent_fuse(client):
    resp = await client.get(f"/fuse/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_07_trip_intact_no_payment(client, intact_fuse_id):
    resp = await client.post(f"/fuse/{intact_fuse_id}/trip")
    assert resp.status_code == 402
    assert "payment-required" in resp.headers
    body = resp.json()
    assert body.get("x402Version") == 2
    assert body.get("error") == "Payment required"
    assert "accepts" in body
    assert isinstance(body["accepts"][0].get("resource"), dict)
    assert "resource" in body


@pytest.mark.asyncio
async def test_08_trip_intact_with_payment(client, intact_fuse_id):
    resp = await client.post(
        f"/fuse/{intact_fuse_id}/trip",
        headers={"PAYMENT-SIGNATURE": "test-token"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["fuse_id"] == intact_fuse_id
    assert body["state"] == "BLOWN"


@pytest.mark.asyncio
async def test_09_trip_blown_idempotent_no_payment(client, blown_fuse_id):
    # BLOWN fuse → 200, no payment header required
    resp = await client.post(f"/fuse/{blown_fuse_id}/trip")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "BLOWN"


@pytest.mark.asyncio
async def test_10_read_blown_fuse(client, blown_fuse_id):
    resp = await client.get(f"/fuse/{blown_fuse_id}")
    assert resp.status_code == 200
    assert resp.json()["state"] == "BLOWN"


@pytest.mark.asyncio
async def test_11_trip_nonexistent_fuse_no_payment(client):
    # Nonexistent → 404, no payment required
    resp = await client.post(f"/fuse/{uuid.uuid4()}/trip")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_12_concurrent_trip_transitions_exactly_once(client, intact_fuse_id):
    """10 concurrent TRIP attempts on the same INTACT fuse: exactly 1 INTACT→BLOWN transition."""
    import asyncio

    async def trip_once():
        return await client.post(
            f"/fuse/{intact_fuse_id}/trip",
            headers={"PAYMENT-SIGNATURE": "test-token"},
        )

    responses = await asyncio.gather(*[trip_once() for _ in range(10)])

    # All responses must be 200 with state=BLOWN
    for resp in responses:
        assert resp.status_code == 200
        assert resp.json()["state"] == "BLOWN"

    # Actual INTACT→BLOWN transition must have occurred exactly once
    assert _store.transition_count == 1
