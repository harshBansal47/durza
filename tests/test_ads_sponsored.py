"""Sponsored placements: disclosure, scheduling, and tracked redirects.

The properties under test: a paid placement is always labelled, never serves
outside its booked window, never exposes the advertiser URL in public JSON, and
every click leaves a record.
"""

import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import select

from app.models.ad import Ad, AdPosition
from app.models.click import ClickEvent
from app.services import ad_service

API = "/api/v1/ads"
HUMAN = {"user-agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0) AppleWebKit/605.1.15 Safari/604.1"}


def _ad(**overrides):
    body = {
        "position": "header",
        "image_url": "https://cdn.example.com/banner.png",
        "target_url": "https://advertiser.example.com/landing?a=1",
        "advertiser": "Acme Shoes",
        "alt_text": "Acme summer sale",
    }
    body.update(overrides)
    return body


def _iso(delta: timedelta) -> str:
    return (datetime.now(UTC) + delta).isoformat()


async def _make(client, headers, **overrides):
    resp = await client.post(API, json=_ad(**overrides), headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _clicks(async_session_maker):
    async with async_session_maker() as db:
        return list((await db.execute(select(ClickEvent))).scalars())


# ------------------------------------------------------------------ disclosure


async def test_label_defaults_to_sponsored(client, admin_headers):
    ad = await _make(client, admin_headers)
    assert ad["label"] == "Sponsored"


@pytest.mark.parametrize("label", ["Partner", "Ad"])
async def test_other_allowed_labels(client, admin_headers, label):
    assert (await _make(client, admin_headers, label=label))["label"] == label


@pytest.mark.parametrize("label", ["", "Organic", "Recommended", "sponsored"])
async def test_labels_that_do_not_disclose_are_rejected(client, admin_headers, label):
    resp = await client.post(API, json=_ad(label=label), headers=admin_headers)
    assert resp.status_code == 422


async def test_label_cannot_be_blanked_by_update(client, admin_headers):
    ad = await _make(client, admin_headers)
    resp = await client.patch(f"{API}/{ad['id']}", json={"label": ""}, headers=admin_headers)
    assert resp.status_code == 422


# ----------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "bad",
    [
        "javascript:alert(1)",
        "data:text/html,<script>",
        "ftp://x.example/a",
        "//no-scheme.example",
        "not a url",
        "",
    ],
)
async def test_target_url_must_be_http(client, admin_headers, bad):
    resp = await client.post(API, json=_ad(target_url=bad), headers=admin_headers)
    assert resp.status_code == 422


async def test_image_url_must_be_http(client, admin_headers):
    resp = await client.post(API, json=_ad(image_url="javascript:alert(1)"), headers=admin_headers)
    assert resp.status_code == 422


async def test_window_must_end_after_it_starts(client, admin_headers):
    resp = await client.post(
        API,
        json=_ad(starts_at=_iso(timedelta(days=2)), ends_at=_iso(timedelta(days=1))),
        headers=admin_headers,
    )
    assert resp.status_code == 422


# -------------------------------------------------------------- public shape


async def test_public_json_hides_target_url_and_click_count(client, admin_headers):
    ad = await _make(client, admin_headers)
    listed = (await client.get(API)).json()["items"][0]
    single = (await client.get(f"{API}/{ad['id']}")).json()
    for body in (listed, single):
        assert "target_url" not in body
        assert "clicks_count" not in body
        assert body["label"] == "Sponsored"
        assert body["advertiser"] == "Acme Shoes"
        assert body["alt_text"] == "Acme summer sale"


async def test_editors_still_see_the_target_when_they_create_one(client, admin_headers):
    ad = await _make(client, admin_headers)
    assert ad["target_url"] == "https://advertiser.example.com/landing?a=1"
    assert ad["clicks_count"] == 0


# -------------------------------------------------------------- scheduling


async def test_future_ad_is_not_listed_and_does_not_redirect(client, admin_headers):
    ad = await _make(client, admin_headers, starts_at=_iso(timedelta(days=1)))
    assert (await client.get(API)).json()["total"] == 0
    resp = await client.get(f"{API}/{ad['id']}/go", headers=HUMAN, follow_redirects=False)
    assert resp.status_code == 404


async def test_expired_ad_is_not_listed_and_does_not_redirect(
    client, admin_headers, async_session_maker
):
    ad = await _make(client, admin_headers, starts_at=_iso(timedelta(days=-3)))
    # Move the end into the past directly: the API rightly refuses end <= start in one request.
    async with async_session_maker() as db:
        row = await db.get(Ad, uuid.UUID(ad["id"]))
        row.ends_at = datetime.now(UTC) - timedelta(hours=1)
        await db.commit()
    assert (await client.get(API)).json()["total"] == 0
    resp = await client.get(f"{API}/{ad['id']}/go", headers=HUMAN, follow_redirects=False)
    assert resp.status_code == 404


async def test_ad_inside_its_window_is_served(client, admin_headers):
    await _make(
        client,
        admin_headers,
        starts_at=_iso(timedelta(hours=-1)),
        ends_at=_iso(timedelta(days=1)),
    )
    assert (await client.get(API)).json()["total"] == 1


async def test_switched_off_ad_does_not_redirect(client, admin_headers):
    ad = await _make(client, admin_headers)
    await client.patch(f"{API}/{ad['id']}", json={"is_active": False}, headers=admin_headers)
    resp = await client.get(f"{API}/{ad['id']}/go", headers=HUMAN, follow_redirects=False)
    assert resp.status_code == 404


def test_is_live_handles_naive_datetimes():
    """Naive datetimes must not raise, and must be read as UTC.

    The naive value below simulates what SQLite hands back for a
    `DateTime(timezone=True)` column: the same instant, with the offset dropped.
    That is why it is built by stripping the tzinfo off an *aware UTC* value
    rather than by calling `datetime.now()` — a naive `datetime.now()` is local
    time, and a naive value carries no offset, so no amount of coercion can turn
    it back into the instant it was taken from. Asserting that a local naive
    timestamp is interpreted as UTC would be asserting a bug.
    """
    ad = Ad(
        position=AdPosition.header,
        image_url="https://x.example/a.png",
        target_url="https://x.example",
        is_active=True,
        starts_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1),
        ends_at=None,
    )
    assert ad_service.is_live(ad) is True

    # And the window is still enforced after coercion: a naive start in the
    # future must not count as live.
    ad.starts_at = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
    assert ad_service.is_live(ad) is False


# --------------------------------------------------------------- /go redirect


async def test_go_redirects_with_utm_and_records_the_click(
    client, admin_headers, async_session_maker
):
    ad = await _make(client, admin_headers)
    resp = await client.get(
        f"{API}/{ad['id']}/go?src=ad-header", headers=HUMAN, follow_redirects=False
    )
    assert resp.status_code == 302
    loc = urlparse(resp.headers["location"])
    assert loc.netloc == "advertiser.example.com"
    q = parse_qs(loc.query)
    assert q["a"] == ["1"]  # the advertiser's own parameter survives
    assert q["utm_source"] == ["durza"]
    assert q["utm_medium"] == ["sponsored"]
    assert q["utm_campaign"] == [ad["id"]]

    (click,) = await _clicks(async_session_maker)
    assert str(click.ad_id) == ad["id"]
    assert click.src == "ad-header"
    assert click.store_id is None and click.coupon_id is None and click.product_id is None
    assert click.network == "none"
    assert click.is_bot is False


async def test_advertisers_own_utm_values_are_not_overwritten(client, admin_headers):
    ad = await _make(
        client, admin_headers, target_url="https://a.example/p?utm_source=mine&utm_medium=cpc"
    )
    resp = await client.get(f"{API}/{ad['id']}/go", headers=HUMAN, follow_redirects=False)
    q = parse_qs(urlparse(resp.headers["location"]).query)
    assert q["utm_source"] == ["mine"]
    assert q["utm_medium"] == ["cpc"]
    assert q["utm_campaign"] == [ad["id"]]


async def test_click_count_increments_and_repeats_dedupe(
    client, admin_headers, async_session_maker
):
    ad = await _make(client, admin_headers)
    await client.get(f"{API}/{ad['id']}/go", headers=HUMAN, follow_redirects=False)
    await client.get(f"{API}/{ad['id']}/go", headers=HUMAN, follow_redirects=False)
    assert len(await _clicks(async_session_maker)) == 1
    async with async_session_maker() as db:
        row = await db.get(Ad, uuid.UUID(ad["id"]))
        assert row.clicks_count == 1


async def test_bot_clicks_are_flagged(client, admin_headers, async_session_maker):
    ad = await _make(client, admin_headers)
    await client.get(
        f"{API}/{ad['id']}/go", headers={"user-agent": "Googlebot/2.1"}, follow_redirects=False
    )
    (click,) = await _clicks(async_session_maker)
    assert click.is_bot is True


async def test_unknown_ad_404s(client):
    resp = await client.get(f"{API}/{uuid.uuid4()}/go", follow_redirects=False)
    assert resp.status_code == 404


async def test_legacy_non_http_target_is_never_redirected(client, async_session_maker):
    """Rows saved before URL validation existed must not become open redirects."""
    async with async_session_maker() as db:
        ad = Ad(
            position=AdPosition.footer,
            image_url="https://cdn.example.com/x.png",
            target_url="javascript:alert(1)",
        )
        db.add(ad)
        await db.commit()
        ad_id = ad.id
    resp = await client.get(f"{API}/{ad_id}/go", headers=HUMAN, follow_redirects=False)
    assert resp.status_code == 404
    assert await _clicks(async_session_maker) == []


async def test_deleting_an_ad_keeps_its_click_history(client, admin_headers, async_session_maker):
    ad = await _make(client, admin_headers)
    await client.get(f"{API}/{ad['id']}/go", headers=HUMAN, follow_redirects=False)
    assert (await client.delete(f"{API}/{ad['id']}", headers=admin_headers)).status_code == 204
    (click,) = await _clicks(async_session_maker)
    assert click.ad_id is None
