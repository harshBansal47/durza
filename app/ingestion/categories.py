"""Resolve a provider's category wording onto our own taxonomy. Never rejects.

The problem this solves: our categories (Beauty, Electronics, Fashion...) were
chosen before any provider was connected, and providers describe the same things in
their own words ("Electronics & Computers Accessories", "Consumer Electronics",
"Home Appliances"). Matching exactly rejected almost everything, which is the wrong
trade. The offer is real; only its filing is unsettled. So this always answers, and
records what it had to guess so a person can correct it once.

For each offer the provider's wording is tried against, strongest first:

1. **the offer's own slug**, if the provider already speaks our taxonomy (a CSV);
2. **an alias a person set** (`manual`), which beats everything else;
3. **one of our category names**, exactly, ignoring case and punctuation;
4. **keyword rules** (`category_rules.py`) or an alias they previously produced;
5. **the source's `default_category_slug`**, if configured;
6. **the fallback category** ("Other"), created on first need.

An offer can carry several wordings (a merchant sells in many categories); each is
resolved on its own and the best-ranked match wins, so one precise wording is not
outvoted by a vague one.

Everything seen is written back to `category_aliases`: rule results so a wrong guess
is visible, and unmatched wording as `pending`, the review queue. Map a pending row in
the admin and later offers follow it, and `refile_coupons` moves the ones already
parked in the fallback.

Writes are `INSERT ... ON CONFLICT`, not a caught `IntegrityError`: two sources
running at once routinely meet the same new wording, and a failed flush poisons the
session and discards the batch (see `merchant_resolution._claim_domain`).
"""

import re
import uuid
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.observability import get_logger
from app.ingestion.category_rules import NOISE, RULES, Rule
from app.models.category import Category
from app.models.category_alias import CategoryAlias, CategoryAliasMethod
from app.models.coupon import Coupon

logger = get_logger("durza.categories")

_INSERT_FACTORIES = {"postgresql": pg_insert, "sqlite": sqlite_insert}

_QUOTES = re.compile(r"['’`]")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# "Computers > Laptops" is a path; each level is a wording of its own.
_HIERARCHY = re.compile(r"[>|/;]")
_KEY_MAX = 160

# Lower is stronger.
_RANK_MANUAL, _RANK_NAME, _RANK_RULE = 0, 1, 2


# --- wording ------------------------------------------------------------------


def normalize_label(text: str) -> str:
    """Lower-case, punctuation out, "&" read as "and", whitespace collapsed."""
    cleaned = _QUOTES.sub("", text.lower().replace("&", " and "))
    return _NON_ALNUM.sub(" ", cleaned).strip()[:_KEY_MAX]


def label_keys(raw_labels: Iterable[Any]) -> list[tuple[str, str]]:
    """`(key, display)` for every distinct, informative wording, order preserved.

    A path like "Computers > Laptops" yields the whole string and each level, so
    either can be mapped. "Other" and its kin say nothing and are dropped.
    """
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for raw in raw_labels:
        if not raw:
            continue
        text = str(raw).strip()
        pieces = [piece.strip() for piece in _HIERARCHY.split(text) if piece.strip()]
        for candidate in [text, *pieces] if len(pieces) > 1 else [text]:
            key = normalize_label(candidate)
            if key and key not in seen and key not in NOISE:
                seen.add(key)
                out.append((key, candidate[:200]))
    return out


# --- rules --------------------------------------------------------------------


@dataclass(frozen=True)
class _Compiled:
    rule: Rule
    pattern: re.Pattern[str]
    order: int


def _compile(rules: Sequence[Rule]) -> list[_Compiled]:
    compiled: list[_Compiled] = []
    for order, rule in enumerate(rules):
        # Longest first, so "home appliance" is tried before "appliance".
        words = sorted({normalize_label(k) for k in rule.keywords}, key=lambda w: (-len(w), w))
        alternation = "|".join(re.escape(word) for word in words)
        # Whole words, with an optional plural: "computer" also matches "computers".
        compiled.append(_Compiled(rule, re.compile(rf"\b({alternation})(?:s|es)?\b"), order))
    return compiled


_COMPILED = _compile(RULES)


def rule_scores(key: str) -> list[tuple[int, int, Rule]]:
    """Rules that match `key`, best first, as `(score, rule order, rule)`.

    The score is the number of words matched, so a specific phrase outranks a bare
    word; ties go to the earlier rule.
    """
    scored: list[tuple[int, int, Rule]] = []
    for compiled in _COMPILED:
        matched = {m.group(1) for m in compiled.pattern.finditer(key)}
        if matched:
            scored.append(
                (sum(len(word.split()) for word in matched), compiled.order, compiled.rule)
            )
    scored.sort(key=lambda item: (-item[0], item[1]))
    return scored


# --- the resolver -------------------------------------------------------------


@dataclass(frozen=True)
class Resolution:
    """Where an offer was filed, and why. Plain values: safe to keep across commits."""

    id: uuid.UUID
    slug: str
    method: str  # slug | alias | name | rule | default | fallback
    label: str | None = None


@dataclass(frozen=True)
class _Hit:
    category_id: uuid.UUID
    method: str
    rank: int
    label: str


@dataclass
class _Seen:
    label: str
    count: int = 0
    category_id: uuid.UUID | None = None
    method: str = CategoryAliasMethod.pending.value
    source: str | None = None


_UNSET: Any = object()


class CategoryResolver:
    """One per ingestion run: loads the taxonomy once, then answers from memory."""

    def __init__(self, *, fallback_slug: str | None = None) -> None:
        self._fallback_slug = fallback_slug
        self._loaded = False
        self._by_id: dict[uuid.UUID, str] = {}  # id -> slug
        self._by_slug: dict[str, uuid.UUID] = {}
        self._by_name: dict[str, uuid.UUID] = {}
        self._aliases: dict[str, tuple[uuid.UUID | None, str]] = {}
        self._cache: dict[str, _Hit | None] = {}
        self._seen: dict[str, _Seen] = {}
        self.counts: Counter[str] = Counter()
        self.new_pending: list[str] = []

    # loading ------------------------------------------------------------------

    def _register(self, category_id: uuid.UUID, slug: str, name: str) -> None:
        self._by_id[category_id] = slug
        self._by_slug[slug] = category_id
        for text in (name, slug):
            self._by_name.setdefault(normalize_label(text), category_id)

    async def _load(self, db: AsyncSession) -> None:
        if self._loaded:
            return
        for category_id, slug, name in (
            await db.execute(select(Category.id, Category.slug, Category.name))
        ).all():
            self._register(category_id, slug, name)
        for alias, mapped_id, method in (
            await db.execute(
                select(CategoryAlias.alias, CategoryAlias.category_id, CategoryAlias.method)
            )
        ).all():
            self._aliases[alias] = (mapped_id, method)
        self._loaded = True

    async def _fallback(self, db: AsyncSession) -> uuid.UUID:
        slug = self._fallback_slug or get_settings().ingestion_fallback_category_slug
        found = self._by_slug.get(slug)
        if found is not None:
            return found
        statement = (
            _insert(db, Category)
            .values(id=uuid.uuid4(), name=slug.replace("-", " ").title(), slug=slug)
            .on_conflict_do_nothing(index_elements=["slug"])
        )
        await db.execute(statement.execution_options(synchronize_session=False))
        row = (
            await db.execute(
                select(Category.id, Category.slug, Category.name).where(Category.slug == slug)
            )
        ).one()
        self._register(row.id, row.slug, row.name)
        logger.info("fallback category ready", extra={"slug": slug})
        return row.id

    # matching -----------------------------------------------------------------

    def _target(self, rule: Rule) -> uuid.UUID | None:
        for target in rule.targets:
            found = self._by_slug.get(target) or self._by_name.get(normalize_label(target))
            if found is not None:
                return found
        return None

    def _lookup(self, key: str, label: str) -> _Hit | None:
        cached = self._cache.get(key, _UNSET)
        if cached is not _UNSET:
            return cached  # type: ignore[no-any-return]
        hit = self._lookup_uncached(key, label)
        self._cache[key] = hit
        return hit

    def _lookup_uncached(self, key: str, label: str) -> _Hit | None:
        known = self._aliases.get(key)
        if known is not None and known[0] in self._by_id:
            manual = known[1] == CategoryAliasMethod.manual.value
            return _Hit(
                known[0],  # type: ignore[arg-type]
                "alias" if manual else "rule",
                _RANK_MANUAL if manual else _RANK_RULE,
                label,
            )
        by_name = self._by_name.get(key)
        if by_name is not None:
            return _Hit(by_name, "name", _RANK_NAME, label)
        for _score, _order, rule in rule_scores(key):
            target = self._target(rule)
            if target is not None:
                return _Hit(target, "rule", _RANK_RULE, label)
        return None

    def _observe(self, key: str, label: str, hit: _Hit | None, source: str | None) -> None:
        # A match on one of our own category names needs no alias row.
        if key not in self._aliases and hit is not None and hit.method == "name":
            return
        seen = self._seen.setdefault(key, _Seen(label=label))
        seen.count += 1
        seen.source = source
        if hit is not None and hit.method == "rule":
            seen.category_id = hit.category_id
            seen.method = CategoryAliasMethod.rule.value

    async def resolve_labels(
        self,
        db: AsyncSession,
        labels: Iterable[Any],
        *,
        category_slug: str | None = None,
        default_slug: str | None = None,
        source_slug: str | None = None,
    ) -> Resolution:
        await self._load(db)

        if category_slug and category_slug in self._by_slug:
            return self._done(self._by_slug[category_slug], "slug", None)

        best: tuple[int, int, _Hit] | None = None
        for order, (key, display) in enumerate(label_keys(labels)):
            hit = self._lookup(key, display)
            self._observe(key, display, hit, source_slug)
            if hit is not None and (best is None or (hit.rank, order) < best[:2]):
                best = (hit.rank, order, hit)
        if best is not None:
            return self._done(best[2].category_id, best[2].method, best[2].label)

        if default_slug and default_slug in self._by_slug:
            return self._done(self._by_slug[default_slug], "default", None)
        return self._done(await self._fallback(db), "fallback", None)

    async def resolve(self, db: AsyncSession, offer: Any, source: Any) -> Resolution:
        """File a normalised offer. Always returns a category."""
        return await self.resolve_labels(
            db,
            [offer.category_name, *offer.source_categories],
            category_slug=offer.category_slug,
            default_slug=(source.config or {}).get("default_category_slug"),
            source_slug=source.slug,
        )

    def _done(self, category_id: uuid.UUID, method: str, label: str | None) -> Resolution:
        self.counts[method] += 1
        return Resolution(id=category_id, slug=self._by_id[category_id], method=method, label=label)

    # bookkeeping --------------------------------------------------------------

    async def flush(self, db: AsyncSession) -> None:
        """Write what was seen: new wordings, and counters for known ones."""
        if not self._seen:
            return
        seen, self._seen = self._seen, {}
        now = datetime.now(UTC)
        for key, item in seen.items():
            known = self._aliases.get(key)
            if known is None:
                statement = (
                    _insert(db, CategoryAlias)
                    .values(
                        id=uuid.uuid4(),
                        alias=key,
                        label=item.label[:200],
                        category_id=item.category_id,
                        method=item.method,
                        seen_count=item.count,
                        last_seen_at=now,
                        last_source=item.source,
                    )
                    .on_conflict_do_update(
                        index_elements=["alias"],
                        set_={
                            "seen_count": CategoryAlias.seen_count + item.count,
                            "last_seen_at": now,
                            "last_source": item.source,
                            "updated_at": now,
                        },
                    )
                )
                await db.execute(statement.execution_options(synchronize_session=False))
                self._aliases[key] = (item.category_id, item.method)
                if item.method == CategoryAliasMethod.pending.value:
                    self.new_pending.append(item.label)
                continue

            await db.execute(
                update(CategoryAlias)
                .where(CategoryAlias.alias == key)
                .values(
                    seen_count=CategoryAlias.seen_count + item.count,
                    last_seen_at=now,
                    last_source=item.source,
                    updated_at=now,
                )
                .execution_options(synchronize_session=False)
            )
            # A wording that was unmapped now matches a rule (a rule was added, or
            # the category it targets was created): record the answer.
            if known[0] is None and item.category_id is not None:
                await db.execute(
                    update(CategoryAlias)
                    .where(CategoryAlias.alias == key, CategoryAlias.category_id.is_(None))
                    .values(category_id=item.category_id, method=CategoryAliasMethod.rule.value)
                    .execution_options(synchronize_session=False)
                )
                self._aliases[key] = (item.category_id, CategoryAliasMethod.rule.value)

    def log_summary(self, *, source: str) -> None:
        if not self.counts:
            return
        logger.info(
            "offers filed by category",
            extra={
                "source": source,
                "by_method": dict(self.counts),
                "new_unmapped_wordings": len(self.new_pending),
                "examples": self.new_pending[:10],
            },
        )


def _insert(db: AsyncSession, model: Any) -> Any:
    dialect = getattr(getattr(db, "bind", None), "dialect", None)
    if dialect is None or dialect.name not in _INSERT_FACTORIES:
        raise RuntimeError(
            "category resolution requires Postgres or SQLite: INSERT ... ON CONFLICT is how "
            f"concurrent runs are kept from colliding. Got dialect "
            f"{dialect.name if dialect is not None else None!r}."
        )
    return _INSERT_FACTORIES[dialect.name](model)


# --- moving what is already filed ----------------------------------------------


async def _fallback_id(db: AsyncSession, fallback_slug: str | None) -> uuid.UUID | None:
    slug = fallback_slug or get_settings().ingestion_fallback_category_slug
    return await db.scalar(select(Category.id).where(Category.slug == slug))


async def refile_coupons(
    db: AsyncSession,
    *,
    alias_key: str,
    category_id: uuid.UUID,
    fallback_slug: str | None = None,
    batch: int = 500,
) -> int:
    """Move coupons parked in the fallback onto `category_id`, for one wording.

    Called when a person maps a wording, so offers filed before they decided do not
    stay behind. Deliberately touches only the fallback bucket: a coupon someone or
    some rule already placed elsewhere is not second-guessed.
    """
    fallback_id = await _fallback_id(db, fallback_slug)
    if fallback_id is None or fallback_id == category_id:
        return 0
    rows = (
        await db.execute(
            select(Coupon.id, Coupon.source_categories).where(Coupon.category_id == fallback_id)
        )
    ).all()
    ids = [
        coupon_id
        for coupon_id, categories in rows
        if any(key == alias_key for key, _ in label_keys(categories or []))
    ]
    for start in range(0, len(ids), batch):
        await db.execute(
            update(Coupon)
            .where(Coupon.id.in_(ids[start : start + batch]))
            .values(category_id=category_id)
            .execution_options(synchronize_session=False)
        )
    await db.commit()
    return len(ids)


async def recategorize_fallback(
    db: AsyncSession, resolver: CategoryResolver, *, dry_run: bool = False
) -> dict[str, int]:
    """Re-run the resolver over everything parked in the fallback category.

    For after the rules or the taxonomy change: add a "Home & Kitchen" category and
    the coupons that were waiting for it can move. Returns `{category slug: moved}`.
    """
    fallback_id = await _fallback_id(db, resolver._fallback_slug)
    if fallback_id is None:
        return {}
    rows = (
        await db.execute(
            select(Coupon.id, Coupon.source_categories).where(Coupon.category_id == fallback_id)
        )
    ).all()
    moves: dict[uuid.UUID, list[uuid.UUID]] = {}
    for coupon_id, categories in rows:
        if not categories:
            continue
        resolution = await resolver.resolve_labels(db, categories, source_slug="recategorize")
        if resolution.id != fallback_id:
            moves.setdefault(resolution.id, []).append(coupon_id)
    summary: dict[str, int] = {}
    for category_id, ids in moves.items():
        summary[resolver._by_id[category_id]] = len(ids)
        if not dry_run:
            await db.execute(
                update(Coupon)
                .where(Coupon.id.in_(ids))
                .values(category_id=category_id)
                .execution_options(synchronize_session=False)
            )
    if dry_run:
        await db.rollback()
    else:
        await resolver.flush(db)
        await db.commit()
    return summary


__all__ = [
    "CategoryResolver",
    "Resolution",
    "label_keys",
    "normalize_label",
    "recategorize_fallback",
    "refile_coupons",
    "rule_scores",
]
