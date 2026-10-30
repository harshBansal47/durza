from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Schemes this app treats as Postgres. All of them are rewritten to the one
# spelling the async driver understands; anything else (sqlite in tests) must
# round-trip untouched, because urlsplit/urlunsplit are lossy for schemes
# outside the standard set.
_ASYNC_PG_SCHEMES = {"postgres", "postgresql", "postgresql+asyncpg"}


def _as_asyncpg_url(url: str) -> str:
    """Rewrite a libpq-style Postgres URL into one asyncpg can connect with.

    SQLAlchemy forwards every URL query option to `asyncpg.connect()` as a
    keyword argument, and asyncpg only knows its own option names. Neon's
    connection string opts into libpq spellings that therefore abort the
    connection inside the driver, before a packet leaves the process:

        TypeError: connect() got an unexpected keyword argument 'sslmode'

    `sslmode` is renamed to asyncpg's `ssl`, which parses the same values
    (`require`, `verify-full`, ...). `channel_binding` has no asyncpg
    equivalent and is dropped — asyncpg does plain SCRAM over TLS, which Neon
    accepts.
    """
    parts = urlsplit(url)
    if parts.scheme not in _ASYNC_PG_SCHEMES:
        return url
    query = urlencode(
        [
            ("ssl" if key == "sslmode" else key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if key != "channel_binding"
        ]
    )
    return urlunsplit(("postgresql+asyncpg", parts.netloc, parts.path, query, parts.fragment))


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
    # Production: Neon Postgres (serverless; its connection string carries
    # libpq-only options — see `_as_asyncpg_url`) and Upstash Redis (TLS,
    # rediss://). When ENVIRONMENT=production these override
    # DATABASE_URL/REDIS_URL.
    neon_database_url: str | None = None
    upstash_redis_url: str | None = None
    access_token_expire_minutes: int = 30
    refresh_token_expire_days: int = 7
    algorithm: str = "HS256"
    # Comma-separated list of origins the frontend is served from, e.g.
    # "http://localhost:3000,https://durza.in". Without this,
    # browsers block every client-side fetch from the Next.js app to this API
    # (server-side fetches in Next.js Server Components aren't affected —
    # only same-origin-policy-enforcing browser requests are).
    cors_origins: str = "http://localhost:3000"

    # ---- Public site ----------------------------------------------------
    # Absolute base for canonical URLs, sitemaps and email links. Emails that
    # link to a relative path are useless in an inbox, so this must be set in
    # production.
    site_url: str = "http://localhost:3000"
    google_client_id: str | None = None

    # ---- Email (aiosmtplib) ---------------------------------------------
    # Only SMTP is implemented. The field exists so an unsupported provider in
    # the environment fails loudly instead of silently falling back to a
    # delivery path that will never send anything.
    email_provider: str = "smtp"
    # Unset SMTP_HOST means "log and report queued" — the alert engine keeps
    # working locally with no mail server, and nothing silently disappears.
    smtp_host: str | None = None
    smtp_port: int = 587
    # EMAIL_PROVIDER=smtp deployments commonly use SMTP_USERNAME; the older
    # SMTP_USER spelling is kept so existing local environments keep working.
    smtp_user: str | None = Field(
        default=None,
        validation_alias=AliasChoices("SMTP_USERNAME", "SMTP_USER", "smtp_user"),
    )
    smtp_password: str | None = None
    # "starttls" for port 587, "ssl" for implicit TLS on 465, "none" for a
    # local relay on 25 (MailHog, python -m smtpd) with no encryption.
    smtp_security: str = "starttls"
    email_from: str = "alerts@durza.in"
    email_from_name: str = "Durza Coupons"
    # Replies to alert and sign-in mail should reach a monitored mailbox rather
    # than the noreply-style sender address.
    email_reply_to: str | None = None

    # ---- Web push (pywebpush) -------------------------------------------
    # Generate with: python -m app.core.push_keys  (prints a VAPID key pair).
    vapid_private_key: str | None = None
    vapid_public_key: str | None = None
    vapid_subject: str = "mailto:alerts@durza.in"
    # Push payloads over ~4KB are rejected by browsers, so keep bodies small.
    push_ttl_seconds: int = 3600

    # ---- Passwordless email sign-in (OTP) -------------------------------
    # Six digits, valid for a single short window. The digest key is separate
    # from the JWT secret when set; falling back to SECRET_KEY keeps existing
    # deployments working, but an explicit OTP_HASH_KEY means rotating one
    # secret does not invalidate in-flight sign-in codes in the other.
    otp_ttl_minutes: int = 5
    otp_max_attempts: int = 5
    otp_resend_cooldown_seconds: int = 60
    otp_requests_per_email_per_hour: int = 5
    otp_hash_key: str | None = None

    # ---- Telegram ------------------------------------------------------
    # Optional fourth channel. Off unless a bot token is supplied; the chat id
    # itself is stored per-user in notification_preferences.
    telegram_bot_token: str | None = None

    # ---- Scheduler ------------------------------------------------------
    # Off by default so tests and one-off containers never start background
    # jobs. Enable explicitly in the deployed service.
    scheduler_enabled: bool = False
    # Which jobs run when the scheduler is on, as a comma-separated list of job
    # names (see `scheduler.JOBS`). Empty means all of them. `SCHEDULER_ENABLED`
    # alone starts every job, including ingestion and verification, which reach
    # out to third parties; this lets a deployment turn on just the cheap ones,
    # e.g. SCHEDULER_JOBS=send_deal_alerts,send_alerts,expire_coupons
    scheduler_jobs: str = ""
    # Minutes between alert scans. Alerts are cheap; ingestion is not.
    alert_scan_interval_minutes: int = 30
    # ---- Deal alerts (followed stores, tracked offers) -------------------
    # Only offers that appeared / ended this recently are worth an email. This
    # also stops a first scan after an outage, or after the scheduler is first
    # enabled, from mailing people about things that happened weeks ago.
    deal_alert_window_days: int = 7
    # "Ending soon" fires when a tracked offer expires within this many hours.
    deal_alert_ending_soon_hours: int = 48
    # At most one deal-alert email per person in this many hours. Everything
    # pending is batched into that one message.
    deal_alert_min_hours_between_emails: int = 24
    # Offers listed in a single digest; the rest are summarised as a count.
    deal_alert_max_items: int = 6
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
    # A community verification keeps the hash that rate-limits it this long.
    verification_retention_days: int = 90
    verification_scrub_interval_hours: int = 24
    # A dedicated salt for IP hashes. Empty means "use secret_key", so the
    # default behaviour is unchanged and no value has to be invented. Set it in
    # the environment, never in this repository.
    ip_hash_salt: str | None = None
    # The analytics consent cookie. Signed with the secret key, HttpOnly, and
    # scoped to the parent domain so the API subdomain receives it.
    analytics_consent_cookie: str = "cz_consent"
    analytics_consent_max_age_days: int = 365
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
    # Admitad Publisher API OAuth credentials used by conversion sync.
    admitad_client_id: str | None = None
    admitad_client_secret: str | None = None
    # How often the conversion sync job runs, and how far back each sweep
    # looks. Networks validate sales over a window of days-to-weeks, so the
    # lookback must be wider than any single validation window to catch
    # pending -> approved/declined transitions on late reports.
    conversion_sync_interval_hours: int = 6
    conversion_sync_lookback_days: int = 45

    # ---- Ingestion quarantine -------------------------------------------
    ingestion_quarantine_mode: str = "shadow"
    ingestion_quarantine_discount_pct: int = 90

    # ---- Personalization ------------------------------------------------
    personalization_segment_version: int = 1
    personalization_enabled: bool = False
    personalization_purpose_required: bool = True
    personalization_half_life_days: int = 14
    personalization_ttl_days: int = 90
    personalization_allowed_categories: str = ""
    personalization_blocked_categories: str = (
        "health,medical,loans,debt,insurance,adult,gambling,alcohol,weapons"
    )
    personalization_max_signals: int = 50

    # ---- Ranking --------------------------------------------------------
    # ---- Ranking exploration --------------------------------------------
    exploration_enabled: bool = False
    exploration_share: float = 0.1667
    exploration_max_share: float = 0.25
    exploration_min_impressions: int = 100
    exploration_trust_success_threshold: float = 0.5
    exploration_under_sampled_clicks: int = 100
    exploration_under_sampled_reports: int = 10
    exploration_under_sampled_age_days: int = 30
    exploration_seed_day_boundary: str = "UTC"

    ranking_version: str = "v1"
    ranking_weight_trust_floor: float = 0.7

    # ---- Observability --------------------------------------------------
    sentry_dsn: str | None = None
    sentry_traces_sample_rate: float = 0.1
    log_format: str = "text"  # "text" locally, "json" anywhere it gets shipped
    log_level: str = "INFO"
    # Stored application log (app/core/log_store.py). Off until switched on, so a
    # deploy never starts writing rows nobody asked for.
    log_store_enabled: bool = False
    # Floor for the app's own loggers. Third-party loggers are stored from WARNING.
    log_store_min_level: str = "INFO"
    # Rows older than this are deleted by the `scrub_logs` job.
    log_retention_days: int = 14
    log_scrub_interval_hours: int = 6
    # A request slower than this is logged at WARNING, which also stores it.
    slow_request_seconds: float = 2.0
    # Expose /metrics. Left off locally so a stray scrape isn't talking to a
    # developer's laptop.
    metrics_enabled: bool = False

    @property
    def cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.cors_origins.split(",") if origin.strip()]

    @property
    def scheduler_job_names(self) -> set[str] | None:
        """Allowed job names, or None for "all". Whitespace and case are forgiven."""
        names = {part.strip().lower() for part in self.scheduler_jobs.split(",") if part.strip()}
        return names or None

    @property
    def email_configured(self) -> bool:
        return self.email_provider == "smtp" and bool(self.smtp_host)

    @property
    def otp_hash_secret(self) -> str:
        """The key used for HMAC digests of sign-in codes.

        A dedicated key is preferable; the JWT secret is a safe fallback
        because it is already required to be strong in production.
        """
        return self.otp_hash_key or self.secret_key

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

    @field_validator("smtp_user", "smtp_password", "email_reply_to", "otp_hash_key", mode="before")
    @classmethod
    def _blank_optional_value_is_unset(cls, value: object) -> object:
        """An empty env var means "not configured", not an empty credential.

        Passing `username=""` to aiosmtplib takes the login branch and fails at
        send time, which looks like a mail outage rather than the missing value
        it actually is.
        """
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("email_provider", mode="before")
    @classmethod
    def _default_blank_email_provider(cls, value: object) -> object:
        if isinstance(value, str):
            value = value.strip().lower()
            return value or "smtp"
        return value

    @field_validator("email_provider")
    @classmethod
    def _only_smtp_is_supported(cls, value: str) -> str:
        if value != "smtp":
            raise ValueError("EMAIL_PROVIDER must be 'smtp'")
        return value

    @field_validator(
        "otp_ttl_minutes",
        "otp_max_attempts",
        "otp_resend_cooldown_seconds",
        "otp_requests_per_email_per_hour",
    )
    @classmethod
    def _otp_limits_are_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("OTP limits must be at least 1")
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

    @model_validator(mode="after")
    def _apply_production_overrides(self) -> "Settings":
        """In production, prefer Neon Postgres and Upstash Redis.

        Local dev keeps using the plain DATABASE_URL / REDIS_URL so nothing
        changes on a laptop. Upstash uses `rediss://` (TLS), which
        slowapi/limits accepts directly. The Postgres URL is normalized for
        asyncpg by `_database_url_is_asyncpg_ready`, which runs after this
        validator.
        """
        if not self.is_production:
            return self
        if self.neon_database_url:
            self.database_url = self.neon_database_url
        if self.upstash_redis_url:
            self.redis_url = self.upstash_redis_url
        return self

    @model_validator(mode="after")
    def _database_url_is_asyncpg_ready(self) -> "Settings":
        """Rewrite the Postgres URL into the dialect asyncpg actually speaks.

        Runs in every environment — the engine reads this URL through the
        async driver no matter where it came from. Defined after
        `_apply_production_overrides` so the Neon URL swapped in there is
        normalized too (after-validators run in definition order).
        """
        self.database_url = _as_asyncpg_url(self.database_url)
        return self


@lru_cache
def get_settings() -> Settings:
    # mypy can't see that required fields are populated from the environment
    # at runtime, not from this call site — known pydantic-settings false positive.
    return Settings()  # type: ignore[call-arg]
