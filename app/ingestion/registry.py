"""Which adapter serves which source kind.

Two separate failure modes are being told apart here, because they have opposite
answers:

* A kind nobody registered, because it was added to `SourceKind` and no adapter
  was written. That is a gap in this codebase.
* A kind that is deliberately not implemented yet — every network here is,
  until its real API has been read and its adapter written against that API
  rather than against an assumption.

Both are a `ValueError`, so `pipeline.run_ingestion` turns either into a run row
saying `failed` and records nothing against the source's health: an adapter that
does not exist is not evidence that a provider's API is unhealthy. The
difference is purely in what the message tells the person who hit it.

The reason this is a named set rather than an inferred one: `SourceKind` is an
enum, so `get_adapter` could in principle be asked for a kind that does not exist
at all. `tests/test_registry.py` asserts that every member of the enum is either
registered here or listed in `PENDING_KINDS`, which is what stops a new network
from being added to the enum and quietly failing forever at three in the morning.
"""

from app.ingestion.base import SourceAdapter
from app.ingestion.sources.awin import AwinSource
from app.ingestion.sources.csv_source import CsvSource
from app.ingestion.sources.feed import FeedSource
from app.ingestion.sources.static import StaticSource
from app.models.source import SourceKind

_ADAPTERS: dict[SourceKind, type] = {
    SourceKind.csv: CsvSource,
    SourceKind.static: StaticSource,
    SourceKind.feed: FeedSource,
    SourceKind.awin: AwinSource,
}

#: Kinds we know about and have deliberately not written. Each needs its
#: provider's real API read first — endpoints, authentication, pagination, the
#: shape of a coupon — rather than an adapter written from a guess, because a
#: plausible-looking adapter against an invented endpoint fails at runtime in
#: exactly the way that looks like a provider outage.
PENDING_KINDS: frozenset[SourceKind] = frozenset(
    {SourceKind.rakuten, SourceKind.cj, SourceKind.admitad, SourceKind.impact}
)


def get_adapter(kind: SourceKind) -> SourceAdapter:
    adapter = _ADAPTERS.get(kind)
    if adapter is not None:
        return adapter()
    if kind in PENDING_KINDS:
        raise ValueError(
            f"Source kind {kind.value!r} is recognised but has no adapter yet. "
            f"Its {kind.value} API adapter is still to be written against that "
            f"provider's documented interface; no row of this kind will ingest "
            f"until it is."
        )
    raise ValueError(f"No adapter registered for source kind {kind!r}")


__all__ = ["PENDING_KINDS", "get_adapter"]
