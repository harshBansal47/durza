"""Fetch, validate and reconcile vCommission campaigns without creating coupons."""

import html
import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx
from sqlalchemy import Text, cast, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.observability import get_logger, is_secret_key
from app.ingestion import merchants
from app.ingestion.retry import with_retries
from app.models.affiliate_campaign import AffiliateCampaign, AffiliateCampaignSync
from app.models.store import Store
from app.services.vcommission_terms import parse_campaign_terms

logger = get_logger("durza.vcommission")
_NETWORK = "vcommission"
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_MAX_PAGES = 1000
_JSON = json.dumps


class VCommissionSyncError(RuntimeError):
    """A safe-to-display synchronization error that never includes the feed URL."""

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class SyncSummary:
    pages_fetched: int
    inserted: int
    updated: int
    unchanged: int
    skipped: int
    deactivated: int
    status: str


class _TextOnlyHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._blocked_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "iframe", "object", "svg"}:
            self._blocked_depth += 1
        elif tag.lower() in {"br", "p", "div", "li"} and not self._blocked_depth:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "iframe", "object", "svg"} and self._blocked_depth:
            self._blocked_depth -= 1
        elif tag.lower() in {"p", "div", "li"} and not self._blocked_depth:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._blocked_depth:
            self.parts.append(data)


def _plain_text(value: object, *, limit: int = 12000) -> str | None:
    if not isinstance(value, str):
        return None
    parser = _TextOnlyHTML()
    parser.feed(html.unescape(value))
    parser.close()
    result = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
    return result[:limit] or None


def _string(value: object, *, limit: int = 255) -> str | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (str, int, float)):
        text = str(value).strip()
        return text[:limit] or None
    return None


def _safe_url(value: object, *, limit: int = 2048) -> str | None:
    candidate = _string(value, limit=limit)
    if not candidate:
        return None
    try:
        parts = urlsplit(candidate)
        valid = (
            parts.scheme.lower() in {"http", "https"}
            and bool(parts.hostname)
            and parts.username is None
            and parts.password is None
            and not any(ord(char) < 32 for char in candidate)
        )
    except ValueError:
        valid = False
    return candidate if valid else None


def _safe_json(value: object, *, depth: int = 0) -> object:
    if depth > 6:
        return None
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        candidate = value[:4000]
        try:
            parts = urlsplit(candidate)
            if parts.scheme in {"http", "https"} and parts.query:
                from urllib.parse import parse_qsl, urlencode, urlunsplit

                query = [
                    (key, "[redacted]" if is_secret_key(key) else item)
                    for key, item in parse_qsl(parts.query, keep_blank_values=True)
                ]
                candidate = urlunsplit(parts._replace(query=urlencode(query)))
        except ValueError:
            pass
        return candidate
    if isinstance(value, list):
        return [_safe_json(item, depth=depth + 1) for item in value[:100]]
    if isinstance(value, dict):
        return {
            str(key)[:100]: _safe_json(item, depth=depth + 1)
            for key, item in list(value.items())[:100]
            if not is_secret_key(str(key))
        }
    return _string(value, limit=4000)


def _list_of_names(value: object, *, country_codes: bool = False) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        return []
    names: list[str] = []
    for item in values[:200]:
        if isinstance(item, dict):
            item = (
                item.get("code")
                or item.get("country_code")
                or item.get("name")
                or item.get("title")
            )
        text = _string(item, limit=120)
        if text:
            text = text.upper() if country_codes else text
            if text not in names:
                names.append(text)
    return names


def _campaign_data(item: object) -> dict[str, object] | None:
    if not isinstance(item, dict):
        return None
    external_id = _string(item.get("id"), limit=255)
    title = _string(item.get("title"), limit=255)
    tracking_url = _safe_url(item.get("tracking_link"))
    if not external_id or not title or not tracking_url:
        return None

    merchant_value = item.get("merchant") or item.get("advertiser")
    if isinstance(merchant_value, dict):
        merchant_value = merchant_value.get("name") or merchant_value.get("title")
    merchant_name = (
        _string(item.get("merchant_name"), limit=200)
        or _string(item.get("advertiser_name"), limit=200)
        or _string(item.get("brand"), limit=200)
        or _string(merchant_value, limit=200)
        or title
    )
    known_fields = {
        "id",
        "title",
        "description",
        "kpi",
        "tracking_link",
        "thumbnail",
        "preview_url",
        "merchant",
        "merchant_name",
        "advertiser",
        "advertiser_name",
        "brand",
        "model",
        "categories",
        "countries",
        "currency",
        "payouts",
        "defaultGoal",
        "goals",
        "device",
        "os",
        "app_name",
        "app_id",
        "active",
        "is_active",
        "status",
        "availability",
        "user_id",
        "HidePayoutPubs",
        "HidePayoutFromPubsList",
    }
    metadata = _safe_json({key: value for key, value in item.items() if key not in known_fields})
    if not isinstance(metadata, dict):
        metadata = {}
    # Keep these documented-but-network-specific fields even though the public
    # response intentionally exposes only a normalized subset.
    for key in ("cap", "caps", "customFields"):
        if key in item:
            metadata[key] = _safe_json(item[key])
    try:
        if len(_JSON(metadata, ensure_ascii=True)) > 30000:
            return None
    except (TypeError, ValueError):
        return None

    availability = str(item.get("status") or item.get("availability") or "").strip().lower()
    active_value = item.get("is_active", item.get("active"))
    is_active = (
        active_value
        if isinstance(active_value, bool)
        else availability
        not in {
            "inactive",
            "paused",
            "closed",
            "expired",
            "rejected",
            "disabled",
        }
    )
    # Parsed from the raw HTML: `_plain_text` flattens the description to one line, which
    # destroys the line breaks the labels depend on, so this cannot be done later.
    terms = parse_campaign_terms(
        item.get("description"), payouts=item.get("payouts"), model=item.get("model")
    )
    return {
        "external_campaign_id": external_id,
        "title": title,
        "description": _plain_text(item.get("description")),
        "kpi": _plain_text(item.get("kpi"), limit=2000),
        "merchant_name": merchant_name,
        "preview_url": _safe_url(item.get("preview_url")),
        "tracking_url": tracking_url,
        "thumbnail_url": _safe_url(item.get("thumbnail")),
        "categories": _list_of_names(item.get("categories")),
        "countries": _list_of_names(item.get("countries"), country_codes=True),
        "conversion_model": _string(item.get("model"), limit=120),
        "currency": (_string(item.get("currency"), limit=3) or "").upper() or None,
        "payouts": _safe_json(item.get("payouts")) if item.get("payouts") is not None else None,
        "default_goal": _safe_json(item.get("defaultGoal"))
        if item.get("defaultGoal") is not None
        else None,
        "goals": _safe_json(item.get("goals")) if item.get("goals") is not None else None,
        "device": _safe_json(item.get("device")) if item.get("device") is not None else None,
        "operating_system": _safe_json(item.get("os")) if item.get("os") is not None else None,
        "app_name": _string(item.get("app_name"), limit=255),
        "app_id": _string(item.get("app_id"), limit=255),
        "network_metadata": metadata,
        "terms": terms.to_dict(),
        "needs_review": terms.needs_review,
        "is_active": is_active,
    }


def _origin(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    return parts.scheme.lower(), (parts.hostname or "").lower(), parts.port


def _next_url(payload: dict, current_url: str) -> str | None:
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    pagination = data.get("pagination")
    pagination = pagination if isinstance(pagination, dict) else {}
    links = pagination.get("links")
    links = links if isinstance(links, dict) else {}
    candidate = (
        pagination.get("next_url")
        or pagination.get("nextPageUrl")
        or pagination.get("next")
        or links.get("next")
        or data.get("next")
    )
    if isinstance(candidate, dict):
        candidate = candidate.get("url") or candidate.get("href")
    if isinstance(candidate, str) and candidate.strip():
        target = urljoin(current_url, candidate.strip())
        try:
            target_parts = urlsplit(target)
            valid = (
                target_parts.scheme in {"http", "https"}
                and target_parts.username is None
                and target_parts.password is None
                and _origin(target) == _origin(current_url)
            )
        except ValueError:
            valid = False
        if not valid:
            raise VCommissionSyncError(
                "Campaign API returned an invalid or cross-origin next-page URL."
            )
        return target

    more = pagination.get("has_next", pagination.get("hasNext", pagination.get("has_more")))
    current_page = pagination.get("current_page", pagination.get("currentPage"))
    total_pages = pagination.get("total_pages", pagination.get("totalPages"))
    total = pagination.get("total", pagination.get("count"))
    page_size = pagination.get("per_page", pagination.get("limit"))
    indicates_more = more is True
    try:
        if current_page is not None and total_pages is not None:
            indicates_more |= int(current_page) < int(total_pages)
        if total is not None and page_size is not None and current_page is not None:
            indicates_more |= int(current_page) * int(page_size) < int(total)
    except (TypeError, ValueError):
        raise VCommissionSyncError("Campaign API returned malformed pagination metadata.") from None
    if indicates_more:
        raise VCommissionSyncError(
            "Campaign API reports additional pages but provides no next-page URL."
        )
    return None


async def _fetch_campaigns(
    url: str, *, client: httpx.AsyncClient | None = None
) -> tuple[list[dict[str, object]], int]:
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError
    except ValueError:
        raise VCommissionSyncError(
            "VCOMMISSIONS_CAMPAIGNS_URL must be an HTTP(S) URL without embedded user credentials.",
            status_code=503,
        ) from None

    pages: list[dict[str, object]] = []
    current_url: str | None = url
    visited: set[str] = set()
    page_count = 0
    expected_campaign_count: int | None = None
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False)
    try:
        while current_url is not None:
            if current_url in visited:
                raise VCommissionSyncError("Campaign API pagination contains a repeated page link.")
            if page_count >= _MAX_PAGES:
                raise VCommissionSyncError("Campaign API exceeded the 1000-page safety limit.")
            visited.add(current_url)

            async def _request(page_url: str = current_url) -> tuple[httpx.Response, dict]:
                response = await client.get(
                    page_url,
                    headers={"Accept": "application/json", "User-Agent": "DurzaBot/1.0"},
                )
                response.raise_for_status()
                try:
                    body = response.json()
                except ValueError:
                    raise VCommissionSyncError("Campaign API returned malformed JSON.") from None
                if not isinstance(body, dict):
                    raise VCommissionSyncError("Campaign API response must be a JSON object.")
                return response, body

            try:
                _response, body = await with_retries(_request, retryable=(httpx.HTTPError,))
            except httpx.TimeoutException:
                raise VCommissionSyncError("Timed out while requesting campaign data.") from None
            except httpx.HTTPStatusError as exc:
                raise VCommissionSyncError(
                    f"Campaign API returned HTTP {exc.response.status_code}."
                ) from None
            except httpx.HTTPError as exc:
                raise VCommissionSyncError(
                    f"Campaign API request failed ({type(exc).__name__})."
                ) from None

            if body.get("success") is not True:
                raise VCommissionSyncError("Campaign API reported an unsuccessful response.")
            data = body.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("campaigns"), list):
                raise VCommissionSyncError("Campaign API response has no data.campaigns list.")
            count_value = data.get("count")
            if count_value is not None:
                if isinstance(count_value, bool) or not (
                    isinstance(count_value, int)
                    or isinstance(count_value, str)
                    and count_value.isdecimal()
                ):
                    raise VCommissionSyncError("Campaign API returned a malformed campaign count.")
                page_expected_count = int(count_value)
                if (
                    expected_campaign_count is not None
                    and expected_campaign_count != page_expected_count
                ):
                    raise VCommissionSyncError(
                        "Campaign API returned inconsistent campaign counts."
                    )
                expected_campaign_count = page_expected_count
            pages.extend(data["campaigns"])
            page_count += 1
            current_url = _next_url(body, current_url)
    finally:
        if owns_client:
            await client.aclose()
    if expected_campaign_count is not None and expected_campaign_count != len(pages):
        raise VCommissionSyncError(
            "Campaign API count does not match the campaigns returned; refusing to sync an "
            "incomplete feed."
        )
    return pages, page_count


async def _store_matches(
    db: AsyncSession, campaigns: list[dict[str, object]]
) -> dict[tuple[str, str], uuid.UUID]:
    domains: set[str] = set()
    names: set[str] = set()
    for item in campaigns:
        domain = merchants.normalize_domain(item.get("preview_url"))  # type: ignore[arg-type]
        name = merchants.normalize_name(str(item["merchant_name"]))
        if domain:
            domains.add(domain)
        if name:
            names.add(name)
    if not domains and not names:
        return {}
    filters = []
    if domains:
        filters.append(Store.normalized_domain.in_(domains))
    if names:
        filters.append(Store.normalized_name.in_(names))
    rows = (await db.execute(select(Store).where(or_(*filters)))).scalars().all()
    by_domain: dict[str, uuid.UUID] = {}
    by_name: dict[str, uuid.UUID] = {}
    for store in rows:
        if store.normalized_domain:
            by_domain[store.normalized_domain] = store.id
        if store.normalized_name:
            by_name.setdefault(store.normalized_name, store.id)
    matches: dict[tuple[str, str], uuid.UUID] = {}
    for item in campaigns:
        domain = merchants.normalize_domain(item.get("preview_url"))  # type: ignore[arg-type]
        name = merchants.normalize_name(str(item["merchant_name"]))
        store_id = by_domain.get(domain) if domain else None
        store_id = store_id or by_name.get(name)
        if store_id:
            matches[("campaign", str(item["external_campaign_id"]))] = store_id
    return matches


async def _finish_failed(
    db: AsyncSession, run: AffiliateCampaignSync, error: VCommissionSyncError
) -> None:
    run.status = "failed"
    run.error = str(error)[:500]
    run.finished_at = datetime.now(UTC)
    await db.commit()


async def sync_campaigns(
    db: AsyncSession, *, client: httpx.AsyncClient | None = None
) -> SyncSummary:
    """Fetch all pages before mutating campaign rows; reconcile only complete valid feeds."""
    settings = get_settings()
    run = AffiliateCampaignSync(status="running")
    db.add(run)
    await db.commit()
    url = settings.vcommissions_campaigns_url
    if not url:
        error = VCommissionSyncError(
            "VCOMMISSIONS_CAMPAIGNS_URL is not configured in the backend environment.",
            status_code=503,
        )
        await _finish_failed(db, run, error)
        raise error
    try:
        raw_campaigns, pages_fetched = await _fetch_campaigns(url, client=client)
    except VCommissionSyncError as exc:
        await _finish_failed(db, run, exc)
        raise

    skipped = 0
    normalized: dict[str, dict[str, object]] = {}
    for raw in raw_campaigns:
        item = _campaign_data(raw)
        if item is None:
            skipped += 1
            continue
        external_id = str(item["external_campaign_id"])
        if external_id in normalized:
            skipped += 1
            continue
        normalized[external_id] = item

    if not raw_campaigns:
        existing_active = await db.scalar(
            select(AffiliateCampaign.id)
            .where(AffiliateCampaign.network == _NETWORK, AffiliateCampaign.is_active.is_(True))
            .limit(1)
        )
        if existing_active is not None:
            skipped = 1

    rows = list(normalized.values())
    store_matches = await _store_matches(db, rows)
    existing_rows = (
        (await db.execute(select(AffiliateCampaign).where(AffiliateCampaign.network == _NETWORK)))
        .scalars()
        .all()
    )
    existing = {row.external_campaign_id: row for row in existing_rows}
    now = datetime.now(UTC)
    inserted = updated = unchanged = deactivated = 0
    seen = set(normalized)
    mutable_fields = (
        "title",
        "description",
        "kpi",
        "merchant_name",
        "store_id",
        "preview_url",
        "tracking_url",
        "thumbnail_url",
        "categories",
        "countries",
        "conversion_model",
        "currency",
        "payouts",
        "default_goal",
        "goals",
        "device",
        "operating_system",
        "app_name",
        "app_id",
        "network_metadata",
        "terms",
        "needs_review",
        "is_active",
    )

    for external_id, item in normalized.items():
        item["store_id"] = store_matches.get(("campaign", external_id))
        campaign = existing.get(external_id)
        if campaign is None:
            campaign = AffiliateCampaign(
                network=_NETWORK,
                external_campaign_id=external_id,
                last_synced_at=now,
                **{key: value for key, value in item.items() if key != "external_campaign_id"},
            )
            db.add(campaign)
            inserted += 1
            continue
        changed = any(getattr(campaign, key) != item[key] for key in mutable_fields)
        for key in mutable_fields:
            setattr(campaign, key, item[key])
        campaign.last_synced_at = now
        if changed:
            updated += 1
        else:
            unchanged += 1

    # A feed containing malformed/duplicate rows may still update usable
    # campaigns, but cannot prove absent campaigns should be deactivated.
    if skipped == 0 and rows:
        for campaign in existing_rows:
            if campaign.external_campaign_id not in seen and campaign.is_active:
                campaign.is_active = False
                campaign.last_synced_at = now
                deactivated += 1

    run.pages_fetched = pages_fetched
    run.inserted = inserted
    run.updated = updated
    run.unchanged = unchanged
    run.skipped = skipped
    run.deactivated = deactivated
    run.status = "partial" if skipped else "success"
    if skipped and not raw_campaigns:
        run.error = "The feed was empty; existing campaigns were preserved."
    else:
        run.error = (
            f"Skipped {skipped} malformed or duplicate campaign record(s)." if skipped else None
        )
    run.finished_at = now
    await db.commit()
    summary = SyncSummary(
        pages_fetched=pages_fetched,
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
        skipped=skipped,
        deactivated=deactivated,
        status=run.status,
    )
    logger.info(
        "vCommission campaign sync finished",
        extra={
            "status": summary.status,
            "pages": pages_fetched,
            "inserted": inserted,
            "updated": updated,
            "unchanged": unchanged,
            "skipped": skipped,
            "deactivated": deactivated,
        },
    )
    return summary


async def list_campaigns(
    db: AsyncSession,
    *,
    search: str | None = None,
    category: str | None = None,
    country: str | None = None,
    needs_review: bool | None = None,
    skip: int = 0,
    limit: int = 20,
) -> tuple[list[AffiliateCampaign], int]:
    from app.core.pagination import paginate

    stmt = select(AffiliateCampaign).where(
        AffiliateCampaign.network == _NETWORK,
        AffiliateCampaign.is_active.is_(True),
    )
    if search:
        pattern = f"%{search.strip()}%"
        stmt = stmt.where(
            or_(
                AffiliateCampaign.title.ilike(pattern),
                AffiliateCampaign.merchant_name.ilike(pattern),
                AffiliateCampaign.description.ilike(pattern),
            )
        )
    if category:
        category_json = _JSON(category, ensure_ascii=True)
        stmt = stmt.where(
            cast(AffiliateCampaign.categories, Text).contains(category_json, autoescape=True)
        )
    if country:
        country_json = _JSON(country.upper(), ensure_ascii=True)
        stmt = stmt.where(
            or_(
                AffiliateCampaign.countries == [],
                cast(AffiliateCampaign.countries, Text).contains(country_json, autoescape=True),
            )
        )
    if needs_review is not None:
        stmt = stmt.where(AffiliateCampaign.needs_review.is_(needs_review))
    stmt = stmt.order_by(AffiliateCampaign.merchant_name, AffiliateCampaign.title)
    items, total = await paginate(db, stmt, skip, limit)
    return list(items), total


async def get_campaign(db: AsyncSession, campaign_id: uuid.UUID) -> AffiliateCampaign | None:
    campaign = await db.get(AffiliateCampaign, campaign_id)
    if campaign is None or campaign.network != _NETWORK or not campaign.is_active:
        return None
    return campaign
