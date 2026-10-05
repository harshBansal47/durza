"""Discovery orderings must only ever claim what the data supports.

"Trending" means real, recent, non-bot clicks. "Top rated" needs enough reports to
mean anything. "Ending soon" needs a real expiry that has not passed yet.
"""

import uuid
from datetime import UTC, datetime, timedelta

from app.models.click import ClickEvent
from app.models.coupon import Coupon, DiscountType

API = "/api/v1/coupons"


async def _setup(client, headers):
    store = (await client.post("/api/v1/stores", json={"name": "Shop"}, headers=headers)).json()
    cat = (await client.post("/api/v1/categories", json={"name": "Misc"}, headers=headers)).json()
    return store["id"], cat["id"]


async def _coupon(client, headers, store_id, cat_id, title, **fields):
    resp = await client.post(
        API,
        json={
            "title": title,
            "code": title.upper().replace(" ", "")[:12],
            "discount_type": "percentage",
            "discount_value": 10,
            "store_id": store_id,
            "category_id": cat_id,
            "destination_url": "https://shop.example/x",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    coupon = resp.json()
    return coupon


async def _set(async_session_maker, coupon_id, **fields):
    async with async_session_maker() as db:
        row = await db.get(Coupon, uuid.UUID(coupon_id))
        for k, v in fields.items():
            setattr(row, k, v)
        await db.commit()


async def _titles(client, **params):
    resp = await client.get(API, params=params)
    assert resp.status_code == 200, resp.text
    return [c["title"] for c in resp.json()["items"]]


def _in(**kw):
    return datetime.now(UTC) + timedelta(**kw)


# ------------------------------------------------------------------ expiry


async def test_active_only_hides_a_coupon_that_expired_before_the_cleanup_job_ran(
    client, admin_headers, async_session_maker
):
    store, cat = await _setup(client, admin_headers)
    live = await _coupon(client, admin_headers, store, cat, "Live")
    gone = await _coupon(client, admin_headers, store, cat, "Gone")
    await _set(async_session_maker, gone["id"], expires_at=_in(hours=-1))  # still is_active=True
    assert await _titles(client) == ["Live"]
    assert live["id"]
    assert sorted(await _titles(client, active_only="false")) == ["Gone", "Live"]


async def test_ending_soon_orders_by_soonest_and_skips_no_expiry_and_expired(
    client, admin_headers, async_session_maker
):
    store, cat = await _setup(client, admin_headers)
    a = await _coupon(client, admin_headers, store, cat, "In 5 days")
    b = await _coupon(client, admin_headers, store, cat, "In 1 day")
    await _coupon(client, admin_headers, store, cat, "No expiry")
    c = await _coupon(client, admin_headers, store, cat, "Expired")
    await _set(async_session_maker, a["id"], expires_at=_in(days=5))
    await _set(async_session_maker, b["id"], expires_at=_in(days=1))
    await _set(async_session_maker, c["id"], expires_at=_in(days=-1))
    assert await _titles(client, sort="ending_soon") == ["In 1 day", "In 5 days"]


async def test_expires_within_days_filters_to_the_window(
    client, admin_headers, async_session_maker
):
    store, cat = await _setup(client, admin_headers)
    near = await _coupon(client, admin_headers, store, cat, "Near")
    far = await _coupon(client, admin_headers, store, cat, "Far")
    await _set(async_session_maker, near["id"], expires_at=_in(days=2))
    await _set(async_session_maker, far["id"], expires_at=_in(days=20))
    assert await _titles(client, expires_within_days=3) == ["Near"]
    assert sorted(await _titles(client, expires_within_days=30)) == ["Far", "Near"]


# ------------------------------------------------------------ verification


async def test_recently_verified_excludes_never_verified(
    client, admin_headers, async_session_maker
):
    store, cat = await _setup(client, admin_headers)
    old = await _coupon(client, admin_headers, store, cat, "Old check")
    fresh = await _coupon(client, admin_headers, store, cat, "Fresh check")
    await _coupon(client, admin_headers, store, cat, "Never checked")
    await _set(async_session_maker, old["id"], last_verified_at=_in(days=-9))
    await _set(async_session_maker, fresh["id"], last_verified_at=_in(hours=-2))
    assert await _titles(client, sort="recently_verified") == ["Fresh check", "Old check"]


async def test_top_rated_needs_enough_reports_to_rank(client, admin_headers, async_session_maker):
    store, cat = await _setup(client, admin_headers)
    lucky = await _coupon(client, admin_headers, store, cat, "One report")
    solid = await _coupon(client, admin_headers, store, cat, "Solid")
    ok = await _coupon(client, admin_headers, store, cat, "Okay")
    await _set(async_session_maker, lucky["id"], success_count=1, fail_count=0)  # 100% of 1
    await _set(async_session_maker, solid["id"], success_count=45, fail_count=3)  # 94% of 48
    await _set(async_session_maker, ok["id"], success_count=6, fail_count=4)  # 60% of 10
    assert await _titles(client, sort="top_rated") == ["Solid", "Okay"]


async def test_most_used_orders_by_all_time_clicks(client, admin_headers, async_session_maker):
    store, cat = await _setup(client, admin_headers)
    a = await _coupon(client, admin_headers, store, cat, "Few")
    b = await _coupon(client, admin_headers, store, cat, "Many")
    await _set(async_session_maker, a["id"], clicks_count=3)
    await _set(async_session_maker, b["id"], clicks_count=300)
    assert (await _titles(client, sort="most_used"))[0] == "Many"


# ---------------------------------------------------------------- trending


async def _click(async_session_maker, coupon_id, *, age, bot=False, n=1):
    async with async_session_maker() as db:
        for i in range(n):
            db.add(
                ClickEvent(
                    clickref=uuid.uuid4().hex,
                    coupon_id=uuid.UUID(coupon_id),
                    src="coupon-page",
                    network="none",
                    is_bot=bot,
                    created_at=datetime.now(UTC) - age,
                )
            )
        await db.commit()


async def test_trending_ranks_by_real_clicks_this_week(client, admin_headers, async_session_maker):
    store, cat = await _setup(client, admin_headers)
    hot = await _coupon(client, admin_headers, store, cat, "Hot")
    warm = await _coupon(client, admin_headers, store, cat, "Warm")
    await _coupon(client, admin_headers, store, cat, "Nobody clicked")
    await _click(async_session_maker, hot["id"], age=timedelta(days=1), n=5)
    await _click(async_session_maker, warm["id"], age=timedelta(days=2), n=2)
    assert await _titles(client, sort="trending") == ["Hot", "Warm"]


async def test_trending_ignores_bots_and_clicks_older_than_a_week(
    client, admin_headers, async_session_maker
):
    store, cat = await _setup(client, admin_headers)
    botty = await _coupon(client, admin_headers, store, cat, "Bot only")
    stale = await _coupon(client, admin_headers, store, cat, "Stale")
    real = await _coupon(client, admin_headers, store, cat, "Real")
    await _click(async_session_maker, botty["id"], age=timedelta(hours=1), bot=True, n=50)
    await _click(async_session_maker, stale["id"], age=timedelta(days=8), n=50)
    await _click(async_session_maker, real["id"], age=timedelta(hours=3), n=1)
    assert await _titles(client, sort="trending") == ["Real"]


async def test_trending_is_empty_rather_than_padded_when_nothing_was_clicked(client, admin_headers):
    store, cat = await _setup(client, admin_headers)
    await _coupon(client, admin_headers, store, cat, "Quiet")
    resp = await client.get(API, params={"sort": "trending"})
    assert resp.json()["items"] == [] and resp.json()["total"] == 0


# ----------------------------------------------------------------- defaults


async def test_default_order_is_still_newest_first(client, admin_headers, async_session_maker):
    store, cat = await _setup(client, admin_headers)
    first = await _coupon(client, admin_headers, store, cat, "First")
    second = await _coupon(client, admin_headers, store, cat, "Second")
    # Distinct timestamps: two rows created in the same instant would tie.
    await _set(async_session_maker, first["id"], created_at=_in(days=-2))
    await _set(async_session_maker, second["id"], created_at=_in(days=-1))
    assert (await _titles(client))[0] == "Second"
    assert (await _titles(client, sort="newest"))[0] == "Second"


async def test_unknown_sort_is_rejected(client):
    assert (await client.get(API, params={"sort": "best_deals"})).status_code == 422
    assert (await client.get(API, params={"expires_within_days": 0})).status_code == 422


# -------------------------------------------------------------- recommended


async def test_recommended_orders_by_the_ranking_engines_score(
    client, admin_headers, async_session_maker
):
    store, cat = await _setup(client, admin_headers)
    low = await _coupon(client, admin_headers, store, cat, "Low score")
    high = await _coupon(client, admin_headers, store, cat, "High score")
    mid = await _coupon(client, admin_headers, store, cat, "Mid score")
    await _set(async_session_maker, low["id"], quality_score=10)
    await _set(async_session_maker, high["id"], quality_score=92)
    await _set(async_session_maker, mid["id"], quality_score=55)
    assert await _titles(client, sort="recommended") == ["High score", "Mid score", "Low score"]


async def test_public_json_shows_verification_status_but_never_the_quality_score(
    client, admin_headers
):
    store, cat = await _setup(client, admin_headers)
    await _coupon(client, admin_headers, store, cat, "Plain")
    item = (await client.get(API)).json()["items"][0]
    assert item["verification_status"] in {
        "unverified",
        "pending",
        "verified",
        "disputed",
        "failed",
    }
    assert "quality_score" not in item  # internal ranking input, includes a revenue component


# ------------------------------------------------- exact counts and biggest %


async def test_verified_within_hours_counts_only_the_window(
    client, admin_headers, async_session_maker
):
    store, cat = await _setup(client, admin_headers)
    today = await _coupon(client, admin_headers, store, cat, "Today")
    last_week = await _coupon(client, admin_headers, store, cat, "Last week")
    await _coupon(client, admin_headers, store, cat, "Never")
    await _set(async_session_maker, today["id"], last_verified_at=_in(hours=-3))
    await _set(async_session_maker, last_week["id"], last_verified_at=_in(days=-6))
    resp = await client.get(API, params={"verified_within_hours": 24})
    body = resp.json()
    assert [c["title"] for c in body["items"]] == ["Today"]
    assert body["total"] == 1  # an exact count, not the size of a fetched sample


async def test_biggest_discount_ranks_percentages_only(client, admin_headers, async_session_maker):
    store, cat = await _setup(client, admin_headers)
    small = await _coupon(client, admin_headers, store, cat, "Ten percent")
    big = await _coupon(client, admin_headers, store, cat, "Fifty percent")
    fixed = await _coupon(client, admin_headers, store, cat, "Fixed amount")
    deal = await _coupon(client, admin_headers, store, cat, "Plain deal")
    await _set(async_session_maker, small["id"], discount_value=10)
    await _set(async_session_maker, big["id"], discount_value=50)
    # A fixed 5000 must not outrank 50%: the two are not comparable without the basket.
    await _set(
        async_session_maker, fixed["id"], discount_type=DiscountType.fixed, discount_value=5000
    )
    await _set(
        async_session_maker, deal["id"], discount_type=DiscountType.deal, discount_value=None
    )
    assert await _titles(client, sort="biggest_discount") == ["Fifty percent", "Ten percent"]
