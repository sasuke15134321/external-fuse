"""
External Fuse API — FastAPI service.
Endpoints: POST /provision, GET /fuse/{id}, POST /fuse/{id}/trip, GET /health, GET /
"""

import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

import fuse_store
from payment_verifier import PaymentVerifier

load_dotenv()

_PROVISION_PRICE = "0.001"  # USDC
_TRIP_PRICE      = "0.005"  # USDC
_WALLET_ADDRESS  = os.getenv("WALLET_ADDRESS", "0x60c402878EfcEcAe5733A88075328Aa2320C39BE")

_verifier = PaymentVerifier()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await fuse_store.initialize()
    yield


app = FastAPI(title="External Fuse API", version="1.0.0", lifespan=lifespan)


def _payment_required(amount: str, request: Request) -> JSONResponse:
    body = _verifier.generate_payment_request(
        _WALLET_ADDRESS, amount, "External Fuse", str(request.url)
    )
    return JSONResponse(status_code=402, content=body)


def _get_payment_header(request: Request) -> str | None:
    return (
        request.headers.get("PAYMENT-SIGNATURE")
        or request.headers.get("X-Payment")
    )


@app.get("/")
async def root():
    return {"service": "external-fuse", "version": "1.0.0"}


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/provision")
async def provision(request: Request):
    payment_header = _get_payment_header(request)
    if not payment_header:
        return _payment_required(_PROVISION_PRICE, request)

    ok = await _verifier.verify_payment(payment_header, _WALLET_ADDRESS, _PROVISION_PRICE)
    if not ok:
        return _payment_required(_PROVISION_PRICE, request)

    return await fuse_store.provision()


@app.get("/fuse/{fuse_id}")
async def get_fuse(fuse_id: str):
    result = await fuse_store.read(fuse_id)
    if result is None:
        raise HTTPException(status_code=404, detail="fuse not found")
    return result


@app.post("/fuse/{fuse_id}/trip")
async def trip_fuse(fuse_id: str, request: Request):
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
