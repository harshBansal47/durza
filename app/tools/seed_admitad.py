"""Create (or update) the two rows Admitad needs, from the command line.

    podman-compose -f docker-compose.prod.yml exec app \
        python -m app.tools.seed_admitad --website-id 3015056

Admitad needs an `AffiliateProvider` (so revenue is attributed to it) and a
`Source` (so coupons are ingested). Both are plain database rows, easy to get
subtly wrong by hand: the provider slug must be exactly `admitad`, and the source
needs both credential names and the website id in its config. This sets them up
the same way every time and is safe to re-run.

It lives in the `app` package (run with `python -m`) because `app/` is what the
container has: the image does not copy `scripts/`.

No secret is read or written. Credentials stay in the environment; the rows only
hold the *names* of the variables.
"""

import argparse
import asyncio

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import AsyncSessionLocal
from app.models.affiliate_provider import AffiliateProvider, ProviderStatus, ProviderType
from app.models.source import Source, SourceKind

CREDENTIAL_REF = "ADMITAD_CLIENT_ID,ADMITAD_CLIENT_SECRET"


async def seed(website_id: str, *, countries: list[str], dry_run: bool) -> list[str]:
    settings = get_settings()
    has_credentials = bool(settings.admitad_client_id and settings.admitad_client_secret)
    notes: list[str] = []

    async with AsyncSessionLocal() as db:
        provider = await db.scalar(
            select(AffiliateProvider).where(AffiliateProvider.slug == "admitad")
        )
        if provider is None:
            db.add(
                AffiliateProvider(
                    name="Admitad",
                    slug="admitad",
                    provider_type=ProviderType.cpa,
                    enabled=True,
                    countries=countries,
                    api_status=ProviderStatus.active
                    if has_credentials
                    else ProviderStatus.unconfigured,
                    credentials_reference=CREDENTIAL_REF,
                    subid_supported=True,
                    deeplink_supported=True,
                )
            )
            notes.append("created affiliate provider 'admitad'")
        else:
            provider.credentials_reference = CREDENTIAL_REF
            provider.subid_supported = True
            provider.deeplink_supported = True
            notes.append("affiliate provider 'admitad' already exists: refreshed its wiring fields")

        source = await db.scalar(select(Source).where(Source.slug == "admitad"))
        if source is None:
            db.add(
                Source(
                    name="Admitad",
                    slug="admitad",
                    kind=SourceKind.admitad,
                    is_enabled=True,
                    credential_ref=CREDENTIAL_REF,
                    countries=countries,
                    config={"website_id": website_id, "page_size": 100},
                    description="Admitad publisher coupons for this website.",
                )
            )
            notes.append(f"created source 'admitad' for website {website_id}")
        else:
            source.credential_ref = CREDENTIAL_REF
            source.config = {**(source.config or {}), "website_id": website_id}
            notes.append(f"source 'admitad' already exists: set website_id={website_id}")

        if dry_run:
            await db.rollback()
            notes.append("dry run: nothing was written")
        else:
            await db.commit()

    if not has_credentials:
        notes.append("ADMITAD_CLIENT_ID / ADMITAD_CLIENT_SECRET are not set in this environment")
    return notes


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--website-id", required=True, help="Admitad ad space id (GET /websites/ shows it)"
    )
    parser.add_argument(
        "--country", action="append", default=None, help="repeat for several; default IN"
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    for line in asyncio.run(
        seed(args.website_id, countries=args.country or ["IN"], dry_run=args.dry_run)
    ):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
