"""Posts new offers to the Telegram channel, each at most once.

Derived, not streamed (the same stance as `deal_alert_service`): nothing hooks
into "a coupon was created", because coupons arrive by four routes (ingestion, the
admin, the API, a re-import) and a hook on each is four places to forget. Instead
a scan looks at what is live and new *now*, and the `telegram_posts` ledger is the
memory that keeps a re-scan from re-posting.

What makes something worth posting:

* it is publicly live (active status, active store, started, not expired, not
  about to expire) and not known to be broken;
* it was created recently, so switching this on does not announce last month;
* it is for the channel's country, since the audience is Indian shoppers.

Rules that keep the channel worth following:

* **Once.** A claim row is written before the send and the database's unique
  constraints refuse a second one. The same offer reappearing as a new coupon row
  is recognised by `dedupe_key` and skipped.
* **Not a firehose.** At most `TELEGRAM_MAX_POSTS_PER_DAY` per rolling day,
  best-scored first. Posts are spaced out to stay under Telegram's per-channel
  rate limit, and sent silently overnight.
* **Honest.** "Verified" appears only on an offer that was actually verified in
  the last three days. Every post carries the affiliate disclosure.
"""

import asyncio
import hashlib
import html
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy import delete, exists, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.observability import get_logger
from app.core.timezones import ensure_utc
from app.integrations.telegram import (
    TelegramClient,
    TelegramConfigError,
    TelegramRateLimited,
    TelegramRejected,
    TelegramTemporary,
    TelegramUncertain,
)
from app.models.coupon import (
    PUBLICLY_VISIBLE_STATUSES,
    Coupon,
    DiscountType,
    VerificationStatus,
)
from app.models.store import Store, StoreStatus
from app.models.telegram_post import TelegramPost, TelegramPostStatus

logger = get_logger("durza.telegram")

# India has no daylight saving, so a fixed offset is exact and needs no tz database
# (which a slim container image may not have).
IST = timezone(timedelta(hours=5, minutes=30))

BUTTON_LABEL = "🛒 Get this offer"
_UTM = "utm_source=telegram&utm_medium=channel&utm_campaign=new_offers"
_MAX_TITLE = 120
_VERIFIED_FOR = timedelta(hours=72)
# A claim older than this never got its answer recorded: the process stopped mid-send.
_STALE_PENDING = timedelta(minutes=15)
# A rate-limit wait longer than this is not worth holding a job open for.
_MAX_RETRY_WAIT = 30


class OfferSender(Protocol):
    async def send_message(
        self, text: str, *, button: tuple[str, str] | None = None, silent: bool = False
    ) -> int: ...


@dataclass(frozen=True)
class PreparedPost:
    """Everything needed to post one offer, copied out of the ORM objects.

    Built up front because the loop commits after each offer, and reading an ORM
    attribute after a commit is exactly where async sessions surprise people.
    """

    coupon_id: uuid.UUID
    dedupe_key: str
    store_name: str
    title: str
    text: str
    button: tuple[str, str] | None


# --- identity -----------------------------------------------------------------


def _normalise(text: str) -> str:
    return " ".join(text.lower().split())


def dedupe_key(coupon: Coupon) -> str:
    """What makes two coupon rows *the same offer* for the channel's purposes.

    A code identifies an offer within a store, and the discount is part of the
    identity so that a code whose terms changed is news again. Without a code the
    offer is a deal, identified by its wording.
    """
    value = "" if coupon.discount_value is None else f"{float(coupon.discount_value):g}"
    discount = f"{coupon.discount_type.value}:{value}"
    code = coupon.normalized_code or (coupon.code or "").strip().upper()
    if code:
        material = f"{coupon.store_id}|code|{code}|{discount}"
    else:
        material = f"{coupon.store_id}|deal|{discount}|{_normalise(coupon.title)}"
    return hashlib.sha256(material.encode()).hexdigest()


# --- wording ------------------------------------------------------------------


def _num(value: float) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else f"{number:.1f}"


def _inr(amount: float) -> str:
    """1999 -> '1,999', 100000 -> '1,00,000' (the Indian grouping)."""
    digits = str(round(abs(amount)))
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    groups: list[str] = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join([*groups, tail])


def _discount_tokens(coupon: Coupon) -> tuple[str | None, tuple[str, ...]]:
    """The discount as a phrase ("20% OFF") and the spellings a title might use."""
    value = coupon.discount_value
    if not value:
        return None, ()
    if coupon.discount_type == DiscountType.percentage:
        return f"{_num(value)}% OFF", (f"{_num(value)}%",)
    if coupon.discount_type == DiscountType.fixed:
        return f"₹{_inr(value)} OFF", (_inr(value), str(int(value)))
    return None, ()


def _headline(coupon: Coupon) -> str:
    title = coupon.title.strip()
    if len(title) > _MAX_TITLE:
        title = title[: _MAX_TITLE - 1].rstrip() + "…"
    phrase, tokens = _discount_tokens(coupon)
    # Titles usually already say "20% off"; repeating it reads like spam.
    if phrase and not any(token in title for token in tokens):
        return f"{phrase} — {title}"
    return title


def _clock(moment: datetime) -> str:
    return f"{(moment.hour % 12) or 12}:{moment.minute:02d} {'AM' if moment.hour < 12 else 'PM'}"


def _valid_till(expires_at: datetime, now: datetime) -> str | None:
    end = ensure_utc(expires_at).astimezone(IST)
    today = now.astimezone(IST).date()
    # A far-off date is a placeholder, not a deadline, and "valid till 2099" is noise.
    if end.date() > today + timedelta(days=180):
        return None
    if end.date() == today:
        return f"today, {_clock(end)}"
    if end.date() == today + timedelta(days=1):
        return f"tomorrow, {_clock(end)}"
    label = f"{end.day} {end:%b}"
    return f"{label} {end.year}" if end.year != today.year else label


def _verified_phrase(coupon: Coupon, now: datetime) -> str | None:
    if coupon.verification_status != VerificationStatus.verified or not coupon.last_verified_at:
        return None
    age = now - ensure_utc(coupon.last_verified_at)
    if age > _VERIFIED_FOR:
        return None
    hours = int(age.total_seconds() // 3600)
    if hours < 1:
        return "Verified just now"
    if hours < 24:
        return f"Verified {hours}h ago"
    return f"Verified {hours // 24}d ago"


def offer_url(coupon: Coupon, settings: Settings) -> str:
    """The site's own offer page, not the merchant: the click is ours to measure."""
    return f"{settings.site_url.rstrip('/')}/coupons/{coupon.slug}?{_UTM}"


def prepare(coupon: Coupon, store: Store, *, now: datetime, settings: Settings) -> PreparedPost:
    esc = html.escape
    lines = [f"🛍 <b>{esc(store.name)}</b>", esc(_headline(coupon)), ""]
    if coupon.code:
        lines.append(f"🎟 Code: <code>{esc(coupon.code.strip())}</code>")
    else:
        lines.append("✨ Deal — no code needed")
    verified = _verified_phrase(coupon, now)
    if verified:
        lines.append(f"✅ {verified}")
    if coupon.expires_at is not None:
        valid = _valid_till(coupon.expires_at, now)
        if valid:
            lines.append(f"⏳ Valid till {valid}")

    url = offer_url(coupon, settings)
    # Telegram refuses a button whose URL is not https (a local dev site, say). In
    # that case put the link in the text, so a misconfigured site_url degrades to a
    # plain link instead of a message Telegram rejects outright.
    button = (BUTTON_LABEL, url) if url.startswith("https://") else None
    if button is None:
        lines += ["", f"🔗 {esc(url)}"]
    if settings.telegram_footer:
        lines += ["", f"<i>{esc(settings.telegram_footer)}</i>"]

    return PreparedPost(
        coupon_id=coupon.id,
        dedupe_key=dedupe_key(coupon),
        store_name=store.name,
        title=coupon.title,
        text="\n".join(lines),
        button=button,
    )


def _is_night(now: datetime) -> bool:
    hour = now.astimezone(IST).hour
    return hour >= 23 or hour < 7


# --- selection ----------------------------------------------------------------


async def select_candidates(
    db: AsyncSession, *, now: datetime, limit: int, settings: Settings | None = None
) -> list[tuple[Coupon, Store]]:
    settings = settings or get_settings()
    stmt = (
        select(Coupon, Store)
        .join(Store, Store.id == Coupon.store_id)
        .where(
            Coupon.status.in_(PUBLICLY_VISIBLE_STATUSES),
            Coupon.is_active.is_(True),
            Store.is_active.is_(True),
            Store.status == StoreStatus.active,
            Coupon.created_at >= now - timedelta(hours=settings.telegram_post_window_hours),
            or_(Coupon.starts_at.is_(None), Coupon.starts_at <= now),
            # Not about to lapse: an offer that dies in an hour is a bad post.
            or_(
                Coupon.expires_at.is_(None),
                Coupon.expires_at > now + timedelta(hours=settings.telegram_min_hours_to_expiry),
            ),
            Coupon.verification_status.notin_(
                (VerificationStatus.failed, VerificationStatus.disputed)
            ),
            ~exists().where(TelegramPost.coupon_id == Coupon.id),
        )
        # Best first, so a cap trims the weakest offers and not the strongest.
        .order_by(Coupon.quality_score.desc(), Coupon.created_at.desc())
        .limit(limit)
    )
    if settings.telegram_country:
        country = settings.telegram_country.upper()
        stmt = stmt.where(
            or_(Coupon.country.is_(None), Coupon.country == country),
            or_(Store.country_code.is_(None), Store.country_code == country),
        )
    if settings.telegram_only_verified:
        stmt = stmt.where(Coupon.verification_status == VerificationStatus.verified)
    result = await db.execute(stmt)
    return [(coupon, store) for coupon, store in result.all()]


# --- ledger -------------------------------------------------------------------


async def _claim(db: AsyncSession, post: PreparedPost, chat_id: str) -> uuid.UUID | None:
    """Write the claim. None means the database says someone already has this offer."""
    post_id = uuid.uuid4()
    db.add(
        TelegramPost(
            id=post_id,
            coupon_id=post.coupon_id,
            dedupe_key=post.dedupe_key,
            status=TelegramPostStatus.pending.value,
            chat_id=chat_id,
            store_name=post.store_name[:150],
            title=post.title[:200],
        )
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return None
    return post_id


async def _settle(db: AsyncSession, post_id: uuid.UUID, **values: object) -> None:
    await db.execute(update(TelegramPost).where(TelegramPost.id == post_id).values(**values))
    await db.commit()


async def _release(db: AsyncSession, post_id: uuid.UUID) -> None:
    """Forget a claim whose send definitely did not happen, so the offer is retried."""
    await db.execute(delete(TelegramPost).where(TelegramPost.id == post_id))
    await db.commit()


async def _key_taken(db: AsyncSession, key: str) -> bool:
    found = await db.scalar(select(TelegramPost.id).where(TelegramPost.dedupe_key == key).limit(1))
    return found is not None


async def _mark_duplicate(db: AsyncSession, post: PreparedPost, chat_id: str) -> None:
    # The real key is already taken by the offer that was posted, so this row gets
    # a key of its own. Its job is only to stop this coupon being picked up again.
    db.add(
        TelegramPost(
            coupon_id=post.coupon_id,
            dedupe_key=f"dup:{post.coupon_id}",
            status=TelegramPostStatus.duplicate.value,
            chat_id=chat_id,
            store_name=post.store_name[:150],
            title=post.title[:200],
        )
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()


async def _retire_stale_claims(db: AsyncSession, now: datetime) -> None:
    result = await db.execute(
        update(TelegramPost)
        .where(
            TelegramPost.status == TelegramPostStatus.pending.value,
            TelegramPost.created_at < now - _STALE_PENDING,
        )
        .values(
            status=TelegramPostStatus.uncertain.value,
            error="the process stopped while sending; not re-sent in case it was delivered",
        )
    )
    await db.commit()
    stale = int(result.rowcount or 0)  # type: ignore[attr-defined]
    if stale:
        logger.warning("telegram claims were left mid-send", extra={"count": stale})


async def _send(
    client: OfferSender,
    post: PreparedPost,
    *,
    silent: bool,
    sleep: Callable[[float], Awaitable[None]],
) -> int:
    """Send once; if Telegram says slow down for a moment, wait it out and send once more."""
    try:
        return await client.send_message(post.text, button=post.button, silent=silent)
    except TelegramRateLimited as exc:
        if exc.retry_after > _MAX_RETRY_WAIT:
            raise
        await sleep(exc.retry_after + 1)
        return await client.send_message(post.text, button=post.button, silent=silent)


# --- the run ------------------------------------------------------------------


async def post_new_offers(
    db: AsyncSession,
    *,
    client: OfferSender | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> str:
    """One scan: post what is new and live, within the caps. Returns a summary line."""
    settings = get_settings()
    now = now or datetime.now(UTC)
    if not settings.telegram_channel_enabled:
        return "telegram disabled"
    token, chat_id = settings.telegram_bot_token, settings.telegram_channel_id
    if client is None and (not token or not chat_id):
        logger.warning(
            "telegram is enabled but TELEGRAM_BOT_TOKEN or TELEGRAM_CHANNEL_ID is not set"
        )
        return "telegram not configured"
    chat = chat_id or "-"

    await _retire_stale_claims(db, now)

    sent_today = (
        await db.scalar(
            select(func.count())
            .select_from(TelegramPost)
            .where(
                TelegramPost.status == TelegramPostStatus.sent.value,
                TelegramPost.sent_at >= now - timedelta(hours=24),
            )
        )
        or 0
    )
    budget = settings.telegram_max_posts_per_day - sent_today
    if budget <= 0:
        return "daily cap reached"

    # More than the budget, because some will turn out to be copies of an offer
    # that was already posted and cost nothing.
    candidates = await select_candidates(db, now=now, limit=budget * 4, settings=settings)
    if not candidates:
        return "nothing new"
    prepared = [prepare(coupon, store, now=now, settings=settings) for coupon, store in candidates]

    if client is not None:
        return await _post(db, client, prepared, chat, budget, now, settings, sleep)
    assert token and chat_id
    async with TelegramClient(token, chat_id) as real:
        return await _post(db, real, prepared, chat, budget, now, settings, sleep)


async def _post(
    db: AsyncSession,
    client: OfferSender,
    prepared: list[PreparedPost],
    chat: str,
    budget: int,
    now: datetime,
    settings: Settings,
    sleep: Callable[[float], Awaitable[None]],
) -> str:
    posted = skipped = 0
    seen: set[str] = set()
    silent = settings.telegram_silent_at_night and _is_night(now)

    for index, post in enumerate(prepared):
        if posted >= budget:
            break
        if post.dedupe_key in seen or await _key_taken(db, post.dedupe_key):
            await _mark_duplicate(db, post, chat)
            skipped += 1
            continue
        seen.add(post.dedupe_key)

        post_id = await _claim(db, post, chat)
        if post_id is None:
            skipped += 1
            continue

        try:
            message_id = await _send(client, post, silent=silent, sleep=sleep)
        except TelegramConfigError as exc:
            await _release(db, post_id)
            logger.error(
                "telegram refused the bot; fix the token, chat id or channel permissions",
                extra={"reason": str(exc), "telegram_code": exc.code},
            )
            return f"posted {posted}; stopped: telegram config error ({exc})"
        except (TelegramTemporary, TelegramRateLimited) as exc:
            await _release(db, post_id)
            logger.warning("telegram is not accepting posts right now", extra={"reason": str(exc)})
            return f"posted {posted}; stopped: {exc}"
        except TelegramRejected as exc:
            await _settle(
                db, post_id, status=TelegramPostStatus.rejected.value, error=str(exc)[:300]
            )
            logger.warning(
                "telegram rejected an offer",
                extra={"coupon_id": str(post.coupon_id), "reason": str(exc)},
            )
            continue
        except TelegramUncertain as exc:
            await _settle(
                db, post_id, status=TelegramPostStatus.uncertain.value, error=str(exc)[:300]
            )
            logger.warning(
                "telegram did not confirm an offer; not re-sending in case it was delivered",
                extra={"coupon_id": str(post.coupon_id), "reason": str(exc)},
            )
            return f"posted {posted}; stopped: {exc}"

        await _settle(
            db,
            post_id,
            status=TelegramPostStatus.sent.value,
            message_id=message_id,
            sent_at=datetime.now(UTC),
        )
        posted += 1
        logger.info(
            "offer posted to telegram",
            extra={
                "coupon_id": str(post.coupon_id),
                "store": post.store_name,
                "message_id": message_id,
            },
        )
        # Space posts out for Telegram's per-channel rate limit, but only if another
        # offer is actually queued behind this one.
        if posted < budget and index < len(prepared) - 1:
            await sleep(settings.telegram_send_gap_seconds)

    return f"posted {posted}, skipped {skipped} duplicate(s)"
