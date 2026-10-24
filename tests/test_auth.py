"""Authentication: passwordless email codes, Google, refresh, and account lifecycle.

Public password sign-in and password reset are gone. These tests exercise what
replaced them end to end through the HTTP API, and keep the Google profile
behaviour that predates the change. Delivery is always stubbed at the sender
boundary: no test talks to a real SMTP server, and no test reads a code from a
log because the application never writes one there.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Self

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import select

from app.core.config import get_settings
from app.core.security import create_access_token
from app.models.user import EmailOtpChallenge, Role, User
from app.schemas.user import UserCreate
from app.services import email_otp, user_service


class OtpOutbox:
    """Captures the plaintext code at the only boundary where it exists."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []

    async def __call__(self, to: str, content: object) -> bool:
        self.sent.append((to, content))
        return True

    @property
    def last_code(self) -> str:
        return self.sent[-1][1].code  # type: ignore[attr-defined]


@pytest.fixture
def otp_outbox(monkeypatch) -> OtpOutbox:
    outbox = OtpOutbox()
    monkeypatch.setattr(email_otp, "send_otp_email", outbox)
    return outbox


async def _request_code(client: AsyncClient, email: str) -> None:
    response = await client.post("/api/v1/auth/email/request", json={"email": email})
    assert response.status_code == 202


async def _otp_tokens(client: AsyncClient, outbox: OtpOutbox, email: str) -> dict:
    await _request_code(client, email)
    code = outbox.last_code
    response = await client.post("/api/v1/auth/email/verify", json={"email": email, "code": code})
    assert response.status_code == 200, response.text
    return response.json()


async def _otp_headers(client: AsyncClient, outbox: OtpOutbox, email: str) -> dict[str, str]:
    tokens = await _otp_tokens(client, outbox, email)
    return {"Authorization": f"Bearer {tokens['access_token']}"}


async def _signed_in(
    async_session_maker,
    email: str,
    *,
    full_name: str | None = None,
    role: Role = Role.user,
    password: str | None = None,
) -> dict[str, str]:
    """Mint a token for an existing account without going through sign-in.

    Used by tests whose subject is not the sign-in flow itself.
    """
    async with async_session_maker() as session:
        user = await user_service.create_user(
            session,
            UserCreate(email=email, password=password, full_name=full_name),
            role=role,
        )
        token = create_access_token(str(user.id), user.role.value, user.token_version)
    return {"Authorization": f"Bearer {token}"}


# ---- Email OTP ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_is_generic_for_known_and_unknown_addresses(
    client, otp_outbox, async_session_maker
):
    await _signed_in(async_session_maker, "known@example.com")

    unknown = await client.post("/api/v1/auth/email/request", json={"email": "nobody@example.com"})
    known = await client.post("/api/v1/auth/email/request", json={"email": "known@example.com"})

    assert unknown.status_code == known.status_code == 202
    assert unknown.json() == known.json()
    assert [to for to, _ in otp_outbox.sent] == ["nobody@example.com", "known@example.com"]


@pytest.mark.asyncio
async def test_otp_sign_in_creates_a_plain_user_and_stores_only_a_digest(
    client, otp_outbox, async_session_maker
):
    tokens = await _otp_tokens(client, otp_outbox, "New.User@Example.COM")
    code = otp_outbox.last_code

    me = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert me.status_code == 200
    body = me.json()
    assert body["email"] == "new.user@example.com"
    assert body["role"] == "user"
    assert "has_password" not in body

    async with async_session_maker() as db:
        user = (await db.execute(select(User))).scalar_one()
        challenge = (await db.execute(select(EmailOtpChallenge))).scalar_one()

    assert user.hashed_password is None
    assert user.email_verified_at is not None
    assert user.token_version == 0
    assert challenge.email == "new.user@example.com"
    assert challenge.code_hash != code
    assert challenge.consumed_at is not None


@pytest.mark.asyncio
async def test_otp_sign_in_preserves_an_existing_staff_password(
    client, otp_outbox, async_session_maker
):
    async with async_session_maker() as db:
        existing = await user_service.create_user(
            db, UserCreate(email="staff@example.com", password="supersecret123"), role=Role.editor
        )
        original_hash = existing.hashed_password

    tokens = await _otp_tokens(client, otp_outbox, "staff@example.com")
    me = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )

    assert me.json()["role"] == "editor"
    async with async_session_maker() as db:
        refreshed = await db.get(User, existing.id)
    assert refreshed.hashed_password == original_hash


@pytest.mark.asyncio
async def test_wrong_code_exhausts_attempts_and_then_rejects_the_right_one(client, otp_outbox):
    await _request_code(client, "attempts@example.com")
    code = otp_outbox.last_code
    wrong = "000000" if code != "000000" else "111111"

    for _ in range(get_settings().otp_max_attempts):
        response = await client.post(
            "/api/v1/auth/email/verify",
            json={"email": "attempts@example.com", "code": wrong},
        )
        assert response.status_code == 400

    # The right code cannot rescue a challenge that has spent its budget.
    response = await client.post(
        "/api/v1/auth/email/verify", json={"email": "attempts@example.com", "code": code}
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_code_is_single_use(client, otp_outbox):
    await _request_code(client, "once@example.com")
    code = otp_outbox.last_code

    first = await client.post(
        "/api/v1/auth/email/verify", json={"email": "once@example.com", "code": code}
    )
    second = await client.post(
        "/api/v1/auth/email/verify", json={"email": "once@example.com", "code": code}
    )

    assert first.status_code == 200
    assert second.status_code == 400


@pytest.mark.asyncio
async def test_resend_invalidates_the_previous_code(client, otp_outbox, monkeypatch):
    monkeypatch.setattr(get_settings(), "otp_resend_cooldown_seconds", 0)
    await _request_code(client, "resend@example.com")
    first_code = otp_outbox.last_code

    await _request_code(client, "resend@example.com")
    second_code = otp_outbox.last_code

    stale = await client.post(
        "/api/v1/auth/email/verify",
        json={"email": "resend@example.com", "code": first_code},
    )
    fresh = await client.post(
        "/api/v1/auth/email/verify",
        json={"email": "resend@example.com", "code": second_code},
    )

    assert stale.status_code == 400
    assert fresh.status_code == 200


@pytest.mark.asyncio
async def test_expired_code_is_rejected(client, otp_outbox, async_session_maker):
    await _request_code(client, "expired@example.com")
    code = otp_outbox.last_code

    async with async_session_maker() as db:
        challenge = (await db.execute(select(EmailOtpChallenge))).scalar_one()
        challenge.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        await db.commit()

    response = await client.post(
        "/api/v1/auth/email/verify", json={"email": "expired@example.com", "code": code}
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_inactive_account_cannot_finish_otp_sign_in(client, otp_outbox, async_session_maker):
    async with async_session_maker() as db:
        user = await user_service.create_user(db, UserCreate(email="off@example.com"))
        user.is_active = False
        await db.commit()

    await _request_code(client, "off@example.com")
    response = await client.post(
        "/api/v1/auth/email/verify",
        json={"email": "off@example.com", "code": otp_outbox.last_code},
    )
    assert response.status_code == 403


# ---- Refresh and logout ------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_issues_new_tokens(client, otp_outbox):
    tokens = await _otp_tokens(client, otp_outbox, "refresh@example.com")

    response = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert response.status_code == 200
    assert "access_token" in response.json()


@pytest.mark.asyncio
async def test_refresh_rejects_an_access_token(client, otp_outbox):
    tokens = await _otp_tokens(client, otp_outbox, "wrongtype@example.com")

    response = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["access_token"]}
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_logout_revokes_access_and_refresh_tokens(client, otp_outbox):
    tokens = await _otp_tokens(client, otp_outbox, "logout@example.com")
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}

    assert (await client.post("/api/v1/auth/logout", headers=headers)).status_code == 204
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401
    refreshed = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
    )
    assert refreshed.status_code == 401


# ---- Profile and deletion ----------------------------------------------------


@pytest.mark.asyncio
async def test_update_me_changes_and_clears_display_name(client, async_session_maker):
    headers = await _signed_in(async_session_maker, "profile@example.com", full_name="Temp")

    changed = await client.patch(
        "/api/v1/auth/me", json={"full_name": "Ada Lovelace"}, headers=headers
    )
    cleared = await client.patch("/api/v1/auth/me", json={"full_name": ""}, headers=headers)

    assert changed.status_code == 200
    assert changed.json()["full_name"] == "Ada Lovelace"
    assert cleared.status_code == 200
    assert cleared.json()["full_name"] is None


@pytest.mark.asyncio
async def test_update_me_rejects_credentials_and_unknown_fields(client, async_session_maker):
    headers = await _signed_in(async_session_maker, "escalate@example.com")

    response = await client.patch(
        "/api/v1/auth/me",
        json={"email": "someone-else@example.com", "role": "admin", "password": "new-password"},
        headers=headers,
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_update_me_requires_authentication(client):
    assert (await client.patch("/api/v1/auth/me", json={"full_name": "Nobody"})).status_code == 401


@pytest.mark.asyncio
async def test_delete_account_requires_the_matching_email(client, async_session_maker):
    headers = await _signed_in(async_session_maker, "delete@example.com")

    wrong = await client.request(
        "DELETE", "/api/v1/auth/me", json={"confirm_email": "other@example.com"}, headers=headers
    )
    me_after_wrong = await client.get("/api/v1/auth/me", headers=headers)

    assert wrong.status_code == 400
    assert me_after_wrong.status_code == 200


@pytest.mark.asyncio
async def test_delete_account_succeeds_with_matching_email(client, async_session_maker):
    headers = await _signed_in(async_session_maker, "delete-me@example.com")

    response = await client.request(
        "DELETE",
        "/api/v1/auth/me",
        json={"confirm_email": "delete-me@example.com"},
        headers=headers,
    )

    assert response.status_code == 204
    assert (await client.get("/api/v1/auth/me", headers=headers)).status_code == 401


@pytest.mark.asyncio
async def test_delete_account_requires_email_confirmation(client, async_session_maker):
    headers = await _signed_in(async_session_maker, "missing-confirm@example.com")
    response = await client.request("DELETE", "/api/v1/auth/me", json={}, headers=headers)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_delete_account_requires_authentication(client):
    response = await client.request("DELETE", "/api/v1/auth/me", json={"confirm_email": "a@b.co"})
    assert response.status_code == 401


# ---- Google sign-in ----------------------------------------------------------


class _StubTokeninfo:
    """Stands in for httpx.AsyncClient around the tokeninfo GET."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def get(self, url: str, params: dict | None = None) -> Response:
        return Response(200, json=self._payload)


def _stub_google(monkeypatch, payload: dict) -> None:
    from app.routers.api.v1 import auth as auth_router

    monkeypatch.setattr(
        auth_router, "get_settings", lambda: SimpleNamespace(google_client_id="test-client-id")
    )
    monkeypatch.setattr(auth_router.httpx, "AsyncClient", lambda **kwargs: _StubTokeninfo(payload))


def _tokeninfo(email: str, **extra: object) -> dict:
    return {
        "aud": "test-client-id",
        "iss": "https://accounts.google.com",
        "email": email,
        "email_verified": True,
        **extra,
    }


@pytest.mark.asyncio
async def test_google_login_stores_avatar_name_and_verified_email(
    client, monkeypatch, async_session_maker
):
    _stub_google(
        monkeypatch,
        _tokeninfo(
            "avatar@example.com",
            name="Ava Tar",
            picture="https://lh3.googleusercontent.com/a/abc=s96-c",
        ),
    )

    response = await client.post("/api/v1/auth/google", json={"id_token": "stub"})
    assert response.status_code == 200
    headers = {"Authorization": f"Bearer {response.json()['access_token']}"}

    me = await client.get("/api/v1/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["full_name"] == "Ava Tar"
    assert me.json()["avatar_url"] == "https://lh3.googleusercontent.com/a/abc=s96-c"

    async with async_session_maker() as db:
        user = (await db.execute(select(User))).scalar_one()
    assert user.email_verified_at is not None
    assert user.hashed_password is None


@pytest.mark.asyncio
async def test_google_login_refreshes_avatar_on_each_sign_in(client, monkeypatch):
    _stub_google(
        monkeypatch,
        _tokeninfo("refresh@example.com", picture="https://lh3.googleusercontent.com/old=s96-c"),
    )
    first = await client.post("/api/v1/auth/google", json={"id_token": "stub"})
    assert first.status_code == 200

    _stub_google(
        monkeypatch,
        _tokeninfo("refresh@example.com", picture="https://lh3.googleusercontent.com/new=s96-c"),
    )
    second = await client.post("/api/v1/auth/google", json={"id_token": "stub"})
    headers = {"Authorization": f"Bearer {second.json()['access_token']}"}
    me = await client.get("/api/v1/auth/me", headers=headers)

    assert me.json()["avatar_url"] == "https://lh3.googleusercontent.com/new=s96-c"


@pytest.mark.asyncio
async def test_google_login_links_existing_account_without_removing_staff_password(
    client, monkeypatch, async_session_maker
):
    await _signed_in(async_session_maker, "linkme@example.com", password="supersecret123")

    _stub_google(monkeypatch, _tokeninfo("LinkMe@Example.com", name="Linked User"))
    response = await client.post("/api/v1/auth/google", json={"id_token": "stub"})

    assert response.status_code == 200
    async with async_session_maker() as db:
        user = (
            await db.execute(select(User).where(User.email == "linkme@example.com"))
        ).scalar_one()
    assert user.hashed_password is not None
    assert user.email_verified_at is not None


@pytest.mark.asyncio
async def test_google_login_rejects_unverified_email(client, monkeypatch):
    # The string "false" is the case a truthiness check gets wrong.
    _stub_google(monkeypatch, _tokeninfo("unverified@example.com", email_verified="false"))

    response = await client.post("/api/v1/auth/google", json={"id_token": "stub"})
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_google_login_rejects_wrong_issuer(client, monkeypatch):
    _stub_google(
        monkeypatch,
        _tokeninfo("issuer@example.com", iss="https://example.com"),
    )

    response = await client.post("/api/v1/auth/google", json={"id_token": "stub"})
    assert response.status_code == 401


# ---- RBAC --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_only_endpoint_forbidden_for_regular_user(client, otp_outbox):
    headers = await _otp_headers(client, otp_outbox, "regular@example.com")
    response = await client.get("/api/v1/auth/admin-ping", headers=headers)
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_admin_only_endpoint_allowed_for_admin(client, async_session_maker):
    headers = await _signed_in(async_session_maker, "admin@example.com", role=Role.admin)
    response = await client.get("/api/v1/auth/admin-ping", headers=headers)
    assert response.status_code == 200
    assert "admin@example.com" in response.json()["message"]


@pytest.mark.asyncio
async def test_removed_password_endpoints_are_not_routed(client):
    for path in (
        "/api/v1/auth/register",
        "/api/v1/auth/login",
        "/api/v1/auth/forgot-password",
        "/api/v1/auth/reset-password",
        "/api/v1/auth/set-password",
    ):
        response = await client.post(path, json={})
        assert response.status_code == 404, path
