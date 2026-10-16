import uuid

API = "/api/v1/coupons"


async def _store_and_category(client, headers):
    store = (await client.post("/api/v1/stores", json={"name": "Amazon"}, headers=headers)).json()
    cat = (
        await client.post("/api/v1/categories", json={"name": "Electronics"}, headers=headers)
    ).json()
    return store["id"], cat["id"]


def _payload(store_id, category_id, **overrides):
    body = {
        "title": "20% off headphones",
        "code": "SAVE20",
        "discount_type": "percentage",
        "discount_value": 20,
        "store_id": store_id,
        "category_id": category_id,
        "destination_url": "https://amazon.com/deal?tag=aff",
    }
    body.update(overrides)
    return body


async def test_coupon_crud(client, admin_headers):
    store_id, cat_id = await _store_and_category(client, admin_headers)

    created = await client.post(API, json=_payload(store_id, cat_id), headers=admin_headers)
    assert created.status_code == 201
    coupon = created.json()
    assert coupon["slug"] == "20-off-headphones"
    assert coupon["discount_value"] == 20
    assert coupon["clicks_count"] == 0

    assert (await client.get(f"{API}/by-slug/20-off-headphones")).json()["id"] == coupon["id"]

    updated = await client.patch(
        f"{API}/{coupon['id']}", json={"code": "SAVE25"}, headers=admin_headers
    )
    assert updated.json()["code"] == "SAVE25"

    assert (await client.delete(f"{API}/{coupon['id']}", headers=admin_headers)).status_code == 204


async def test_deal_without_code(client, admin_headers):
    store_id, cat_id = await _store_and_category(client, admin_headers)
    body = _payload(store_id, cat_id, code=None, discount_type="deal", discount_value=None)
    created = await client.post(API, json=body, headers=admin_headers)
    assert created.status_code == 201
    assert created.json()["code"] is None


async def test_unknown_store_or_category_returns_404(client, admin_headers):
    store_id, cat_id = await _store_and_category(client, admin_headers)

    bad_store = _payload(str(uuid.uuid4()), cat_id)
    assert (await client.post(API, json=bad_store, headers=admin_headers)).status_code == 404

    bad_cat = _payload(store_id, str(uuid.uuid4()))
    assert (await client.post(API, json=bad_cat, headers=admin_headers)).status_code == 404


async def test_invalid_discount_type_rejected(client, admin_headers):
    store_id, cat_id = await _store_and_category(client, admin_headers)
    body = _payload(store_id, cat_id, discount_type="bogus")
    assert (await client.post(API, json=body, headers=admin_headers)).status_code == 422


async def test_filters_and_active_only(client, admin_headers):
    store_id, cat_id = await _store_and_category(client, admin_headers)
    other_store = (
        await client.post("/api/v1/stores", json={"name": "eBay"}, headers=admin_headers)
    ).json()["id"]

    await client.post(
        API, json=_payload(store_id, cat_id, title="Laptop deal"), headers=admin_headers
    )
    await client.post(
        API, json=_payload(other_store, cat_id, title="Phone deal"), headers=admin_headers
    )
    hidden = (
        await client.post(
            API, json=_payload(store_id, cat_id, title="Old deal"), headers=admin_headers
        )
    ).json()
    await client.patch(f"{API}/{hidden['id']}", json={"is_active": False}, headers=admin_headers)

    assert (await client.get(API)).json()["total"] == 2  # inactive hidden by default
    assert (await client.get(API, params={"active_only": False})).json()["total"] == 3
    assert (await client.get(API, params={"store_id": other_store})).json()["total"] == 1
    assert (await client.get(API, params={"search": "laptop"})).json()["total"] == 1
    assert (await client.get(API, params={"category_id": cat_id})).json()["total"] == 2


async def test_offer_shape_and_trust_filters(client, admin_headers):
    """Every storefront filter narrows the row set server-side, so `total` is exact."""
    store_id, cat_id = await _store_and_category(client, admin_headers)

    coded = (
        await client.post(
            API, json=_payload(store_id, cat_id, title="Coded percent off"), headers=admin_headers
        )
    ).json()
    deal = (
        await client.post(
            API,
            json=_payload(
                store_id,
                cat_id,
                title="Codeless deal",
                code=None,
                discount_type="deal",
                discount_value=None,
            ),
            headers=admin_headers,
        )
    ).json()
    # Fixed-amount coupon — created for the discount_type filter assertions below.
    await client.post(
        API,
        json=_payload(
            store_id, cat_id, title="Flat amount off", discount_type="fixed", discount_value=500
        ),
        headers=admin_headers,
    )

    # One confirmation behind the coded coupon, one failed report behind the deal,
    # so the success-rate filter has something to separate.
    assert (
        await client.post(f"{API}/{coded['id']}/verify", json={"worked": True})
    ).status_code == 200
    assert (
        await client.post(f"{API}/{deal['id']}/verify", json={"worked": False})
    ).status_code == 200

    assert (await client.get(API, params={"discount_type": "percentage"})).json()["total"] == 1
    assert (await client.get(API, params={"discount_type": "fixed"})).json()["total"] == 1
    assert (await client.get(API, params={"discount_type": "deal"})).json()["total"] == 1
    assert (await client.get(API, params={"discount_type": "bogus"})).status_code == 422

    assert (await client.get(API, params={"has_code": True})).json()["total"] == 2
    assert (await client.get(API, params={"has_code": False})).json()["total"] == 1

    # "Somebody has confirmed this" vs "nobody has tried it yet".
    assert (await client.get(API, params={"verified": True})).json()["total"] == 2
    assert (await client.get(API, params={"verified": False})).json()["total"] == 1

    # 1/1 reports passes 0.5; 0/1 fails it; the never-reported coupon has no
    # rate at all and is excluded even when the threshold is zero.
    assert (await client.get(API, params={"min_success_rate": 0.0})).json()["total"] == 2
    assert (await client.get(API, params={"min_success_rate": 0.5})).json()["total"] == 1
    assert (await client.get(API, params={"min_success_rate": 1.5})).status_code == 422

    # Filters compose: a coded percentage offer at a store, and the count stays exact.
    combined = await client.get(
        API, params={"store_id": store_id, "discount_type": "percentage", "has_code": True}
    )
    assert combined.json()["total"] == 1
    assert combined.json()["items"][0]["id"] == coded["id"]


async def test_created_by_is_recorded(client, editor_headers, async_session_maker):
    store_id, cat_id = await _store_and_category(client, editor_headers)
    created = await client.post(API, json=_payload(store_id, cat_id), headers=editor_headers)
    assert created.status_code == 201

    from app.models.coupon import Coupon

    async with async_session_maker() as session:
        coupon = await session.get(Coupon, uuid.UUID(created.json()["id"]))
        assert coupon.created_by is not None


async def test_coupon_rbac(client, user_headers, editor_headers, admin_headers):
    store_id, cat_id = await _store_and_category(client, admin_headers)
    body = _payload(store_id, cat_id)

    assert (await client.post(API, json=body)).status_code == 401
    assert (await client.post(API, json=body, headers=user_headers)).status_code == 403

    coupon_id = (await client.post(API, json=body, headers=editor_headers)).json()["id"]
    assert (await client.delete(f"{API}/{coupon_id}", headers=editor_headers)).status_code == 403
    assert (await client.delete(f"{API}/{coupon_id}", headers=admin_headers)).status_code == 204


async def test_naive_expiry_rejected_but_aware_accepted(client, admin_headers):
    """Postgres (asyncpg) can't store naive datetimes in timestamptz columns, so reject them."""
    store_id, cat_id = await _store_and_category(client, admin_headers)

    naive = _payload(store_id, cat_id, expires_at="2030-01-01T00:00:00")
    resp = await client.post(API, json=naive, headers=admin_headers)
    assert resp.status_code == 422

    aware = _payload(store_id, cat_id, expires_at="2030-01-01T00:00:00Z")
    assert (await client.post(API, json=aware, headers=admin_headers)).status_code == 201


async def test_public_read_hides_destination_url(client, admin_headers):
    store_id, cat_id = await _store_and_category(client, admin_headers)
    coupon = (await client.post(API, json=_payload(store_id, cat_id), headers=admin_headers)).json()
    assert "destination_url" in coupon  # staff-facing create response still has it

    public_view = (await client.get(f"{API}/{coupon['id']}")).json()
    assert "destination_url" not in public_view

    public_list_item = (await client.get(API)).json()["items"][0]
    assert "destination_url" not in public_list_item


async def test_go_redirect_logs_click_and_reveals_url(client, admin_headers):
    store_id, cat_id = await _store_and_category(client, admin_headers)
    coupon = (
        await client.post(
            API,
            json=_payload(store_id, cat_id, destination_url="https://amazon.com/real-deal"),
            headers=admin_headers,
        )
    ).json()
    assert coupon["clicks_count"] == 0

    resp = await client.get(f"{API}/{coupon['id']}/go", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://amazon.com/real-deal"

    updated = await client.get(f"{API}/{coupon['id']}")
    assert updated.json()["clicks_count"] == 1


async def test_go_redirect_404s_for_inactive_or_missing(client, admin_headers):
    store_id, cat_id = await _store_and_category(client, admin_headers)
    coupon = (await client.post(API, json=_payload(store_id, cat_id), headers=admin_headers)).json()
    await client.patch(f"{API}/{coupon['id']}", json={"is_active": False}, headers=admin_headers)

    assert (await client.get(f"{API}/{coupon['id']}/go")).status_code == 404
    assert (await client.get(f"{API}/{uuid.uuid4()}/go")).status_code == 404


async def test_verify_updates_counters_and_rate_limits(client, admin_headers):
    store_id, cat_id = await _store_and_category(client, admin_headers)
    coupon = (await client.post(API, json=_payload(store_id, cat_id), headers=admin_headers)).json()

    ok = await client.post(f"{API}/{coupon['id']}/verify", json={"worked": True})
    assert ok.status_code == 200
    body = ok.json()
    assert body["success_count"] == 1 and body["fail_count"] == 0
    assert body["success_rate"] == 1.0
    assert body["last_verified_at"] is not None

    again = await client.post(f"{API}/{coupon['id']}/verify", json={"worked": False})
    assert again.status_code == 429  # same client IP, still in cooldown


async def test_verify_success_rate_reflects_in_get(client, admin_headers):
    store_id, cat_id = await _store_and_category(client, admin_headers)
    coupon = (await client.post(API, json=_payload(store_id, cat_id), headers=admin_headers)).json()

    await client.post(f"{API}/{coupon['id']}/verify", json={"worked": True})
    fetched = await client.get(f"{API}/{coupon['id']}")
    assert fetched.json()["success_rate"] == 1.0
    assert fetched.json()["success_count"] == 1


async def test_verify_nonexistent_coupon_404s(client):
    resp = await client.post(f"{API}/{uuid.uuid4()}/verify", json={"worked": True})
    assert resp.status_code == 404


async def test_verification_history_endpoint(client, admin_headers):
    """Test the verification history endpoint returns history items."""
    store_id, cat_id = await _store_and_category(client, admin_headers)
    coupon = (await client.post(API, json=_payload(store_id, cat_id), headers=admin_headers)).json()

    # Add some verifications via direct API (simulating different IPs)
    # We can't easily test different IPs, but we can test the endpoint exists
    history = await client.get(f"{API}/{coupon['id']}/verification-history")
    assert history.status_code == 200
    data = history.json()
    assert isinstance(data, list)
    # Initially empty
    assert len(data) == 0

    # After a verification
    await client.post(f"{API}/{coupon['id']}/verify", json={"worked": True})
    history = await client.get(f"{API}/{coupon['id']}/verification-history")
    assert history.status_code == 200
    data = history.json()
    assert len(data) == 1
    assert data[0]["worked"] is True
    assert "id" in data[0]
    assert "created_at" in data[0]


async def test_verification_history_limit(client, admin_headers):
    """Test the limit parameter on verification history."""
    store_id, cat_id = await _store_and_category(client, admin_headers)
    coupon = (await client.post(API, json=_payload(store_id, cat_id), headers=admin_headers)).json()

    history = await client.get(f"{API}/{coupon['id']}/verification-history?limit=5")
    assert history.status_code == 200
    data = history.json()
    assert len(data) <= 5


async def test_verify_accepts_optional_note(client, admin_headers):
    """A conditional code needs to explain itself, not just vote yes/no."""
    store_id, cat_id = await _store_and_category(client, admin_headers)
    coupon = (await client.post(API, json=_payload(store_id, cat_id), headers=admin_headers)).json()

    resp = await client.post(
        f"{API}/{coupon['id']}/verify",
        json={"worked": True, "note": "Works, but excludes sale items."},
    )
    assert resp.status_code == 200

    history = await client.get(f"{API}/{coupon['id']}/verification-history")
    assert history.json()[0]["note"] == "Works, but excludes sale items."


async def test_verify_rejects_overlong_note(client, admin_headers):
    store_id, cat_id = await _store_and_category(client, admin_headers)
    coupon = (await client.post(API, json=_payload(store_id, cat_id), headers=admin_headers)).json()

    resp = await client.post(
        f"{API}/{coupon['id']}/verify",
        json={"worked": True, "note": "x" * 281},
    )
    assert resp.status_code == 422


async def test_verification_history_never_exposes_ip_hash(client, admin_headers):
    """Visitors get the aggregate signal, never a per-voter identifier."""
    store_id, cat_id = await _store_and_category(client, admin_headers)
    coupon = (await client.post(API, json=_payload(store_id, cat_id), headers=admin_headers)).json()

    await client.post(f"{API}/{coupon['id']}/verify", json={"worked": True})

    history = await client.get(f"{API}/{coupon['id']}/verification-history")
    item = history.json()[0]
    assert set(item) == {"id", "worked", "created_at", "note"}
    assert "ip_hash" not in item
