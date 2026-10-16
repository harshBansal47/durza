import json

import pytest
from httpx import AsyncClient


async def _signed_in(
    client: AsyncClient, email: str, full_name: str | None = None
) -> dict[str, str]:
    """Register, sign in, and return auth headers for that account.

    Goes through the real login flow rather than minting a token by hand, so
    these tests exercise the credentials the API actually accepts.
    """
    await client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "supersecret123", "full_name": full_name},
    )
    login_resp = await client.post(
        "/api/v1/auth/login",
        data={"username": email, "password": "supersecret123"},
    )
    assert login_resp.status_code == 200
    return {"Authorization": f"Bearer {login_resp.json()['access_token']}"}


@pytest.mark.asyncio
async def test_register_and_login_flow(client):
    register_resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "user@example.com", "password": "supersecret123"},
    )
    assert register_resp.status_code == 201
    body = register_resp.json()
    assert body["email"] == "user@example.com"
    assert body["role"] == "user"

    login_resp = await client.post(
        "/api/v1/auth/login",
        data={"username": "user@example.com", "password": "supersecret123"},
    )
    assert login_resp.status_code == 200
    tokens = login_resp.json()
    assert "access_token" in tokens
    assert "refresh_token" in tokens


@pytest.mark.asyncio
async def test_duplicate_registration_rejected(client):
    payload = {"email": "dupe@example.com", "password": "supersecret123"}
    first = await client.post("/api/v1/auth/register", json=payload)
    assert first.status_code == 201

    second = await client.post("/api/v1/auth/register", json=payload)
    assert second.status_code == 400


@pytest.mark.asyncio
async def test_login_rejects_wrong_password(client):
    await client.post(
        "/api/v1/auth/register",
        json={"email": "wrongpw@example.com", "password": "supersecret123"},
    )
    resp = await client.post(
        "/api/v1/auth/login",
        data={"username": "wrongpw@example.com", "password": "not-the-password"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_requires_auth(client):
    resp = await client.get("/api/v1/auth/me")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_me_returns_current_user(client):
    await client.post(
        "/api/v1/auth/register",
        json={"email": "me@example.com", "password": "supersecret123"},
    )
    login_resp = await client.post(
        "/api/v1/auth/login",
        data={"username": "me@example.com", "password": "supersecret123"},
    )
    token = login_resp.json()["access_token"]

    resp = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["email"] == "me@example.com"


@pytest.mark.asyncio
async def test_admin_only_endpoint_forbidden_for_regular_user(client):
    await client.post(
        "/api/v1/auth/register",
        json={"email": "regular@example.com", "password": "supersecret123"},
    )
    login_resp = await client.post(
        "/api/v1/auth/login",
        data={"username": "regular@example.com", "password": "supersecret123"},
    )
    token = login_resp.json()["access_token"]

    resp = await client.get("/api/v1/auth/admin-ping", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_only_endpoint_allowed_for_admin(client, async_session_maker):
    from app.core.security import create_access_token
    from app.models.user import Role
    from app.schemas.user import UserCreate
    from app.services import user_service

    async with async_session_maker() as session:
        admin = await user_service.create_user(
            session,
            UserCreate(email="admin@example.com", password="supersecret123"),
            role=Role.admin,
        )
        token = create_access_token(str(admin.id), admin.role.value)

    resp = await client.get("/api/v1/auth/admin-ping", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert "admin@example.com" in resp.json()["message"]


@pytest.mark.asyncio
async def test_refresh_token_issues_new_access_token(client):
    await client.post(
        "/api/v1/auth/register",
        json={"email": "refresh@example.com", "password": "supersecret123"},
    )
    login_resp = await client.post(
        "/api/v1/auth/login",
        data={"username": "refresh@example.com", "password": "supersecret123"},
    )
    refresh_token = login_resp.json()["refresh_token"]

    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert resp.status_code == 200
    assert "access_token" in resp.json()


@pytest.mark.asyncio
async def test_access_token_rejected_by_refresh_endpoint(client):
    login_payload = {"email": "wrongtype@example.com", "password": "supersecret123"}
    await client.post("/api/v1/auth/register", json=login_payload)
    login_resp = await client.post(
        "/api/v1/auth/login",
        data={"username": "wrongtype@example.com", "password": "supersecret123"},
    )
    access_token = login_resp.json()["access_token"]

    resp = await client.post("/api/v1/auth/refresh", json={"refresh_token": access_token})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_update_me_changes_display_name(client):
    """PATCH /auth/me is how a user sets the name shown in the header."""
    headers = await _signed_in(client, "profile@example.com")

    response = await client.patch(
        "/api/v1/auth/me",
        json={"full_name": "Ada Lovelace"},
        headers={**headers, "Content-Type": "application/json"},
    )

    assert response.status_code == 200
    assert response.json()["full_name"] == "Ada Lovelace"

    me = await client.request(
        "GET", "/api/v1/auth/me", headers={**headers, "Content-Type": "application/json"}
    )
    assert me.json()["full_name"] == "Ada Lovelace"


@pytest.mark.asyncio
async def test_update_me_clears_display_name(client):
    headers = await _signed_in(client, "clearname@example.com", full_name="Temporary")

    response = await client.patch(
        "/api/v1/auth/me",
        json={"full_name": ""},
        headers={**headers, "Content-Type": "application/json"},
    )

    assert response.status_code == 200
    assert response.json()["full_name"] is None


@pytest.mark.asyncio
async def test_update_me_rejects_password_change_without_current_password(client):
    """Changing a password must be proven, or a stolen session could lock the owner out."""
    headers = await _signed_in(client, "nopassword@example.com")

    response = await client.patch(
        "/api/v1/auth/me",
        json={"password": "brand-new-password"},
        headers={**headers, "Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert "current password" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_update_me_rejects_wrong_current_password(client):
    headers = await _signed_in(client, "wrongcurrent@example.com")

    response = await client.patch(
        "/api/v1/auth/me",
        json={"password": "brand-new-password", "current_password": "not-the-password"},
        headers={**headers, "Content-Type": "application/json"},
    )

    assert response.status_code == 400


@pytest.mark.asyncio
async def test_update_me_changes_password(client):
    headers = await _signed_in(client, "rotate@example.com")

    accepted = await client.patch(
        "/api/v1/auth/me",
        json={"password": "brand-new-password", "current_password": "supersecret123"},
        headers={**headers, "Content-Type": "application/json"},
    )
    assert accepted.status_code == 200

    stale = await client.post(
        "/api/v1/auth/login",
        data={"username": "rotate@example.com", "password": "supersecret123"},
    )
    assert stale.status_code == 401

    fresh = await client.post(
        "/api/v1/auth/login",
        data={"username": "rotate@example.com", "password": "brand-new-password"},
    )
    assert fresh.status_code == 200


@pytest.mark.asyncio
async def test_update_me_rejects_unknown_fields(client):
    """Role and email must not be settable through the profile endpoint."""
    headers = await _signed_in(client, "escalate@example.com")

    response = await client.patch(
        "/api/v1/auth/me",
        json={"email": "someone-else@example.com", "role": "admin"},
        headers={**headers, "Content-Type": "application/json"},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_update_me_rejects_short_password(client):
    headers = await _signed_in(client, "short@example.com")

    response = await client.patch(
        "/api/v1/auth/me",
        json={"password": "short", "current_password": "supersecret123"},
        headers={**headers, "Content-Type": "application/json"},
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_update_me_requires_authentication(client):
    response = await client.patch("/api/v1/auth/me", json={"full_name": "Nobody"})
    assert response.status_code == 401


# ---- Account deletion tests ----


@pytest.mark.asyncio
async def test_delete_account_requires_correct_password(client):
    """Deleting an account must require the correct current password."""
    headers = await _signed_in(client, "delete_me@example.com", full_name="Delete Me")
    # Wrong password should be rejected
    resp = await client.request(
        "DELETE",
        "/api/v1/auth/me",
        content=json.dumps({"current_password": "wrong-password"}),
        headers={**headers, "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert "The password you entered is not correct" in resp.json()["detail"]
    # Verify account still exists
    me = await client.request(
        "GET", "/api/v1/auth/me", headers={**headers, "Content-Type": "application/json"}
    )
    assert me.status_code == 200


@pytest.mark.asyncio
async def test_delete_account_rejects_missing_password(client):
    """Omitting the password field must be rejected."""
    headers = await _signed_in(client, "missing_pw@example.com")
    resp = await client.request(
        "DELETE",
        "/api/v1/auth/me",
        content=json.dumps({}),
        headers={**headers, "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert "The password you entered is not correct" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_delete_account_rejects_empty_password(client):
    """Empty string password must be rejected."""
    headers = await _signed_in(client, "empty_pw@example.com")
    resp = await client.request(
        "DELETE",
        "/api/v1/auth/me",
        content=json.dumps({"current_password": ""}),
        headers={**headers, "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    assert "The password you entered is not correct" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_delete_account_succeeds_with_correct_password(client):
    """Correct password deletes the account and logs the user out."""
    headers = await _signed_in(client, "delete_ok@example.com")
    resp = await client.request(
        "DELETE",
        "/api/v1/auth/me",
        content=json.dumps({"current_password": "supersecret123"}),
        headers={**headers, "Content-Type": "application/json"},
    )
    assert resp.status_code == 204
    # Subsequent requests with the same token should fail
    me = await client.request(
        "GET", "/api/v1/auth/me", headers={**headers, "Content-Type": "application/json"}
    )
    assert me.status_code == 401


@pytest.mark.asyncio
async def test_delete_account_requires_authentication(client):
    """Unauthenticated requests must be rejected."""
    resp = await client.request(
        "DELETE", "/api/v1/auth/me", content=json.dumps({"current_password": "anything"})
    )
    assert resp.status_code == 401
