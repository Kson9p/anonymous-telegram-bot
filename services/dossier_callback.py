"""Server-to-server confirmation for the separate dossier bot."""

from __future__ import annotations

import aiohttp


async def notify_dossier_verification(url: str, *, user_id: int, proof: str) -> bool:
    timeout = aiohttp.ClientTimeout(total=8)
    try:
        async with (
            aiohttp.ClientSession(timeout=timeout) as session,
            session.post(url, json={"user_id": user_id, "proof": proof}) as response,
        ):
            return response.status == 200
    except (aiohttp.ClientError, TimeoutError):
        return False
