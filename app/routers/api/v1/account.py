"""Retention endpoints: everything a signed-in user manages about themselves."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.coupon import Coupon
from app.models.product import Product
from app.models.store import Store
from app.models.tracking import (
    AlertEvent,
    NotificationPreference,
    SavedCoupon,
    SavedStore,
    TrackedProduct,
)
from app.models.user import User
from app.schemas.tracking import (
    AlertEventRead,
    NotificationPreferenceRead,
    NotificationPreferenceUpdate,
    PushPublicKeyRead,
    PushSubscription,
    PushSubscriptionRead,
    SavedItemRead,
    TrackedProductCreate,
    TrackedProductRead,
    TrackedProductUpdate,
)
from app.services.push import load_subscription

router = APIRouter(prefix="/me", tags=["me"])


# ---- Saved stores ----


@router.get("/saved-stores", response_model=list[SavedItemRead])
async def list_saved_stores(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[SavedItemRead]:
    result = await db.execute(select(SavedStore).where(SavedStore.user_id == user.id))
    rows = result.scalars().all()
    return [SavedItemRead(kind="store", item_id=row.store_id, saved_id=row.id) for row in rows]


@router.post("/saved-stores/{store_id}", status_code=status.HTTP_201_CREATED)
async def save_store(
    store_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    if await db.get(Store, store_id) is None:
        raise HTTPException(status_code=404, detail="Store not found")
    exists = await db.execute(
        select(SavedStore).where(SavedStore.user_id == user.id, SavedStore.store_id == store_id)
    )
    if exists.scalar_one_or_none() is None:
        db.add(SavedStore(user_id=user.id, store_id=store_id))
        await db.commit()
    return {"status": "saved"}


@router.delete("/saved-stores/{store_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unsave_store(
    store_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(
        select(SavedStore).where(SavedStore.user_id == user.id, SavedStore.store_id == store_id)
    )
    row = result.scalar_one_or_none()
    if row is not None:
        await db.delete(row)
        await db.commit()


# ---- Saved coupons ----


@router.get("/saved-coupons", response_model=list[SavedItemRead])
async def list_saved_coupons(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[SavedItemRead]:
    result = await db.execute(select(SavedCoupon).where(SavedCoupon.user_id == user.id))
    return [
        SavedItemRead(kind="coupon", item_id=row.coupon_id, saved_id=row.id)
        for row in result.scalars().all()
    ]


@router.post("/saved-coupons/{coupon_id}", status_code=status.HTTP_201_CREATED)
async def save_coupon(
    coupon_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    if await db.get(Coupon, coupon_id) is None:
        raise HTTPException(status_code=404, detail="Coupon not found")
    exists = await db.execute(
        select(SavedCoupon).where(
            SavedCoupon.user_id == user.id, SavedCoupon.coupon_id == coupon_id
        )
    )
    if exists.scalar_one_or_none() is None:
        db.add(SavedCoupon(user_id=user.id, coupon_id=coupon_id))
        await db.commit()
    return {"status": "saved"}


@router.delete("/saved-coupons/{coupon_id}", status_code=status.HTTP_204_NO_CONTENT)
async def unsave_coupon(
    coupon_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(
        select(SavedCoupon).where(
            SavedCoupon.user_id == user.id, SavedCoupon.coupon_id == coupon_id
        )
    )
    row = result.scalar_one_or_none()
    if row is not None:
        await db.delete(row)
        await db.commit()


# ---- Tracked products ----


@router.get("/tracked-products", response_model=list[TrackedProductRead])
async def list_tracked(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[TrackedProductRead]:
    result = await db.execute(select(TrackedProduct).where(TrackedProduct.user_id == user.id))
    return [TrackedProductRead.model_validate(row) for row in result.scalars().all()]


@router.post("/tracked-products", response_model=TrackedProductRead, status_code=201)
async def track_product(
    payload: TrackedProductCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TrackedProduct:
    if await db.get(Product, payload.product_id) is None:
        raise HTTPException(status_code=404, detail="Product not found")
    exists = await db.execute(
        select(TrackedProduct).where(
            TrackedProduct.user_id == user.id, TrackedProduct.product_id == payload.product_id
        )
    )
    row = exists.scalar_one_or_none()
    if row is None:
        row = TrackedProduct(
            user_id=user.id, product_id=payload.product_id, target_price=payload.target_price
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
    return row


@router.patch("/tracked-products/{tracked_id}", response_model=TrackedProductRead)
async def update_tracking(
    tracked_id: uuid.UUID,
    payload: TrackedProductUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> TrackedProduct:
    row = await db.get(TrackedProduct, tracked_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status_code=404, detail="Tracked product not found")
    if payload.target_price is not None:
        row.target_price = payload.target_price
    await db.commit()
    await db.refresh(row)
    return row


@router.delete("/tracked-products/{tracked_id}", status_code=status.HTTP_204_NO_CONTENT)
async def untrack_product(
    tracked_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    row = await db.get(TrackedProduct, tracked_id)
    if row is not None and row.user_id == user.id:
        await db.delete(row)
        await db.commit()


# ---- Notification preferences ----


async def _get_prefs(db: AsyncSession, user: User) -> NotificationPreference:
    result = await db.execute(
        select(NotificationPreference).where(NotificationPreference.user_id == user.id)
    )
    prefs = result.scalar_one_or_none()
    if prefs is None:
        prefs = NotificationPreference(user_id=user.id)
        db.add(prefs)
        await db.commit()
        await db.refresh(prefs)
    return prefs


@router.get("/notification-preferences", response_model=NotificationPreferenceRead)
async def get_notification_preferences(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> NotificationPreference:
    return await _get_prefs(db, user)


@router.patch("/notification-preferences", response_model=NotificationPreferenceRead)
async def update_notification_preferences(
    payload: NotificationPreferenceUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NotificationPreference:
    prefs = await _get_prefs(db, user)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(prefs, field, value)
    await db.commit()
    await db.refresh(prefs)
    return prefs


# ---- Browser push ----
#
# Push has its own endpoints rather than living entirely in
# notification-preferences because it has one extra step nothing else does: the
# browser has to hand us a subscription *before* the preference can mean
# anything. Splitting it out keeps that handshake explicit.


@router.get("/push/key", response_model=PushPublicKeyRead)
async def get_push_key() -> PushPublicKeyRead:
    """The VAPID public key.

    Unauthenticated on purpose — the key is public by definition (it ships to
    every browser that subscribes) and the settings page needs it before login
    completes, to decide whether to even offer the push toggle.
    """
    settings = get_settings()
    return PushPublicKeyRead(public_key=settings.vapid_public_key, enabled=settings.push_configured)


@router.put("/push/subscription", response_model=PushSubscriptionRead)
async def set_push_subscription(
    payload: PushSubscription,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PushSubscriptionRead:
    """Register this browser's push subscription.

    Enables push as a side effect, because a subscription the user did not ask
    for is not something anyone stores: the browser only produces one after an
    explicit permission grant, so its arrival *is* the consent.
    """
    settings = get_settings()
    if not settings.push_configured:
        # Failing loudly beats accepting a subscription we could never deliver
        # to, which would leave the UI showing "on" and silently drop alerts.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Push notifications are not configured on this deployment.",
        )

    prefs = await _get_prefs(db, user)
    # Round-trip through push.load_subscription before committing: a subscription
    # that parses as JSON but lacks a key half is the failure mode that makes
    # push_enabled a lie.
    serialised = payload.model_dump_json()
    if load_subscription(serialised) is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Subscription is missing the keys the push service requires.",
        )
    prefs.push_subscription = serialised
    prefs.push_enabled = True
    await db.commit()
    return PushSubscriptionRead(push_enabled=True, has_subscription=True)


@router.delete("/push/subscription", status_code=status.HTTP_204_NO_CONTENT)
async def clear_push_subscription(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Forget this browser's subscription.

    The server-side counterpart of `pushManager.unsubscribe()`. Clearing only in
    the browser leaves the server sending to an endpoint nobody listens to, which
    the push service answers with a 410 on every future alert.
    """
    prefs = await _get_prefs(db, user)
    prefs.push_subscription = None
    prefs.push_enabled = False
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- Alert history ----


@router.get("/alerts", response_model=list[AlertEventRead])
async def list_alerts(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
) -> list[AlertEventRead]:
    result = await db.execute(
        select(AlertEvent)
        .where(AlertEvent.user_id == user.id)
        .order_by(AlertEvent.created_at.desc())
        .limit(limit)
    )
    return [AlertEventRead.model_validate(row) for row in result.scalars().all()]
