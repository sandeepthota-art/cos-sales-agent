from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.api.auth import create_session_token, verify_google_id_token, verify_password
from app.api.dependencies import SESSION_COOKIE_NAME, get_settings_dependency
from app.api.schemas import GoogleLoginRequest, PasswordLoginRequest
from app.config.settings import Settings

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


def _is_email_authorized(email: str, settings: Settings) -> bool:
    """allowed_emails, when set, is an exact-match allowlist checked INSTEAD
    of allowed_email_domain -- not in addition to it. This is deliberate:
    "only ashok@databeat.io" must mean exactly that person, not that person
    OR anyone else on the databeat.io domain. allowed_email_domain is only
    ever consulted when allowed_emails is unset."""
    allowed_emails = settings.allowed_emails or ""
    emails = {e.strip().lower() for e in allowed_emails.split(",") if e.strip()}
    if emails:
        return email in emails
    allowed_domain = (settings.allowed_email_domain or "").strip().lower()
    if allowed_domain:
        return email.endswith(f"@{allowed_domain}")
    return True


def _set_session_cookie(response: Response, settings: Settings) -> None:
    token = create_session_token(settings.api_secret_key, settings.api_session_ttl_minutes)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        httponly=True,
        secure=True,
        samesite="lax",
        max_age=settings.api_session_ttl_minutes * 60,
    )


@router.get("/config")
def auth_config(settings: Settings = Depends(get_settings_dependency)) -> dict[str, str | bool | None]:
    """Public, unauthenticated -- the frontend needs this before any session
    exists, to decide which login UI to render (and, for Google, to
    initialize the Sign-In-With-Google button with the right OAuth client
    id). Carries no secret: a Client ID is meant to be public (every browser
    that loads the sign-in button already embeds it), and
    `password_auth_enabled` is just a boolean, unlike api_secret_key or
    api_password_hash itself."""
    return {
        "google_client_id": settings.google_oauth_client_id,
        "password_auth_enabled": bool(settings.api_password_hash),
    }


@router.post("/login")
def login(
    body: GoogleLoginRequest, response: Response, settings: Settings = Depends(get_settings_dependency)
) -> dict[str, bool]:
    """Google SSO login. If google_oauth_client_id is unset, login always
    succeeds without checking anything -- consistent with require_auth's own
    bypass-when-unconfigured contract (app.api.dependencies), so a
    deployment that never configured Google SSO never gets stuck unable to
    log in to an API it didn't ask to protect."""
    if settings.google_oauth_client_id:
        if not settings.api_secret_key:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "API auth misconfigured")
        claims = verify_google_id_token(body.credential, settings.google_oauth_client_id)
        if claims is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid Google credential")
        if not claims.get("email_verified"):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Google account email is not verified")
        email = (claims.get("email") or "").strip().lower()
        if not _is_email_authorized(email, settings):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN, "This Google account is not authorized for this dashboard"
            )
        _set_session_cookie(response, settings)
    return {"ok": True}


@router.post("/login/password")
def login_with_password(
    body: PasswordLoginRequest, response: Response, settings: Settings = Depends(get_settings_dependency)
) -> dict[str, bool]:
    """Password login -- the alternative to Google SSO for a deployment that
    sets api_password_hash instead (e.g. a testing deployment without a
    Google OAuth client set up). If api_password_hash is unset, login always
    succeeds without checking anything, same bypass-when-unconfigured
    contract as the Google endpoint above."""
    if settings.api_password_hash:
        if not settings.api_secret_key:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "API auth misconfigured")
        if not verify_password(body.password, settings.api_password_hash):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect password")
        _set_session_cookie(response, settings)
    return {"ok": True}


@router.post("/logout")
def logout(response: Response) -> dict[str, bool]:
    # Cookie identity is (name, domain, path) per RFC 6265, so this clears
    # the cookie regardless -- but echoing the same httponly/secure/samesite
    # attributes set on login is cheap, explicit belt-and-suspenders
    # consistency across browsers, not load-bearing on its own.
    response.delete_cookie(SESSION_COOKIE_NAME, httponly=True, secure=True, samesite="lax")
    return {"ok": True}
