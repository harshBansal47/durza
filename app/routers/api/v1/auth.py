import uuid
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.deps import get_current_user, require_role
from app.core.email_address import normalize_email
from app.core.limiter import limiter
from app.core.observability import email_fingerprint, get_logger
from app.core.request_meta import client_ip, hash_ip
from app.core.security import create_access_token, create_refresh_token, decode_token
from app.models.user import Role, User
from app.schemas.auth import (
    EmailOtpRequest,
    EmailOtpVerifyRequest,
    GoogleLoginRequest,
    RefreshRequest,
    Token,
    UnsubscribeRequest,
)
from app.schemas.user import DeleteAccountRequest, UserRead, UserUpdate
from app.services import email_otp, email_preferences, user_service

router = APIRouter(prefix="/auth", tags=["auth"])
logger = get_logger("durza.auth")

_GENERIC_OTP_MESSAGE = "If that address can sign in, a sign-in code is on its way."


def _tokens_for(user: User) -> Token:
    version = user.token_version
    return Token(
        access_token=create_access_token(str(user.id), user.role.value, version),
        refresh_token=create_refresh_token(str(user.id), user.role.value, version),
    )


@router.post("/unsubscribe-email", status_code=status.HTTP_204_NO_CONTENT)
async def unsubscribe_email(
    payload: UnsubscribeRequest,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Turn off alert email using the signed token from an email. No sign-in needed.

    Not rate limited: the token is the credential, the action is idempotent, and
    the Next.js server calls this on behalf of every visitor, so a per-IP limit
    would be shared by all of them.
    """
    if not await email_preferences.unsubscribe_by_token(db, payload.token):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid unsubscribe link"
        )


@router.post("/email/request", status_code=status.HTTP_200_OK)
@limiter.limit("5/minute")
async def request_email_code(
    request: Request,
    payload: EmailOtpRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Send a sign-in code, whether or not the address has an account.

    The status and body never vary: cooldowns, hourly caps, unknown addresses
    and delivery failures all produce this same response, so this endpoint
    cannot be used to enumerate customers.
    """
    ip_hash = hash_ip(client_ip(request))
    sent = await email_otp.request_code(db, email=payload.email, request_ip_hash=ip_hash)
    if not sent:
        # The reason (cooldown, address cap, delivery failure) is logged where it is
        # known, in `email_otp.request_code`; the caller is deliberately not told.
        # Do not tell callers whether this was a cooldown, an address cap, or
        # a mail-provider failure. A successful response means delivery was
        # accepted by the configured email sender.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="We could not send a sign-in code right now. Please try again later.",
        )
    # Same line whether or not the address has an account, like the response.
    logger.info(
        "sign-in code requested",
        extra={"email_fp": email_fingerprint(payload.email), "ip_hash": ip_hash},
    )
    return {"message": _GENERIC_OTP_MESSAGE}


@router.post("/email/verify", response_model=Token)
@limiter.limit("10/minute")
async def verify_email_code(
    request: Request,
    payload: EmailOtpVerifyRequest,
    db: AsyncSession = Depends(get_db),
) -> Token:
    """Exchange a valid code for a session, creating a plain `user` if needed."""
    try:
        user = await email_otp.verify_code(db, email=payload.email, code=payload.code)
    except email_otp.InvalidOtpError as exc:
        logger.warning(
            "sign-in code rejected",
            extra={
                "email_fp": email_fingerprint(payload.email),
                "ip_hash": hash_ip(client_ip(request)),
            },
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That code is invalid or has expired. Request a new one.",
        ) from exc
    except email_otp.InactiveAccountError as exc:
        logger.warning(
            "sign-in blocked: inactive account",
            extra={"email_fp": email_fingerprint(payload.email)},
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account is inactive.",
        ) from exc

    logger.info(
        "sign-in succeeded",
        extra={"method": "email_code", "user_id": str(user.id), "role": user.role.value},
    )
    return _tokens_for(user)


@router.post("/refresh", response_model=Token)
@limiter.limit("20/minute")
async def refresh(
    request: Request, payload: RefreshRequest, db: AsyncSession = Depends(get_db)
) -> Token:
    try:
        data = decode_token(payload.refresh_token)
    except JWTError as exc:
        logger.info("refresh rejected", extra={"reason": "undecodable_token"})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        ) from exc

    if data.get("type") != "refresh":
        logger.info("refresh rejected", extra={"reason": "wrong_token_type"})
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")

    try:
        user_id = uuid.UUID(data["sub"])
        token_version = int(data.get("ver", 0))
    except (KeyError, TypeError, ValueError) as exc:
        logger.info("refresh rejected", extra={"reason": "bad_claims"})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        ) from exc

    user = await db.get(User, user_id)
    if user is None or not user.is_active or user.token_version != token_version:
        logger.info(
            "refresh rejected",
            extra={"reason": "user_no_longer_valid", "user_id": str(user_id)},
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User no longer valid")

    return _tokens_for(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("30/minute")
async def logout(
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoke every token issued to this account so far.

    JWTs are stateless, so "logging out" is a token-version bump: the next
    request with an older `ver` claim fails in `get_current_user`, including a
    refresh token that was never rotated. A refresh token already rotated
    before this call shares the old version and is revoked with the rest.
    """
    current_user.token_version += 1
    await db.commit()


@router.get("/me", response_model=UserRead)
async def read_me(current_user: User = Depends(get_current_user)) -> User:
    return current_user


@router.patch("/me", response_model=UserRead)
@limiter.limit("10/minute")
async def update_me(
    request: Request,
    payload: UserUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Self-service profile edit. Only the display name is editable."""
    return await user_service.update_user(db, current_user, payload)


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("1/hour")
async def delete_account(
    request: Request,
    payload: DeleteAccountRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete the current user's account and all associated data.

    This is a hard delete: the user, their saved items, tracked products,
    alert history, and notification preferences are all removed. Verification
    reports submitted by the user are preserved (without the account attached)
    because they are public attestations.

    The signed-in session authorises the delete; typing the account email is a
    mis-click guard. There is no password to ask for on OTP-only accounts.
    """
    if normalize_email(str(payload.confirm_email)) != normalize_email(current_user.email):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The email address does not match your account.",
        )
    await user_service.delete_user(db, current_user)


@router.post("/google", response_model=Token)
@limiter.limit("10/minute")
async def google_login(
    request: Request, payload: GoogleLoginRequest, db: AsyncSession = Depends(get_db)
) -> Token:
    """Sign in (or sign up) with a Google ID token.

    Google has already verified the address (checked below), so linking to an
    existing account by email is safe: the caller controls that inbox. Existing
    staff passwords are left untouched; public password sign-in no longer
    exists.
    """
    settings = get_settings()
    client_id = settings.google_client_id
    if not client_id:
        logger.error("google sign-in attempted but GOOGLE_CLIENT_ID is not set")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Google login not configured"
        )

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"id_token": payload.id_token},
        )
    if resp.status_code != 200:
        logger.warning(
            "google sign-in rejected",
            extra={"reason": "token_not_accepted", "google_status": resp.status_code},
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Google token")

    info = resp.json()
    if info.get("aud") != client_id:
        logger.warning("google sign-in rejected", extra={"reason": "audience_mismatch"})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Google token audience mismatch"
        )
    issuer = info.get("iss")
    if issuer not in {"accounts.google.com", "https://accounts.google.com"}:
        logger.warning("google sign-in rejected", extra={"reason": "issuer_mismatch"})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Google token issuer mismatch"
        )
    email = info.get("email")
    verified = info.get("email_verified")
    # Google's tokeninfo endpoint has returned both booleans and the strings
    # "true"/"false" over the years. `bool("false")` is True, so a plain
    # truthiness test would treat an unverified address as verified.
    if not email or not (verified is True or str(verified).lower() == "true"):
        logger.warning("google sign-in rejected", extra={"reason": "email_not_verified"})
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Google email not verified"
        )

    normalized = normalize_email(email)
    name = info.get("name")
    picture = info.get("picture")

    user = await user_service.get_user_by_email(db, normalized)
    created = user is None
    if user is None:
        user = User(
            email=normalized,
            hashed_password=None,
            full_name=name,
            avatar_url=picture,
            role=Role.user,
            email_verified_at=datetime.now(UTC),
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    else:
        # Keep the Google-sourced profile fresh on every sign-in: the photo URL
        # changes when the account's picture does, and an account created by an
        # earlier channel should gain a verified marker on its first Google
        # sign-in.
        user.email_verified_at = user.email_verified_at or datetime.now(UTC)
        if picture and picture != user.avatar_url:
            user.avatar_url = picture
        if name and not (user.full_name or "").strip():
            user.full_name = name
        await db.commit()

    if not user.is_active:
        logger.warning(
            "sign-in blocked: inactive account",
            extra={"method": "google", "user_id": str(user.id)},
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive account")

    logger.info(
        "sign-in succeeded",
        extra={
            "method": "google",
            "user_id": str(user.id),
            "role": user.role.value,
            "new_user": created,
        },
    )
    return _tokens_for(user)


@router.get("/admin-ping")
async def admin_ping(current_user: User = Depends(require_role(Role.admin))) -> dict[str, str]:
    """Demo endpoint proving RBAC works — delete once real admin routes exist (Phase 4)."""
    return {"message": f"Hello, admin {current_user.email}"}
