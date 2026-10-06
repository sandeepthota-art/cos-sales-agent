"""Session-cookie auth for the FastAPI layer (docs/REACT_MIGRATION_PLAN.md
section 4) -- a deliberately SEPARATE mechanism from both:
  - the Streamlit password gate (app.ui.dashboard._check_password_gate):
    plaintext-compared, Streamlit session-state only, never a signed token;
  - the MCP server's bearer-token auth (app.mcp.server._BearerAuthMiddleware):
    a completely independent, unrelated secret (MCP_AUTH_TOKEN), untouched by
    this module.

Identity can be established either of two independent ways, sharing the same
session cookie once issued:
  - Google SSO (Sign-In-With-Google): the frontend's Google Identity Services
    button returns a Google-issued ID token ("credential"), and
    `verify_google_id_token` below verifies it against Google's own public
    keys (signature, expiry, and that it was issued for this app's own OAuth
    client id).
  - Password: `settings.api_password_hash` is a bcrypt hash the operator
    precomputes once (see `main()` below, invoked via `python -m
    app.api.auth hash "<password>"`) -- kept as an alternative for a
    deployment that hasn't set up (or doesn't want) a Google OAuth client.
Unset `google_oauth_client_id` AND `api_password_hash` means the API requires
no login at all, mirroring `dashboard_password`'s own
zero-configuration-for-local-dev contract -- never a silent, accidental auth
requirement nobody configured.

The session token is a signed JWT (HS256, `settings.api_secret_key`) stored by
the browser as an httpOnly cookie -- React itself never reads or stores it;
the browser sends it automatically on same-origin requests. It carries no
MongoDB credentials, no MCP token, and no other secret -- only an issued-at/
expiry pair and a fixed subject string, so there is nothing of value to leak
even if the cookie itself were somehow exposed.
"""
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token as google_id_token

_JWT_ALGORITHM = "HS256"
_JWT_SUBJECT = "cos-staff-ea-operator"


def hash_password(password: str) -> str:
    """Bcrypt-hash a plaintext password for storage in `API_PASSWORD_HASH`.
    Never call this at request time with an untrusted input -- it's a
    precompute-once operation, run via this module's own CLI (see main()).
    """
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """Timing-safe by construction (bcrypt's own comparison) -- never a plain
    `==` on the hash or the plaintext."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except ValueError:
        # Malformed stored hash (e.g. a misconfigured API_PASSWORD_HASH that
        # isn't actually a bcrypt hash) -- never crash the login attempt into
        # a 500; just treat it as an authentication failure.
        return False


def verify_google_id_token(credential: str, client_id: str) -> dict[str, Any] | None:
    """Verifies a Google Identity Services ID token (the `credential` the
    frontend's Sign-In-With-Google button returns) against Google's own
    public keys, checking signature, expiry, and that `client_id` (this
    app's own OAuth client id) matches the token's audience. Returns the
    decoded claims (includes `email`, `email_verified`, `name`) on success,
    None for ANY failure (expired, bad signature, wrong audience, malformed)
    -- the caller only ever needs a yes/no, same not-leaking-which-check-
    failed rationale as verify_session_token below. Never raises."""
    try:
        return google_id_token.verify_oauth2_token(credential, google_requests.Request(), client_id)
    except ValueError:
        return None


def create_session_token(secret_key: str, ttl_minutes: int, now: datetime | None = None) -> str:
    issued_at = now or datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "sub": _JWT_SUBJECT,
        "iat": issued_at,
        "exp": issued_at + timedelta(minutes=ttl_minutes),
    }
    return jwt.encode(payload, secret_key, algorithm=_JWT_ALGORITHM)


def verify_session_token(token: str, secret_key: str) -> bool:
    """Returns False for ANY failure mode (expired, bad signature, malformed,
    wrong subject) -- the caller (app.api.dependencies.require_auth) only
    ever needs a yes/no, never a reason, to avoid leaking which specific
    check failed."""
    try:
        payload = jwt.decode(token, secret_key, algorithms=[_JWT_ALGORITHM])
    except jwt.PyJWTError:
        return False
    return payload.get("sub") == _JWT_SUBJECT


def main(argv: list[str] | None = None) -> int:
    """CLI to precompute a bcrypt hash for `API_PASSWORD_HASH`:
        python -m app.api.auth hash "<your chosen password>"
    Prints only the hash -- never echoes or logs the plaintext password
    anywhere beyond this one-shot stdout line, and performs no network or
    database access.
    """
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 2 or args[0] != "hash":
        print('Usage: python -m app.api.auth hash "<password>"', file=sys.stderr)
        return 1
    print(hash_password(args[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
