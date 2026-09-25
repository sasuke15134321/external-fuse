"""
External Fuse API — FastAPI service.
Endpoints: POST /provision, GET /fuse/{id}, POST /fuse/{id}/trip, GET /health, GET /
"""

import base64
import json
import os
from contextlib import asynccontextmanager
from typing import Annotated

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Request
from fastapi.responses import JSONResponse, Response

import fuse_store
from payment_verifier import PaymentVerifier

load_dotenv()

_PROVISION_PRICE = "0.001"  # USDC
_TRIP_PRICE      = "0.005"  # USDC
_WALLET_ADDRESS  = os.getenv("WALLET_ADDRESS", "0x60c402878EfcEcAe5733A88075328Aa2320C39BE")

_verifier = PaymentVerifier()

_FUSE_ID_EXAMPLE = "a1b2c3d4-e5f6-7890-abcd-ef1234567890"

# MCP: initialize at module level so session_manager is available for lifespan.
_mcp_server = None
_mcp_starlette_app = None
try:
    from mcp_server import mcp as _mcp_server
    _mcp_starlette_app = _mcp_server.streamable_http_app()
except Exception as _mcp_init_err:
    import logging as _log
    _log.getLogger(__name__).warning(f"MCP init skipped: {_mcp_init_err}")

# Minimal valid ICO: 1×1 pixel, 32-bit BGRA
_FAVICON_ICO = (
    b"\x00\x00\x01\x00\x01\x00"           # ICO header: reserved, type=1, count=1
    b"\x01\x01\x00\x00\x01\x00\x20\x00"   # dir: w=1, h=1, colors=0, res=0, planes=1, bpp=32
    b"\x30\x00\x00\x00\x16\x00\x00\x00"   # dir: imagesize=48, offset=22
    b"\x28\x00\x00\x00"                    # BITMAPINFOHEADER: size=40
    b"\x01\x00\x00\x00"                    # width=1
    b"\x02\x00\x00\x00"                    # height=2 (×2 for ICO format)
    b"\x01\x00\x20\x00"                    # planes=1, bitcount=32
    b"\x00\x00\x00\x00"                    # compression=BI_RGB
    b"\x00\x00\x00\x00"                    # imagesize=0
    b"\x00\x00\x00\x00"                    # xpixelspermeter=0
    b"\x00\x00\x00\x00"                    # ypixelspermeter=0
    b"\x00\x00\x00\x00"                    # colorsused=0
    b"\x00\x00\x00\x00"                    # colorsimportant=0
    b"\x4F\x6A\x2D\xFF"                    # pixel BGRA (opaque green-grey)
    b"\x00\x00\x00\x00"                    # AND mask
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await fuse_store.initialize()
    if _mcp_server is not None:
        async with _mcp_server.session_manager.run():
            yield
    else:
        yield


app = FastAPI(
    title="External Fuse API",
    version="1.0.0",
    contact={"email": "segawa4321@gmail.com"},
    lifespan=lifespan,
)


def _payment_required(amount: str, request: Request) -> JSONResponse:
    body = _verifier.generate_payment_request(
        _WALLET_ADDRESS, amount, "External Fuse", str(request.url)
    )
    if request.url.path == "/provision":
        body["extensions"] = {
            "bazaar": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "fuse_id": {"type": "string"},
                        "state": {"type": "string", "enum": ["INTACT"]}
                    },
                    "required": ["fuse_id", "state"],
                    "additionalProperties": False
                },
                "info": {
                    "input": {
                        "type": "http",
                        "method": "POST",
                        "bodyType": "json",
                        "body": {}
                    },
                    "output": {
                        "type": "json",
                        "example": {
                            "fuse_id": _FUSE_ID_EXAMPLE,
                            "state": "INTACT"
                        }
                    }
                }
            }
        }

    header_value = base64.b64encode(json.dumps(body).encode()).decode()
    return JSONResponse(
        status_code=402,
        content=body,
        headers={"PAYMENT-REQUIRED": header_value},
    )


def _get_payment_header(request: Request) -> str | None:
    return (
        request.headers.get("PAYMENT-SIGNATURE")
        or request.headers.get("X-Payment")
    )


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return Response(content=_FAVICON_ICO, media_type="image/x-icon")


@app.get("/.well-known/x402", include_in_schema=False)
async def x402_well_known():
    return {
        "name": "External Fuse API",
        "payTo": _WALLET_ADDRESS,
        "network": "eip155:8453",
        "resources": [
            {
                "url": "https://external-fuse.onrender.com/provision",
                "method": "POST",
                "price": "0.001",
                "currency": "USDC",
                "description": "Provision a new fuse in INTACT state",
            },
            {
                "url": "https://external-fuse.onrender.com/fuse/{fuse_id}/trip",
                "method": "POST",
                "price": "0.005",
                "currency": "USDC",
                "description": (
                    "Trip an INTACT fuse to BLOWN state. "
                    "Payment required only when fuse is INTACT. "
                    "BLOWN returns 200 free. Nonexistent returns 404 free."
                ),
            },
        ],
    }


@app.get(
    "/",
    openapi_extra={"security": []},
)
async def root():
    return {"service": "external-fuse", "version": "1.0.0"}


@app.get(
    "/health",
    openapi_extra={"security": []},
)
async def health():
    return {"status": "ok"}


@app.get("/llms.txt", include_in_schema=False)
async def llms_txt():
    llms_path = os.path.join(os.path.dirname(__file__), "llms.txt")
    with open(llms_path, "r", encoding="utf-8-sig") as f:
        return Response(content=f.read(), media_type="text/plain; charset=utf-8")

@app.get("/.well-known/mcp/server-card.json", include_in_schema=False)
async def mcp_server_card():
    return {
        "serverInfo": {"name": "external-fuse-api", "version": "1.0.0"},
        "tools": [
            {
                "name": "provision",
                "description": "Provision a new fuse in INTACT state. Requires x402 payment of 0.001 USDC.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "read",
                "description": "Read the current state (INTACT or BLOWN) of a fuse by its UUID. Free, no payment required.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "fuse_id": {"type": "string", "description": "UUID of the fuse"},
                    },
                    "required": ["fuse_id"],
                },
            },
            {
                "name": "trip",
                "description": "Trip a fuse to BLOWN state. Requires x402 payment of 0.005 USDC when fuse is INTACT. BLOWN fuse returns 200 free. Nonexistent fuse returns 404 free.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "fuse_id": {"type": "string", "description": "UUID of the fuse to trip"},
                    },
                    "required": ["fuse_id"],
                },
            },
        ],
    }


@app.post(
    "/provision",
    summary="Provision a new fuse",
    description="Create a new fuse in INTACT state. Requires x402 payment of 0.001 USDC.",
    openapi_extra={
        "x-payment-info": {
            "protocols": [{"x402": {}}],
            "price": {"amount": "0.001", "currency": "USDC", "mode": "fixed"},
        },
        "requestBody": {
            "required": False,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {},
                        "description": "No request body required. Payment via PAYMENT-SIGNATURE header.",
                    }
                }
            },
        },
        "responses": {
            "402": {"description": "x402 payment required (0.001 USDC on Base)"},
        },
    },
)
async def provision(request: Request):
    payment_header = _get_payment_header(request)
    if not payment_header:
        return _payment_required(_PROVISION_PRICE, request)

    ok = await _verifier.verify_payment(payment_header, _WALLET_ADDRESS, _PROVISION_PRICE)
    if not ok:
        return _payment_required(_PROVISION_PRICE, request)

    return await fuse_store.provision()


@app.get(
    "/fuse/{fuse_id}",
    summary="Get fuse state",
    description="Read the current state of a fuse (INTACT or BLOWN). Free endpoint, no payment required.",
    openapi_extra={"security": []},
)
async def get_fuse(
    fuse_id: Annotated[str, Path(description="UUID of the fuse", examples=[_FUSE_ID_EXAMPLE])],
):
    result = await fuse_store.read(fuse_id)
    if result is None:
        raise HTTPException(status_code=404, detail="fuse not found")
    return result


@app.post(
    "/fuse/{fuse_id}/trip",
    summary="Trip a fuse",
    description=(
        "Trip an INTACT fuse to BLOWN state. "
        "x402 payment of 0.005 USDC required only when fuse is INTACT. "
        "BLOWN fuse returns 200 free (idempotent). "
        "Nonexistent fuse returns 404 free (no payment)."
    ),
    openapi_extra={
        "x-payment-info": {
            "protocols": [{"x402": {}}],
            "price": {"amount": "0.005", "currency": "USDC", "mode": "fixed"},
            "condition": "Payment required only when fuse state is INTACT.",
        },
        "responses": {
            "402": {"description": "x402 payment required (0.005 USDC) — fuse is INTACT"},
            "200": {"description": "Success — fuse tripped to BLOWN, or already BLOWN (free)"},
            "404": {"description": "Fuse not found — free, no payment required"},
        },
    },
)
async def trip_fuse(
    fuse_id: Annotated[str, Path(description="UUID of the fuse to trip", examples=[_FUSE_ID_EXAMPLE])],
    request: Request,
):
    # State check FIRST — BLOWN and NONEXISTENT never require payment
    current = await fuse_store.read(fuse_id)

    if current is None:
        raise HTTPException(status_code=404, detail="fuse not found")

    if current["state"] == "BLOWN":
        return current  # idempotent, no charge

    # INTACT only → require x402 payment before transitioning
    payment_header = _get_payment_header(request)
    if not payment_header:
        return _payment_required(_TRIP_PRICE, request)

    ok = await _verifier.verify_payment(payment_header, _WALLET_ADDRESS, _TRIP_PRICE)
    if not ok:
        return _payment_required(_TRIP_PRICE, request)

    result, _ = await fuse_store.trip(fuse_id)
    if result is None:
        raise HTTPException(status_code=404, detail="fuse not found")

    return result


if _mcp_starlette_app is not None:
    # Mounting at "/" lets FastAPI's explicit routes take priority while
    # /mcp falls through to the Starlette sub-app's /mcp route.
    # session_manager.run() is started in lifespan() above.
    app.mount("/", _mcp_starlette_app)
