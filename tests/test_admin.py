import uuid
from datetime import UTC, datetime

import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from app.admin import setup_admin
from app.admin.views import ensure_utc
from app.core.security import verify_password
from app.models.category import Category
from app.models.coupon import Coupon
from app.models.store import Store
from app.models.user import Role, User
from app.schemas.user import UserCreate
from app.services import user_service

PASSWORD = "supersecret123"


@pytest_asyncio.fixture
async def admin_client(engine, async_session_maker):
    """A separate app with only the admin mounted, wired to the in-memory test database."""
    app = FastAPI()
    setup_admin(app, engine, async_session_maker, secret_key="test-secret", https_only=False)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def make_user(async_session_maker):
    async def _make(role: Role, email: str | None = None, **kwargs) -> User:
        email = email or f"{role.value}-{uuid.uuid4().hex[:6]}@example.com"
        async with async_session_maker() as session:
            return await user_service.create_user(
                session, UserCreate(email=email, password=PASSWORD, **kwargs), role=role
            )

    return _make


async def login(client, email, password=PASSWORD):
    return await client.post("/admin/login", data={"username": email, "password": password})


async def staff_login(client, make_user, role):
    user = await make_user(role)
    resp = await login(client, user.email)
    assert resp.status_code == 302, resp.text
    return user


async def fetch_all(session_maker, model):
    async with session_maker() as session:
        return (await session.execute(select(model))).scalars().all()


# ---------------------------------------------------------------- authentication


async def test_admin_requires_login(admin_client):
    resp = await admin_client.get("/admin/")
    assert resp.status_code == 302
    assert resp.headers["location"].endswith("/admin/login")


async def test_staff_can_log_in_and_see_dashboard(admin_client, make_user):
    for role in (Role.admin, Role.editor):
        await staff_login(admin_client, make_user, role)
        assert (await admin_client.get("/admin/")).status_code == 200


async def test_regular_user_cannot_log_in(admin_client, make_user):
    user = await make_user(Role.user)
    resp = await login(admin_client, user.email)
    assert resp.status_code != 302
    assert (await admin_client.get("/admin/")).status_code == 302  # still logged out


async def test_wrong_password_and_unknown_email_rejected(admin_client, make_user):
    admin = await make_user(Role.admin)
    assert (await login(admin_client, admin.email, "not-the-password")).status_code != 302
    assert (await login(admin_client, "ghost@example.com")).status_code != 302


async def test_logout_ends_session(admin_client, make_user):
    await staff_login(admin_client, make_user, Role.admin)
    await admin_client.get("/admin/logout")
    assert (await admin_client.get("/admin/")).status_code == 302


async def test_deactivated_staff_loses_access_immediately(
    admin_client, make_user, async_session_maker
):
    user = await staff_login(admin_client, make_user, Role.editor)
    assert (await admin_client.get("/admin/")).status_code == 200

    async with async_session_maker() as session:
        db_user = await session.get(User, user.id)
        db_user.is_active = False
        await session.commit()

    assert (await admin_client.get("/admin/")).status_code == 302


async def test_demoted_editor_loses_access_immediately(
    admin_client, make_user, async_session_maker
):
    user = await staff_login(admin_client, make_user, Role.editor)
    async with async_session_maker() as session:
        db_user = await session.get(User, user.id)
        db_user.role = Role.user
        await session.commit()
    assert (await admin_client.get("/admin/category/list")).status_code == 302


# ------------------------------------------------------------------ content editing


async def test_editor_creates_category_with_generated_slug(
    admin_client, make_user, async_session_maker
):
    await staff_login(admin_client, make_user, Role.editor)
    for _ in range(2):
        resp = await admin_client.post("/admin/category/create", data={"name": "Home Decor"})
        assert resp.status_code == 302, resp.text

    slugs = sorted(c.slug for c in await fetch_all(async_session_maker, Category))
    assert slugs == ["home-decor", "home-decor-2"]


async def test_renaming_regenerates_slug(admin_client, make_user, async_session_maker):
    await staff_login(admin_client, make_user, Role.editor)
    await admin_client.post("/admin/category/create", data={"name": "Old Name"})
    cat = (await fetch_all(async_session_maker, Category))[0]

    resp = await admin_client.post(f"/admin/category/edit/{cat.id}", data={"name": "New Name"})
    assert resp.status_code == 302, resp.text
    updated = (await fetch_all(async_session_maker, Category))[0]
    assert updated.slug == "new-name"


async def test_category_parent_dropdown_and_self_parent_guard(
    admin_client, make_user, async_session_maker
):
    await staff_login(admin_client, make_user, Role.editor)
    await admin_client.post("/admin/category/create", data={"name": "Parent"})
    parent = (await fetch_all(async_session_maker, Category))[0]

    resp = await admin_client.post(
        "/admin/category/create", data={"name": "Child", "parent": str(parent.id)}
    )
    assert resp.status_code == 302, resp.text
    child = next(c for c in await fetch_all(async_session_maker, Category) if c.name == "Child")
    assert child.parent_id == parent.id

    resp = await admin_client.post(
        f"/admin/category/edit/{parent.id}", data={"name": "Parent", "parent": str(parent.id)}
    )
    assert resp.status_code == 400
    assert "own parent" in resp.text


async def test_coupon_create_via_dropdowns_records_author(
    admin_client, make_user, async_session_maker
):
    editor = await staff_login(admin_client, make_user, Role.editor)
    await admin_client.post("/admin/store/create", data={"name": "Amazon"})
    await admin_client.post("/admin/category/create", data={"name": "Electronics"})
    store = (await fetch_all(async_session_maker, Store))[0]
    cat = (await fetch_all(async_session_maker, Category))[0]

    resp = await admin_client.post(
        "/admin/coupon/create",
        data={
            "title": "20% off headphones",
            "code": "SAVE20",
            "discount_type": "percentage",
            "discount_value": "20",
            "store": str(store.id),
            "category": str(cat.id),
            "destination_url": "https://amazon.com/deal",
            "expires_at": "2030-01-01 12:00:00",
            "is_active": "y",
        },
    )
    assert resp.status_code == 302, resp.text

    coupon = (await fetch_all(async_session_maker, Coupon))[0]
    assert coupon.slug == "20-off-headphones"
    assert coupon.store_id == store.id and coupon.category_id == cat.id
    assert coupon.created_by == editor.id
    assert coupon.discount_value == 20

    # The list page renders store/category names (not raw UUIDs) without lazy-load errors.
    listing = await admin_client.get("/admin/coupon/list")
    assert listing.status_code == 200
    assert "Amazon" in listing.text and "Electronics" in listing.text


async def test_all_list_pages_render(admin_client, make_user):
    await staff_login(admin_client, make_user, Role.admin)
    for identity in ("category", "store", "coupon", "ad", "page", "user"):
        resp = await admin_client.get(f"/admin/{identity}/list")
        assert resp.status_code == 200, identity


async def test_ad_and_page_forms(admin_client, make_user, async_session_maker):
    await staff_login(admin_client, make_user, Role.editor)
    resp = await admin_client.post(
        "/admin/ad/create",
        data={
            "position": "sidebar",
            "image_url": "https://cdn.example.com/b.png",
            "target_url": "https://advertiser.example.com",
            "starts_at": "2030-01-01 00:00:00",
            "is_active": "y",
        },
    )
    assert resp.status_code == 302, resp.text
    resp = await admin_client.post(
        "/admin/page/create",
        data={"title": "About Us", "content": "<p>Hi</p>", "is_published": "y"},
    )
    assert resp.status_code == 302, resp.text

    from app.models.page import Page

    assert (await fetch_all(async_session_maker, Page))[0].slug == "about-us"


# ------------------------------------------------------------------------ permissions


async def test_editor_cannot_delete_but_admin_can(admin_client, make_user, async_session_maker):
    await staff_login(admin_client, make_user, Role.editor)
    await admin_client.post("/admin/category/create", data={"name": "Doomed"})
    cat = (await fetch_all(async_session_maker, Category))[0]

    resp = await admin_client.delete(f"/admin/category/delete?pks={cat.id}")
    assert resp.status_code == 403
    assert len(await fetch_all(async_session_maker, Category)) == 1

    await admin_client.get("/admin/logout")
    await staff_login(admin_client, make_user, Role.admin)
    resp = await admin_client.delete(f"/admin/category/delete?pks={cat.id}")
    assert resp.status_code == 200
    assert await fetch_all(async_session_maker, Category) == []


async def test_editor_cannot_access_users(admin_client, make_user):
    await staff_login(admin_client, make_user, Role.editor)
    assert (await admin_client.get("/admin/user/list")).status_code == 403
    assert (await admin_client.get("/admin/user/create")).status_code == 403
    assert "/admin/user/list" not in (await admin_client.get("/admin/")).text  # hidden in menu


async def test_user_pages_never_expose_password_hash(admin_client, make_user):
    admin = await staff_login(admin_client, make_user, Role.admin)
    details = await admin_client.get(f"/admin/user/details/{admin.id}")
    assert details.status_code == 200
    assert "$2b$" not in details.text and "hashed_password" not in details.text
    assert "$2b$" not in (await admin_client.get("/admin/user/list")).text
    assert (await admin_client.get("/admin/user/export/csv")).status_code == 403


# ------------------------------------------------------------------------ user management


async def test_admin_creates_user_with_hashed_password(
    admin_client, make_user, async_session_maker
):
    await staff_login(admin_client, make_user, Role.admin)
    resp = await admin_client.post(
        "/admin/user/create",
        data={
            "email": "newbie@example.com",
            "full_name": "New Editor",
            "role": "editor",
            "is_active": "y",
            "password": "a-long-password",
        },
    )
    assert resp.status_code == 302, resp.text

    created = next(
        u for u in await fetch_all(async_session_maker, User) if u.email == "newbie@example.com"
    )
    assert created.role == Role.editor
    assert created.hashed_password != "a-long-password"
    assert verify_password("a-long-password", created.hashed_password)


async def test_user_form_validation(admin_client, make_user):
    await staff_login(admin_client, make_user, Role.admin)
    base = {"full_name": "X", "role": "user", "is_active": "y"}

    no_password = await admin_client.post(
        "/admin/user/create", data={**base, "email": "a@example.com"}
    )
    assert no_password.status_code == 400 and "password is required" in no_password.text

    short = await admin_client.post(
        "/admin/user/create", data={**base, "email": "b@example.com", "password": "short"}
    )
    assert short.status_code == 400 and "at least 8" in short.text

    bad_email = await admin_client.post(
        "/admin/user/create", data={**base, "email": "not-an-email", "password": "long-enough-1"}
    )
    assert bad_email.status_code == 400 and "valid email" in bad_email.text


async def test_editing_user_without_password_keeps_old_hash(
    admin_client, make_user, async_session_maker
):
    await staff_login(admin_client, make_user, Role.admin)
    target = await make_user(Role.user, email="keep@example.com")

    resp = await admin_client.post(
        f"/admin/user/edit/{target.id}",
        data={
            "email": "keep@example.com",
            "full_name": "Renamed",
            "role": "editor",
            "is_active": "y",
        },
    )
    assert resp.status_code == 302, resp.text
    async with async_session_maker() as session:
        updated = await session.get(User, target.id)
    assert updated.role == Role.editor and updated.full_name == "Renamed"
    assert verify_password(PASSWORD, updated.hashed_password)


async def test_admin_cannot_lock_themselves_out(admin_client, make_user, async_session_maker):
    me = await staff_login(admin_client, make_user, Role.admin)

    demote = await admin_client.post(
        f"/admin/user/edit/{me.id}",
        data={"email": me.email, "role": "user", "is_active": "y"},
    )
    assert demote.status_code == 400 and "own account" in demote.text

    deactivate = await admin_client.post(
        f"/admin/user/edit/{me.id}", data={"email": me.email, "role": "admin"}
    )  # is_active omitted = unchecked
    assert deactivate.status_code == 400

    await admin_client.delete(f"/admin/user/delete?pks={me.id}")
    async with async_session_maker() as session:
        still_there = await session.get(User, me.id)
    assert still_there is not None and still_there.role == Role.admin


# ---------------------------------------------------------------------------- helpers


def test_ensure_utc():
    naive = datetime(2030, 1, 1, 12, 0)
    assert ensure_utc(naive).tzinfo is UTC
    aware = datetime(2030, 1, 1, tzinfo=UTC)
    assert ensure_utc(aware) is aware
    assert ensure_utc(None) is None


async def test_main_app_mounts_admin(client):
    resp = await client.get("/admin/")
    assert resp.status_code == 302
    assert resp.headers["location"].endswith("/admin/login")


# ------------------------------------------------- job runs and verification attempts


async def test_job_runs_and_verification_attempts_are_listed_in_the_admin(
    admin_client, make_user, async_session_maker
):
    # These two tables are the whole answer to "why is this coupon stale?" and
    # "why did that code disappear?". If they are not reachable from the panel,
    # the only way to read them is psql.
    await staff_login(admin_client, make_user, Role.admin)
    for path in ("/admin/job-run/list", "/admin/verification-attempt/list"):
        resp = await admin_client.get(path)
        assert resp.status_code == 200, f"{path}: {resp.text[:400]}"
        assert "Job Runs" in resp.text or "Verification Attempts" in resp.text


async def test_job_run_detail_page_renders(admin_client, make_user, async_session_maker):
    from app.models.job import JobRun, JobStatus

    async with async_session_maker() as session:
        session.add(
            JobRun(
                job="send_alerts",
                status=JobStatus.failed,
                detail="ConnectionError: SMTP unreachable",
                started_at=datetime.now(UTC),
            )
        )
        await session.commit()

    await staff_login(admin_client, make_user, Role.admin)
    resp = await admin_client.get("/admin/job-run/list")
    assert resp.status_code == 200
    assert "send_alerts" in resp.text
    assert "SMTP unreachable" in resp.text


async def test_job_runs_cannot_be_edited_by_hand(admin_client, make_user, async_session_maker):
    # Every column here is a fact about a run. An admin form that could rewrite
    # one would let a repaired job history be manufactured, which is the only
    # thing this table must never be.
    from app.models.job import JobRun, JobStatus

    async with async_session_maker() as session:
        run = JobRun(
            job="send_alerts",
            status=JobStatus.failed,
            detail="original",
            started_at=datetime.now(UTC),
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    await staff_login(admin_client, make_user, Role.admin)
    resp = await admin_client.post(
        f"/admin/job-run/edit/{run_id}",
        data={"job": "send_alerts", "status": "success", "started_at": "2030-01-01 00:00:00"},
        follow_redirects=False,
    )
    # 403 is what SQLAdmin's authorization backend returns for a denied action,
    # which is the strongest of these answers: the form is not merely hidden, the
    # endpoint refuses.
    assert resp.status_code in (302, 303, 405, 403), resp.status_code

    async with async_session_maker() as session:
        row = await session.get(JobRun, run_id)
        assert row.status is JobStatus.failed
        assert row.detail == "original"


async def test_verification_attempt_keeps_inconclusive_separate_from_failed(
    admin_client, make_user, async_session_maker
):
    # "We could not check" and "we checked and it did not work" must not look the
    # same in the admin list, or a broken checker reads as a pile of dead coupons.
    from app.models.coupon import DiscountType
    from app.models.verification_attempt import AttemptOutcome, VerificationAttempt

    async with async_session_maker() as session:
        store = Store(name="Verity Store", slug=f"verity-{uuid.uuid4().hex[:8]}")
        session.add(store)
        await session.flush()
        category = Category(name="Verity Category", slug=f"vc-{uuid.uuid4().hex[:8]}")
        session.add(category)
        await session.flush()
        coupon = Coupon(
            title="Verity deal",
            slug=f"verity-deal-{uuid.uuid4().hex[:8]}",
            discount_type=DiscountType.percentage,
            discount_value=10.0,
            store_id=store.id,
            category_id=category.id,
            destination_url="https://example.com/deal",
        )
        session.add(coupon)
        await session.flush()
        for outcome in (AttemptOutcome.inconclusive, AttemptOutcome.failed):
            session.add(
                VerificationAttempt(
                    coupon_id=coupon.id,
                    checker="headless_fetch",
                    outcome=outcome,
                    # `valid` is null for inconclusive and false for failed: the
                    # tri-state is the whole point of the nullable column.
                    valid=None if outcome is AttemptOutcome.inconclusive else False,
                    checked_at=datetime.now(UTC),
                )
            )
        await session.commit()

    await staff_login(admin_client, make_user, Role.admin)
    resp = await admin_client.get("/admin/verification-attempt/list")
    assert resp.status_code == 200
    assert "inconclusive" in resp.text
    assert "failed" in resp.text
