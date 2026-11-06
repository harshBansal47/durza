"""Where a click on a store ("Go to store") actually goes, decided server-side.

`affiliate_router` answers this for one coupon. A store page has no single coupon: its
"Go to store" button needs a destination for the store as a whole. This module is that
decision, under the same rules and for the same reasons as `affiliate_router`:

* **The operator's stated choice decides; nothing is scored.** In order:

  1. the store's own configured affiliate link (`Store.link_template`). Someone set this up
     on purpose for this store, so it always wins and is never second-guessed;
  2. an advertiser campaign matched to the store, for stores with no configured link of
     their own, which is the gap a second network exists to fill;
  3. the store's plain website, untracked, so a shopper is never stranded.

  There is deliberately no payout- or EPC-based ranking here. Campaign payouts are the
  advertiser's own figures (often a base rate that slabs and code rules change), and the
  traffic to measure real performance does not exist yet. Ranking on that would be
  arithmetic on noise that favours whichever network pays best, which is not the same as
  what is best for the shopper. If real per-network performance data ever exists, it belongs
  as a tie-break among equally trusted candidates, never as an override of rule 1.

* **Compliance can only remove an option, never add one.** An advertiser's own rules say
  which channels we may promote it on. A campaign whose terms do not clearly allow the
  channel the click came from (or whose terms we could not read) is skipped, and the shopper
  continues to the next candidate. Giving up a commission is always preferred to breaking a
  programme's rules.

* **Network identity stays here.** The result carries the network so the click can be
  recorded and the right click-reference attached. Callers must not serialise it: a shopper's
  browser has no need to know which network we use.
"""

import enum
import logging
from dataclasses import dataclass
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.affiliate_campaign import AffiliateCampaign
from app.models.store import Store
from app.services.vcommission_terms import can_promote

logger = logging.getLogger("durza.routing")

_VCOMMISSION = "vcommission"

# `src` is a free-form tag the page sets (see click_service.normalize_src). Only these name a
# channel the advertiser rules distinguish; every other value is our own website.
_SRC_CHANNELS = {
    "telegram": "telegram",
    "email": "email",
    "newsletter": "email",
    "digest": "email",
    "sms": "sms",
}


def channel_for_src(src: str | None) -> str:
    """Which advertiser-rule channel a click from `src` counts as.

    A click from one of our own pages (store page, category, homepage, search, ...) is website
    traffic, which advertisers call "content". Telegram, email and SMS are channels advertisers
    can and do ban individually, so they must be recognised as themselves.
    """
    return _SRC_CHANNELS.get((src or "").strip().lower(), "content")


class StoreDecision(str, enum.Enum):
    """Which rule produced the URL, for logs and the admin view."""

    store_link = "store_link"  # the store's own configured affiliate link
    campaign = "campaign"  # a matched advertiser campaign
    website = "website"  # the plain website, untracked


@dataclass(frozen=True)
class StoreDestination:
    """Where to send one click on a store, and why.

    `url` is bare. Attaching the click reference is the caller's job
    (`affiliate.build_outbound_url`), using `network` and `link_template`.
    """

    url: str
    decision: StoreDecision
    network: str | None = None
    link_template: str | None = None
    campaign_id: str | None = None
    reason: str = ""


def _is_http(url: str | None) -> bool:
    return bool(url) and urlparse(url).scheme in {"http", "https"}


def _covers(campaign: AffiliateCampaign, country: str | None) -> bool:
    """Empty `countries` means "no opinion", eligible everywhere (same rule as sources)."""
    if not country:
        return True
    markets = [c.upper() for c in (campaign.countries or []) if isinstance(c, str) and c]
    return not markets or country.upper() in markets


async def matched_campaigns(db: AsyncSession, store: Store) -> list[AffiliateCampaign]:
    """Active campaigns matched to this store, in a deterministic order.

    The order is not a quality ranking: the operator has stated no preference between two
    campaigns for one store, so campaigns that need no human review come first (the safer
    default), then a stable name order so the same store always routes the same way.
    """
    stmt = select(AffiliateCampaign).where(
        AffiliateCampaign.store_id == store.id,
        AffiliateCampaign.network == _VCOMMISSION,
        AffiliateCampaign.is_active.is_(True),
    )
    rows = list((await db.execute(stmt)).scalars().all())
    return sorted(
        (c for c in rows if _is_http(c.tracking_url) and _covers(c, store.country_code)),
        key=lambda c: (c.needs_review, c.title or "", c.external_campaign_id),
    )


async def route_store(
    db: AsyncSession, store: Store, *, src: str | None = None
) -> StoreDestination | None:
    """Pick the destination for one click on one store, or None if there is nowhere to send it.

    None means the store has no usable link of any kind; the caller answers 404. Routing is
    not an entitlement check: callers refuse inactive stores before asking.
    """
    website = store.website_url if _is_http(store.website_url) else None

    if store.link_template and website:
        return StoreDestination(
            url=website,
            decision=StoreDecision.store_link,
            network=store.affiliate_network,
            link_template=store.link_template,
            reason="the store has its own configured affiliate link",
        )

    channel = channel_for_src(src)
    skipped: list[str] = []
    for campaign in await matched_campaigns(db, store):
        verdict = can_promote(campaign.terms, channel)
        if not verdict.allowed:
            skipped.append(f"{campaign.external_campaign_id}: {verdict.reason}")
            continue
        return StoreDestination(
            url=campaign.tracking_url,
            decision=StoreDecision.campaign,
            network=_VCOMMISSION,
            campaign_id=str(campaign.id),
            reason=f"advertiser campaign allows '{channel}'",
        )

    if website:
        reason = "no usable tracked link"
        if skipped:
            reason += f"; skipped campaigns whose rules do not allow '{channel}'"
        logger.debug(
            "store routed to plain website", extra={"store": store.slug, "skipped": skipped}
        )
        return StoreDestination(url=website, decision=StoreDecision.website, reason=reason)

    return None
