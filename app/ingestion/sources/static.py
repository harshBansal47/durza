from typing import TYPE_CHECKING, Any

from app.ingestion.base import RawOffer
from app.ingestion.normalize import offer_from_dict

if TYPE_CHECKING:
    from app.ingestion.credentials import SourceCredentials
    from app.models.ingestion_run import SyncMode


class StaticSource:
    """Offers embedded directly in the Source's config — tests, seed data, demos."""

    async def fetch(
        self,
        config: dict[str, Any],
        *,
        mode: "SyncMode | None" = None,
        credentials: "SourceCredentials | None" = None,
    ) -> list[RawOffer]:
        # `mode` is ignored: the whole list is in `config`, so an incremental run
        # has nothing to narrow. Accepting the argument is what lets a source be
        # narrowed later without changing this class's signature.
        #
        # `credentials` is ignored because there is nothing to authenticate to —
        # which is also why `static` and `csv` are the two kinds exempt from
        # requiring a `credential_ref`.
        return [offer_from_dict(entry) for entry in config.get("offers", [])]
