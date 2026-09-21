import os
import uuid
from datetime import datetime, timezone
from typing import Optional, Tuple

import asyncpg

DATABASE_URL = os.getenv("DATABASE_URL", "")

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS external_fuses (
    fuse_id    TEXT PRIMARY KEY,
    state      TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    tripped_at TIMESTAMPTZ NULL
);
"""


async def _connect() -> asyncpg.Connection:
    return await asyncpg.connect(DATABASE_URL)


async def initialize() -> None:
    conn = await _connect()
    try:
        await conn.execute(_CREATE_TABLE)
    finally:
        await conn.close()


async def provision() -> dict:
    fuse_id = str(uuid.uuid4())
    conn = await _connect()
    try:
        await conn.execute(
            "INSERT INTO external_fuses (fuse_id, state) VALUES ($1, 'INTACT')",
            fuse_id,
        )
    finally:
        await conn.close()
    return {"fuse_id": fuse_id, "state": "INTACT"}


async def read(fuse_id: str) -> Optional[dict]:
    conn = await _connect()
    try:
        row = await conn.fetchrow(
            "SELECT fuse_id, state FROM external_fuses WHERE fuse_id = $1",
            fuse_id,
        )
    finally:
        await conn.close()
    if row is None:
        return None
    return {"fuse_id": row["fuse_id"], "state": row["state"]}


# Returns (result_dict, actually_transitioned)
async def trip(fuse_id: str) -> Tuple[Optional[dict], bool]:
    conn = await _connect()
    try:
        # First check existence and current state
        row = await conn.fetchrow(
            "SELECT fuse_id, state, tripped_at FROM external_fuses WHERE fuse_id = $1",
            fuse_id,
        )
        if row is None:
            return None, False

        if row["state"] == "BLOWN":
            return {
                "fuse_id": row["fuse_id"],
                "state": "BLOWN",
                "tripped_at": row["tripped_at"].isoformat() if row["tripped_at"] else None,
            }, False

        # INTACT — attempt conditional update
        now = datetime.now(timezone.utc)
        result = await conn.execute(
            """UPDATE external_fuses
               SET state = 'BLOWN', tripped_at = $1
               WHERE fuse_id = $2 AND state = 'INTACT'""",
            now, fuse_id,
        )
        # result is like "UPDATE 1"
        updated = int(result.split()[-1])
        if updated == 0:
            # Lost the race — another request already tripped it
            row2 = await conn.fetchrow(
                "SELECT fuse_id, state, tripped_at FROM external_fuses WHERE fuse_id = $1",
                fuse_id,
            )
            return {
                "fuse_id": row2["fuse_id"],
                "state": row2["state"],
                "tripped_at": row2["tripped_at"].isoformat() if row2["tripped_at"] else None,
            }, False

        return {
            "fuse_id": fuse_id,
            "state": "BLOWN",
            "tripped_at": now.isoformat(),
        }, True
    finally:
        await conn.close()
