"""Resolving provider credentials without ever storing or logging them.

Affiliate credentials are the one thing in this system that must never reach the
database, the admin panel, an API response, a log line, or an exception message.
They live in the process environment and nowhere else.

The indirection is a `credential_ref` on the Source row — the *name* of an
environment variable, not its value. That is what makes the `/admin` detail view
safe to render without a redaction pass: there is nothing in that row to leak.
The cost is one env lookup per fetch, which is not a cost.
"""

import os
from dataclasses import dataclass, field

from app.core.observability import get_logger

logger = get_logger("couponza.ingestion.credentials")


class MissingCredentialError(RuntimeError):
    """A source needs a credential and the environment does not have it.

    Deliberately carries no value, no variable name and no config — only the
    *kind* of credential. The message reaches an admin panel and a log file, and
    the temptation to include "AWIN_API_KEY=sk-live-abc" for debugging is exactly
    how a real key ends up in a Slack channel.
    """

    def __init__(self, provider: str, purpose: str) -> None:
        super().__init__(
            f"{provider} source requires a {purpose} credential, which is not set in the "
            f"environment. Configure it on the Source row's credential reference."
        )
        self.provider = provider
        self.purpose = purpose


@dataclass(frozen=True)
class SourceCredentials:
    """Resolved credentials for one source, held in memory only.

    `__repr__` is overridden because the default dataclass repr prints every
    field — and this object gets interpolated into exception messages and log
    context by mistake eventually. `as_dict` exists for the same reason: it is
    the single, auditable place a secret becomes a plain string.
    """

    values: dict[str, str] = field(default_factory=dict, repr=False)
    # Names of the variables that were resolved. Safe to log; the values are not.
    names: tuple[str, ...] = field(default_factory=tuple)

    def __repr__(self) -> str:
        return f"SourceCredentials(names={list(self.names)!r})"

    def __str__(self) -> str:  # pragma: no cover - defensive
        return repr(self)

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def only_value(self) -> str:
        """The single resolved secret.

        For providers that authenticate with one value (Awin's API key, Admitad's
        API key). Adapters call *this* rather than naming an environment variable,
        so renaming `AWIN_API_KEY` in a deployment's `credential_ref` does not
        require a code change — and, more importantly, so the variable name lives
        in exactly one place per source instead of being duplicated between the
        deployment config and the adapter.

        Raises `MissingCredentialError` when nothing resolved, because a source
        with no key is a configuration error that must be reported rather than
        fetched with an empty header.
        """
        if not self.values:
            raise MissingCredentialError("source", "publisher API key")
        if len(self.values) > 1:
            raise MissingCredentialError(
                "source",
                "exactly one credential (this provider authenticates with one value)",
            )
        return next(iter(self.values.values()))

    def require(self, key: str) -> str:
        value = self.values.get(key)
        if not value:
            raise MissingCredentialError("source", key)
        return value

    def redact(self) -> dict[str, str]:
        """Values with the secret replaced. The only form safe to log."""
        return {name: "***" for name in self.names}


def resolve(source_credential_ref: str | None, *, kind: str) -> SourceCredentials:
    """Look up the credentials a source refers to, by environment variable name.

    `kind` is the provider ("awin", "cj", ...) and is used only to build an
    error message. Nothing provider-specific is inferred here: which variables a
    network needs is the adapter's business, and a network that needs a token
    refresh flow should own that, not this function.

    Returns an empty `SourceCredentials` when nothing is referenced, so an
    adapter can ask for what it needs and get a clear error only if it really
    does need it.
    """
    if not source_credential_ref:
        return SourceCredentials()

    values: dict[str, str] = {}
    for name in _referenced_names(source_credential_ref):
        raw = os.environ.get(name)
        if raw and raw.strip():
            values[name] = raw.strip()

    if not values:
        # Logged at the names only. "AWIN_API_KEY is not set" is the message a
        # deployment needs; its value never is.
        logger.warning(
            "source credential reference resolved to nothing",
            extra={"kind": kind, "credential_ref": source_credential_ref},
        )

    return SourceCredentials(values=values, names=tuple(values))


def _referenced_names(reference: str) -> list[str]:
    """Variable names in a `credential_ref`.

    A single name ("AWIN_API_KEY") is the normal case. A comma-separated list
    ("AWIN_API_KEY,AWIN_PUBLISHER_ID") is supported because several networks
    need two values and a second row would mean a second Source, which is the
    wrong model: one network is one source with two credentials.
    """
    return [part.strip() for part in reference.split(",") if part.strip()]


def is_configured(source_credential_ref: str | None) -> bool:
    """Whether the referenced variables are present.

    Drives `Source.api_status = unconfigured`. Distinct from "failed": a source we
    have no key for was never attempted, so counting it as a failure would
    depress a health score for a problem that is really an unfinished deployment
    step.

    Only meaningful for kinds that need a credential at all — see
    `requires_credentials`.
    """
    if not source_credential_ref:
        return False
    return any(os.environ.get(name, "").strip() for name in _referenced_names(source_credential_ref))


#: Source kinds whose adapters authenticate to a remote API and therefore cannot
#: be fetched at all without a key. `static` and `csv` read local data; `feed` is
#: deliberately *not* here, because most JSON feeds are public and demanding a key
#: for them would fail the majority of sources for no reason. A `feed` that does
#: set `credential_ref` sends it as a bearer token, and health still requires it
#: to resolve — see `credentials_satisfied`.
CREDENTIAL_KINDS = frozenset({"awin", "rakuten", "cj", "admitad", "impact"})


def requires_credentials(kind: str) -> bool:
    """Whether this kind of source cannot be fetched without a credential."""
    return kind in CREDENTIAL_KINDS


def credentials_satisfied(kind: str, credential_ref: str | None) -> bool:
    """Whether this source has whatever it needs to be fetched.

    Two separate questions, deliberately not collapsed into one:

    * A `credential_ref` was set, so it must resolve. Otherwise a source whose key
      was rotated out of the deployment looks configured.
    * No `credential_ref` was set, so the kind decides — `awin` without a key is
      an unfinished deployment step, a public `feed` is finished.

    Getting this backwards is how a source ends up permanently `unconfigured`
    (asking a key-less `csv` whether it has a key) or permanently `healthy` (not
    asking a key-less `awin`).
    """
    if credential_ref:
        return is_configured(credential_ref)
    return not requires_credentials(kind)
