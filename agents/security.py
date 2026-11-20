"""API-key hashing helpers.

WHAT: Generates API keys and hashes them for storage/lookup.
WHERE: Used by `agents/admin.py` (minting) and `api/auth.py` (verification).
WHY: Only the hash is stored, so a database leak does not expose usable keys. Keys are
256-bit random tokens, so plain SHA-256 is sufficient (no need for slow password hashes)
and keeps the per-request lookup a single indexed equality match.
"""
import hashlib
import secrets


def generate_api_key() -> str:
    return "di_" + secrets.token_urlsafe(32)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()
