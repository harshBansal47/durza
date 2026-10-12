import pytest
from pydantic import ValidationError

from app.core.config import Settings

DB = "postgresql+asyncpg://u:p@localhost/db"

# Every production settings object needs these three; the defaults are all
# deliberately unsafe for production (placeholder secret, plain-http site URL).
PROD = {
    "environment": "production",
    "secret_key": "x" * 48,
    "database_url": DB,
    "site_url": "https://durza.in",
}


def test_production_rejects_placeholder_secret():
    with pytest.raises(ValidationError):
        Settings(**{**PROD, "secret_key": "change-me-to-a-random-secret"})


def test_production_accepts_real_secret():
    settings = Settings(**PROD)
    assert settings.environment == "production"


def test_development_allows_placeholder_secret():
    Settings(environment="development", secret_key="change-me", database_url=DB)


# ---- Production-only validation ----
#
# Each of these is a misconfiguration that boots fine locally and produces a
# failure nobody sees until an email lands in spam, or a push never arrives.
# Failing at startup is the only point where it is cheap to notice.


def test_production_requires_https_site_url():
    # Every alert email links back to the site, and the unsubscribe link travels
    # in the clear over http.
    with pytest.raises(ValidationError, match="SITE_URL"):
        Settings(**{**PROD, "site_url": "http://durza.in"})


def test_production_allows_a_trailing_slash_in_site_url():
    # Validators should not be stricter than the thing they check.
    Settings(**{**PROD, "site_url": "https://durza.in/"})


def test_production_rejects_smtp_user_without_a_password():
    # Auth with a blank password fails at send time, once per recipient, and
    # looks like a deliverability problem rather than a config error.
    with pytest.raises(ValidationError, match="SMTP_PASSWORD"):
        Settings(**PROD, smtp_host="smtp.example.com", smtp_user="mailer")


def test_production_accepts_smtp_user_with_a_password():
    Settings(**PROD, smtp_host="smtp.example.com", smtp_user="mailer", smtp_password="secret")


def test_production_rejects_a_half_configured_vapid_key_pair():
    # VAPID signing with a public key and no private key cannot work, and the
    # failure is a cryptic crypto error on the first push of the day.
    with pytest.raises(ValidationError, match="VAPID"):
        Settings(**PROD, vapid_public_key="pub")


def test_production_accepts_a_complete_vapid_key_pair():
    Settings(**PROD, vapid_public_key="pub", vapid_private_key="priv")


def test_production_needs_neither_smtp_nor_vapid_to_boot():
    # Optional channels must stay optional: a deployment that only does
    # on-demand coupons should not be forced to configure mail it never sends.
    settings = Settings(**PROD)
    assert settings.email_configured is False
    assert settings.push_configured is False


def test_delivery_reports_configuration_state():
    settings = Settings(
        **PROD, smtp_host="smtp.example.com", vapid_public_key="pub", vapid_private_key="priv"
    )
    assert settings.email_configured is True
    assert settings.push_configured is True


def test_scheduler_and_metrics_are_off_by_default():
    # Both start background work or open an endpoint; neither should happen
    # because somebody forgot a flag in a local .env.
    settings = Settings(environment="development", secret_key="change-me", database_url=DB)
    assert settings.scheduler_enabled is False
    assert settings.metrics_enabled is False
