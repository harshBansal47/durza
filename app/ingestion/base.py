"""What an external source hands us, before normalization."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from app.ingestion.credentials import SourceCredentials
    from app.models.ingestion_run import SyncMode


@dataclass
class RawOffer:
    title: str
    store_slug: str | None = None
    store_name: str | None = None
    category_slug: str | None = None
    category_name: str | None = None
    code: str | None = None
    description: str | None = None
    discount_type: str | None = None  # "percentage" | "fixed" | "deal"
    discount_value: float | None = None
    discount_text: str | None = None  # e.g. "50% off", "₹200 off", "BOGO"
    destination_url: str = ""
    expires_at: datetime | str | None = None
    external_id: str | None = None
    store_website: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class PartialFetchError(Exception):
    """The adapter got some of the data and knows it did not get all of it.

    The distinction this exception exists to express is the one the whole
    ingestion layer is built around: **"this provider returned what it had" is
    not the same claim as "this provider listed everything it has".** Only the
    second justifies downgrading the rows a run did not mention.

    An adapter raises this after yielding everything it managed to retrieve — the
    records already yielded are kept. Raising rather than returning an empty list
    is deliberate: returning nothing looks identical to a provider whose catalogue
    genuinely emptied, and that is how one flaky API call deletes a catalogue.
    """

    def __init__(self, reason: str, *, pages_completed: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.pages_completed = pages_completed


class SourceAdapter(Protocol):
    """Turns a Source's config into raw offers. One method, async, no DB access.

    `fetch` receives the source's config and the `SyncMode` for this run, so an
    adapter that can narrow its query (only changed offers, only offers expiring
    soon) is able to. Ignoring `mode` is fine — most sources cannot narrow — but
    accepting it means adding a narrowable source later is a one-method change.

    `credentials` is the resolved secret material for this source, passed in
    rather than read by the adapter. The adapter never sees the *name* of an
    environment variable and never decides where a secret lives, which is what
    keeps the "credentials live in the environment and nowhere else" rule
    enforceable from one place instead of four. Adapters with no credentials —
    static, csv — ignore it.

    `stream` is an optional async iterator for sources large enough that
    buffering the whole payload is worth avoiding. The pipeline uses it when
    present and falls back to `fetch` when not.
    """

    async def fetch(
        self,
        config: dict[str, Any],
        *,
        mode: "SyncMode | None" = None,
        credentials: "SourceCredentials | None" = None,
    ) -> list[RawOffer]: ...
