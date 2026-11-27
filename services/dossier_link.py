"""Signed handoff proving that a user started the source bot."""

from __future__ import annotations

import hashlib
import hmac
import re
import time

NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{8}$")
_PROOF_RE = re.compile(r"^p(\d{1,13})_(\d{10})_([A-Za-z0-9_-]{8})_([0-9a-f]{24})$")


def make_proof(
    *, user_id: int, nonce: str, secret: str, issued_at: int | None = None
) -> str:
    if user_id <= 0 or not NONCE_RE.fullmatch(nonce):
        raise ValueError("Invalid dossier confirmation data")
    timestamp = int(time.time()) if issued_at is None else issued_at
    body = f"{user_id}_{timestamp}_{nonce}"
    signature = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()[:24]
    proof = f"p{body}_{signature}"
    if len(proof) + 2 > 64:
        raise ValueError("Dossier proof is too long for a Telegram start parameter")
    return proof


def dossier_confirmation_url(
    *, bot_username: str, user_id: int, nonce: str, secret: str, issued_at: int | None = None
) -> str:
    proof = make_proof(user_id=user_id, nonce=nonce, secret=secret, issued_at=issued_at)
    return f"https://t.me/{bot_username}?start={proof}"


def verify_proof(
    payload: str,
    *, user_id: int,
    nonce: str,
    secret: str,
    now: int | None = None,
    max_age: int = 600,
) -> bool:
    match = _PROOF_RE.fullmatch(payload)
    if match is None:
        return False
    raw_user, raw_timestamp, raw_nonce, signature = match.groups()
    if int(raw_user) != user_id or raw_nonce != nonce:
        return False
    current_time = int(time.time()) if now is None else now
    timestamp = int(raw_timestamp)
    if timestamp > current_time + 30 or current_time - timestamp > max_age:
        return False
    body = f"{raw_user}_{raw_timestamp}_{raw_nonce}"
    expected = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()[:24]
    return hmac.compare_digest(signature, expected)
