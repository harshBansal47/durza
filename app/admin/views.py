"""SQLAdmin model views — the point-and-click back office.

The JSON API's services generate slugs, hash passwords and record authors. The admin
panel writes to the database directly, so the same rules are re-applied here in
`on_model_change` to keep both paths producing identical data.
"""

import uuid
from datetime import UTC, datetime
from typing import Any, ClassVar, cast

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqladmin import ModelView
from sqladmin.filters import BooleanFilter, StaticValuesFilter
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from wtforms import PasswordField

from app.core.security import hash_password
from app.core.slugs import generate_unique_slug
from app.models.ad import Ad
from app.models.category import Category
from app.models.click import ClickEvent
from app.models.coupon import Coupon
from app.models.ingestion_run import IngestionRun
from app.models.job import JobRun, JobStatus
from app.models.page import Page
from app.models.product import PricePoint, Product
from app.models.source import Source
from app.models.store import Store
from app.models.user import Role, User
from app.models.verification_attempt import AttemptOutcome, VerificationAttempt

_email_adapter: TypeAdapter[str] = TypeAdapter(EmailStr)


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
    name = "Store"
    name_plural = "Stores"
    icon = "fa-solid fa-store"

    column_list = [Store.name, Store.slug, Store.website_url, Store.is_active, Store.created_at]
    column_searchable_list = [Store.name, Store.slug]
    column_sortable_list = [Store.name, Store.is_active, Store.created_at]
    column_default_sort = [(Store.name, False)]
    form_columns = [
        Store.name,
        Store.logo_url,
        Store.website_url,
        Store.description,
        Store.commission_disclosure,
        Store.affiliate_network,
        Store.link_template,
        Store.cookie_days,
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
        Coupon.is_active,
        Coupon.expires_at,
        Coupon.success_count,
        Coupon.fail_count,
        Coupon.clicks_count,
        Coupon.status,
        Coupon.failure_reason,
    ]
    column_details_list = [
        Coupon.title,
        Coupon.slug,
        Coupon.code,
        Coupon.description,
        "store",
        "category",
        Coupon.discount_type,
        Coupon.discount_value,
        Coupon.destination_url,
        Coupon.is_active,
        Coupon.expires_at,
        Coupon.clicks_count,
        Coupon.views_count,
        Coupon.success_count,
        Coupon.fail_count,
        Coupon.last_verified_at,
        Coupon.status,
        Coupon.failure_reason,
        Coupon.last_checked_at,
        Coupon.source_id,
        Coupon.created_at,
        Coupon.updated_at,
    ]
    column_searchable_list = [Coupon.title, Coupon.code, Coupon.slug]
    # The "admin queue" for suspicious offers: filter by status in one click.
    column_filters = [
        StaticValuesFilter(
            Coupon.status,
            values=[("active", "active"), ("failed", "failed"), ("expired", "expired")],
            title="Status",
        ),
        BooleanFilter(Coupon.is_active, title="Active"),
    ]
    column_sortable_list = [
        Coupon.title,
        Coupon.discount_type,
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
    name = "Source"
    name_plural = "Sources"
    icon = "fa-solid fa-database"

    column_list = [
        Source.name,
        Source.kind,
        Source.is_enabled,
        Source.probe_destinations,
        Source.created_at,
    ]
    column_searchable_list = [Source.name, Source.slug]
    column_sortable_list = [Source.name, Source.kind, Source.is_enabled, Source.created_at]
    column_default_sort = [(Source.name, False)]
    # JSON config edited as raw text is error-prone in a form; manage it via
    # scripts/seed or the DB, and show it read-only in the details view.
    # (form_columns below therefore omits Source.config.)
    column_details_list = [
        Source.name,
        Source.slug,
        Source.kind,
        Source.description,
        Source.is_enabled,
        Source.probe_destinations,
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
    ]
    slug_source = "name"


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
    name = "Ingestion Run"
    name_plural = "Ingestion Runs"
    icon = "fa-solid fa-rotate"

    column_list = [
        IngestionRun.status,
        IngestionRun.started_at,
        IngestionRun.finished_at,
        IngestionRun.fetched,
        IngestionRun.created,
        IngestionRun.updated,
        IngestionRun.skipped,
        IngestionRun.failed,
    ]
    column_sortable_list = [IngestionRun.started_at, IngestionRun.status]
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


ALL_VIEWS = (
    CategoryAdmin,
    StoreAdmin,
    CouponAdmin,
    AdAdmin,
    PageAdmin,
    UserAdmin,
    SourceAdmin,
    IngestionRunAdmin,
    ProductAdmin,
    PricePointAdmin,
    JobRunAdmin,
    VerificationAttemptAdmin,
    ClickEventAdmin,
)
