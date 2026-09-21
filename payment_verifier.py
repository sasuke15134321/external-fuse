"""
Payment verification for x402 protocol v2.
Adapted from agent_budget_api/payment_verifier.py.
Supports CDP Facilitator (primary) and embedded x402FacilitatorSync (fallback).
TEST_MODE=true bypasses real payment for local testing.
"""

import asyncio
import base64
import json
import os
import re
import time
import uuid
from typing import Optional

TEST_MODE = os.getenv("TEST_MODE", "false").lower() in ("true", "1", "yes")

# ── CDP Facilitator ────────────────────────────────────────────────────────────
CDP_API_KEY_ID     = os.getenv("CDP_API_KEY_ID", "")
CDP_API_KEY_SECRET = os.getenv("CDP_API_KEY_SECRET", "").replace("\\n", "\n")

_CDP_FACILITATOR_URL  = "https://api.cdp.coinbase.com/platform/v2/x402"
_CDP_HOST             = "api.cdp.coinbase.com"
_CDP_VERIFY_PATH      = "/platform/v2/x402/verify"
_CDP_SETTLE_PATH      = "/platform/v2/x402/settle"

# ── Embedded facilitator (fallback) ───────────────────────────────────────────
FACILITATOR_PRIVATE_KEY = os.getenv("FACILITATOR_PRIVATE_KEY", "")
BASE_RPC_URL            = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")

# ── Common constants ──────────────────────────────────────────────────────────
_WALLET_ADDRESS = "0x60c402878EfcEcAe5733A88075328Aa2320C39BE"
_USDC_ADDRESS   = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
_NETWORK        = "eip155:8453"

_embedded_facilitator = None


def _decode_payment_header(payment_header: str) -> Optional[dict]:
    try:
        return json.loads(base64.b64decode(payment_header).decode("utf-8"))
    except Exception:
        pass
    try:
        return json.loads(payment_header)
    except Exception:
        return None


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _normalize_pem(key_str: str) -> str:
    if "\\n" in key_str and "\n" not in key_str:
        key_str = key_str.replace("\\n", "\n")
    key_str = key_str.replace("\r\n", "\n").replace("\r", "\n").strip()

    if "\n-----" in key_str or "-----\n" in key_str:
        return key_str + "\n"

    header_m = re.match(r'^(-----BEGIN [A-Z ]+-----)', key_str)
    footer_m = re.search(r'(-----END [A-Z ]+-----)$', key_str)
    if header_m and footer_m:
        header = header_m.group(1)
        footer = footer_m.group(1)
        body = key_str[len(header) : key_str.rfind(footer)].strip()
        body = "".join(body.split())
        lines = [body[i : i + 64] for i in range(0, len(body), 64)]
        return header + "\n" + "\n".join(lines) + "\n" + footer + "\n"

    return key_str


def _load_cdp_private_key():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    raw = CDP_API_KEY_SECRET
    normalized = _normalize_pem(raw)
    if "BEGIN" in normalized:
        try:
            return serialization.load_pem_private_key(normalized.encode(), password=None)
        except Exception as e:
            print(f"[WARN] PEM load failed: {e}")

    try:
        try:
            key_bytes = base64.b64decode(raw)
        except Exception:
            key_bytes = base64.urlsafe_b64decode(raw + "==")

        if len(key_bytes) >= 32:
            key_int = int.from_bytes(key_bytes[:32], "big")
            return ec.derive_private_key(key_int, ec.SECP256R1())
    except Exception as e:
        print(f"[WARN] raw EC P-256 load failed: {e}")

    raise ValueError(f"Cannot load CDP private key (len={len(raw)})")


def _generate_cdp_jwt(method: str, path: str) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    private_key = _load_cdp_private_key()

    now = int(time.time())
    header  = {"alg": "ES256", "kid": CDP_API_KEY_ID, "nonce": uuid.uuid4().hex, "typ": "JWT"}
    payload = {
        "sub": CDP_API_KEY_ID,
        "iss": "cdp",
        "aud": ["cdp_service"],
        "nbf": now,
        "exp": now + 120,
        "uris": [f"{method} {_CDP_HOST}{path}"],
    }

    h_b64 = _b64url(json.dumps(header,  separators=(",", ":")).encode())
    p_b64 = _b64url(json.dumps(payload, separators=(",", ":")).encode())
    signing_input = f"{h_b64}.{p_b64}".encode()

    der_sig  = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s     = decode_dss_signature(der_sig)
    raw_sig  = r.to_bytes(32, "big") + s.to_bytes(32, "big")

    return f"{h_b64}.{p_b64}.{_b64url(raw_sig)}"


def _get_embedded_facilitator():
    global _embedded_facilitator
    if _embedded_facilitator is not None:
        return _embedded_facilitator
    if not FACILITATOR_PRIVATE_KEY:
        print("[WARN] No CDP credentials and no FACILITATOR_PRIVATE_KEY — v2 payment verification unavailable")
        return None
    try:
        from x402.facilitator import x402FacilitatorSync
        from x402.mechanisms.evm.exact import register_exact_evm_facilitator
        from x402.mechanisms.evm.signers import FacilitatorWeb3Signer

        signer = FacilitatorWeb3Signer(private_key=FACILITATOR_PRIVATE_KEY, rpc_url=BASE_RPC_URL)
        fac    = x402FacilitatorSync()
        register_exact_evm_facilitator(fac, signer, networks=[_NETWORK])
        _embedded_facilitator = fac
        print(f"[x402] Embedded facilitator ready (fallback): {signer.address}")
        return _embedded_facilitator
    except Exception as e:
        print(f"[ERROR] Embedded facilitator init failed: {e}")
        return None


class PaymentVerifier:
    def __init__(self):
        self.supported_networks = ["eip155:8453", "base", "base-mainnet"]
        self.supported_assets   = {_USDC_ADDRESS: "USDC"}

    async def verify_payment(
        self,
        payment_header: str,
        wallet_address: str,
        expected_amount: str,
    ) -> bool:
        if TEST_MODE:
            print(f"[TEST_MODE] Payment bypassed: amount={expected_amount} USDC")
            return True

        payload_dict = _decode_payment_header(payment_header)
        if payload_dict is None:
            print("[WARN] Could not decode payment header")
            return False

        if payload_dict.get("x402Version", 1) == 2:
            return await self._verify_v2(payload_dict, wallet_address, expected_amount)
        return self._verify_legacy(payload_dict, wallet_address, expected_amount)

    async def _verify_v2(self, payload_dict: dict, wallet_address: str, expected_amount: str) -> bool:
        if CDP_API_KEY_ID and CDP_API_KEY_SECRET:
            try:
                result = await self._verify_v2_cdp(payload_dict, wallet_address, expected_amount)
                if result:
                    return True
            except Exception as e:
                print(f"[WARN] CDP path exception: {e}")
            print("[WARN] CDP failed, falling back to embedded facilitator")
        return await self._verify_v2_embedded(payload_dict, wallet_address, expected_amount)

    async def _verify_v2_cdp(self, payload_dict: dict, wallet_address: str, expected_amount: str) -> bool:
        import httpx

        amount_units = str(round(float(expected_amount) * 1_000_000))
        pay_to       = wallet_address or _WALLET_ADDRESS
        print(f"[x402/CDP] using payTo={pay_to} amount={amount_units}")

        requirements = {
            "scheme":            "exact",
            "network":           _NETWORK,
            "asset":             _USDC_ADDRESS,
            "amount":            amount_units,
            "payTo":             pay_to,
            "maxTimeoutSeconds": 300,
            "extra":             {"name": "USD Coin", "version": "2"},
        }

        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                verify_resp = await client.post(
                    f"{_CDP_FACILITATOR_URL}/verify",
                    json={
                        "x402Version":        2,
                        "paymentPayload":     payload_dict,
                        "paymentRequirements": requirements,
                    },
                    headers={
                        "Authorization": f"Bearer {_generate_cdp_jwt('POST', _CDP_VERIFY_PATH)}",
                        "Content-Type":  "application/json",
                    },
                )
                verify_data = verify_resp.json()
                print(f"[x402/CDP] verify status={verify_resp.status_code} isValid={verify_data.get('isValid')}")

                if not verify_data.get("isValid"):
                    print(f"[WARN] CDP verify failed: {verify_data.get('invalidReason')} full={verify_data}")
                    return False

                settle_resp = await client.post(
                    f"{_CDP_FACILITATOR_URL}/settle",
                    json={
                        "x402Version":        2,
                        "paymentPayload":     payload_dict,
                        "paymentRequirements": requirements,
                    },
                    headers={
                        "Authorization": f"Bearer {_generate_cdp_jwt('POST', _CDP_SETTLE_PATH)}",
                        "Content-Type":  "application/json",
                    },
                )
                settle_data = settle_resp.json()
                print(f"[x402/CDP] settle status={settle_resp.status_code} success={settle_data.get('success')}")

                if settle_data.get("success"):
                    tx = settle_data.get("transaction", "")
                    print(f"[OK] Payment settled via CDP Facilitator: {expected_amount} USDC tx={tx}")
                    return True

                print(f"[WARN] CDP settle failed: {settle_data}")
                return False

        except Exception as e:
            print(f"[ERROR] CDP Facilitator error: {e}")
            return False

    async def _verify_v2_embedded(self, payload_dict: dict, wallet_address: str, expected_amount: str) -> bool:
        facilitator = _get_embedded_facilitator()
        if facilitator is None:
            return False

        try:
            from x402.schemas import PaymentPayload, PaymentRequirements

            payment_payload = PaymentPayload.model_validate(payload_dict)
            amount_units    = str(round(float(expected_amount) * 1_000_000))
            pay_to          = wallet_address or _WALLET_ADDRESS

            requirements = PaymentRequirements(
                scheme="exact",
                network=_NETWORK,
                asset=_USDC_ADDRESS,
                amount=amount_units,
                pay_to=pay_to,
                max_timeout_seconds=300,
                extra={"name": "USD Coin", "version": "2"},
            )
        except Exception as e:
            print(f"[WARN] Failed to build payment objects: {e}")
            return False

        try:
            verify_result = await asyncio.to_thread(facilitator.verify, payment_payload, requirements)
            print(f"[x402/embedded] verify: is_valid={verify_result.is_valid}")
            if not verify_result.is_valid:
                return False

            settle_result = await asyncio.to_thread(facilitator.settle, payment_payload, requirements)
            print(f"[x402/embedded] settle: success={settle_result.success}")
            if settle_result.success:
                print(f"[OK] Payment settled via embedded: {expected_amount} USDC tx={settle_result.transaction}")
                return True

            return False

        except Exception as e:
            print(f"[ERROR] Embedded facilitator error: {e}")
            return False

    def _verify_legacy(self, payment_data: dict, wallet_address: str, expected_amount: str) -> bool:
        for field in ["amount", "asset", "network", "to", "txHash"]:
            if field not in payment_data:
                print(f"[WARN] Missing required payment field: {field}")
                return False

        if payment_data["network"] not in self.supported_networks:
            return False
        if payment_data["asset"] not in self.supported_assets:
            return False
        if payment_data["to"].lower() != wallet_address.lower():
            return False

        expected_wei = int(float(expected_amount) * 1_000_000)
        if int(payment_data["amount"]) < expected_wei:
            return False

        tx_hash = payment_data["txHash"]
        if not tx_hash.startswith("0x") or len(tx_hash) != 66:
            return False

        print(f"[OK] Legacy payment verified: {int(payment_data['amount']) / 1_000_000} USDC")
        return True

    def generate_payment_request(
        self, wallet_address: str, amount: str, description: str, resource_url: str
    ) -> dict:
        amount_wei = int(float(amount) * 1_000_000)
        return {
            "x402Version": 2,
            "error": "Payment required",
            "accepts": [{
                "scheme":            "exact",
                "network":           _NETWORK,
                "asset":             _USDC_ADDRESS,
                "amount":            str(amount_wei),
                "payTo":             wallet_address,
                "maxTimeoutSeconds": 300,
                "extra":             {"name": "USD Coin", "version": "2"},
                "resource":          {"method": "POST", "mimeType": "application/json"},
            }],
            "resource": {
                "url":         resource_url,
                "description": description,
                "mimeType":    "application/json",
            },
        }
