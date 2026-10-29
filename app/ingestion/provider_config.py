"""Which ingestion providers this deployment uses.

This file is the single place to turn a provider on or off. On every deploy,
`python -m app.tools.sync_sources` makes the `sources` and `affiliate_providers`
tables match it, so nobody has to create those rows by hand.

What lives where:

* **Here (committed):** that a provider exists, whether it is enabled, and its
  non-secret settings.
* **Environment (the VM's `.env`):** every secret, and per-environment values such
  as an Admitad website id. `credential_ref` is the *name* of the variable(s), never
  the value, and `env_config` maps a config key to the variable that supplies it.
* **Database:** the rows themselves, plus the state a provider accumulates at
  runtime (health, last sync, priority tuned in /admin).

To add a provider whose kind already has an adapter, append a `ProviderSpec`.
A brand-new kind additionally needs a `SourceKind` value (with an Alembic
migration), an adapter, a line in `registry.py` and an entry in
`credentials.CREDENTIAL_KINDS`.
"""

from dataclasses import dataclass, field

from app.models.affiliate_provider import ProviderType
from app.models.source import SourceKind


@dataclass(frozen=True)
class AffiliateSpec:
    """The `AffiliateProvider` row that revenue is attributed to."""

    provider_type: ProviderType = ProviderType.cpa
    subid_supported: bool = True
    deeplink_supported: bool = True


@dataclass(frozen=True)
class ProviderSpec:
    slug: str
    name: str
    kind: SourceKind
    enabled: bool
    #: Name(s) of the environment variable(s) holding the credentials,
    #: comma-separated. Never the values.
    credential_ref: str
    countries: tuple[str, ...] = ("IN",)
    #: Static, non-secret adapter config.
    config: dict[str, object] = field(default_factory=dict)
    #: Adapter config that comes from the environment: {config key: variable name}.
    #: A variable that is unset or blank means the provider cannot be set up yet.
    env_config: dict[str, str] = field(default_factory=dict)
    description: str | None = None
    #: Set for networks that pay commission, so conversions can be attributed.
    affiliate: AffiliateSpec | None = None


PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec(
        slug="admitad",
        name="Admitad",
        kind=SourceKind.admitad,
        enabled=True,
        credential_ref="ADMITAD_CLIENT_ID,ADMITAD_CLIENT_SECRET",
        config={"page_size": 100},
        env_config={"website_id": "ADMITAD_WEBSITE_ID"},
        description="Admitad publisher coupons for this website.",
        affiliate=AffiliateSpec(),
    ),
)
