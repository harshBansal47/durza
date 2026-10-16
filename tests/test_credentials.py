"""Credential resolution: fetch secrets from the environment, never store them.

The security property under test is as much "the value never appears in a log,
repr, or error" as "the right value is returned". Both are asserted here.
"""

import pytest

from app.ingestion import credentials
from app.ingestion.credentials import (
    MissingCredentialError,
    SourceCredentials,
    credentials_satisfied,
    is_configured,
    requires_credentials,
    resolve,
)

pytestmark = pytest.mark.asyncio


# ---- MissingCredentialError --------------------------------------------------


async def test_missing_credential_error_carries_provider_and_purpose():
    error = MissingCredentialError("awin", "publisher API key")

    assert error.provider == "awin"
    assert error.purpose == "publisher API key"
    assert "awin" in str(error)
    assert "publisher API key" in str(error)


# ---- SourceCredentials -------------------------------------------------------


async def test_repr_shows_names_but_never_values():
    creds = SourceCredentials(values={"AWIN_API_KEY": "sk-live-secret"}, names=("AWIN_API_KEY",))

    text = repr(creds)

    assert "AWIN_API_KEY" in text
    assert "sk-live-secret" not in text


async def test_get_returns_value_or_none():
    creds = SourceCredentials(values={"A": "1"}, names=("A",))

    assert creds.get("A") == "1"
    assert creds.get("MISSING") is None


async def test_only_value_returns_the_single_secret():
    creds = SourceCredentials(values={"A": "1"}, names=("A",))

    assert creds.only_value() == "1"


async def test_only_value_raises_when_nothing_resolved():
    with pytest.raises(MissingCredentialError):
        SourceCredentials().only_value()


async def test_only_value_raises_when_more_than_one_resolved():
    creds = SourceCredentials(values={"A": "1", "B": "2"}, names=("A", "B"))

    with pytest.raises(MissingCredentialError, match="exactly one"):
        creds.only_value()


async def test_require_returns_present_value():
    creds = SourceCredentials(values={"A": "1"}, names=("A",))

    assert creds.require("A") == "1"


async def test_require_raises_for_missing_and_for_empty():
    with pytest.raises(MissingCredentialError):
        SourceCredentials().require("A")
    with pytest.raises(MissingCredentialError):
        SourceCredentials(values={"A": ""}, names=("A",)).require("A")


async def test_redact_replaces_every_value_with_asterisks():
    creds = SourceCredentials(values={"A": "secret", "B": "hunter2"}, names=("A", "B"))

    assert creds.redact() == {"A": "***", "B": "***"}


# ---- resolve() ---------------------------------------------------------------


async def test_resolve_without_ref_returns_empty_credentials():
    assert resolve(None, kind="awin").values == {}
    assert resolve("", kind="awin").values == {}


async def test_resolve_reads_single_env_var(monkeypatch):
    monkeypatch.setenv("AWIN_API_KEY", "sk-123")

    creds = resolve("AWIN_API_KEY", kind="awin")

    assert creds.values == {"AWIN_API_KEY": "sk-123"}
    assert creds.names == ("AWIN_API_KEY",)


async def test_resolve_reads_comma_separated_vars(monkeypatch):
    monkeypatch.setenv("AWIN_API_KEY", "sk-123")
    monkeypatch.setenv("AWIN_PUBLISHER_ID", "555")

    creds = resolve("AWIN_API_KEY,AWIN_PUBLISHER_ID", kind="awin")

    assert creds.values == {"AWIN_API_KEY": "sk-123", "AWIN_PUBLISHER_ID": "555"}


async def test_resolve_strips_whitespace(monkeypatch):
    monkeypatch.setenv("AWIN_API_KEY", "  sk-123  \n")

    assert resolve("AWIN_API_KEY", kind="awin").values == {"AWIN_API_KEY": "sk-123"}


async def test_resolve_skips_unset_and_empty_vars(monkeypatch):
    monkeypatch.setenv("SET_VAR", "value")
    monkeypatch.setenv("EMPTY_VAR", "   ")
    monkeypatch.delenv("UNSET_VAR", raising=False)

    creds = resolve("SET_VAR,EMPTY_VAR,UNSET_VAR", kind="awin")

    assert creds.values == {"SET_VAR": "value"}


async def test_resolve_logs_a_warning_when_nothing_resolves(monkeypatch, caplog):
    monkeypatch.delenv("DEFINITELY_UNSET_VAR", raising=False)

    creds = resolve("DEFINITELY_UNSET_VAR", kind="awin")

    assert creds.values == {}
    assert any("resolved to nothing" in record.getMessage() for record in caplog.records)


# ---- _referenced_names -------------------------------------------------------


async def test_referenced_names_split_and_strip():
    names = credentials._referenced_names(" A , B ,C ")

    assert names == ["A", "B", "C"]


async def test_referenced_names_filter_empty_parts():
    assert credentials._referenced_names("A,,B,") == ["A", "B"]
    assert credentials._referenced_names("") == []
    assert credentials._referenced_names(",,,") == []


# ---- is_configured -----------------------------------------------------------


async def test_is_configured_false_without_ref():
    assert is_configured(None) is False
    assert is_configured("") is False


async def test_is_configured_true_when_var_is_set(monkeypatch):
    monkeypatch.setenv("MY_KEY", "x")

    assert is_configured("MY_KEY") is True


async def test_is_configured_false_when_unset_or_empty(monkeypatch):
    monkeypatch.delenv("NOPE_KEY", raising=False)
    monkeypatch.setenv("EMPTY_KEY", "  ")

    assert is_configured("NOPE_KEY") is False
    assert is_configured("EMPTY_KEY") is False


async def test_is_configured_true_when_any_referenced_var_is_set(monkeypatch):
    monkeypatch.delenv("MISSING_ONE", raising=False)
    monkeypatch.setenv("PRESENT_ONE", "x")

    assert is_configured("MISSING_ONE,PRESENT_ONE") is True


# ---- requires_credentials / credentials_satisfied ---------------------------


async def test_requires_credentials_for_network_kinds():
    for kind in ("awin", "rakuten", "cj", "admitad", "impact"):
        assert requires_credentials(kind) is True, kind


async def test_requires_credentials_false_for_local_and_public_feeds():
    for kind in ("static", "csv", "feed"):
        assert requires_credentials(kind) is False, kind


async def test_credentials_satisfied_with_resolving_ref(monkeypatch):
    monkeypatch.setenv("MY_KEY", "x")

    assert credentials_satisfied("awin", "MY_KEY") is True


async def test_credentials_satisfied_with_unresolvable_ref(monkeypatch):
    monkeypatch.delenv("GONE_KEY", raising=False)

    assert credentials_satisfied("awin", "GONE_KEY") is False


async def test_credentials_satisfied_without_ref_is_kind_dependent():
    # An awin source with no key is an unfinished deployment step...
    assert credentials_satisfied("awin", None) is False
    # ...while a local csv source is complete without one.
    assert credentials_satisfied("csv", None) is True
