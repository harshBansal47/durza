import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordRequestForm
from jose import JWTError
from sqlalchemy.ext.asyncio import AsyncSession

# ... (keep existing imports)
from app.core.config import get_settings
from app.core.database import get_db
from app.core.deps import get_current_user, require_role
from app.core.limiter import limiter
from app.core.security import (
    create_access_token,
    create_refresh_token,
    decode_token,
    hash_password,
    verify_password,
)
from app.models.user import Role, User
from app.schemas.auth import GoogleLoginRequest, RefreshRequest, Token
from app.schemas.user import (
    ForgotPasswordRequest,
    ResetPasswordRequest,
    SetPasswordRequest,
    UserCreate,
    UserRead,
    UserUpdate,
)
from app.services import user_service

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=UserRead, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def register(
    request: Request, user_in: UserCreate, db: AsyncSession = Depends(get_db)
) -> User:
    """Public self-registration. Always creates a plain 'user' — admins are seeded, not signed up."""
    existing = await user_service.get_user_by_email(db, user_in.email)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Email already registered"
        )
    return await user_service.create_user(db, user_in, role=Role.user)


@router.post("/login", response_model=Token)
@limiter.limit("10/minute")
async def login(
    request: Request,
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
) -> Token:
    """OAuth2 password flow: send email as 'username' and the password, form-encoded."""
    user = await user_service.get_user_by_email(db, form_data.username)
    if (
        user is None
        or user.hashed_password is None
        or not verify_password(form_data.password, user.hashed_password)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive account")

    return Token(
        access_token=create_access_token(str(user.id), user.role.value),
        refresh_token=create_refresh_token(str(user.id), user.role.value),
    )


@router.post("/refresh", response_model=Token)
@limiter.limit("20/minute")
async def refresh(
    request: Request, payload: RefreshRequest, db: AsyncSession = Depends(get_db)
) -> Token:
    try:
        data = decode_token(payload.refresh_token)
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
        ) from exc

    if data.get("type") != "refresh":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token type")

    user = await db.get(User, uuid.UUID(data["sub"]))
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User no longer valid")

    return Token(
        access_token=create_access_token(str(user.id), user.role.value),
        refresh_token=create_refresh_token(str(user.id), user.role.value),
    )


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
    """Self-service profile edit: display name and password."""
    try:
        return await user_service.update_user(db, current_user, payload)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/forgot-password", status_code=status.HTTP_202_ACCEPTED)
@limiter.limit("3/hour")
async def forgot_password(
    request: Request,
    payload: ForgotPasswordRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Request a password reset email.

    Always returns 202 even if the email doesn't exist, to prevent user enumeration.
    """
    await user_service.send_password_reset_email(db, payload.email)
    return {"message": "If an account exists, a reset email has been sent."}


@router.post("/reset-password", status_code=status.HTTP_200_OK)
@limiter.limit("5/hour")
async def reset_password(
    request: Request,
    payload: ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    """Reset the password using a valid token."""
    if not await user_service.reset_password(db, payload.token, payload.new_password):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired reset token",
        )
    return {"message": "Password has been reset. You can now log in."}


@router.delete("/me", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("1/hour")
async def delete_account(
    request: Request,
    payload: UserUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Delete the current user's account and all associated data.

    This is a hard delete: the user, their saved items, tracked products,
    alert history, and notification preferences are all removed.
    Verification reports submitted by the user are preserved (without the
    account attached) because they are public attestations.

    Requires the current password to confirm the deletion.
    """
    if (
        not payload.current_password
        or current_user.hashed_password is None
        or not verify_password(payload.current_password, current_user.hashed_password)
    ):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="The password you entered is not correct.",
        )
    await user_service.delete_user(db, current_user)
    # The response is 204 with no content


@router.post("/google", response_model=Token)
@limiter.limit("10/minute")
async def google_login(
    request: Request, payload: GoogleLoginRequest, db: AsyncSession = Depends(get_db)
) -> Token:
    """Sign in (or sign up) with a Google ID token.

    Verifies the token with Google, then finds or creates a local user.
    Google-only accounts have no password.
    """
    settings = get_settings()
    client_id = settings.google_client_id
    if not client_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Google login not configured"
        )

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            "https://oauth2.googleapis.com/tokeninfo",
            params={"id_token": payload.id_token},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid Google token")

    info = resp.json()
    if info.get("aud") != client_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Google token audience mismatch"
        )
    email = info.get("email")
    if not email or not info.get("email_verified"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Google email not verified"
        )

    name = info.get("name")
    picture = info.get("picture")

    user = await user_service.get_user_by_email(db, email)
    if user is None:
        user = User(
            email=email,
            hashed_password=None,
            full_name=name,
            avatar_url=picture,
            role=Role.user,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
    else:
        # Keep the Google-sourced profile fresh on every sign-in: the photo URL
        # changes when the account's picture does, and an account that started
        # with email + password may only sign in with Google much later.
        changed = False
        if picture and picture != user.avatar_url:
            user.avatar_url = picture
            changed = True
        if name and not (user.full_name or "").strip():
            user.full_name = name
            changed = True
        if changed:
            await db.commit()

    if not user.is_active:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Inactive account")

    return Token(
        access_token=create_access_token(str(user.id), user.role.value),
        refresh_token=create_refresh_token(str(user.id), user.role.value),
    )


@router.post("/set-password", status_code=status.HTTP_204_NO_CONTENT)
@limiter.limit("5/hour")
async def set_password(
    request: Request,
    payload: SetPasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Set a password for an account that has none (e.g. Google-only)."""
    current_user.hashed_password = hash_password(payload.new_password)
    await db.commit()


@router.get("/admin-ping")
async def admin_ping(current_user: User = Depends(require_role(Role.admin))) -> dict[str, str]:
    """Demo endpoint proving RBAC works — delete once real admin routes exist (Phase 4)."""
    return {"message": f"Hello, admin {current_user.email}"}
