"""Retention endpoints: everything a signed-in user manages about themselves."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import noload

from app.core.database import get_db
from app.core.deps import get_current_user
from app.models.coupon import Coupon
from app.models.saved_items import (
    NotificationPreference,
    SavedCoupon,
    SavedStore,
)
from app.models.store import Store
from app.models.user import User
from app.schemas.saved_items import (
    NotificationPreferenceRead,
    NotificationPreferenceUpdate,
    SavedItemRead,
)

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
    # Existence check only: skip the eager `sources` load nothing here reads.
    if await db.get(Coupon, coupon_id, options=[noload(Coupon.sources)]) is None:
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
