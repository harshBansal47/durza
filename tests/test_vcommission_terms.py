"""Terms parsing for vCommission campaigns.

The fixtures reproduce the structure and the wording variants found in a real 57-campaign
feed: inline-style noise around every line, hyphenated channel names, qualifiers on the
search channel, validation written two ways, cancellation rules phrased a dozen ways, and
rules about which discount codes affect payout.
"""

import json
from datetime import date

import httpx
import pytest
from sqlalchemy import select

from app.models.affiliate_campaign import AffiliateCampaign
from app.services import vcommission_service as service
from app.services.vcommission_terms import (
    ALLOWED,
    CONDITIONAL,
    NOT_ALLOWED,
    UNAVAILABLE,
    can_promote,
    html_to_lines,
    parse_campaign_terms,
)

TODAY = date(2026, 11, 5)
NOISE = (
    'style="box-sizing: border-box; outline-style: initial; margin: 0px 0px 10px; '
    'padding: 0px; background-image: initial; width: auto;"'
)

CHANNELS = [
    "Content - Allowed",
    "Coupon - Allowed",
    "Cashback - Allowed",
    "Mobile App - Not Allowed",
    "Email - Not Allowed",
    "SMS - Not Allowed",
    "Telegram - Allowed",
    "Influencer - Not Allowed",
    "Software - Not Allowed",
    "Media Buying : Search - Not Allowed",
    "Media Buying : Social - Not Allowed",
    "Media Buying : Native - Allowed",
    "Media Buying : Display - Allowed",
    "Sub-Network - Allowed",
    "Pop - Allowed",
    "Push - Allowed",
    "Video - Allowed",
    "Reseller - Not Allowed",
    "Incent - Allowed",
    "Smartlink - Not Available",
]


def html(lines: list[str]) -> str:
    """Each line wrapped in nested styled elements, as the real feed does."""
    return "".join(
        f'<p {NOISE}><font face="Helvetica" style="color: rgb(32, 37, 45);">'
        f"<span {NOISE}>{line}</span></font></p>"
        for line in lines
    )


def description(
    *,
    channels: list[str] | None = None,
    cookie: str | None = "Cookie Duration - 30 days",
    validation: str | None = "Validation Period: 15 Days",
    payout: list[str] | None = None,
    kpi: list[str] | None = None,
) -> str:
    lines = [
        "Name: Example Brand Ecommerce CPS - India",
        "Conversion Type: CPS",
        "Category: Ecommerce",
        *(payout or []),
        "Tracking Information:",
        "Frequency - Realtime",
        *([cookie] if cookie else []),
        "Web - Available",
        "Report Frequency: Once a week",
        *([validation] if validation else []),
        "Promotion Methods:",
        *(CHANNELS if channels is None else channels),
        "Sale Amount Currency: INR",
        "SUB1: Order ID",
        "Missing sales process: - Allowed",
        "1. Affiliate shares Missing conversion till 5th of every month",
    ]
    if kpi is not None:
        lines += ["Detailed KPI:", *[f"{i}. {rule}" for i, rule in enumerate(kpi, 1)]]
    return html(lines)


FULL_KPI = [
    "Canceled/Returned orders will not be payable.",
    "No brand bidding is allowed in any form, including through related terms.",
    "Geo Targeting: Pan India",
    "Validation will be done on Shopify Report",
    (
        "Sales may get attributed to other channels as per attribution logic in cases where "
        "another affiliate/partner coupon is used."
    ),
]


def parse(**kwargs):
    return parse_campaign_terms(description(**kwargs), today=TODAY)


# ------------------------------------------------------------------ html


def test_html_noise_is_stripped_and_lines_are_kept():
    lines = html_to_lines(
        f"<p {NOISE}>Cookie&nbsp;Duration - 30 days</p><p>Tom &amp; Jerry<br>Next line</p>"
        "<script>alert(1)</script><style>p{color:red}</style>"
    )
    assert lines == ["Cookie Duration - 30 days", "Tom & Jerry", "Next line"]


# ------------------------------------------------------------------ the full template


def test_full_template_is_parsed_completely():
    terms = parse(kpi=FULL_KPI)

    assert len(terms.channels) == 20
    assert terms.channels["telegram"].status == ALLOWED
    assert terms.channels["email"].status == NOT_ALLOWED
    assert terms.channels["smartlink"].status == UNAVAILABLE
    assert terms.cookie_days == 30
    assert terms.validation_days == 15
    assert terms.conversion_type == "CPS"
    assert terms.geo == "Pan India"
    assert terms.brand_bidding_banned is True
    assert terms.other_affiliate_code_voids_sale is True
    assert {r.kind for r in terms.rules} >= {
        "cancelled_not_payable",
        "brand_bidding_banned",
        "geo",
        "other_affiliate_attribution",
    }
    assert terms.needs_review is False


def test_hyphenated_and_colon_channel_names_split_in_the_right_place():
    terms = parse(kpi=FULL_KPI)
    assert terms.channels["sub_network"].status == ALLOWED  # "Sub-Network - Allowed"
    assert terms.channels["search"].status == NOT_ALLOWED  # "Media Buying : Search - Not Allowed"
    assert terms.channels["mobile_app"].status == NOT_ALLOWED


# ------------------------------------------------------------------ search qualifiers


@pytest.mark.parametrize(
    ("verdict", "brand_banned"),
    [
        ("Allowed (Only generic)", True),
        ("Allowed (Only Generic)", True),
        ("Allowed (Only Generic KWs)", True),
        ("Allowed(Only Generic KWs Allowed)", True),
        ("Allowed (Brand + Generic)", False),  # brand IS allowed here
        ("Allowed", False),
    ],
)
def test_search_qualifiers(verdict, brand_banned):
    channels = [c for c in CHANNELS if "Search" not in c] + [f"Media Buying : Search - {verdict}"]
    terms = parse(channels=channels, kpi=["Geo Targeting: India"])

    search = terms.channels["search"]
    assert search.status == (ALLOWED if verdict == "Allowed" else CONDITIONAL)
    assert terms.brand_bidding_banned is brand_banned


# ------------------------------------------------------------------ cookie and validation


@pytest.mark.parametrize(
    ("line", "days"),
    [
        ("Cookie Duration - 30 days", 30),
        ("Cookie Duration - 30 Days", 30),
        ("Cookie Duration - 1 Day", 1),
        ("Cookie Duration - 24 hours", 1),
        ("Cookie Duration - 12 hours", 0.5),
        ("Cookie Duration: 90 Days", 90),
    ],
)
def test_cookie_formats(line, days):
    assert parse(cookie=line, kpi=FULL_KPI).cookie_days == days


@pytest.mark.parametrize(
    ("line", "days"),
    [
        ("Validation Period: 30 days", 30),
        ("Validation Period - 45 Days", 45),  # the second spelling used in the real feed
        ("Validation Period: 2 weeks", 14),
        ("Validation Period: 1 month", 30),
    ],
)
def test_validation_formats(line, days):
    assert parse(validation=line, kpi=FULL_KPI).validation_days == days


def test_missing_cookie_and_validation_are_none_not_guessed():
    terms = parse(cookie=None, validation=None, kpi=FULL_KPI)
    assert terms.cookie_days is None
    assert terms.validation_days is None
    assert {"cookie_missing", "validation_missing"} <= set(terms.review_reasons)
    assert terms.needs_review is False  # informational only


def test_absurd_values_are_rejected_rather_than_stored():
    assert parse(cookie="Cookie Duration - 99999 days", kpi=FULL_KPI).cookie_days is None
    assert parse(validation="Validation Period: 9999 days", kpi=FULL_KPI).validation_days is None


# ------------------------------------------------------------------ payout text


def test_flat_payout_statement_matches_the_api():
    terms = parse(payout=["Payout: 23.1% of order sale amount"], kpi=FULL_KPI)
    assert terms.payout_structure == "api_only"
    assert "payout_text_differs_from_api" not in terms.review_reasons


@pytest.mark.parametrize(
    ("line", "structure"),
    [
        ("Payout: Below slab wise", "slab"),
        ("Payout: Delivered GMV slabs", "slab"),
        ("Payout: Below User-type wise", "user_type"),
        ("Payout: Below User Type:", "user_type"),
        ("Payout: Below category wise", "category"),
        ("Payout: Below goal wise", "other"),
        ("Payout: Below subscription wise", "other"),
    ],
)
def test_payout_structures_the_api_number_cannot_express(line, structure):
    terms = parse(payout=[line], kpi=FULL_KPI)
    assert terms.payout_structure == structure
    assert "payout_text_differs_from_api" in terms.review_reasons
    assert terms.needs_review is False  # a ranking concern, not a safety one


def test_dated_payout_change_in_the_future_needs_review_past_does_not():
    future = parse(payout=["Payout: Below slab wise w.e.f 1st December,2026"], kpi=FULL_KPI)
    past = parse(payout=["Payout: Below slab wise w.e.f 1st August,2026"], kpi=FULL_KPI)

    assert future.payout_effective_from == "2026-12-01"
    assert "payout_change_upcoming" in future.review_reasons
    assert future.needs_review is True

    assert past.payout_effective_from == "2026-08-01"
    assert "payout_change_upcoming" not in past.review_reasons
    assert past.needs_review is False


@pytest.mark.parametrize(
    "line",
    [
        "Payout: Below slab wise w.e.f 15th July 2026 - Onwards",
        "Payout: slab wise w.e.f 2nd Nov 2026.",
    ],
)
def test_date_forms(line):
    assert parse(payout=[line], kpi=FULL_KPI).payout_effective_from is not None


def test_impossible_date_is_ignored():
    terms = parse(payout=["Payout: Below slab wise w.e.f 31st February 2026"], kpi=FULL_KPI)
    assert terms.payout_effective_from is None


# ------------------------------------------------------------------ KPI rules


@pytest.mark.parametrize(
    "rule",
    [
        "Canceled/Returned orders will not be payable.",
        "Returned & cancelled order will not be considered as payable",
        "Cancelled and returned order will not be validated",
        "Cancelled & Returned orders will not be consider for the final billing.",
        "Online Purchase (Cancel / Returns orders will not get paid).",
        "Orders marked as returned/refunded and cancelled will be deducted.",
        (
            "Only the genuine delivered orders will be considered for final billing "
            "(Returned and Cancelled orders won't be paid)"
        ),
    ],
)
def test_cancellation_rule_wordings(rule):
    assert parse(kpi=[rule]).rules[0].kind == "cancelled_not_payable"


@pytest.mark.parametrize(
    "rule",
    [
        "No brand bidding is allowed in any form.",
        "Brand keywords is not allowed",
        "No PPC and brand bidding allowed.",
        "Restrictions on Branded keywords, & there will be no bidding on our data",
        "Not Approved:- Pop-ups, ClickUnder, SEM, brand bidding, adult traffic.",
    ],
)
def test_brand_bidding_ban_wordings(rule):
    terms = parse(kpi=[rule])
    assert terms.rules[0].kind == "brand_bidding_banned"
    assert terms.brand_bidding_banned is True


def test_a_brand_rule_that_permits_is_not_a_ban():
    terms = parse(kpi=["Brand and generic keywords allowed with prior approval."])
    assert terms.brand_bidding_banned is False


def test_rule_that_reduces_payout_for_exclusive_codes_is_flagged_with_its_subpoints():
    rule = (
        "If you make any discount codes live or any of customer use the already live exclusive "
        "coupons of yours, below is the revised payout structure for your reference:"
    )
    lines = description(kpi=["Geo Targeting: India", rule]).replace(
        "</span></font></p>",
        "</span></font></p>",
    )
    lines += html(
        [
            (
                "a. If any of your 10% or 15% exclusive discount codes used (VC10, VC15): "
                "Your payout for that specific order will be 7%."
            ),
            "b. If 5% discount code(VC5): your payout will remain to 10.5% only",
        ]
    )
    terms = parse_campaign_terms(lines, today=TODAY)

    assert terms.code_payout_rule is not None
    assert "7%" in terms.code_payout_rule and "10.5%" in terms.code_payout_rule
    assert "code_payout_rule" in terms.review_reasons
    assert terms.needs_review is True


def test_rule_that_only_certain_codes_are_payable_is_flagged():
    terms = parse(kpi=["Only website coupon codes and exclusive coupon codes will be payable."])
    assert terms.code_payout_rule is not None
    assert terms.needs_review is True


def test_tax_wording_is_not_mistaken_for_a_code_rule():
    terms = parse(
        kpi=[
            (
                "The final payout will be calculated as per the actual sale amount "
                "(exclusive of the taxes) and will be provided at the time of validation."
            )
        ]
    )
    assert terms.code_payout_rule is None
    assert terms.needs_review is False


def test_advertiser_approved_code_is_captured():
    terms = parse(kpi=["Affiliates can use a discount code - VCOM15 offering 15% discount."])
    assert terms.rules[0].kind == "approved_discount_code"
    assert terms.approved_codes == ["VCOM15"]


def test_missing_kpi_block_needs_review():
    terms = parse(kpi=None)
    assert terms.rules == []
    assert "no_detailed_kpi" in terms.review_reasons
    assert terms.needs_review is True


# ------------------------------------------------------------------ never raises


@pytest.mark.parametrize("raw", [None, "", "   ", 123, [], {}, b"bytes"])
def test_empty_or_non_text_input_is_unparsed_not_an_error(raw):
    terms = parse_campaign_terms(raw, today=TODAY)
    assert terms.review_reasons == ["description_missing"]
    assert terms.needs_review is True
    assert terms.channels == {}


def test_garbage_html_does_not_raise_and_finds_nothing_it_cannot_prove():
    terms = parse_campaign_terms("<p><span>Telegram - Allowed<div></p></span><<<>>>", today=TODAY)
    assert terms.channels == {}  # no "Promotion Methods" header, so nothing is assumed
    assert "no_promotion_methods" in terms.review_reasons
    assert terms.needs_review is True


def test_flattened_text_is_not_misread():
    """The stored description is one line; a caller handing us that must get 'unknown'."""
    flat = (
        "Promotion Methods: Telegram - Allowed Coupon - Allowed Email - Allowed "
        "Cookie Duration - 30 days"
    )
    terms = parse_campaign_terms(flat, today=TODAY)
    assert terms.channels == {}
    assert can_promote(terms.to_dict(), "telegram").allowed is False


def test_conversion_type_falls_back_to_the_api_model():
    raw = html(["Promotion Methods:", "Coupon - Allowed"])
    assert parse_campaign_terms(raw, model="cpa", today=TODAY).conversion_type == "CPA"


# ------------------------------------------------------------------ the compliance gate


@pytest.fixture
def stored():
    """The persisted, JSON-round-tripped form: what the Telegram bot and site will read."""
    channels = [c for c in CHANNELS if "Search" not in c] + [
        "Media Buying : Search - Allowed (Only generic)"
    ]
    return json.loads(json.dumps(parse(channels=channels, kpi=FULL_KPI).to_dict()))


def test_gate_allows_what_the_advertiser_allowed(stored):
    assert can_promote(stored, "telegram").allowed is True
    assert can_promote(stored, "coupon").allowed is True


def test_gate_refuses_what_the_advertiser_refused(stored):
    for channel in ("email", "sms", "influencer", "mobile_app", "smartlink"):
        assert can_promote(stored, channel).allowed is False, channel


def test_gate_treats_a_condition_as_no_unless_the_caller_accepts_it(stored):
    refused = can_promote(stored, "search")
    assert refused.allowed is False and "generic" in refused.reason.lower()
    accepted = can_promote(stored, "search", accept_conditions=True)
    assert accepted.allowed is True and "generic" in accepted.reason.lower()


@pytest.mark.parametrize("terms", [None, {}, {"channels": {}}, {"channels": "oops"}])
def test_gate_fails_closed_without_usable_terms(terms):
    assert can_promote(terms, "telegram").allowed is False


def test_gate_fails_closed_on_an_unknown_channel(stored):
    assert can_promote(stored, "whatsapp").allowed is False


def test_persisted_form_is_plain_json(stored):
    assert isinstance(stored["channels"]["telegram"], dict)
    assert stored["needs_review"] is False
    assert stored["version"] == 1


# ------------------------------------------------------------------ importer integration


def _feed_campaign(raw_description, *, external_id="c1"):
    return {
        "id": external_id,
        "title": "Example Brand",
        "description": raw_description,
        "kpi": "",
        "tracking_link": "https://track.example/offer?campaign_id=1&pub_id=2",
        "merchant_name": "Example Brand",
        "model": "cps",
        "currency": "inr",
        "payouts": [
            {"payout": 8.4, "geo": ["IN"], "payout_model": "percentage", "currency": "INR"}
        ],
        "categories": ["ecommerce"],
        "countries": ["IN"],
        "active": True,
    }


async def _sync(async_session_maker, monkeypatch, campaigns):
    settings = service.get_settings()
    monkeypatch.setattr(
        service,
        "get_settings",
        lambda: settings.model_copy(update={"vcommissions_campaigns_url": "https://api.example/c"}),
    )
    payload = {"success": True, "data": {"campaigns": campaigns}}
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=payload))
    async with async_session_maker() as db, httpx.AsyncClient(transport=transport) as client:
        result = await service.sync_campaigns(db, client=client)
        rows = list((await db.execute(select(AffiliateCampaign))).scalars().all())
    return result, rows


@pytest.mark.asyncio
async def test_import_stores_terms_parsed_from_the_raw_html(async_session_maker, monkeypatch):
    raw = description(kpi=FULL_KPI)
    result, rows = await _sync(async_session_maker, monkeypatch, [_feed_campaign(raw)])

    assert result.status == "success"
    campaign = rows[0]
    # The searchable description is still the flattened one-liner...
    assert "\n" not in campaign.description
    # ...but the terms were read from the structured HTML before that happened.
    assert campaign.terms["cookie_days"] == 30
    assert campaign.terms["validation_days"] == 15
    assert campaign.terms["channels"]["telegram"]["status"] == ALLOWED
    assert campaign.terms["channels"]["email"]["status"] == NOT_ALLOWED
    assert campaign.needs_review is False
    assert can_promote(campaign.terms, "telegram").allowed is True
    assert can_promote(campaign.terms, "email").allowed is False


@pytest.mark.asyncio
async def test_resync_of_unchanged_terms_is_idempotent(async_session_maker, monkeypatch):
    feed = [_feed_campaign(description(kpi=FULL_KPI))]
    first, _ = await _sync(async_session_maker, monkeypatch, feed)
    second, rows = await _sync(async_session_maker, monkeypatch, feed)

    assert first.inserted == 1
    assert second.unchanged == 1 and second.updated == 0
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_a_policy_change_at_the_advertiser_flips_the_stored_decision(
    async_session_maker, monkeypatch
):
    """The advertiser bans Telegram between two syncs; the gate must follow, not keep a stale yes."""
    allowed_channels = CHANNELS
    banned_channels = [
        "Telegram - Not Allowed" if c.startswith("Telegram") else c for c in CHANNELS
    ]
    campaign_id = "c-policy"
    _, rows = await _sync(
        async_session_maker,
        monkeypatch,
        [
            _feed_campaign(
                description(channels=allowed_channels, kpi=FULL_KPI), external_id=campaign_id
            )
        ],
    )
    assert can_promote(rows[0].terms, "telegram").allowed is True

    after, rows = await _sync(
        async_session_maker,
        monkeypatch,
        [
            _feed_campaign(
                description(channels=banned_channels, kpi=FULL_KPI), external_id=campaign_id
            )
        ],
    )
    assert after.updated == 1
    assert can_promote(rows[0].terms, "telegram").allowed is False


@pytest.mark.asyncio
async def test_campaigns_that_need_a_human_are_queryable(async_session_maker, monkeypatch):
    clean = _feed_campaign(description(kpi=FULL_KPI), external_id="clean")
    risky = _feed_campaign(
        description(kpi=["Only website coupon codes and exclusive coupon codes will be payable."]),
        external_id="risky",
    )
    await _sync(async_session_maker, monkeypatch, [clean, risky])

    async with async_session_maker() as db:
        flagged = (
            (
                await db.execute(
                    select(AffiliateCampaign).where(AffiliateCampaign.needs_review.is_(True))
                )
            )
            .scalars()
            .all()
        )
    assert [c.external_campaign_id for c in flagged] == ["risky"]


@pytest.mark.asyncio
async def test_a_malformed_description_never_fails_the_import(async_session_maker, monkeypatch):
    broken = _feed_campaign(None, external_id="broken")
    fine = _feed_campaign(description(kpi=FULL_KPI), external_id="fine")
    result, rows = await _sync(async_session_maker, monkeypatch, [broken, fine])

    assert result.status == "success"
    by_id = {r.external_campaign_id: r for r in rows}
    assert by_id["broken"].needs_review is True
    assert by_id["broken"].terms["review_reasons"] == ["description_missing"]
    assert can_promote(by_id["broken"].terms, "telegram").allowed is False
    assert by_id["fine"].needs_review is False


@pytest.mark.asyncio
async def test_admin_api_exposes_terms_and_anonymous_still_cannot(
    async_session_maker, monkeypatch, client, admin_headers
):
    await _sync(async_session_maker, monkeypatch, [_feed_campaign(description(kpi=FULL_KPI))])

    anonymous = await client.get("/api/v1/vcommission/campaigns")
    assert anonymous.status_code == 401

    listing = await client.get("/api/v1/vcommission/campaigns", headers=admin_headers)
    assert listing.status_code == 200
    item = listing.json()["items"][0]
    assert item["needs_review"] is False
    assert item["terms"]["channels"]["telegram"]["status"] == ALLOWED
    assert item["terms"]["cookie_days"] == 30


@pytest.mark.asyncio
async def test_admin_can_filter_to_the_review_queue(
    async_session_maker, monkeypatch, client, admin_headers
):
    clean = _feed_campaign(description(kpi=FULL_KPI), external_id="clean")
    risky = _feed_campaign(
        description(kpi=["Only website coupon codes and exclusive coupon codes will be payable."]),
        external_id="risky",
    )
    await _sync(async_session_maker, monkeypatch, [clean, risky])

    queue = await client.get(
        "/api/v1/vcommission/campaigns?needs_review=true", headers=admin_headers
    )
    safe = await client.get(
        "/api/v1/vcommission/campaigns?needs_review=false", headers=admin_headers
    )
    everything = await client.get("/api/v1/vcommission/campaigns", headers=admin_headers)

    assert [i["terms"]["code_payout_rule"] is not None for i in queue.json()["items"]] == [True]
    assert [i["needs_review"] for i in safe.json()["items"]] == [False]
    assert everything.json()["total"] == 2
