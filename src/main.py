"""Synthetic payer HTTP integration with ambiguous-timeout retries and reconciliation."""

import asyncio
import json
from pathlib import Path
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


class Claim(BaseModel):
    claim_id: str = Field(pattern=r"^SYN-\d+$")
    amount_cents: int = Field(gt=0)


class Payer:
    def __init__(self) -> None:
        self.claims = {}
        self.app = FastAPI()
        self.app.post("/claims")(self.submit)
        self.app.get("/claims/{claim_id}")(self.status)

    def submit(self, claim: Claim) -> dict:
        existing = self.claims.get(claim.claim_id)
        if existing and existing["amount_cents"] != claim.amount_cents:
            raise HTTPException(409, "idempotency conflict")
        if not existing:
            self.claims[claim.claim_id] = {
                "claim_id": claim.claim_id,
                "amount_cents": claim.amount_cents,
                "status": "submitted",
                "eligible": int(claim.claim_id[4:]) % 7 != 0,
            }
        return dict(self.claims[claim.claim_id])

    def status(self, claim_id: str) -> dict:
        if claim_id not in self.claims:
            raise HTTPException(404, "unknown synthetic claim")
        self.claims[claim_id]["status"] = (
            "accepted" if self.claims[claim_id]["eligible"] else "denied"
        )
        return dict(self.claims[claim_id])


class Chaos(httpx.AsyncBaseTransport):
    def __init__(self, payer: Payer) -> None:
        self.inner = httpx.ASGITransport(app=payer.app)
        self.seen = set()
        self.timeouts = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raw = json.loads(await request.aread()) if request.method == "POST" else None
        key = (request.method, raw["claim_id"] if raw else request.url.path.rsplit("/", 1)[1])
        number = int(key[1][4:])
        first = key not in self.seen
        self.seen.add(key)
        if request.method == "GET" and number % 5 == 0 and first:
            self.timeouts += 1
            raise httpx.ReadTimeout("injected pre-response timeout")
        response = await self.inner.handle_async_request(request)
        if request.method == "POST" and number % 11 == 0 and first:
            await response.aread()
            self.timeouts += 1
            raise httpx.ReadTimeout("injected timeout after payer committed")
        return response


async def retry(
    client: httpx.AsyncClient, method: str, path: str, payload: dict | None = None
) -> tuple[dict, int]:
    for attempt in range(1, 4):
        try:
            response = await client.request(method, path, json=payload)
            response.raise_for_status()
            return response.json(), attempt
        except httpx.TimeoutException:
            if attempt == 3:
                raise
            await asyncio.sleep(0.001 * attempt)
    raise RuntimeError("unreachable retry state")


async def run() -> dict:
    payer = Payer()
    chaos = Chaos(payer)
    local = {}
    attempts = 0
    async with httpx.AsyncClient(transport=chaos, base_url="http://mock-payer") as client:
        for i in range(120):
            payload = {"claim_id": f"SYN-{i}", "amount_cents": 1000 + i}
            row, count = await retry(client, "POST", "/claims", payload)
            attempts += count
            local[row["claim_id"]] = row
        pending_before = sum(r["status"] == "submitted" for r in local.values())
        for claim_id in local:
            row, count = await retry(client, "GET", "/claims/" + claim_id)
            attempts += count
            local[claim_id] = row
    mismatches = sum(local[k] != v for k, v in payer.claims.items())
    assert mismatches == 0 and len(payer.claims) == 120
    Path("evidence/reconciliation.json").write_text(
        json.dumps(
            {
                "claims": list(local.values()),
                "pending_before": pending_before,
                "discrepancies": mismatches,
            },
            indent=2,
        )
    )
    return {
        "synthetic_claims": len(local),
        "payer_timeouts_injected": chaos.timeouts,
        "discrepancies_after_reconciliation": mismatches,
        "pending_before_reconciliation": pending_before,
        "http_attempts": attempts,
        "duplicate_payer_claims": len(payer.claims) - len(local),
    }


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run()), indent=2))
