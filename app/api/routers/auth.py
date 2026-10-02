from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.api.auth import create_session_token, verify_password
from app.api.dependencies import SESSION_COOKIE_NAME, get_settings_dependency
from app.api.schemas import LoginRequest
from app.config.settings import Settings

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


@router.post("/login")
def login(
    body: LoginRequest, response: Response, settings: Settings = Depends(get_settings_dependency)
) -> dict[str, bool]:
    """If api_password_hash is unset, login always succeeds without checking
    anything -- consistent with require_auth's own bypass-when-unconfigured
    contract (app.api.dependencies), so a deployment that never configured
    auth never gets stuck unable to log in to an API it didn't ask to
    protect."""
    if settings.api_password_hash:
        if not settings.api_secret_key:
            raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "API auth misconfigured")
        if not verify_password(body.password, settings.api_password_hash):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect password")
        token = create_session_token(settings.api_secret_key, settings.api_session_ttl_minutes)
        response.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=token,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=settings.api_session_ttl_minutes * 60,
        )
    return {"ok": True}


@router.post("/logout")
def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"ok": True}
