from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
import json
import os
import httpx

BASE_URL = os.getenv("EXTERNAL_FUSE_URL", "https://external-fuse.onrender.com").rstrip("/")
PAYMENT_TOKEN = os.getenv("MCP_PAYMENT_TOKEN", "")

mcp = FastMCP(
    "External Fuse API",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if PAYMENT_TOKEN:
        h["PAYMENT-SIGNATURE"] = PAYMENT_TOKEN
    return h


@mcp.tool()
async def provision() -> str:
    """Provision a new fuse in INTACT state. Requires x402 payment of 0.001 USDC."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(f"{BASE_URL}/provision", headers=_headers())
        if resp.status_code == 402:
            return json.dumps({"error": "Payment Required (x402)", "x402": resp.json()})
        resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False, indent=2)


@mcp.tool()
async def read(fuse_id: str) -> str:
    """Read the current state (INTACT or BLOWN) of a fuse by its UUID. Free, no payment required."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(
            f"{BASE_URL}/fuse/{fuse_id}",
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code == 404:
            return json.dumps({"error": "Fuse not found", "fuse_id": fuse_id})
        resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False, indent=2)


@mcp.tool()
async def trip(fuse_id: str) -> str:
    """Trip a fuse to BLOWN state. Requires x402 payment of 0.005 USDC when fuse is INTACT. BLOWN fuse returns 200 free. Nonexistent fuse returns 404 free."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{BASE_URL}/fuse/{fuse_id}/trip",
            headers=_headers(),
        )
        if resp.status_code == 402:
            return json.dumps({"error": "Payment Required (x402)", "x402": resp.json()})
        if resp.status_code == 404:
            return json.dumps({"error": "Fuse not found", "fuse_id": fuse_id})
        resp.raise_for_status()
        return json.dumps(resp.json(), ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run()
