"""Phase 8 — Sponsored placement tests."""

import uuid

from app.models.sponsored import (
    CampaignModel,
    CampaignStatus,
    PlacementType,
    SponsoredCampaign,
    SponsoredOffer,
    SponsoredPlacement,
)
from app.models.store import Store


async def _make_store(async_session_maker, name="Test Store") -> Store:
    async with async_session_maker() as db:
        store = Store(
            name=name,
            slug=name.lower().replace(" ", "-") + f"-{uuid.uuid4().hex[:6]}",
            normalized_name=name.lower(),
            normalized_domain=f"{name.lower().replace(' ', '')}.com",
        )
        db.add(store)
        await db.commit()
        await db.refresh(store)
        return store


async def test_create_campaign(async_session_maker):
    store = await _make_store(async_session_maker)
    async with async_session_maker() as db:
        campaign = SponsoredCampaign(
            merchant_id=store.id,
            name="Nike Summer Sale",
            status=CampaignStatus.active,
            budget=5000.0,
            currency="USD",
            campaign_model=CampaignModel.cpc,
        )
        db.add(campaign)
        await db.commit()
        await db.refresh(campaign)

        assert campaign.id is not None
        assert campaign.name == "Nike Summer Sale"
        assert campaign.status == CampaignStatus.active


async def test_create_placement(async_session_maker):
    store = await _make_store(async_session_maker)
    async with async_session_maker() as db:
        campaign = SponsoredCampaign(
            merchant_id=store.id,
            name="Test Campaign",
            status=CampaignStatus.active,
        )
        db.add(campaign)
        await db.commit()
        await db.refresh(campaign)

        placement = SponsoredPlacement(
            campaign_id=campaign.id,
            merchant_id=store.id,
            placement_type=PlacementType.merchant_page,
            page_type="merchant_page",
            max_impressions_per_visitor_per_day=3,
        )
        db.add(placement)
        await db.commit()
        await db.refresh(placement)

        assert placement.id is not None
        assert placement.placement_type == PlacementType.merchant_page


async def test_create_sponsored_offer(async_session_maker):
    store = await _make_store(async_session_maker)
    async with async_session_maker() as db:
        campaign = SponsoredCampaign(
            merchant_id=store.id,
            name="Test Campaign",
            status=CampaignStatus.active,
        )
        db.add(campaign)
        await db.commit()
        await db.refresh(campaign)

        offer = SponsoredOffer(
            campaign_id=campaign.id,
            merchant_id=store.id,
            coupon_id=None,
        )
        db.add(offer)
        await db.commit()
        await db.refresh(offer)

        assert offer.id is not None
        assert offer.merchant_id == store.id


async def test_campaign_enum_validation():
    assert CampaignStatus("draft") == CampaignStatus.draft
    assert CampaignStatus("active") == CampaignStatus.active
    assert CampaignStatus("paused") == CampaignStatus.paused
    assert CampaignStatus("ended") == CampaignStatus.ended

    assert CampaignModel("cpm") == CampaignModel.cpm
    assert CampaignModel("cpc") == CampaignModel.cpc
    assert CampaignModel("cpa") == CampaignModel.cpa
    assert CampaignModel("fixed_placement") == CampaignModel.fixed_placement
    assert CampaignModel("fixed_monthly") == CampaignModel.fixed_monthly

    assert PlacementType("homepage") == PlacementType.homepage
    assert PlacementType("merchant_page") == PlacementType.merchant_page
    assert PlacementType("category_page") == PlacementType.category_page
    assert PlacementType("search") == PlacementType.search
    assert PlacementType("coupon_list") == PlacementType.coupon_list
    assert PlacementType("featured_section") == PlacementType.featured_section
