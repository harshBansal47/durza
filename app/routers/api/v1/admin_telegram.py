"""Manual Telegram publishing for admins.

Unlike the automated ingestion hook, every message here is sent only after an
admin explicitly approves it from the /admin/telegram-publisher page.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.database import get_db
from app.core.deps import require_role
from app.integrations.telegram import TelegramClient
from app.models.coupon import Coupon
from app.models.store import Store
from app.models.user import Role, User
from app.services.telegram_service import prepare

router = APIRouter(prefix="/admin/telegram", tags=["admin-telegram"])


class SendRequest(BaseModel):
    coupon_id: UUID


class SendResponse(BaseModel):
    message_id: int


@router.post("/send", response_model=SendResponse)
async def send_coupon_to_telegram(
    body: SendRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(require_role(Role.admin)),
) -> SendResponse:
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_channel_id:
        raise HTTPException(status_code=400, detail="Telegram bot token or channel id not configured")

    coupon = await db.get(Coupon, body.coupon_id)
    if coupon is None:
        raise HTTPException(status_code=404, detail="Coupon not found")
    store = await db.get(Store, coupon.store_id)
    if store is None:
        raise HTTPException(status_code=404, detail="Store not found")

    post = prepare(coupon, store, now=__import__("datetime").datetime.now(__import__("datetime").timezone.utc), settings=settings)
    async with TelegramClient(settings.telegram_bot_token, settings.telegram_channel_id) as client:
        message_id = await client.send_message(post.text, button=post.button, silent=False)
    return SendResponse(message_id=message_id)
