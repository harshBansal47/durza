"""SQLAdmin model views — the point-and-click back office.

The JSON API's services generate slugs, hash passwords and record authors. The admin
panel writes to the database directly, so the same rules are re-applied here in
`on_model_change` to keep both paths producing identical data.
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar, cast

from fastapi import HTTPException
from pydantic import EmailStr, TypeAdapter, ValidationError
from sqladmin import ModelView, action
from sqladmin.filters import BooleanFilter, StaticValuesFilter
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from wtforms import PasswordField

from app.core.security import hash_password
from app.core.slugs import generate_unique_slug
from app.models.ad import Ad
from app.models.app_log import AppLog
from app.models.category import Category
from app.models.click import ClickEvent
from app.models.coupon import Coupon
from app.models.coupon_source import CouponSource
from app.models.ingestion_run import IngestionRun
from app.models.job import JobRun, JobStatus
from app.models.page import Page
from app.models.product import PricePoint, Product
from app.models.sale import SaleEdition, SaleEditionCouponRule, SaleSeries
from app.models.source import Source
from app.models.store import Store
from app.models.user import Role, User
from app.models.verification_attempt import AttemptOutcome, VerificationAttempt
from app.services import sale_service
from app.services.manual_ingestion import start_ingestion_now

_email_adapter: TypeAdapter[str] = TypeAdapter(EmailStr)

logger = logging.getLogger("durza.admin")


def _actor(request: Request) -> str:
    """Who is acting, for the audit trail. The session holds just the user id."""
    return str(request.session.get("user_id", "unknown"))


def ensure_utc(value: Any) -> Any:
    """Treat naive datetimes from admin forms as UTC.

    asyncpg refuses to bind a naive datetime to a timestamptz column, and HTML
    form fields always produce naive values.
    """
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


class StaffModelView(ModelView):
    """Shared behaviour for every view. No `model=` here, so it is not registered itself."""

    page_size = 25
    page_size_options = [25, 50, 100]

    # Name of the field the slug is derived from (None = this model has no slug).
    slug_source: ClassVar[str | None] = None
    # Datetime fields that must be made timezone-aware before saving.
    datetime_fields: ClassVar[tuple[str, ...]] = ()

    async def on_model_change(
        self, data: dict, model: Any, is_created: bool, request: Request
    ) -> None:
        for field in self.datetime_fields:
            if field in data:
                data[field] = ensure_utc(data[field])
        if self.slug_source is not None:
            await self._apply_slug(data, model, is_created)

    async def after_model_change(
        self, data: dict, model: Any, is_created: bool, request: Request
    ) -> None:
        # Audit trail for every create and edit in the back office. Field *names*
        # only: values can hold credentials, and the row itself has the new state.
        logger.info(
            "admin created record" if is_created else "admin updated record",
            extra={
                "actor": _actor(request),
                "model": type(model).__name__,
                "record_id": str(getattr(model, "id", "")),
                "changed_fields": sorted(data),
            },
        )

    async def after_model_delete(self, model: Any, request: Request) -> None:
        logger.warning(
            "admin deleted record",
            extra={
                "actor": _actor(request),
                "model": type(model).__name__,
                "record_id": str(getattr(model, "id", "")),
            },
        )

    async def _apply_slug(self, data: dict, model: Any, is_created: bool) -> None:
        source_value = data.get(cast(str, self.slug_source))
        if not source_value:
            return
        if not is_created and source_value == getattr(model, cast(str, self.slug_source)):
            return  # name unchanged, keep the existing slug
        session_maker = cast(async_sessionmaker[AsyncSession], self.session_maker)
        async with session_maker() as session:
            data["slug"] = await generate_unique_slug(
                session,
                self.model,
                source_value,
                exclude_id=None if is_created else getattr(model, "id", None),
            )


class CategoryAdmin(StaffModelView, model=Category):
    name = "Category"
    name_plural = "Categories"
    icon = "fa-solid fa-folder-tree"

    column_list = [Category.name, Category.slug, "parent", Category.created_at]
    column_searchable_list = [Category.name, Category.slug]
    column_sortable_list = [Category.name, Category.created_at]
    column_default_sort = [(Category.name, False)]
    form_columns = [Category.name, Category.icon, "parent"]
    slug_source = "name"

    async def on_model_change(
        self, data: dict, model: Any, is_created: bool, request: Request
    ) -> None:
        # The dropdown submits the chosen parent's id as a string.
        if not is_created and data.get("parent") and str(data["parent"]) == str(model.id):
            raise ValueError("A category cannot be its own parent")
        await super().on_model_change(data, model, is_created, request)


class StoreAdmin(StaffModelView, model=Store):
    """The canonical merchant. `normalized_domain` is the identity that matters.

    Four networks spell "Nike" four ways. The domain is what collapses them into
    one row, so it is shown and editable here: an operator who notices two
    merchants that are actually one fixes it by setting the domain, which is a
    visible, reviewable edit — rather than by a merge the pipeline performs on its
    own and nobody sees.

    `aliases` is editable for the same reason: "NIKE" and "Nike Official Store UK"
    are one merchant by human knowledge that no algorithm has, and an alias
    encodes exactly that, without weakening the domain guarantee.
    """

    name = "Store"
    name_plural = "Stores"
    icon = "fa-solid fa-store"

    column_list = [
        Store.name,
        Store.slug,
        Store.normalized_domain,
        Store.status,
        Store.is_active,
        Store.created_at,
    ]
    column_searchable_list = [Store.name, Store.slug, Store.normalized_domain]
    column_sortable_list = [
        Store.name,
        Store.status,
        Store.is_active,
        Store.country_code,
        Store.created_at,
    ]
    column_default_sort = [(Store.name, False)]
    column_filters = [
        StaticValuesFilter(
            Store.status,
            values=[
                ("active", "active"),
                ("pending", "pending"),
                ("inactive", "inactive"),
                ("merged", "merged"),
            ],
            title="Status",
        ),
        BooleanFilter(Store.is_active, title="Active"),
    ]
    form_columns = [
        Store.name,
        Store.logo_url,
        Store.website_url,
        Store.description,
        Store.commission_disclosure,
        Store.affiliate_network,
        Store.link_template,
        Store.cookie_days,
        Store.country_code,
        Store.currency,
        # The three identity fields. Left out of `column_list`'s filters and
        # treated as derived-but-correctable rather than fully derived, because a
        # wrong domain here sends every future coupon for that retailer to the
        # wrong merchant page and no automated step will notice.
        Store.normalized_domain,
        Store.aliases,
        Store.status,
        Store.is_active,
    ]
    slug_source = "name"


class CouponAdmin(StaffModelView, model=Coupon):
    name = "Coupon"
    name_plural = "Coupons"
    icon = "fa-solid fa-ticket"

    column_list = [
        Coupon.title,
        "store",
        "category",
        Coupon.discount_type,
        Coupon.discount_value,
        Coupon.status,
        Coupon.verification_status,
        Coupon.quality_score,
        "sources",
        Coupon.source_count,
        Coupon.is_active,
        Coupon.expires_at,
    ]
    column_details_list = [
        Coupon.title,
        Coupon.slug,
        Coupon.code,
        Coupon.normalized_code,
        Coupon.description,
        "store",
        "category",
        Coupon.discount_type,
        Coupon.discount_value,
        Coupon.country,
        # `destination_url` is a resolved denormalisation for display and as the
        # fallback for the /go redirect. The authoritative per-provider
        # destinations are on the CouponSource rows linked in "sources" below,
        # which is what AffiliateRouter actually chooses between.
        Coupon.destination_url,
        "sources",
        Coupon.source_count,
        Coupon.restrictions,
        Coupon.source_categories,
        Coupon.status,
        Coupon.verification_status,
        Coupon.quality_score,
        Coupon.is_active,
        Coupon.starts_at,
        Coupon.expires_at,
        Coupon.clicks_count,
        Coupon.views_count,
        Coupon.success_count,
        Coupon.fail_count,
        Coupon.last_verified_at,
        Coupon.failure_reason,
        Coupon.last_checked_at,
        Coupon.content_hash,
        Coupon.source_id,
        Coupon.created_at,
        Coupon.updated_at,
    ]
    column_searchable_list = [Coupon.title, Coupon.code, Coupon.slug]
    # The "admin queue" for suspicious offers: filter by status in one click.
    column_filters = [
        # `pending_validation` is the state every newly ingested coupon lands in
        # and the one a reviewer actually works through, so it leads.
        StaticValuesFilter(
            Coupon.status,
            values=[
                ("active", "active"),
                ("pending_validation", "pending_validation"),
                ("validation_failed", "validation_failed"),
                ("source_removed", "source_removed"),
                ("failed", "failed"),
                ("expired", "expired"),
                ("disabled", "disabled"),
            ],
            title="Status",
        ),
        # Verification is kept as a separate filter from status on purpose: "is
        # it showing" and "does it work" are different questions, and one filter
        # for both would hide coupons that are live but untrusted.
        StaticValuesFilter(
            Coupon.verification_status,
            values=[
                ("unverified", "unverified"),
                ("pending", "pending"),
                ("verified", "verified"),
                ("disputed", "disputed"),
                ("failed", "failed"),
            ],
            title="Verification",
        ),
        BooleanFilter(Coupon.is_active, title="Active"),
    ]
    column_sortable_list = [
        Coupon.title,
        Coupon.discount_type,
        Coupon.quality_score,
        Coupon.source_count,
        Coupon.is_active,
        Coupon.expires_at,
        Coupon.clicks_count,
        Coupon.views_count,
        Coupon.created_at,
    ]
    column_default_sort = [(Coupon.created_at, True)]
    form_columns = [
        Coupon.title,
        Coupon.code,
        Coupon.description,
        Coupon.discount_type,
        Coupon.discount_value,
        "store",
        "category",
        Coupon.destination_url,
        Coupon.starts_at,
        Coupon.expires_at,
        Coupon.status,
        Coupon.is_active,
    ]
    slug_source = "title"
    datetime_fields = ("expires_at",)

    async def on_model_change(
        self, data: dict, model: Any, is_created: bool, request: Request
    ) -> None:
        await super().on_model_change(data, model, is_created, request)
        if is_created:
            data["created_by"] = uuid.UUID(request.session["user_id"])


class AdAdmin(StaffModelView, model=Ad):
    name = "Ad"
    name_plural = "Ads"
    icon = "fa-solid fa-rectangle-ad"

    column_list = [
        Ad.advertiser,
        Ad.label,
        Ad.position,
        Ad.image_url,
        Ad.target_url,
        Ad.clicks_count,
        Ad.starts_at,
        Ad.ends_at,
        Ad.is_active,
    ]
    column_sortable_list = [
        Ad.advertiser,
        Ad.position,
        Ad.clicks_count,
        Ad.starts_at,
        Ad.ends_at,
        Ad.is_active,
        Ad.created_at,
    ]
    column_default_sort = [(Ad.created_at, True)]
    form_columns = [
        Ad.advertiser,
        Ad.label,
        Ad.position,
        Ad.image_url,
        Ad.alt_text,
        Ad.target_url,
        Ad.starts_at,
        Ad.ends_at,
        Ad.is_active,
    ]
    datetime_fields = ("starts_at", "ends_at")


class PageAdmin(StaffModelView, model=Page):
    name = "Page"
    name_plural = "Pages"
    icon = "fa-solid fa-file-lines"

    column_list = [Page.title, Page.slug, Page.is_published, Page.created_at]
    column_searchable_list = [Page.title, Page.slug]
    column_sortable_list = [Page.title, Page.is_published, Page.created_at]
    column_default_sort = [(Page.title, False)]
    form_columns = [Page.title, Page.content, Page.meta_description, Page.is_published]
    slug_source = "title"


class UserAdmin(StaffModelView, model=User):
    """Admin-only (see RoleAuthorization). Passwords are write-only: hashed on save, never shown."""

    name = "User"
    name_plural = "Users"
    icon = "fa-solid fa-users"

    column_list = [User.email, User.full_name, User.role, User.is_active, User.created_at]
    column_details_exclude_list = [User.hashed_password]
    column_searchable_list = [User.email, User.full_name]
    column_sortable_list = [User.email, User.role, User.is_active, User.created_at]
    column_default_sort = [(User.created_at, True)]
    form_columns = [User.email, User.full_name, User.role, User.is_active]
    can_export = False  # an export must never be able to include password hashes

    async def scaffold_form(self, rules: list[str] | None = None) -> Any:
        form_class = await super().scaffold_form(rules)
        form_class.password = PasswordField(
            "Password",
            description="Required for new users. When editing, leave blank to keep the current one.",
        )
        return form_class

    async def on_model_change(
        self, data: dict, model: Any, is_created: bool, request: Request
    ) -> None:
        # Always remove the virtual field: it is not a column and must never reach the model.
        password = data.pop("password", None)

        try:
            data["email"] = _email_adapter.validate_python(str(data.get("email", "")).strip())
        except ValidationError as exc:
            raise ValueError("Enter a valid email address") from exc

        if password:
            if len(password) < 8:
                raise ValueError("Password must be at least 8 characters")
            data["hashed_password"] = hash_password(password)
        elif is_created:
            raise ValueError("A password is required when creating a user")

        # Lock-out protection: you may not demote or deactivate the account you are using.
        if (
            not is_created
            and str(model.id) == request.session.get("user_id")
            and (
                Role(data.get("role", model.role)) != Role.admin or not data.get("is_active", True)
            )
        ):
            raise ValueError("You cannot demote or deactivate your own account")

        await super().on_model_change(data, model, is_created, request)

    async def on_model_delete(self, model: Any, request: Request) -> None:
        if str(model.id) == request.session.get("user_id"):
            raise ValueError("You cannot delete your own account")


class SourceAdmin(StaffModelView, model=Source):
    """A provider, plus how much we trust it and whether its API is answering.

    `credential_ref` is editable and it is safe to render: it holds the *name* of
    an environment variable ("AWIN_API_KEY"), never its value. The value is read
    from the process environment at fetch time by `app.ingestion.credentials`.
    That indirection is the whole reason this form is safe to show to every
    editor — there is no secret in this row to redact, so no redaction pass can be
    forgotten later.

    `api_status` is deliberately a health reading and not an on/off switch. A
    provider with a failing API becomes `degraded`, which slows its schedule and
    stops it being counted as corroboration — it does *not* remove every coupon
    it already contributed. `is_enabled` remains the only human kill switch.
    """

    name = "Source"
    name_plural = "Sources"
    icon = "fa-solid fa-database"

    column_list = [
        Source.name,
        Source.kind,
        Source.is_enabled,
        Source.api_status,
        Source.priority,
        Source.last_success_at,
        Source.consecutive_failures,
        Source.created_at,
    ]
    column_searchable_list = [Source.name, Source.slug]
    column_sortable_list = [
        Source.name,
        Source.kind,
        Source.is_enabled,
        Source.api_status,
        Source.priority,
        Source.last_success_at,
        Source.consecutive_failures,
        Source.created_at,
    ]
    column_default_sort = [(Source.name, False)]
    column_filters = [
        StaticValuesFilter(
            Source.api_status,
            values=[
                ("healthy", "healthy"),
                ("degraded", "degraded"),
                ("failing", "failing"),
                ("paused", "paused"),
                ("unconfigured", "unconfigured"),
            ],
            title="API health",
        ),
        BooleanFilter(Source.is_enabled, title="Enabled"),
        BooleanFilter(Source.probe_destinations, title="Probe destinations"),
    ]
    # JSON config edited as raw text is error-prone in a form; manage it via
    # scripts/seed or the DB, and show it read-only in the details view.
    # (form_columns below therefore omits Source.config.)
    #
    # Note `api_status`, `consecutive_failures` and the `last_*` timestamps are
    # absent from form_columns as well. They are written by the pipeline; letting
    # a form set them would let an operator mark a broken feed healthy and have
    # the dashboard agree with the lie.
    column_details_list = [
        Source.name,
        Source.slug,
        Source.kind,
        Source.description,
        Source.is_enabled,
        Source.probe_destinations,
        Source.priority,
        Source.confidence,
        Source.countries,
        Source.credential_ref,
        Source.sync_interval_minutes,
        Source.timeout_seconds,
        Source.rate_limit_per_minute,
        Source.requests_per_second,
        Source.api_status,
        Source.consecutive_failures,
        Source.last_sync_at,
        Source.last_success_at,
        Source.last_failure_at,
        Source.last_error,
        Source.config,
        Source.created_at,
        Source.updated_at,
    ]
    form_columns = [
        Source.name,
        Source.kind,
        Source.description,
        Source.is_enabled,
        Source.probe_destinations,
        # Precedence and confidence are data, not code: re-ordering which
        # provider wins a metadata conflict should be a reviewed config edit,
        # not a redeploy.
        Source.priority,
        Source.confidence,
        Source.countries,
        Source.credential_ref,
        Source.sync_interval_minutes,
        Source.timeout_seconds,
    ]
    slug_source = "name"

    @action(
        "ingest_now",
        label="Ingest now",
        confirmation_message="Fetch coupons from the selected providers now?",
        add_in_detail=True,
        add_in_list=True,
    )
    async def ingest_now(self, request: Request) -> Response:
        """Run ingestion for the selected providers now, without waiting on the schedule.

        It starts in the background and returns at once, because a provider's
        pagination can outlast a browser request. The outcome is in Ingestion
        Runs (per provider) and Job Runs (the sweep), where this redirects.

        It goes through the same job as the scheduler, so it takes the same
        locks and is recorded the same way. `force` ignores the provider's own
        interval and any failure backoff; a *disabled* provider is still skipped,
        because `is_enabled` is the operator's kill switch.

        Admin-only: it makes outbound calls to third-party APIs.
        """
        if getattr(request.state, "admin_role", None) != Role.admin:
            raise HTTPException(status_code=403, detail="Only admins can trigger ingestion")
        raw = request.query_params.get("pks", "")
        try:
            ids = [uuid.UUID(part) for part in raw.split(",") if part]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid selection") from exc
        if not ids:
            raise HTTPException(status_code=400, detail="No provider selected")
        session_maker = cast(async_sessionmaker[AsyncSession], self.session_maker)
        async with session_maker() as session:
            result = await session.execute(select(Source.slug).where(Source.id.in_(ids)))
            slugs = list(result.scalars().all())
        if not slugs:
            raise HTTPException(status_code=404, detail="Provider not found")
        start_ingestion_now(slugs, triggered_by=f"admin-ui:{_actor(request)}")
        return RedirectResponse(
            url=str(request.url_for("admin:list", identity="ingestion-run")), status_code=303
        )


class CouponSourceAdmin(StaffModelView, model=CouponSource):
    """One provider's claim about one canonical coupon — the provenance ledger.

    This is the view that answers "where did this coupon come from?" and "why
    does it say it expires on the 30th when the other three say the 31st?". Each
    row is a single network's opinion, kept separately from the resolved
    `Coupon` precisely because they disagree.

    `raw_data` is in the details list and not the form: it is a verbatim provider
    payload, useful when a network changes a field name, and useless to type by
    hand. It is also the only place a provider's *original* claim survives, which
    is why it is never overwritten by anything except that same provider's next
    run.
    """

    name = "Coupon Source"
    name_plural = "Coupon Sources"
    icon = "fa-solid fa-link"

    column_list = [
        "coupon",
        "source",
        CouponSource.external_id,
        CouponSource.status,
        CouponSource.source_expires_at,
        CouponSource.last_seen_at,
        CouponSource.updated_at,
    ]
    column_details_list = column_list + [
        CouponSource.source_title,
        CouponSource.affiliate_url,
        CouponSource.last_verified_at,
        CouponSource.source_created_at,
        CouponSource.source_updated_at,
        CouponSource.created_at,
    ]
    column_searchable_list = [
        CouponSource.external_id,
        CouponSource.source_title,
    ]
    column_filters = [
        # The distinction this table exists to preserve: a provider reporting the
        # coupon as gone (`deleted`) is NOT the same as it going quiet, and only
        # one of the two is acted upon automatically.
        StaticValuesFilter(
            CouponSource.status,
            values=[
                ("active", "active"),
                ("inactive", "inactive"),
                ("expired", "expired"),
                ("failed", "failed"),
                ("deleted", "deleted"),
            ],
            title="Source status",
        ),
    ]
    column_sortable_list = [
        CouponSource.status,
        CouponSource.source_expires_at,
        CouponSource.last_seen_at,
        CouponSource.created_at,
    ]
    column_default_sort = [(CouponSource.last_seen_at, True)]
    form_columns = [CouponSource.status]
    # Rows are written by the pipeline. An editable `affiliate_url` here would be
    # an unaudited override of the one field the affiliate router trusts.
    can_create = False
    can_edit = False


class ProductAdmin(StaffModelView, model=Product):
    name = "Product"
    name_plural = "Products"
    icon = "fa-solid fa-box-open"

    column_list = [
        Product.name,
        "store",
        Product.current_price,
        Product.lowest_price_90d,
        Product.last_price_drop_pct,
        Product.in_stock,
        Product.last_captured_at,
    ]
    column_searchable_list = [Product.name, Product.slug]
    column_sortable_list = [Product.name, Product.current_price, Product.created_at]
    column_default_sort = [(Product.created_at, True)]
    column_filters = [BooleanFilter(Product.in_stock, title="In stock")]
    form_columns = [
        Product.name,
        "store",
        "category",
        Product.url,
        Product.image_url,
        Product.currency,
    ]
    slug_source = "name"


class PricePointAdmin(StaffModelView, model=PricePoint):
    name = "Price Point"
    name_plural = "Price Points"
    icon = "fa-solid fa-chart-line"

    column_list = [
        PricePoint.product_id,
        PricePoint.price,
        PricePoint.original_price,
        PricePoint.shipping,
        PricePoint.in_stock,
        PricePoint.captured_at,
    ]
    column_sortable_list = [PricePoint.captured_at, PricePoint.price]
    column_default_sort = [(PricePoint.captured_at, True)]
    # History is recorded data, not hand-maintained state.
    can_edit = False
    can_create = False


class IngestionRunAdmin(StaffModelView, model=IngestionRun):
    """Every execution of every source, with counters that say *why* it ended.

    The counter list is the point of this view. `skipped` was removed because it
    lumped four different outcomes into one number — a payload that no longer
    parses, a record that failed validation, an offer that arrived already
    expired and one we folded into an existing coupon. Those need four different
    fixes, so they are four counters now:

    * `malformed` — the provider changed their payload shape.
    * `failed` — well-formed but unusable (bad URL, impossible discount).
    * `expired` — arrived after its own end date.
    * `duplicated` — matched a coupon another source had already given us.

    `duplicated` is not an error and a high value is a sign the multi-source
    setup is working: it is the direct measure of redundancy absorbed.
    """

    name = "Ingestion Run"
    name_plural = "Ingestion Runs"
    icon = "fa-solid fa-rotate"

    column_list = [
        "source",
        IngestionRun.status,
        IngestionRun.mode,
        IngestionRun.started_at,
        IngestionRun.duration_ms,
        IngestionRun.fetched,
        IngestionRun.created,
        IngestionRun.updated,
        IngestionRun.duplicated,
        IngestionRun.malformed,
        IngestionRun.failed,
        IngestionRun.expired,
    ]
    column_details_list = column_list + [
        IngestionRun.finished_at,
        IngestionRun.pages_fetched,
        IngestionRun.cancel_requested,
        IngestionRun.triggered_by,
        IngestionRun.error,
    ]
    column_searchable_list = [IngestionRun.error]
    column_filters = [
        # "Partial" is the filter an operator reaches for first: it is the only
        # status that means the provider answered but the answer was incomplete,
        # which is invisible in every other column.
        StaticValuesFilter(
            IngestionRun.status,
            values=[
                ("success", "success"),
                ("partial", "partial"),
                ("failed", "failed"),
                ("cancelled", "cancelled"),
                ("running", "running"),
            ],
            title="Status",
        ),
        StaticValuesFilter(
            IngestionRun.mode,
            values=[("incremental", "incremental"), ("full", "full")],
            title="Mode",
        ),
    ]
    column_sortable_list = [
        IngestionRun.started_at,
        IngestionRun.duration_ms,
        IngestionRun.fetched,
        IngestionRun.created,
        IngestionRun.failed,
        IngestionRun.status,
    ]
    column_default_sort = [(IngestionRun.started_at, True)]
    # Audit rows: no create/edit, only view + delete.
    can_create = False
    can_edit = False


class JobRunAdmin(StaffModelView, model=JobRun):
    """The job ledger, and the page to read first when a coupon looks stale.

    Nothing here is editable. Every column is either a fact about the world or a
    fact about the run, and letting an admin form rewrite either would let a
    "fixed" job history be manufactured — which is the one thing this table has
    to be trusted for.
    """

    name = "Job Run"
    name_plural = "Job Runs"
    icon = "fa-solid fa-list-check"

    column_list = [
        JobRun.job,
        JobRun.status,
        JobRun.started_at,
        JobRun.duration_ms,
        JobRun.detail,
    ]
    column_details_list = column_list + [JobRun.finished_at]
    column_searchable_list = [JobRun.job, JobRun.detail]
    column_filters = [
        # The two questions this table is opened for: "which job is broken?" and
        # "what did it say when it broke?"
        StaticValuesFilter(
            JobRun.job,
            values=[
                ("expire_coupons", "expire_coupons"),
                ("refresh_prices", "refresh_prices"),
                ("send_alerts", "send_alerts"),
                ("verify_coupons", "verify_coupons"),
                ("scrub_clicks", "scrub_clicks"),
            ],
            title="Job",
        ),
        StaticValuesFilter(
            JobRun.status,
            values=[
                (JobStatus.success.value, "success"),
                (JobStatus.failed.value, "failed"),
                (JobStatus.skipped.value, "skipped"),
                (JobStatus.running.value, "running"),
            ],
            title="Status",
        ),
    ]
    column_sortable_list = [JobRun.job, JobRun.status, JobRun.started_at, JobRun.duration_ms]
    # Newest first, always: nobody opens a history table to read the oldest row.
    column_default_sort = [(JobRun.started_at, True)]
    can_create = False
    can_edit = False
    can_export = True
    page_size = 50


class AppLogAdmin(StaffModelView, model=AppLog):
    """The stored application log. Admin-only and read-only.

    Rows expire on their own (`LOG_RETENTION_DAYS`), so this answers "what just
    happened?", not "what happened last quarter?". The permanent record of jobs
    and ingestion is Job Runs and Ingestion Runs.
    """

    name = "App Log"
    name_plural = "App Logs"
    icon = "fa-solid fa-scroll"

    column_list = [
        AppLog.created_at,
        AppLog.level,
        AppLog.logger,
        AppLog.message,
        AppLog.request_id,
    ]
    column_details_list = column_list + [AppLog.fields, AppLog.exception]
    column_searchable_list = [AppLog.message, AppLog.logger, AppLog.request_id]
    column_filters = [
        StaticValuesFilter(
            AppLog.level,
            values=[
                ("WARNING", "Warning"),
                ("ERROR", "Error"),
                ("CRITICAL", "Critical"),
                ("INFO", "Info"),
            ],
            title="Level",
        ),
    ]
    column_sortable_list = [AppLog.created_at, AppLog.level, AppLog.logger]
    column_default_sort = [(AppLog.created_at, True)]
    can_create = False
    can_edit = False
    can_delete = False
    page_size = 50

    def is_visible(self, request: Request) -> bool:
        return getattr(request.state, "admin_role", None) == Role.admin

    def is_accessible(self, request: Request) -> bool:
        return getattr(request.state, "admin_role", None) == Role.admin


class VerificationAttemptAdmin(StaffModelView, model=VerificationAttempt):
    """Automated checker results, kept apart from human reports.

    The distinction is the reason this is a separate view from
    `CouponVerification`: a store employee confirming a code by eye and a script
    guessing from an HTTP status are different kinds of evidence, and averaging
    them produces a success rate that means nothing.
    """

    name = "Verification Attempt"
    name_plural = "Verification Attempts"
    icon = "fa-solid fa-magnifying-glass-chart"

    column_list = [
        VerificationAttempt.checker,
        VerificationAttempt.outcome,
        "coupon",
        VerificationAttempt.valid,
        VerificationAttempt.checked_at,
        VerificationAttempt.detail,
    ]
    column_details_list = column_list + [VerificationAttempt.created_at]
    column_searchable_list = [VerificationAttempt.checker, VerificationAttempt.detail]
    column_filters = [
        StaticValuesFilter(
            VerificationAttempt.outcome,
            values=[
                (AttemptOutcome.worked.value, "worked"),
                (AttemptOutcome.failed.value, "failed"),
                # Surfaced as its own filter because it is the one a checker
                # author needs: too many of these means the checker is broken,
                # not that the coupons are.
                (AttemptOutcome.inconclusive.value, "inconclusive"),
            ],
            title="Outcome",
        ),
        BooleanFilter(VerificationAttempt.valid, title="Valid"),
    ]
    column_sortable_list = [
        VerificationAttempt.checker,
        VerificationAttempt.outcome,
        VerificationAttempt.checked_at,
    ]
    column_default_sort = [(VerificationAttempt.checked_at, True)]
    # Append-only evidence. Editing an attempt would be forging it.
    can_create = False
    can_edit = False
    page_size = 50


class ClickEventAdmin(StaffModelView, model=ClickEvent):
    """Outbound clicks, newest first. Recorded data: view and filter only."""

    name = "Click"
    name_plural = "Clicks"
    icon = "fa-solid fa-arrow-pointer"

    column_list = [
        ClickEvent.created_at,
        ClickEvent.src,
        ClickEvent.network,
        ClickEvent.is_bot,
        ClickEvent.coupon_id,
        ClickEvent.product_id,
        ClickEvent.clickref,
    ]
    column_searchable_list = [ClickEvent.clickref, ClickEvent.src]
    column_sortable_list = [ClickEvent.created_at, ClickEvent.src, ClickEvent.network]
    column_default_sort = [(ClickEvent.created_at, True)]
    can_create = False
    can_edit = False


class SaleSeriesAdmin(StaffModelView, model=SaleSeries):
    """A sale hub: one brand or event, one series of editions.

    A series is the spine of a hub — "Diwali 2026" — and an edition is a dated
    run of it. Both are created here rather than in the database, so the
    slug, the publication status and the lifecycle status are edited by
    someone who can see what the storefront will show.
    """

    name = "Sale series"
    name_plural = "Sale series"
    icon = "fa-solid fa-tags"

    column_list = [
        SaleSeries.slug,
        SaleSeries.title,
        SaleSeries.publication_status,
        "editions",
        SaleSeries.created_at,
    ]
    column_searchable_list = [SaleSeries.slug, SaleSeries.title]
    column_sortable_list = [SaleSeries.slug, SaleSeries.created_at]
    column_default_sort = [(SaleSeries.slug, False)]
    form_columns = [
        SaleSeries.slug,
        SaleSeries.title,
        SaleSeries.description,
        SaleSeries.publication_status,
    ]


class SaleEditionAdmin(StaffModelView, model=SaleEdition):
    """One dated run of a hub, and the button that publishes it.

    Publishing is not a field edit: an edition can only be published once
    the guide it points at is published too, and the rules that decide what
    the hub shows have to be in place. The service enforces that, so the
    button is the only way in.
    """

    name = "Sale edition"
    name_plural = "Sale editions"
    icon = "fa-solid fa-calendar-days"

    column_list = [
        SaleEdition.slug,
        "series",
        SaleEdition.year,
        SaleEdition.lifecycle_status,
        SaleEdition.publication_status,
        SaleEdition.start_at,
        SaleEdition.end_at,
    ]
    column_searchable_list = [SaleEdition.slug]
    column_sortable_list = [SaleEdition.year, SaleEdition.start_at]
    column_default_sort = [(SaleEdition.start_at, False)]
    form_columns = [
        "series",
        SaleEdition.slug,
        SaleEdition.year,
        SaleEdition.start_at,
        SaleEdition.end_at,
        SaleEdition.dates_confirmed,
        SaleEdition.source_url,
        "editorial_content_id",
        SaleEdition.lifecycle_status,
        SaleEdition.publication_status,
        SaleEdition.coupon_rules_version,
    ]
    datetime_fields = ("start_at", "end_at")

    async def on_model_change(
        self, data: dict, model: Any, is_created: bool, request: Request
    ) -> None:
        await super().on_model_change(data, model, is_created, request)

    @action("Publish", label="Publish this edition once its guide is published")
    async def publish(self, request: Request) -> Response:
        """Publish the edition, if the service agrees it is ready.

        The service is the judge: an edition whose linked guide is not
        published, or whose confirmed dates have no source, is refused. The
        button does not second-guess it.
        """
        raw = request.query_params.get("pks")
        if not raw:
            raise HTTPException(status_code=400, detail="No edition selected")
        session_maker = cast(async_sessionmaker[AsyncSession], self.session_maker)
        async with session_maker() as session:
            edition = await session.get(SaleEdition, uuid.UUID(raw))
            if edition is None:
                raise HTTPException(status_code=404, detail="Edition not found")
            try:
                await sale_service.publish_edition(session, edition)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        return RedirectResponse(url=request.url, status_code=303)


class SaleEditionCouponRuleAdmin(StaffModelView, model=SaleEditionCouponRule):
    """What a hub shows: one rule per store, category or tag.

    A rule is a slug, not an id, so the rule survives a store being renamed
    and reads the same in the database as in the admin.
    """

    name = "Sale coupon rule"
    name_plural = "Sale coupon rules"
    icon = "fa-solid fa-list-check"

    column_list = [
        "edition",
        SaleEditionCouponRule.rule_type,
        SaleEditionCouponRule.rule_value,
        SaleEditionCouponRule.sort_order,
    ]
    column_searchable_list = [SaleEditionCouponRule.rule_value]
    column_default_sort = [(SaleEditionCouponRule.sort_order, False)]
    form_columns = [
        "edition",
        SaleEditionCouponRule.rule_type,
        SaleEditionCouponRule.rule_value,
        SaleEditionCouponRule.sort_order,
    ]


ALL_VIEWS = (
    CategoryAdmin,
    StoreAdmin,
    CouponAdmin,
    CouponSourceAdmin,
    AdAdmin,
    PageAdmin,
    UserAdmin,
    SourceAdmin,
    IngestionRunAdmin,
    ProductAdmin,
    PricePointAdmin,
    JobRunAdmin,
    AppLogAdmin,
    VerificationAttemptAdmin,
    ClickEventAdmin,
    SaleSeriesAdmin,
    SaleEditionAdmin,
    SaleEditionCouponRuleAdmin,
)
