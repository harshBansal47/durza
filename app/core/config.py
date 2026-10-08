from functools import lru_cache

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    environment: str = "development"
    secret_key: str = "change-me"
    database_url: str
    # None means "no Redis", which is not the same as "Redis at localhost".
    # `redis://localhost:6379/0` as a default means a bare `uvicorn` on a machine
    # without Redis fails at the first rate-limited request, so the default has
    # to be the thing that works everywhere. Set it explicitly when running more
    # than one replica — see `app/core/limiter.py`.
    redis_url: str | None = None
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7
    algorithm: str = "HS256"
    # Comma-separated list of origins the frontend is served from, e.g.
    # "http://localhost:3000,https://BachatBase.example.com". Without this,
    # browsers block every client-side fetch from the Next.js app to this API
    # (server-side fetches in Next.js Server Components aren't affected —
    # only same-origin-policy-enforcing browser requests are).
    cors_origins: str = "http://localhost:3000"

    # ---- Public site ----------------------------------------------------
    # Absolute base for canonical URLs, sitemaps and email links. Emails that
    # link to a relative path are useless in an inbox, so this must be set in
    # production.
    site_url: str = "http://localhost:3000"

    # ---- Email (aiosmtplib) ---------------------------------------------
    # Unset SMTP_HOST means "log and report queued" — the alert engine keeps
    # working locally with no mail server, and nothing silently disappears.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: str | None = None
    # "starttls" for port 587, "ssl" for implicit TLS on 465, "none" for a
    # local relay on 25 (MailHog, python -m smtpd) with no encryption.
    smtp_security: str = "starttls"
    email_from: str = "alerts@BachatBase.example"
    email_from_name: str = "BachatBase"

    # ---- Web push (pywebpush) -------------------------------------------
    # Generate with: python -m app.core.push_keys  (prints a VAPID key pair).
    vapid_private_key: str | None = None
    vapid_public_key: str | None = None
    vapid_subject: str = "mailto:alerts@BachatBase.example"
    # Push payloads over ~4KB are rejected by browsers, so keep bodies small.
    push_ttl_seconds: int = 3600

    # ---- Account security ----------------------------------------------
    # How long a password reset token is valid. Short by default because a
    # leaked token in a sent-mail folder should not be usable days later.
    reset_token_ttl_hours: int = 2

    # ---- Telegram ------------------------------------------------------
    # Optional fourth channel. Off unless a bot token is supplied; the chat id
    # itself is stored per-user in notification_preferences.
    telegram_bot_token: str | None = None

    # ---- Scheduler ------------------------------------------------------
    # Off by default so tests and one-off containers never start background
    # jobs. Enable explicitly in the deployed service.
    scheduler_enabled: bool = False
    # Minutes between alert scans. Alerts are cheap; ingestion is not.
    alert_scan_interval_minutes: int = 30
    # Hours between ingestion sweeps. Each run re-fetches every enabled source.
    ingestion_interval_hours: int = 6
    # Hours between expiry sweeps — just a status flip, so this can be frequent.
    expire_interval_hours: int = 1
    # How many times a failed scheduled job is retried before it is parked in
    # the dead-letter table. Retries use exponential backoff with jitter.
    scheduler_max_retries: int = 3
    # How often to re-check live coupon codes. Infrequent by default: these
    # checks hit merchant sites, and hammering them gets us blocklisted.
    verification_interval_hours: int = 12
    # Click events keep their visitor hash this long, then it is nulled (the row
    # stays, so revenue can still be attributed). Matches the privacy page.
    click_retention_days: int = 90
    click_scrub_interval_hours: int = 24
    # Only one worker may run a given job at a time. Without this, every
    # uvicorn replica would duplicate the whole ingestion sweep.
    scheduler_lock_ttl_seconds: int = 900

    # ---- Affiliate network credentials & sync --------------------------------
    # Cuelinks: deeplink wrapper base + our publisher id for outbound links.
    cuelinks_publisher_id: str | None = None
    cuelinks_tracker_base: str = "https://go.cuelinks.com/ald"
    cuelinks_api_key: str | None = None
    # Awin transaction report credentials.
    awin_api_key: str | None = None
    awin_publisher_id: str | None = None
    # How often the conversion sync job runs, and how far back each sweep
    # looks. Networks validate sales over a window of days-to-weeks, so the
    # lookback must be wider than any single validation window to catch
    # pending -> approved/declined transitions on late reports.
    conversion_sync_interval_hours: int = 6
    conversion_sync_lookback_days: int = 45

    # ---- Observability --------------------------------------------------
    sentry_dsn: str | None = None
    sentry_traces_sample_rate: float = 0.1
    log_format: str = "text"  # "text" locally, "json" anywhere it gets shipped
    log_level: str = "INFO"
    # Expose /metrics. Left off locally so a stray scrape isn't talking to a
    # developer's laptop.
    metrics_enabled: bool = False

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def email_configured(self) -> bool:
        return bool(self.smtp_host)

    @property
    def push_configured(self) -> bool:
        return bool(self.vapid_private_key and self.vapid_public_key)

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @field_validator("redis_url", mode="before")
    @classmethod
    def _blank_redis_url_is_disabled(cls, value: object) -> object:
        """`REDIS_URL=` means off, not "connect to the empty host".

        An empty string left as-is is a parse error inside the limits library
        rather than a fallback to in-memory storage, which is the opposite of
        what someone writing an empty value intends.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _reject_placeholder_secret_in_production(self) -> "Settings":
        # JWTs and the admin session cookie are both signed with this key.
        if self.environment == "production" and self.secret_key.startswith("change-me"):
            raise ValueError("SECRET_KEY must be set to a real random value in production")
        return self

    @model_validator(mode="after")
    def _require_https_site_url_in_production(self) -> "Settings":
        """Every alert email links back to the site.

        An `http://` link in an outbound message is one of the strongest negative
        signals a bulk sender can produce, and it also means the unsubscribe link
        — the one thing that keeps a marketing complaint from becoming a support
        ticket — travels in the clear. Failing at boot is much cheaper than
        discovering it in a deliverability report.
        """
        if self.is_production and not self.site_url.startswith("https://"):
            raise ValueError("SITE_URL must start with https:// in production")
        return self

    @model_validator(mode="after")
    def _reject_half_configured_delivery(self) -> "Settings":
        """A partially configured channel fails at send time, not boot time.

        Catching it here means a missing password surfaces on deploy rather
        than as an alert that never arrives and nobody notices.
        """
        if self.smtp_user and not self.smtp_password:
            raise ValueError("SMTP_USER is set but SMTP_PASSWORD is not")
        if bool(self.vapid_private_key) != bool(self.vapid_public_key):
            raise ValueError("VAPID_PRIVATE_KEY and VAPID_PUBLIC_KEY must both be set")
        return self


@lru_cache
def get_settings() -> Settings:
    # mypy can't see that required fields are populated from the environment
    # at runtime, not from this call site — known pydantic-settings false positive.
    return Settings()  # type: ignore[call-arg]
