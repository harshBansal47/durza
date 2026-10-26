"""Pull affiliate transaction reports and write them to our database.

This is the job that closes the revenue loop: clicks are recorded with a
`clickref`, the network reports which clickrefs became sales, and this module
turns those reports into `Conversion` and `AffiliateRevenue` rows.

Design:

* **Idempotent upsert.** The unique key is `(network, network_txn_id)`. A
  transaction can be reported many times — Cuelinks and Awin both re-list the
  same txn as it moves pending -> approved/declined — so every field is
  overwritten on each run rather than inserted fresh. A re-run of the same
  window must never duplicate a row.
* **Unmatched clickrefs are kept.** A conversion whose clickref is not in
  `click_events` (corrupted, or clicked before tracking was added) still gets
  a row with `click_id` NULL. Dropping it would hide real commission from the
  totals; keeping it means totals reconcile with the network's dashboard.
* **Network credentials live in the environment only**, same discipline as the
  ingestion sources: settings carry the values, the database carries none.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.observability import get_logger
from app.ingestion.retry import with_retries
from app.integrations.admitad import get_access_token, page_results
from app.models.affiliate_provider import AffiliateProvider
from app.models.affiliate_revenue import AffiliateRevenue, RevenueStatus
from app.models.click import ClickEvent
from app.models.conversion import Conversion, ConversionStatus

logger = get_logger("durza.conversion_sync")

_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_USER_AGENT = "DurzaBot/1.0 (+https://durza.in/bot)"
_AWIN_MAX_WINDOW_DAYS = 31


# ---- Report shape --------------------------------------------------------


@dataclass(frozen=True)
class NetworkTransaction:
    """One transaction row as we understand it after normalising."""

    network: str
    network_txn_id: str
    clickref: str | None
    raw_status: str
    sale_amount: Decimal | None
    commission_amount: Decimal | None
    currency: str | None
    transaction_at: datetime | None
    validated_at: datetime | None
    decline_reason: str | None


_STATUS_MAP = {
    "pending": ConversionStatus.pending,
    "approved": ConversionStatus.approved,
    "confirmed": ConversionStatus.approved,
    "validated": ConversionStatus.approved,
    "declined": ConversionStatus.declined,
    "rejected": ConversionStatus.declined,
    "cancelled": ConversionStatus.deleted,
    "canceled": ConversionStatus.deleted,
    "deleted": ConversionStatus.deleted,
    "reversed": ConversionStatus.deleted,
}


def map_status(raw: str | None) -> ConversionStatus:
    if not raw:
        return ConversionStatus.pending
    return _STATUS_MAP.get(raw.strip().lower(), ConversionStatus.pending)


_REVENUE_STATUS = {
    ConversionStatus.pending: RevenueStatus.pending,
    ConversionStatus.approved: RevenueStatus.confirmed,
    ConversionStatus.declined: RevenueStatus.reversed,
    ConversionStatus.deleted: RevenueStatus.reversed,
}


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _amount_and_currency(value: Any) -> tuple[Decimal | None, str | None]:
    if isinstance(value, dict):
        amount = _decimal(value.get("amount"))
        currency = value.get("currency")
        return amount, str(currency) if currency else None
    return _decimal(value), None


def _parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


# ---- Network clients -----------------------------------------------------


class AwinTransactionsClient:
    """Fetches the publisher transaction report from the Awin API.

    Awin exposes transaction-level reporting under the publisher service
    (`/publishers/{id}/transactions`). The endpoint page paginates with
    `page`/`pageSize` like the rest of the Awin API. Field names below follow
    Awin's report schema; unknown extra fields are ignored.
    """

    network = "awin"

    def __init__(
        self,
        *,
        api_key: str,
        publisher_id: str,
        base_url: str = "https://api.awin.com",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._publisher_id = publisher_id
        self._base_url = base_url.rstrip("/")
        self._client = client

    async def fetch(self, *, since: datetime) -> list[NetworkTransaction]:
        headers = {
            "User-Agent": _USER_AGENT,
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=_TIMEOUT)
        try:
            out: list[NetworkTransaction] = []
            end_date = datetime.now(UTC).date()
            window_start = since.date()
            while window_start <= end_date:
                window_end = min(window_start + timedelta(days=_AWIN_MAX_WINDOW_DAYS - 1), end_date)
                page = 1
                while True:

                    async def _request(
                        page: int = page,
                        start_date=window_start,
                        end_date=window_end,
                    ) -> httpx.Response:
                        return await client.get(
                            f"{self._base_url}/publishers/{self._publisher_id}/transactions",
                            params={
                                "startDate": start_date.isoformat(),
                                "endDate": end_date.isoformat(),
                                "timezone": "UTC",
                                "page": page,
                                "pageSize": 100,
                            },
                            headers=headers,
                        )

                    response = await with_retries(_request)
                    response.raise_for_status()
                    payload = response.json()
                    if isinstance(payload, list):
                        rows = payload
                        total_pages = None
                    elif isinstance(payload, dict):
                        rows = payload.get("transactions", [])
                        total_pages = payload.get("totalPages")
                    else:
                        raise TypeError("Awin transactions response must be a list or object")
                    if not isinstance(rows, list):
                        raise TypeError("Awin transactions response 'transactions' must be a list")
                    for row in rows:
                        txn = self._normalise(row)
                        if txn is not None:
                            out.append(txn)
                    if total_pages is not None and page >= int(total_pages):
                        break
                    if len(rows) < 100:
                        break
                    page += 1
                window_start = window_end + timedelta(days=1)
            return out
        finally:
            if owns_client:
                await client.aclose()

    def _normalise(self, row: dict[str, Any]) -> NetworkTransaction | None:
        txn_id = row.get("transactionId") or row.get("transaction_id") or row.get("id")
        if txn_id is None:
            return None
        click_refs = row.get("clickRefs")
        clickref = (
            click_refs.get("clickRef") or click_refs.get("clickref")
            if isinstance(click_refs, dict)
            else None
        )
        sale_amount, sale_currency = _amount_and_currency(
            row.get("saleAmount") or row.get("orderAmount") or row.get("sale_amount")
        )
        commission_amount, commission_currency = _amount_and_currency(
            row.get("publisherCommission") or row.get("commission") or row.get("commission_amount")
        )
        return NetworkTransaction(
            network=self.network,
            network_txn_id=str(txn_id),
            clickref=clickref or row.get("clickRef") or row.get("clickref") or row.get("click_ref"),
            raw_status=str(
                row.get("commissionStatus")
                or row.get("status")
                or row.get("transactionStatus")
                or ""
            ),
            sale_amount=sale_amount,
            commission_amount=commission_amount,
            currency=(
                sale_currency
                or commission_currency
                or row.get("currency")
                or row.get("saleCurrency")
                or None
            ),
            transaction_at=_parse_dt(row.get("transactionDate") or row.get("transaction_at")),
            validated_at=_parse_dt(row.get("validationDate") or row.get("validated_at")),
            decline_reason=row.get("declineReason") or row.get("decline_reason"),
        )


class CuelinksTransactionsClient:
    """Fetches the transaction report from the Cuelinks API.

    Cuelinks authenticates with an API token and reports per-transaction
    publisher earnings. The `sub_id` column is the clickref we attach when
    wrapping the outbound link (see `app.services.affiliate`).
    """

    network = "cuelinks"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.cuelinks.com",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client

    async def fetch(self, *, since: datetime) -> list[NetworkTransaction]:
        headers = {
            "User-Agent": _USER_AGENT,
            "Authorization": f"Token {self._api_key}",
            "Accept": "application/json",
        }
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=_TIMEOUT)
        try:
            page = 1
            out: list[NetworkTransaction] = []
            while True:

                async def _request(page: int = page) -> httpx.Response:
                    return await client.get(
                        f"{self._base_url}/v2/transactions",
                        params={
                            "from": since.date().isoformat(),
                            "to": datetime.now(UTC).date().isoformat(),
                            "page": page,
                            "page_size": 100,
                        },
                        headers=headers,
                    )

                response = await with_retries(_request)
                response.raise_for_status()
                payload = response.json()
                rows = payload.get("results", payload if isinstance(payload, list) else [])
                for row in rows:
                    txn = self._normalise(row)
                    if txn is not None:
                        out.append(txn)
                if not payload.get("next"):
                    break
                page += 1
            return out
        finally:
            if owns_client:
                await client.aclose()

    def _normalise(self, row: dict[str, Any]) -> NetworkTransaction | None:
        txn_id = row.get("transaction_id") or row.get("id")
        if txn_id is None:
            return None
        return NetworkTransaction(
            network=self.network,
            network_txn_id=str(txn_id),
            clickref=row.get("subid1")
            or row.get("sub_id")
            or row.get("subid")
            or row.get("clickref"),
            raw_status=str(row.get("status") or ""),
            sale_amount=_decimal(row.get("sale_amount") or row.get("order_value")),
            commission_amount=_decimal(row.get("commission") or row.get("publisher_commission")),
            currency=row.get("currency") or None,
            transaction_at=_parse_dt(row.get("transaction_date") or row.get("datetime")),
            validated_at=_parse_dt(row.get("confirmed_at") or row.get("validated_at")),
            decline_reason=row.get("decline_reason") or row.get("reason"),
        )


class AdmitadTransactionsClient:
    """Fetches publisher actions from the Admitad statistics API."""

    network = "admitad"

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        base_url: str = "https://api.admitad.com",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._base_url = base_url.rstrip("/")
        self._client = client

    async def fetch(self, *, since: datetime) -> list[NetworkTransaction]:
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=_TIMEOUT)
        try:
            token = await get_access_token(
                client, client_id=self._client_id, client_secret=self._client_secret
            )
            headers = {
                "User-Agent": _USER_AGENT,
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }
            offset = 0
            out: list[NetworkTransaction] = []
            while True:

                async def _request(
                    offset: int = offset,
                ) -> tuple[list[dict[str, Any]], bool]:
                    response = await client.get(
                        f"{self._base_url}/statistics/actions/",
                        params={
                            "date_start": since.date().isoformat(),
                            "date_end": datetime.now(UTC).date().isoformat(),
                            "limit": 100,
                            "offset": offset,
                        },
                        headers=headers,
                    )
                    response.raise_for_status()
                    return page_results(
                        response.json(), offset=offset, limit=100, resource="actions"
                    )

                rows, has_next = await with_retries(
                    _request, retryable=(httpx.HTTPError, TypeError)
                )
                for row in rows:
                    txn = self._normalise(row)
                    if txn is not None:
                        out.append(txn)
                if not has_next:
                    break
                offset += len(rows)
            return out
        finally:
            if owns_client:
                await client.aclose()

    def _normalise(self, row: dict[str, Any]) -> NetworkTransaction | None:
        txn_id = row.get("id") or row.get("action_id") or row.get("transaction_id")
        if txn_id is None:
            return None
        sale_amount, sale_currency = _amount_and_currency(
            row.get("order_sum")
            or row.get("order_amount")
            or row.get("sale_amount")
            or row.get("amount")
        )
        commission_amount, commission_currency = _amount_and_currency(
            row.get("payment_sum")
            or row.get("payment")
            or row.get("commission")
            or row.get("commission_amount")
        )
        return NetworkTransaction(
            network=self.network,
            network_txn_id=str(txn_id),
            clickref=(
                row.get("subid1")
                or row.get("subid")
                or row.get("sub_id")
                or row.get("clickref")
                or row.get("click_ref")
            ),
            raw_status=str(row.get("status") or row.get("action_status") or ""),
            sale_amount=sale_amount,
            commission_amount=commission_amount,
            currency=(
                row.get("currency")
                or row.get("order_currency")
                or sale_currency
                or commission_currency
            ),
            transaction_at=_parse_dt(
                row.get("action_date")
                or row.get("date")
                or row.get("created_at")
                or row.get("transaction_date")
            ),
            validated_at=_parse_dt(
                row.get("approval_date") or row.get("validated_at") or row.get("updated_at")
            ),
            decline_reason=row.get("decline_reason") or row.get("reason"),
        )


def build_clients_from_settings() -> list[Any]:
    """Clients for every network with credentials configured, and only those."""
    settings = get_settings()
    clients: list[Any] = []
    if settings.awin_api_key and settings.awin_publisher_id:
        clients.append(
            AwinTransactionsClient(
                api_key=settings.awin_api_key, publisher_id=settings.awin_publisher_id
            )
        )
    if settings.cuelinks_api_key:
        clients.append(CuelinksTransactionsClient(api_key=settings.cuelinks_api_key))
    if settings.admitad_client_id and settings.admitad_client_secret:
        clients.append(
            AdmitadTransactionsClient(
                client_id=settings.admitad_client_id,
                client_secret=settings.admitad_client_secret,
            )
        )
    return clients


# ---- Sync -----------------------------------------------------------------


async def _provider_id_for(db: AsyncSession, network: str) -> Any:
    result = await db.execute(select(AffiliateProvider).where(AffiliateProvider.slug == network))
    provider = result.scalars().first()
    return provider.id if provider else None


async def upsert_transaction(
    db: AsyncSession,
    txn: NetworkTransaction,
    *,
    touched_coupons: set | None = None,
) -> str:
    """Insert or update one transaction and its revenue row. Returns the outcome."""
    click: ClickEvent | None = None
    if txn.clickref:
        click_result = await db.execute(
            select(ClickEvent).where(ClickEvent.clickref == txn.clickref)
        )
        click = click_result.scalars().first()
    if click is not None and click.coupon_id is not None and touched_coupons is not None:
        touched_coupons.add(click.coupon_id)

    conversion_result = await db.execute(
        select(Conversion).where(
            Conversion.network == txn.network, Conversion.network_txn_id == txn.network_txn_id
        )
    )
    conversion: Conversion | None = conversion_result.scalars().first()
    status = map_status(txn.raw_status)
    if conversion is None:
        conversion = Conversion(
            network=txn.network,
            network_txn_id=txn.network_txn_id,
            clickref=txn.clickref,
            click_id=click.id if click else None,
            store_id=click.store_id if click else None,
            status=status,
            sale_amount=txn.sale_amount,
            commission_amount=txn.commission_amount,
            currency=txn.currency,
            transaction_at=txn.transaction_at,
            click_at=click.created_at if click else None,
            validated_at=txn.validated_at,
            decline_reason=txn.decline_reason,
        )
        db.add(conversion)
        await db.flush()
        outcome = "inserted"
    else:
        conversion.status = status
        conversion.clickref = txn.clickref or conversion.clickref
        if click is not None:
            conversion.click_id = click.id
            conversion.store_id = click.store_id
            conversion.click_at = click.created_at
        conversion.sale_amount = txn.sale_amount
        conversion.commission_amount = txn.commission_amount
        conversion.currency = txn.currency
        conversion.transaction_at = txn.transaction_at
        conversion.validated_at = txn.validated_at
        conversion.decline_reason = txn.decline_reason
        outcome = "updated"

    # Revenue row: one per conversion, refreshed in place. A pending txn carries
    # a "pending" revenue row; declined/deleted flip it to "reversed" with the
    # reason preserved for audit.
    revenue_result = await db.execute(
        select(AffiliateRevenue).where(AffiliateRevenue.conversion_id == conversion.id)
    )
    revenue: AffiliateRevenue | None = revenue_result.scalars().first()
    gross = float(txn.commission_amount or 0)
    if revenue is None:
        revenue = AffiliateRevenue(conversion_id=conversion.id)
        db.add(revenue)
    revenue.merchant_id = conversion.store_id
    revenue.provider_id = await _provider_id_for(db, txn.network)
    revenue.gross_commission = gross
    revenue.adjustments = 0.0
    revenue.net_commission = gross
    revenue.currency = txn.currency or "USD"
    revenue.status = _REVENUE_STATUS[status]
    revenue.conversion_date = txn.transaction_at
    revenue.reported_date = datetime.now(UTC)
    if status is ConversionStatus.approved:
        revenue.confirmed_date = txn.validated_at or datetime.now(UTC)
    if status in (ConversionStatus.declined, ConversionStatus.deleted):
        revenue.notes = txn.decline_reason or f"network reported {status.value}"
    return outcome


async def sync_conversions(
    db: AsyncSession,
    *,
    clients: Sequence[Any] | None = None,
    since: datetime | None = None,
) -> str:
    settings = get_settings()
    clients = clients if clients is not None else build_clients_from_settings()
    if not clients:
        return "no network credentials configured; skipped"
    since = since or (datetime.now(UTC) - timedelta(days=settings.conversion_sync_lookback_days))

    inserted = updated = skipped_rows = 0
    touched_coupons: set = set()
    for client in clients:
        transactions = await client.fetch(since=since)
        for txn in transactions:
            if not txn.network_txn_id:
                skipped_rows += 1
                continue
            outcome = await upsert_transaction(db, txn, touched_coupons=touched_coupons)
            if outcome == "inserted":
                inserted += 1
            else:
                updated += 1
    await db.commit()

    # Every sale that moved could change which coupon deserves the top slot, so
    # refresh the quality scores of exactly the coupons touched this run.
    if touched_coupons:
        from app.models.coupon import Coupon
        from app.services.epc import epc_by_coupon
        from app.services.quality import score_for_coupon

        epcs = await epc_by_coupon(db)
        for coupon_id in touched_coupons:
            coupon = await db.get(Coupon, coupon_id)
            if coupon is not None:
                coupon.quality_score = score_for_coupon(coupon, epc=epcs.get(coupon_id))
        await db.commit()

    logger.info(
        "conversion sync finished",
        extra={"inserted": inserted, "updated": updated, "skipped": skipped_rows},
    )
    return (
        f"inserted {inserted}, updated {updated}, skipped {skipped_rows}; networks {len(clients)}"
    )
