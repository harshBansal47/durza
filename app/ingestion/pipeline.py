"""The ingestion pipeline: one Source -> many RawOffers -> one canonical Coupon
per distinct offer, plus one `CouponSource` row per provider.

## The rule everything here serves

**One canonical coupon, many source records.** When Rakuten, Awin, CJ and Admitad
all advertise `SAVE20` at Nike, the shopper sees one card and the site stores one
`Coupon`. What each network said — its own expiry, its own affiliate URL, its own
headline, its own external id — is kept in `coupon_sources`, one row per provider.

That split is not tidiness. It is what makes the disagreement *survivable*. Four
providers report four different expiry dates; one row would force us to either
pick a winner and discard the rest or let the last writer win, and both make the
site worse every time two networks happen to differ. Four rows let us apply a
stated rule (earliest expiry wins, highest-priority source supplies each field)
and keep the losing claims so an operator can see what was overruled and why.

## Order of operations, and why

    fetch -> normalize -> validate -> resolve merchant -> identify coupon
         -> write CouponSource -> resolve conflicts -> apply status -> score

Two of those steps are the whole architecture and everything else is plumbing:

* `merchant_resolution.resolve_or_create` is the only place a `Store` is written.
  Centralising it is what makes "NIKE", "NIKE" and "www.nike.com" end up on one
  merchant page instead of three.
* `_identify_coupon` is the only place the "is this the same coupon?" question is
  asked, and it asks it in a fixed order — merchant+code, then the provider's own
  id, then the full dedup key. The order matters more than the individual
  lookups: a match on merchant+code is near-proof of identity, while a match on
  the fingerprint is a judgement call, and a pipeline that tried them in a
  different order would merge differently on a re-run.

## What this module will not do

* **It will not treat silence as removal.** A source that did not mention a
  coupon in a *completed* run gets its `CouponSource` row downgraded — and
  nothing else. On a partial or failed run, absence is ignored entirely, because
  one 500 on page 3 of 40 has told us nothing about the other 37 pages. The
  coupon itself stays `active`.
* **It will not resurrect a disabled coupon.** `CouponStatus.disabled` means a
  person decided this; a machine that cannot tell that apart from "the data looks
  odd" will eventually undo a person's decision.
* **It will not name a provider anywhere near the public API.** Provider identity
  lives in `coupon_sources`, which no public schema reads. `destination_url` on
  the coupon is a resolved value with the network's tracking parameters already
  attached, and it is what `/go` redirects to.
"""

import time
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import metrics
from app.core.config import get_settings
from app.core.observability import get_logger
from app.core.slugs import generate_unique_slug
from app.ingestion import (
    conflicts,
    credentials,
    dedupe,
    merchant_resolution,
    merchants,
    probe,
    quarantine,
    validate,
)
from app.ingestion import normalize as normalize_mod
from app.ingestion.base import PartialFetchError
from app.ingestion.normalize import NormalizedOffer, NormalizeError
from app.ingestion.registry import get_adapter
from app.ingestion.source_health import MAX_ERROR, record_outcome
from app.ingestion.taxonomy import canonical_category_slug
from app.models.category import Category
from app.models.coupon import Coupon, CouponStatus
from app.models.coupon_source import CouponSource, CouponSourceStatus
from app.models.ingestion_run import IngestionRun, RunStatus, SyncMode
from app.models.source import Source, SourceKind

logger = get_logger("durza.ingestion.pipeline")

#: Records processed between progress flushes. A flush is a round trip, so doing
#: one per record turns a 40,000-record run into 40,000 round trips; doing none
#: until the end means a crash loses the whole run's work and holds a
#: transaction open for the length of the fetch.
BATCH_SIZE = 200

#: Source kinds that need credentials from the environment. A `static` or `csv`
#: source has none, so requiring a credential_ref for it would make the local
#: and test paths fail for no reason. Aliased from `credentials.CREDENTIAL_KINDS`
#: so that adding a network is a one-place change — the same list decides whether a
#: missing key aborts the run *and* whether health calls the source `unconfigured`,
#: and two copies of it would eventually disagree.
NETWORK_KINDS = credentials.CREDENTIAL_KINDS


class Cancelled(Exception):
    """Raised internally when an operator asked a run to stop.

    Carries no message: cancellation is not an error condition and the run row
    should say `cancelled`, not record a diagnostic that reads like a failure.
    """


# ---------------------------------------------------------------------------
# Category
# ---------------------------------------------------------------------------


async def _resolve_category(
    db: AsyncSession, offer: NormalizedOffer, source: Source
) -> Category | None:
    """Map an offer onto our taxonomy, or `None` if we genuinely cannot.

    Three candidates, strongest first: the offer's own slug, the source's
    configured default, then the offer's category *name*. The default is second
    rather than first so an offer that names a real category is not filed under a
    generic bucket just because the source has a fallback configured.

    Returning `None` makes the pipeline count the record as `failed` rather than
    guessing. A coupon filed under the wrong category is discoverable in search
    and wrong; one that is not filed is invisible and honest.
    """
    for candidate in (
        offer.category_slug,
        source.config.get("default_category_slug"),
        offer.category_name,
    ):
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        result = await db.execute(
            select(Category).where(Category.slug == canonical_category_slug(candidate))
        )
        category = result.scalar_one_or_none()
        if category is not None:
            return category

        # Keep support for custom category names whose slug is not derived from
        # the name by the application's normal slug generator.
        result = await db.execute(
            select(Category).where(Category.name.ilike(candidate.strip()))
        )
        category = result.scalar_one_or_none()
        if category is not None:
            return category
    return None


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


async def _identify_coupon(
    db: AsyncSession, source: Source, store_id, offer: NormalizedOffer, content_hash: str
) -> Coupon | None:
    """Find the canonical coupon this offer belongs to, or `None` to create one.

    Tried in a fixed order, strongest evidence first:

    1. **(merchant, normalized_code).** The primary key of the whole design. The
       code is the one string the merchant itself recognises, so within a single
       merchant `SAVE20` is one code and every provider reporting it is
       describing the same fact. The merchant is half the key on purpose: two
       retailers both running `SUMMER` are two coupons, and merging them would
       send half the audience somewhere the code does not work.
    2. **(source, external_id).** The provider's own identifier. Needed because
       a merchant can legitimately run `SAVE20` twice in a year, and because a
       provider that rotates its ids must not orphan its history. Scoped to one
       source so an id collision across networks cannot merge two retailers.
    3. **`content_hash`** — the full dedup key. The backstop, and the only path
       for a code-less offer, whose identity is the fingerprint over merchant +
       normalized title + discount + validity window.

    The order is load-bearing rather than incidental. Running 3 before 1 would
    let a fingerprint collision override a real code match; running 1 alone would
    merge a merchant's genuinely separate `SAVE20` campaigns.
    """
    if offer.code:
        normalized = merchants.normalize_code(offer.code)
        if normalized:
            result = await db.execute(
                select(Coupon).where(
                    Coupon.store_id == store_id,
                    Coupon.normalized_code == normalized,
                )
            )
            found = result.scalars().first()
            if found is not None:
                return found

    if offer.external_id:
        # No `.distinct()`. It was there to be safe about the join fanning out,
        # but `uq_coupon_source (coupon_id, source_id)` already makes duplicate
        # rows impossible for one source — and it is actively harmful: DISTINCT
        # over a whole entity means DISTINCT over every column, two of which are
        # `json`, and Postgres has no equality operator for `json`. The statement
        # fails at runtime with "could not identify an equality operator". Taking
        # `.first()` needs no dedup at all.
        result = await db.execute(
            select(Coupon)
            .join(CouponSource, CouponSource.coupon_id == Coupon.id)
            .where(
                CouponSource.source_id == source.id,
                CouponSource.external_id == str(offer.external_id),
            )
        )
        found = result.scalars().first()
        if found is not None:
            return found

    result = await db.execute(select(Coupon).where(Coupon.content_hash == content_hash))
    return result.scalars().first()


# ---------------------------------------------------------------------------
# Per-source record
# ---------------------------------------------------------------------------


async def _upsert_coupon_source(
    db: AsyncSession,
    *,
    coupon: Coupon,
    source: Source,
    offer: NormalizedOffer,
    now: datetime,
    conflict_audit: dict[str, Any] | None = None,
) -> CouponSource:
    """Create or refresh this provider's record of the coupon.

    Idempotent on `(coupon_id, source_id)`, which is what makes re-ingestion
    safe: the second run of a source updates its own row instead of fanning the
    coupon into N near-identical ones.

    `last_seen_at` is set on *every* path that reaches this function and is never
    downgraded from here. It is the only evidence that distinguishes "this
    provider no longer lists the coupon" from "this provider's API failed before
    it got to page 12", and `conflicts.mark_stale_for_source` compares against it
    for exactly that reason.
    """
    result = await db.execute(
        select(CouponSource).where(
            CouponSource.coupon_id == coupon.id,
            CouponSource.source_id == source.id,
        )
    )
    row = result.scalar_one_or_none()

    expired = offer.expires_at is not None and offer.expires_at <= now
    status = CouponSourceStatus.expired if expired else CouponSourceStatus.active

    payload = conflicts._slim_raw(offer)
    if conflict_audit:
        # The resolution decisions ride along with the provider's own row rather
        # than in a separate audit table: they explain *this provider's* claim,
        # and a disagreement is always between two specific providers.
        payload["resolution"] = conflict_audit

    if row is None:
        row = CouponSource(
            coupon_id=coupon.id,
            source_id=source.id,
            status=status,
            external_id=offer.external_id,
            affiliate_url=offer.destination_url,
            source_expires_at=offer.expires_at,
            source_title=offer.title,
            raw_data=payload,
            last_seen_at=now,
            source_updated_at=offer.expires_at,
        )
        db.add(row)
        return row

    # Only this provider's own fields are touched. `affiliate_url`,
    # `source_expires_at` and `status` describe what *this* network said, and
    # rewriting them from another network's view would destroy the disagreement
    # the table exists to preserve.
    row.status = status
    row.external_id = offer.external_id or row.external_id
    row.affiliate_url = offer.destination_url or row.affiliate_url
    row.source_expires_at = offer.expires_at
    row.source_title = offer.title[:300]
    row.raw_data = payload
    row.last_seen_at = now
    row.source_updated_at = offer.expires_at or row.source_updated_at
    return row


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def _apply_status(
    coupon: Coupon, *, probe_ok: bool | None, probe_reason: str | None, now: datetime
) -> None:
    """Set the coupon's lifecycle status from what we actually know.

    Precedence, and each step is commented because the order is the whole design:

    * **A disabled coupon is left alone.** `disabled` records a human decision.
      Any machine rule that can reach it will eventually undo a person.
    * **Expiry beats everything.** A date that has passed is a fact; a probe
      result is an observation made at a moment in time and may have been a
      transient 503.
    * **A failed probe is a failure.** Only when probing is actually enabled for
      the source. With `probe_destinations=False` nothing has been checked, and
      `None` correctly means "no evidence" rather than "bad evidence".
    * **Otherwise active.** Including for a coupon that has never been checked.
      `VerificationStatus` is the field that carries "how much should the shopper
      trust this", and defaulting new coupons to invisible here would mean the
      site shows nothing until a checker has visited every one of them.
    """
    coupon.last_checked_at = now

    if coupon.status == CouponStatus.disabled:
        return

    if coupon.expires_at is not None and coupon.expires_at <= now:
        coupon.status = CouponStatus.expired
        coupon.is_active = False
        coupon.failure_reason = "expired"
    elif probe_ok is False:
        coupon.status = CouponStatus.failed
        coupon.is_active = False
        coupon.failure_reason = (probe_reason or "destination unreachable")[:255]
    else:
        coupon.status = CouponStatus.active
        coupon.is_active = True
        coupon.failure_reason = None


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


async def _iter_offers(
    source: Source, adapter: Any, mode: SyncMode, creds: Any
) -> AsyncIterator[tuple[Any, int]]:
    """Yield `(raw_offer, pages_fetched_so_far)`, preferring streaming.

    `adapter.stream()` is optional. An adapter that can page a large catalogue
    should implement it: buffering 40,000 `RawOffer` dataclasses to iterate them
    once is a few hundred megabytes of resident memory for no benefit. Adapters
    with a fixed small payload (static, csv) just expose `fetch`.

    Credentials are resolved *here*, once per run, and handed to the adapter. The
    adapter therefore has no way to read the environment itself, which is what
    makes "a secret only ever reaches the HTTP layer" a property of the code
    rather than a convention someone has to remember.

    The page counter is yielded rather than counted internally because "how many
    pages did we get" is the number that tells an operator whether a provider is
    truncating, and it should reach the run row even when the run fails partway.
    """
    stream = getattr(adapter, "stream", None)
    if callable(stream):
        pages = 0
        async for item in stream(source.config, mode=mode, credentials=creds):
            if isinstance(item, tuple) and len(item) == 2:
                offer, pages = item
            else:
                offer = item
            yield offer, pages
        return

    raw_offers = await adapter.fetch(source.config, mode=mode, credentials=creds)
    # A plain loop rather than `yield from`: `yield from` over a synchronous
    # generator inside an async generator is legal but reads as if it were
    # delegating to another coroutine, which it is not.
    for offer in raw_offers:
        yield offer, 1


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


async def run_ingestion(
    db: AsyncSession,
    source: Source,
    *,
    mode: SyncMode = SyncMode.incremental,
    triggered_by: str | None = None,
    cancel: Callable[[], bool] | None = None,
) -> IngestionRun:
    """Ingest one source, returning the run row whatever happens.

    Never raises for a source-level problem. A provider being broken is an
    expected, recurring event and the thing an operator needs to see is a run row
    saying `failed` with the reason on it — not a traceback that took the sweep
    of the other three providers down with it. Genuine bugs still surface:
    anything not on the expected-failure list propagates after the run row has
    been written.
    """
    started = datetime.now(UTC)
    monotonic = time.monotonic()

    # Snapshotted before anything can invalidate them. Both values are needed
    # *after* the rollback that a failure performs, and a rollback expires every
    # attribute of every object in the session — so reading `source.slug` or
    # `source.id` at that point either raises `MissingGreenlet` (attached source)
    # or silently reads a stale object (detached source). Two plain values
    # sidestep both.
    source_id = source.id
    slug = source.slug

    run = IngestionRun(
        source_id=source_id,
        status=RunStatus.running,
        mode=mode,
        started_at=started,
        triggered_by=triggered_by,
    )
    db.add(run)
    await db.flush()

    try:
        outcome = await _execute(
            db,
            source,
            run,
            mode=mode,
            cancel=cancel,
        )
    except _IngestionAbort as abort:
        # Expected, classified aborts (disabled source, missing credentials).
        # The run row must survive the rollback that follows, so it is rewritten
        # in a fresh transaction.
        return await _record_failure(
            db,
            source_id,
            slug,
            run,
            str(abort),
            started_at=started,
            duration_ms=_elapsed_ms(monotonic),
            count_against_health=False,
        )
    except _PartialRun as partial:
        await db.commit()
        run.status = RunStatus.partial
        run.error = partial.reason[:2000]
        run.finished_at = datetime.now(UTC)
        run.duration_ms = _elapsed_ms(monotonic)
        source = await _managed_source(db, source_id)
        await record_outcome(db, source, RunStatus.partial)
        source.last_error = run.error
        await db.commit()
        await db.refresh(run)
        # Labelled by source slug, not name: the slug is the stable identifier,
        # and a source renamed in the admin panel should not fork this series and
        # orphan the history of every alert that fired on it.
        metrics.ingestion_runs.labels(slug, RunStatus.partial.value).inc()
        return run
    except _Cancelled:
        await db.commit()
        run.status = RunStatus.cancelled
        run.cancel_requested = True
        run.finished_at = datetime.now(UTC)
        run.duration_ms = _elapsed_ms(monotonic)
        await record_outcome(db, await _managed_source(db, source_id), RunStatus.cancelled)
        await db.commit()
        await db.refresh(run)
        metrics.ingestion_runs.labels(slug, RunStatus.cancelled.value).inc()
        return run
    except Exception as exc:  # noqa: BLE001 - the run row must capture any failure
        # No rollback here on purpose: `_record_failure` starts with one, and it
        # has to be the one that happens — rolling back first would expire
        # `source` twice and lose the counters the caller wants carried over.
        return await _record_failure(
            db,
            source_id,
            slug,
            run,
            f"{type(exc).__name__}: {exc}",
            started_at=started,
            duration_ms=_elapsed_ms(monotonic),
            keep_counts=True,
        )

    run.status = outcome
    run.error = None
    run.finished_at = datetime.now(UTC)
    run.duration_ms = _elapsed_ms(monotonic)
    await record_outcome(db, await _managed_source(db, source_id), outcome)
    await db.commit()
    await db.refresh(run)
    metrics.ingestion_runs.labels(slug, outcome.value).inc()
    return run


class _IngestionAbort(Exception):
    """A refusal to run at all — disabled source, missing credential."""


class _PartialRun(Exception):
    """The provider answered, but not completely.

    Carries the adapter's own wording rather than a generic one: "failed on page
    3 of 40" is the difference between a provider having a bad afternoon and an
    integration that needs fixing, and it is the only thing the operator has to go
    on when the run row says `partial`.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Cancelled(Exception):
    """An operator asked the run to stop. Carries no message, by design."""


def _elapsed_ms(monotonic_start: float) -> int:
    return int((time.monotonic() - monotonic_start) * 1000)


async def _managed_source(db: AsyncSession, source_id: Any) -> Source:
    """The Source row as an object `db` can actually write through.

    `run_ingestion` accepts a `Source` the caller loaded somewhere else — a
    detached instance from a closed session is the normal case for a caller that
    picked a source out of a list and then opened a fresh session to run it.
    Writing health updates onto such an object is a silent no-op: the commit
    succeeds and nothing is stored.

    So the terminal paths re-resolve the row through the session. `db.get` returns
    the identity-mapped instance when there is one — which also makes it the
    refresh that undoes the attribute expiry a `rollback()` just caused — and
    issues one SELECT only when the caller handed us something detached.
    """
    source = await db.get(Source, source_id)
    if source is None:
        raise RuntimeError(f"source {source_id} vanished between SELECT and run")
    return source


async def _record_failure(
    db: AsyncSession,
    source_id: Any,
    slug: str,
    run: IngestionRun,
    error: str,
    *,
    started_at: datetime,
    duration_ms: int,
    keep_counts: bool = False,
    count_against_health: bool = True,
) -> IngestionRun:
    """Persist a failed run after its work has been rolled back.

    Rolls back first, and that rollback is the reason this function exists.
    Rolling back also discards the INSERT of the run row itself, so the audit
    trail has to be written again in a clean transaction.

    Everything needed afterwards is passed in by value (`source_id`, `slug`, the
    counters) rather than read off `run` or `source` afterwards, because the
    rollback expires every attribute of every object in the session. Reading one
    then is not a style problem: a synchronous attribute refresh cannot await a
    cursor, so it raises `MissingGreenlet`, and a detached object would instead
    quietly hand back values from a transaction that no longer exists.

    The counters are carried over only for the unexpected-failure path, where they
    describe work that genuinely happened before the exception; losing them would
    make a partially-completed run indistinguishable from one that did nothing.

    `count_against_health` is False for aborts that are really *configuration*
    states — a disabled source, a credential that was never set. Those are the
    operator's to-do list, not evidence that a provider is unhealthy, and they
    still write the run row and the `last_error`, so nothing is hidden.
    """
    counts = (
        {
            "fetched": run.fetched,
            "created": run.created,
            "updated": run.updated,
            "duplicated": run.duplicated,
            "malformed": run.malformed,
            "failed": run.failed,
            "expired": run.expired,
            "pages_fetched": run.pages_fetched,
        }
        if keep_counts
        else {}
    )
    mode = run.mode

    await db.rollback()

    failed_run = IngestionRun(
        source_id=source_id,
        status=RunStatus.failed,
        mode=mode,
        started_at=started_at,
        finished_at=datetime.now(UTC),
        fetched=counts.get("fetched", 0),
        created=counts.get("created", 0),
        updated=counts.get("updated", 0),
        duplicated=counts.get("duplicated", 0),
        malformed=counts.get("malformed", 0),
        failed=counts.get("failed", 0),
        expired=counts.get("expired", 0),
        pages_fetched=counts.get("pages_fetched", 0),
        duration_ms=duration_ms,
        error=error[:MAX_ERROR],
    )
    db.add(failed_run)
    await db.commit()
    await db.refresh(failed_run)
    # The failure is the source's problem, not the offer data's, so the source is
    # degraded here rather than only for transport-level exceptions.
    source = await _managed_source(db, source_id)
    await record_outcome(db, source, RunStatus.failed, count=count_against_health)
    source.last_error = error[:MAX_ERROR]
    await db.commit()
    # Same slug label as the success path, so a failure rate is a ratio over one
    # label set rather than two unrelated series.
    metrics.ingestion_runs.labels(slug, RunStatus.failed.value).inc()
    return failed_run


async def _execute(
    db: AsyncSession,
    source: Source,
    run: IngestionRun,
    *,
    mode: SyncMode,
    cancel: Callable[[], bool] | None,
) -> RunStatus:
    """The body of a run. Every return is a `RunStatus`; every raise is classified.

    Split out from `run_ingestion` so the transaction handling, the failure
    bookkeeping and the metrics all live in one place and cannot drift apart
    between the success and error paths.
    """
    if not source.is_enabled:
        raise _IngestionAbort(f"Source {source.slug} is disabled")

    if not credentials.credentials_satisfied(source.kind.value, source.credential_ref):
        # "Not configured" is not a failure: nobody has supplied credentials
        # yet, and counting it as one would depress a health score for an
        # unfinished deployment step. It is its own status precisely so an
        # operator can tell "the API is broken" from "we never gave it a key".
        raise _IngestionAbort(
            f"Source {source.slug} is missing credentials it needs to be fetched; "
            f"set the environment variable named by its credential_ref, or clear "
            f"the reference if this kind does not need one"
        )

    try:
        adapter = get_adapter(source.kind)
    except ValueError as exc:
        raise _IngestionAbort(str(exc)) from exc

    # Resolved once, here, and handed to the adapter. The adapter cannot read the
    # environment itself, so there is exactly one place in the codebase where a
    # secret is turned into a string.
    creds = credentials.resolve(source.credential_ref, kind=source.kind.value)

    # Coupon ids written this run, for the batch recount of source_count and the
    # quality score at the end. Collected in memory rather than re-queried later:
    # a full sync touches tens of thousands of coupons and the run row has no
    # column large enough to hold them.
    touched: list = []

    try:
        async for batch in _batched(
            _iter_offers(source, adapter, mode, creds), BATCH_SIZE, cancel=cancel
        ):
            for raw, pages in batch:
                await _ingest_one(db, source, run, raw, touched)
                run.pages_fetched = pages
            # Flush per batch so a crash keeps the work and the transaction does
            # not stay open across the whole fetch.
            await db.commit()
    except _Cancelled:
        await db.commit()
        await recompute_totals(db, touched)
        await db.commit()
        raise
    except PartialFetchError as partial:
        # The adapter reached the end of what it could get and says so itself.
        # Everything it did deliver is committed above and stays; what is withheld
        # is only the conclusion "this source no longer lists X", which is
        # handled by never reaching the `mark_stale_for_source` call below.
        await recompute_totals(db, touched)
        await db.commit()
        raise _PartialRun(partial.reason) from partial

    rejected = run.failed + run.malformed + run.quarantined
    if rejected:
        # A syntactically complete API response is not a complete ingestion if
        # records were dropped. Keep existing source links intact: otherwise a
        # category/validation regression can make every old coupon look deleted.
        await recompute_totals(db, touched)
        await db.commit()
        raise _PartialRun(
            f"{rejected} of {run.fetched} fetched offers were rejected, malformed, "
            f"or quarantined; existing source links were preserved"
        )

    # Downgrade this source's stale rows only now, and only for a run that
    # reached the end of the provider's list. On the partial path we raise before
    # reaching here, which is the entire reason a partial run cannot retire
    # coupons.
    retired = await conflicts.mark_stale_for_source(
        db,
        source.id,
        run_started_at=run.started_at,
        as_status=CouponSourceStatus.deleted,
    )
    if retired:
        logger.info(
            "source no longer lists coupons",
            extra={"source": source.slug, "retired": retired},
        )
    # After the retirement, not before: retiring a row changes how many sources
    # still back each coupon, and `source_count` has to reflect the final state.
    await recompute_totals(db, touched)

    run.pages_fetched = max(run.pages_fetched, 1)
    return RunStatus.success


async def _batched(
    stream: AsyncIterator[tuple[Any, int]],
    size: int,
    *,
    cancel: Callable[[], bool] | None,
) -> AsyncIterator[list[tuple[Any, int]]]:
    """Group a stream into fixed-size batches, honouring cancellation.

    Cancellation is checked per batch, not per record: an operator who cancels a
    40,000-record run should not wait for the current batch, but they also should
    not cost us a query per record to notice.

    The `except` clause is the whole reason this is not four lines. A tail batch
    smaller than `size` is only yielded *after* the stream ends, so a stream that
    raises part-way through used to lose that tail — every provider that
    truncated mid-page silently dropped up to `BATCH_SIZE - 1` records it had
    already paid for, which is the opposite of what `PartialFetchError` promises.

    Only `PartialFetchError` is caught, and deliberately so. Widening this to
    `BaseException` would also catch `GeneratorExit`, and yielding from inside a
    `GeneratorExit` handler is an error in itself — the generator is being closed
    and must not produce another value. Cancellation is left alone for the same
    reason, and because dropping the tail is the right answer to "stop now".
    """
    batch: list[tuple[Any, int]] = []
    try:
        async for item in stream:
            batch.append(item)
            if len(batch) >= size:
                if cancel is not None and cancel():
                    raise _Cancelled()
                yield batch
                batch = []
        if batch:
            if cancel is not None and cancel():
                raise _Cancelled()
            yield batch
    except PartialFetchError:
        if batch:
            yield batch
        raise


async def _ingest_one(
    db: AsyncSession,
    source: Source,
    run: IngestionRun,
    raw: Any,
    touched: list | None = None,
) -> None:
    """Normalize, validate, dedupe and write one raw offer.

    Every rejection path increments exactly one counter and returns. That is the
    contract: `fetched` must always equal the sum of everything that followed, or
    the run row is lying about how much data was thrown away.
    """
    now = datetime.now(UTC)
    run.fetched += 1

    try:
        offer = normalize_mod.normalize_offer(raw)
    except NormalizeError as exc:
        # A spike here means the provider changed their payload shape, not that
        # the data was bad — a completely different fix from `failed`.
        run.malformed += 1
        logger.debug("offer could not be normalized", extra={"reason": str(exc)})
        return

    errors = validate.validate_offer(offer)
    if errors:
        run.failed += 1
        logger.debug("offer failed validation", extra={"errors": errors})
        return

    category = await _resolve_category(db, offer, source)
    if category is None:
        run.failed += 1
        logger.debug(
            "offer matched no category",
            extra={"category_slug": offer.category_slug, "source": source.slug},
        )
        return

    match = await _resolve_merchant(db, offer, source)
    store = match.store

    try:
        reason = quarantine.quarantine_reason(
            offer, store_domain=getattr(store, "normalized_domain", None)
        )
    except Exception as exc:
        logger.exception("quarantine check failed", extra={"error": str(exc)})
        reason = None
    mode = get_settings().ingestion_quarantine_mode
    if reason and mode == "enforce":
        run.quarantined += 1
        logger.info("offer quarantined", extra={"reason": reason})
        return
    if reason and mode == "shadow":
        logger.info("offer would be quarantined", extra={"reason": reason})

    content_hash = dedupe.content_hash(offer)
    coupon = await _identify_coupon(db, source, store.id, offer, content_hash)

    probe_ok: bool | None = None
    probe_reason: str | None = None
    if source.probe_destinations:
        probe_ok, probe_reason = await probe.probe_destination(offer.destination_url)

    already_expired = offer.expires_at is not None and offer.expires_at <= now
    is_new = coupon is None
    if is_new:
        coupon = Coupon(
            # The primary key is assigned here rather than left to the column
            # default. A Python-side `default=` is evaluated at INSERT time, so
            # without this `coupon.id` stays None until the first flush — and
            # flushing a half-built Coupon is what tripped the NOT NULL on
            # `discount_type`, because `_apply_offer`, which sets it, had not
            # run yet. Assigning the id up front lets the row be written once and
            # fully populated, by the single flush at the bottom of this function.
            id=uuid.uuid4(),
            title=offer.title,
            slug=await generate_unique_slug(db, Coupon, offer.title),
            store_id=store.id,
            category_id=category.id,
            destination_url=offer.destination_url,
            is_active=not already_expired,
            source_id=source.id,
        )
        db.add(coupon)
        run.created += 1
        is_own = True
    else:
        assert coupon is not None
        is_own = coupon.source_id == source.id
        run.updated += 1 if is_own else 0
        run.duplicated += 0 if is_own else 1

    if coupon is None:
        raise RuntimeError("coupon vanished between identification and apply")

    await _apply_offer(
        db,
        coupon=coupon,
        source=source,
        offer=offer,
        content_hash=content_hash,
        category=category,
        store=store,
        now=now,
        is_new=is_new,
    )
    await _upsert_coupon_source(
        db,
        coupon=coupon,
        source=source,
        offer=offer,
        now=now,
    )

    _apply_status(coupon, probe_ok=probe_ok, probe_reason=probe_reason, now=now)
    if already_expired:
        run.expired += 1
    if touched is not None:
        touched.append(coupon.id)

    await db.flush()


async def _resolve_merchant(db: AsyncSession, offer: NormalizedOffer, source: Source):
    """Resolve the merchant through the single funnel.

    `merchant_resolution` owns the matching order (domain, then aliases, then
    exact normalized name, with fuzzy deliberately off the ingestion path).
    Calling it from here and nowhere else is what guarantees that order is
    identical for every provider.
    """
    program_ids = (
        {source.kind.value: str(offer.extra["program_id"])}
        if offer.extra.get("program_id")
        else None
    )

    match = await merchant_resolution.resolve_or_create(
        db,
        offer,
        program_ids=program_ids,
        # Admitad offers carry Admitad tracking links, so their store needs Admitad's click
        # reference (`subid`) on the way out or the conversion cannot be tied back to a page.
        affiliate_network="admitad" if source.kind == SourceKind.admitad else None,
    )
    if match.is_new:
        logger.info(
            "merchant created",
            extra={"slug": match.store.slug, "source": source.slug},
        )
    return match


async def _apply_offer(
    db: AsyncSession,
    *,
    coupon: Coupon,
    source: Source,
    offer: NormalizedOffer,
    content_hash: str,
    category: Category,
    store: Any,
    now: datetime,
    is_new: bool = False,
) -> None:
    """Write the canonical row, having first re-resolved it against other sources.

    The order inside this function is the point. An incoming offer does not get
    to overwrite the canonical coupon by being the most recent writer — it is
    merged with what the other sources already said, by the rules in
    `conflicts`, and only then written.

    `is_new` skips the reconciliation entirely: a coupon created a moment ago has
    no other source's claim to reconcile against, and asking the database anyway
    costs one indexed SELECT per new coupon — which on a first full sync of a new
    network is one round trip per offer, all of it guaranteed to return nothing.
    """
    if is_new:
        expires_at, destination, overridden = offer.expires_at, offer.destination_url, []  # type: ignore[var-annotated]
    else:
        stored = await conflicts.load_stored_claims(
            db, coupon.id, only=(CouponSourceStatus.active, CouponSourceStatus.expired)
        )
        expires_at, destination, overridden = conflicts.resolve_from_stored(
            stored,
            incoming_expires_at=offer.expires_at,
            incoming_destination=offer.destination_url,
            incoming_source=source,
        )
    if overridden:
        logger.info(
            "destination chosen by source precedence",
            extra={
                "coupon_id": str(coupon.id),
                "winner": source.slug,
                "overridden": overridden,
            },
        )

    # Provider-claimed conditions we cannot derive: kept verbatim rather than
    # interpreted into our own fields. See normalize._extract_restrictions.
    restrictions = offer.restrictions or coupon.restrictions

    coupon.title = offer.title
    coupon.code = offer.code
    coupon.normalized_code = merchants.normalize_code(offer.code)
    coupon.description = offer.description
    coupon.discount_type = offer.discount_type
    coupon.discount_value = offer.discount_value
    coupon.store_id = store.id
    coupon.category_id = category.id
    coupon.destination_url = destination or offer.destination_url
    coupon.expires_at = expires_at
    coupon.starts_at = offer.starts_at
    coupon.content_hash = content_hash
    coupon.country = offer.country
    coupon.restrictions = restrictions or None
    coupon.source_categories = offer.source_categories or []
    coupon.source_id = coupon.source_id or source.id
    coupon.external_id = offer.external_id or coupon.external_id


async def recompute_totals(db: AsyncSession, coupon_ids: list) -> None:
    """Refresh `source_count` and the quality score for the coupons a run touched.

    Batch query rather than a recount per coupon: a full sync touches tens of
    thousands of coupons, and one correlated subquery each is the difference
    between a sync that takes seconds and one that takes the database down.
    """
    if not coupon_ids:
        return

    unique_ids = list({coupon_id for coupon_id in coupon_ids})
    rows = (
        await db.execute(
            select(CouponSource.coupon_id, func.count())
            .where(
                CouponSource.coupon_id.in_(unique_ids),
                CouponSource.status == CouponSourceStatus.active,
            )
            .group_by(CouponSource.coupon_id)
        )
    ).all()
    counts = {coupon_id: count for coupon_id, count in rows}

    coupons = list(
        (await db.execute(select(Coupon).where(Coupon.id.in_(unique_ids)))).scalars().all()
    )

    from app.services import quality

    for coupon in coupons:
        coupon.source_count = counts.get(coupon.id, 0)
        coupon.quality_score = quality.score_for_coupon(coupon)
