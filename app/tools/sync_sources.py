"""Make the database match `app/ingestion/provider_config.py`.

    podman-compose run --rm --no-deps -T app python -m app.tools.sync_sources [--dry-run]

Run manually after changing `app/ingestion/provider_config.py`. It is safe to
re-run. It creates a missing `Source` (and `AffiliateProvider`, where the spec
has one), and on an existing row updates only the fields the config owns:

* `Source`: kind, is_enabled, credential_ref, and the keys of `config` that the
  spec supplies.
* `AffiliateProvider`: enabled, credentials_reference, subid/deeplink support.

It never touches runtime state (`api_status`, `consecutive_failures`,
`last_sync_at`, `last_error`) or operator-tuned values (`priority`,
`confidence`, `countries`, `sync_interval_minutes`). A provider removed from the
config is left alone; set `enabled=False` to switch it off.

A provider whose required environment variable is missing is skipped with a
warning and the command still exits 0, so a missing key never blocks a deploy of
unrelated code. Any other failure raises and exits non-zero.

No secret is read or written: rows hold the *names* of variables only.
"""

import argparse
import asyncio
import os

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.ingestion.credentials import credentials_satisfied
from app.ingestion.provider_config import PROVIDERS, ProviderSpec
from app.models.affiliate_provider import AffiliateProvider, ProviderStatus
from app.models.source import Source


def _resolve_config(spec: ProviderSpec) -> tuple[dict[str, object], list[str]]:
    """The spec's adapter config with env-supplied values filled in.

    Returns the config and the names of any required variables that are unset.
    """
    config: dict[str, object] = dict(spec.config)
    missing: list[str] = []
    for key, env_name in spec.env_config.items():
        value = os.environ.get(env_name, "").strip()
        if value:
            config[key] = value
        else:
            missing.append(env_name)
    return config, missing


async def _sync_source(db: AsyncSession, spec: ProviderSpec, config: dict[str, object]) -> str:
    source = await db.scalar(select(Source).where(Source.slug == spec.slug))
    if source is None:
        db.add(
            Source(
                name=spec.name,
                slug=spec.slug,
                kind=spec.kind,
                is_enabled=spec.enabled,
                credential_ref=spec.credential_ref,
                countries=list(spec.countries),
                config=config,
                description=spec.description,
            )
        )
        return f"created source '{spec.slug}' (enabled={spec.enabled})"
    source.kind = spec.kind
    source.is_enabled = spec.enabled
    source.credential_ref = spec.credential_ref
    source.config = {**(source.config or {}), **config}
    return f"updated source '{spec.slug}' (enabled={spec.enabled})"


async def _sync_affiliate(db: AsyncSession, spec: ProviderSpec, has_credentials: bool) -> str:
    affiliate = spec.affiliate
    if affiliate is None:
        return ""
    provider = await db.scalar(select(AffiliateProvider).where(AffiliateProvider.slug == spec.slug))
    if provider is None:
        db.add(
            AffiliateProvider(
                name=spec.name,
                slug=spec.slug,
                provider_type=affiliate.provider_type,
                enabled=spec.enabled,
                countries=list(spec.countries),
                api_status=ProviderStatus.active
                if has_credentials
                else ProviderStatus.unconfigured,
                credentials_reference=spec.credential_ref,
                subid_supported=affiliate.subid_supported,
                deeplink_supported=affiliate.deeplink_supported,
            )
        )
        return f"created affiliate provider '{spec.slug}'"
    provider.enabled = spec.enabled
    provider.credentials_reference = spec.credential_ref
    provider.subid_supported = affiliate.subid_supported
    provider.deeplink_supported = affiliate.deeplink_supported
    return f"updated affiliate provider '{spec.slug}'"


async def sync(*, dry_run: bool) -> list[str]:
    notes: list[str] = []
    async with AsyncSessionLocal() as db:
        for spec in PROVIDERS:
            config, missing = _resolve_config(spec)
            if missing:
                notes.append(
                    f"WARNING {spec.slug}: skipped, required env not set: {', '.join(missing)}"
                )
                continue

            has_credentials = credentials_satisfied(spec.kind.value, spec.credential_ref)
            if spec.enabled and not has_credentials:
                notes.append(
                    f"WARNING {spec.slug}: enabled but credentials are not set in the "
                    f"environment ({spec.credential_ref}); it will report as unconfigured"
                )

            notes.append(await _sync_source(db, spec, config))
            affiliate_note = await _sync_affiliate(db, spec, has_credentials)
            if affiliate_note:
                notes.append(affiliate_note)

        if dry_run:
            await db.rollback()
            notes.append("dry run: nothing was written")
        else:
            await db.commit()
    return notes


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    for line in asyncio.run(sync(dry_run=args.dry_run)):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
