"""Per-source health: is this provider's API answering, and how often?

One module, one job, deliberately not a general-purpose health framework. It
exists because the naive version of this — a boolean `is_enabled` that gets
unset when a fetch fails — is the design that takes a catalogue offline when one
API key expires.

## Why health is not the same as enabled

`Source.is_enabled` is a human's decision: this provider should not be used.
`Source.api_status` is a machine's observation: the last few calls to it did or
did not work. Conflating them means the first provider to have a bad afternoon
silently removes every coupon it contributed, and the site has no way to tell
"we turned CJ off in March" from "CJ stopped working in March".

So a provider going bad changes three things and only those three:

* its `sync_interval_minutes` effectively grows, because the scheduler skips
  degraded sources more often than healthy ones;
* it stops being counted as corroboration for a coupon's `source_count`;
* it disappears from the admin health panel's healthy list.

It does **not** remove a single coupon. A coupon backed by a broken provider is
still real until its expiry date or a checker says otherwise — which is the
opposite of what a cascade looks like from the outside, and is the entire point.

## Thresholds

`DEGRADED_AFTER` and `FAILING_AFTER` are failure counts, not rates. A rate would
need a time window and a sample size, and this is a reading that has to be
explainable to an operator looking at one row: "the last N runs failed" is a
sentence, "health 0.62" is not.

Both thresholds are also *not* used to stop syncing. Only `SourceStatus.paused`
— set by a person — stops the scheduler. `failing` is the loudest state a machine
can put a source in, and it deliberately still syncs, because a provider that has
been down for a week will still be down in an hour and waiting for a human to
notice costs a week of coupons.
"""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.observability import get_logger
from app.models.ingestion_run import RunStatus
from app.models.source import Source, SourceStatus

logger = get_logger("couponza.ingestion.health")

#: One failure is `degraded`. Low on purpose: a single failed run is worth
#: surfacing, and a source that is flaky enough to reach the next threshold will
#: get there on its own.
DEGRADED_AFTER = 1

#: Three consecutive failures is `failing`. Three because affiliate APIs fail
#: transiently constantly — a 502 on one page of forty is not a broken
#: integration, and marking a source failing on the first one would spend the
#: operator's attention on noise.
FAILING_AFTER = 3

#: Longest error kept on the row. Long enough to read a stack-ish message,
#: short enough that the column stays cheap to select on the admin list, which
#: renders every source row.
MAX_ERROR = 2000


def status_for(
    *, consecutive_failures: int, is_enabled: bool, credentials_present: bool
) -> SourceStatus:
    """The health reading implied by a run's outcome and the source's config.

    Pure, so the thresholds are testable without a database or a provider.

    Ordering matters: `paused` beats `unconfigured`, which beats everything
    computed from failures. A source an operator switched off is paused whether
    or not its key happens to be missing, and reporting it as unconfigured would
    invite someone to "fix" the credential rather than leave it off.
    """
    if not is_enabled:
        return SourceStatus.paused
    if not credentials_present:
        return SourceStatus.unconfigured
    if consecutive_failures >= FAILING_AFTER:
        return SourceStatus.failing
    if consecutive_failures >= DEGRADED_AFTER:
        return SourceStatus.degraded
    return SourceStatus.healthy


def effective_interval_minutes(source: Source) -> int:
    """How long to wait before syncing this source again.

    Derived from health rather than stored, so it cannot drift from the status
    it is derived from. A degraded provider is polled three times less often
    than a healthy one: not to abandon it, but so that four healthy providers
    are not queueing behind one that is timing out.
    """
    base = max(1, source.sync_interval_minutes)
    if source.api_status == SourceStatus.failing:
        return base * 6
    if source.api_status == SourceStatus.degraded:
        return base * 3
    return base


async def record_outcome(
    db: AsyncSession, source: Source, outcome: RunStatus, *, count: bool = True
) -> SourceStatus:
    """Update one source's health from how its run ended. Returns the new status.

    Called on every terminal path, including the failure and cancellation paths —
    a source whose runs always crash and are never recorded looks exactly like a
    source nobody is syncing, and those are opposite problems.

    `count=False` leaves the health reading alone and only stamps `last_sync_at`.
    It is for runs that ended without ever talking to the provider: a disabled
    source, a credential that was never set, an adapter kind nobody registered.
    None of those are evidence about the provider, and charging them to the
    counters would mean a source left switched off all week comes back
    `failing` on its first real run.
    """
    from app.ingestion.credentials import credentials_satisfied

    now = datetime.now(UTC)
    succeeded = outcome == RunStatus.success

    # `static`, `csv` and key-less `feed` sources read data that needs no
    # authentication, so asking whether one has a credential would report every
    # perfectly healthy local source as `unconfigured`.
    credentials_present = credentials_satisfied(
        source.kind.value, source.credential_ref
    )

    if not count:
        source.last_sync_at = now
        source.api_status = status_for(
            consecutive_failures=source.consecutive_failures,
            is_enabled=source.is_enabled,
            credentials_present=credentials_present,
        )
        return source.api_status

    if succeeded:
        source.consecutive_failures = 0
        source.last_success_at = now
        source.last_error = None
    else:
        # `cancelled` counts against the source only for `partial` and `failed`.
        # A run an operator stopped by hand says something about the operator,
        # not about the provider's API, and charging it to the health score
        # would let a well-meaning "stop that feed, it's noisy" action degrade a
        # working integration.
        if outcome in (RunStatus.partial, RunStatus.failed):
            source.consecutive_failures += 1
            source.last_failure_at = now

    source.last_sync_at = now
    previous = source.api_status
    source.api_status = status_for(
        consecutive_failures=source.consecutive_failures,
        is_enabled=source.is_enabled,
        credentials_present=credentials_present,
    )

    if source.api_status != previous:
        logger.info(
            "source health changed",
            extra={
                "source": source.slug,
                "was": previous.value,
                "now": source.api_status.value,
                "consecutive_failures": source.consecutive_failures,
            },
        )
    return source.api_status


async def mark_error(db: AsyncSession, source: Source, error: str) -> None:
    """Record the failure text alongside the counters. Caller commits."""
    source.last_error = error[:MAX_ERROR]
    source.last_failure_at = datetime.now(UTC)
