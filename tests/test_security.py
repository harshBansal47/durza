import uuid


async def test_cors_headers_present_for_allowed_origin(client):
    """The exact bug this guards against: without CORS headers, a browser
    silently blocks the frontend's JS from reading the API response, even
    though curl/pytest (which don't enforce CORS) would see it work fine."""
    resp = await client.get("/api/v1/categories", headers={"Origin": "http://localhost:3000"})
    assert resp.headers["access-control-allow-origin"] == "http://localhost:3000"


async def test_cors_rejects_unlisted_origin(client):
    resp = await client.get(
        "/api/v1/categories", headers={"Origin": "https://not-our-frontend.example.com"}
    )
    assert "access-control-allow-origin" not in resp.headers


async def test_security_headers_present_on_every_response(client):
    resp = await client.get("/health")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    # HSTS only makes sense once you're actually serving over HTTPS (production).
    assert "strict-transport-security" not in resp.headers


async def test_tracked_redirects_are_noindex(client):
    """Tracked redirects (/go) resolve a click and carry no content: a search
    engine that finds one must not index it. robots.txt lives on the site
    domain and cannot speak for the API domain, so the response carries the
    directive."""
    resp = await client.get(f"/api/v1/coupons/{uuid.uuid4()}/go", follow_redirects=False)
    assert resp.headers.get("x-robots-tag") == "noindex"

    normal = await client.get("/api/v1/categories")
    assert "x-robots-tag" not in normal.headers


async def test_register_is_rate_limited(client):
    for i in range(5):
        resp = await client.post(
            "/api/v1/auth/register",
            json={"email": f"burst{i}@example.com", "password": "supersecret123"},
        )
        assert resp.status_code == 201

    sixth = await client.post(
        "/api/v1/auth/register",
        json={"email": "burst5@example.com", "password": "supersecret123"},
    )
    assert sixth.status_code == 429


async def test_login_is_rate_limited_independently_of_register(client):
    await client.post(
        "/api/v1/auth/register",
        json={"email": "bruteforce@example.com", "password": "supersecret123"},
    )
    for _ in range(10):
        resp = await client.post(
            "/api/v1/auth/login",
            data={"username": "bruteforce@example.com", "password": "wrong-password"},
        )
        assert resp.status_code == 401  # correctly rejected, not yet rate-limited

    eleventh = await client.post(
        "/api/v1/auth/login",
        data={"username": "bruteforce@example.com", "password": "wrong-password"},
    )
    assert eleventh.status_code == 429  # brute-force attempt now throttled
