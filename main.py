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


@asynccontextmanager
async def lifespan(app: FastAPI):
    await fuse_store.initialize()
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
    return Response(status_code=204)


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
