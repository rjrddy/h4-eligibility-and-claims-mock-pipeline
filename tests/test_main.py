import asyncio
import httpx
from main import Claim, Payer, retry
import pytest
from fastapi import HTTPException


def test_payer_idempotency_and_conflict():
    payer = Payer()
    claim = Claim(claim_id="SYN-1", amount_cents=100)
    assert payer.submit(claim) == payer.submit(claim)
    with pytest.raises(HTTPException) as failure:
        payer.submit(Claim(claim_id="SYN-1", amount_cents=200))
    assert failure.value.status_code == 409
    assert len(payer.claims) == 1


def test_retries_are_bounded():
    async def run():
        def fail(_):
            raise httpx.ReadTimeout("injected permanent timeout")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(fail), base_url="http://payer"
        ) as client:
            with pytest.raises(httpx.ReadTimeout):
                await retry(client, "GET", "/claims/SYN-1")

    asyncio.run(run())
