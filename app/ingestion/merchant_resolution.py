"""Resolving an incoming offer's merchant to a canonical `Store` row.

One function, `resolve_or_create`, is the only place a Store is written during
ingestion. Centralising it is what makes "NIKE", "NIKE" and "www.nike.com" end
up on one merchant page instead of three, and it means the matching order is
stated once rather than re-implemented per adapter.

Signals are tried strongest-first and each must earn its place:

1. **Normalized domain.** Near-proof of identity. Uniqueness is enforced by the
   database as well, because a check-then-insert races two concurrent ingestion
   runs and the loser would otherwise create a duplicate that no amount of
   application-level matching can undo afterwards.
2. **A known alias.** Human-curated, so it can encode knowledge no algorithm has
   ("NIKE" and "Nike Official Store UK" are one merchant).
3. **Exact normalized name.** Case and marketing-suffix differences only.
4. **Fuzzy name match — off.** `should_fuzzy_match` exists so a duplicate-review
   queue can *propose* candidates, and is deliberately never called on the
   ingestion path. Merging two retailers called "Prime" is unrecoverable damage:
   every coupon on the merged page is then wrong for half the audience.

Each step is commented with the failure it prevents, because the ordering looks
over-elaborate until you have debugged a merchant page that had 40 coupons, none
of which worked.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.observability import get_logger
from app.ingestion import merchants
from app.ingestion.normalize import NormalizedOffer
from app.models.store import Store, StoreStatus

logger = get_logger("couponza.ingestion.merchants")

#: Above this, two normalized names are treated as the same merchant for
#: *review purposes only*. Deliberately high: the goal is to surface
#: "Nike" / "Nike Store UK" to a human, not to decide it automatically.
FUZZY_CANDIDATE_THRESHOLD = 0.88


@dataclass(frozen=True)
class MerchantMatch:
    """How a merchant was identified. Carried, not discarded.

    `matched_by` is what tells an operator *why* two providers' coupons ended up
    on one page, and what lets a wrong merge be undone: a domain match is
    near-certain, an alias match is a claim a human made, and those deserve
    different levels of review when one turns out to be wrong.
    """

    store: Store
    matched_by: str  # "created" | "domain" | "alias" | "name"
    is_new: bool


async def resolve_or_create(
    db: AsyncSession,
    offer: NormalizedOffer,
    *,
    program_ids: dict[str, str] | None = None,
) -> MerchantMatch:
    """Find or create the canonical Store for an offer.

    `program_ids` maps provider -> advertiser/program id. Only ever *narrows*
    the search (see `merchants.merchant_ids_match`): a cross-network id
    coincidence must not merge two retailers.
    """
    domain = offer.store_domain
    normalized_name = offer.store_normalized_name or merchants.normalize_name(offer.store_name)

    # ---- 1. Domain ---------------------------------------------------------
    if domain:
        found = await _by_domain(db, domain)
        if found is not None:
            await _learn_aliases(db, found, offer.store_name)
            return MerchantMatch(store=found, matched_by="domain", is_new=False)

    # ---- 2. Alias ----------------------------------------------------------
    alias_hit = await _by_alias(db, offer.store_name, normalized_name, domain)
    if alias_hit is not None:
        # An alias match with no domain on the offer: record the domain we now
        # know, so the next provider to arrive matches on the strong signal
        # instead of the weak one.
        if alias_hit.normalized_domain is None and domain:
            alias_hit.normalized_domain = domain
        await db.flush()
        return MerchantMatch(store=alias_hit, matched_by="alias", is_new=False)

    # ---- 3. Exact normalized name -----------------------------------------
    name_hit = await _by_name(db, normalized_name)
    if name_hit is not None:
        # Only merge on name when we can also rule out a domain conflict. Two
        # stores sharing a name but pointing at different domains are different
        # businesses ("Nike" in the US and "Nike" in the UK), and merging them
        # would show a code that only works in one market on both.
        if domain and name_hit.normalized_domain and name_hit.normalized_domain != domain:
            logger.info(
                "name match rejected: conflicting domains",
                extra={
                    "existing_domain": name_hit.normalized_domain,
                    "incoming_domain": domain,
                    "merchant_name": normalized_name,
                },
            )
        else:
            if name_hit.normalized_domain is None and domain:
                name_hit.normalized_domain = domain
                await db.flush()
            return MerchantMatch(store=name_hit, matched_by="name", is_new=False)

    # ---- 4. Create ---------------------------------------------------------
    created = Store(
        name=offer.store_name,
        slug=offer.store_slug,
        website_url=offer.store_website or (f"https://{domain}" if domain else None),
        normalized_name=normalized_name,
        normalized_domain=domain,
        aliases=[offer.store_name] if offer.store_name else [],
        # A merchant seen for the first time in one provider's feed is not yet
        # corroborated by anything. `pending` keeps it off public merchant pages
        # until a second source agrees, which is what stops a feed typo from
        # producing a public page for a shop that does not exist.
        status=StoreStatus.active if domain else StoreStatus.pending,
    )
    db.add(created)
    try:
        await db.flush()
    except IntegrityError:
        # Lost a race: another ingestion run created this domain between our
        # SELECT and our INSERT. The unique constraint did its job. Fall back to
        # reading the row that now exists rather than failing the run — two
        # providers syncing at once is normal operation, not an error.
        await db.rollback()
        if domain:
            raced = await _by_domain(db, domain)
            if raced is not None:
                return MerchantMatch(store=raced, matched_by="domain", is_new=False)
        raise

    if program_ids:
        # Recorded for operator visibility rather than used for matching here:
        # see `merchants.merchant_ids_match` for why a bare id is not an identity.
        logger.debug("merchant created with program ids", extra={"slug": created.slug})

    return MerchantMatch(store=created, matched_by="created", is_new=True)


async def _by_domain(db: AsyncSession, domain: str) -> Store | None:
    result = await db.execute(select(Store).where(Store.normalized_domain == domain))
    return result.scalar_one_or_none()


async def _by_alias(
    db: AsyncSession, raw_name: str | None, normalized_name: str, domain: str | None
) -> Store | None:
    """Match against recorded aliases, case-insensitively.

    Only stores that actually carry a domain are eligible when the offer has
    one. An alias is a claim about a merchant that predates knowing its domain,
    and trusting it over a concrete domain conflict is how an alias table
    quietly becomes wrong.
    """
    if not raw_name and not normalized_name:
        return None

    candidates = {value for value in (raw_name, normalized_name) if value}
    if not candidates:
        return None

    result = await db.execute(
        select(Store).where(Store.aliases.is_not(None), Store.status != StoreStatus.merged)
    )
    for store in result.scalars().all():
        for alias in store.aliases or []:
            alias_normalized = merchants.normalize_name(alias)
            if alias_normalized not in candidates and alias.strip().lower() not in {
                value.strip().lower() for value in candidates
            }:
                continue
            if domain and store.normalized_domain and store.normalized_domain != domain:
                continue
            return store
    return None


async def _by_name(db: AsyncSession, normalized_name: str) -> Store | None:
    if not normalized_name:
        return None
    result = await db.execute(
        select(Store).where(
            Store.normalized_name == normalized_name, Store.status != StoreStatus.merged
        )
    )
    stores = list(result.scalars().all())
    if not stores:
        return None
    # Several stores sharing a normalized name is the ambiguous case. Returning
    # the oldest (the default order) is deterministic; the caller re-checks the
    # domain and refuses the match when it conflicts, and otherwise prefers a
    # row that already has a domain at all.
    with_domain = [s for s in stores if s.normalized_domain]
    return with_domain[0] if with_domain else stores[0]


async def _learn_aliases(db: AsyncSession, store: Store, provider_name: str | None) -> None:
    """Record a provider's spelling of a merchant we already know.

    Cheap, and it is what makes the next run match on the alias path instead of
    the name path — so a merchant becomes robust against one provider renaming
    itself, and a later provider using a third spelling still resolves.
    """
    if not provider_name or provider_name == store.name:
        return
    aliases = list(store.aliases or [])
    if provider_name not in aliases:
        aliases.append(provider_name[:160])
        store.aliases = aliases[:20]


async def find_fuzzy_candidates(
    db: AsyncSession, name: str, *, limit: int = 5
) -> list[tuple[Store, float]]:
    """Stores whose normalized name resembles `name`. **Candidates, not matches.**

    This exists for an admin duplicate-review queue: "these 340 stores may be
    duplicates" is answerable with a scan, whereas silently merging them is not
    recoverable. Nothing on the ingestion path calls it.
    """
    normalized = merchants.normalize_name(name)
    if not normalized:
        return []

    result = await db.execute(select(Store).where(Store.status != StoreStatus.merged))
    scored: list[tuple[Store, float]] = []
    for store in result.scalars().all():
        score = merchants.name_similarity(normalized, store.normalized_name or "")
        if score >= FUZZY_CANDIDATE_THRESHOLD:
            scored.append((store, round(score, 3)))
    scored.sort(key=lambda pair: pair[1], reverse=True)
    return scored[:limit]


def should_fuzzy_match(left: str, right: str) -> bool:
    """Whether two names are close enough to *propose* a merge to a human.

    Exposed as a named predicate, and documented as advisory, precisely so that
    the moment someone wires it into `resolve_or_create` is a visible decision
    rather than an accident. It is not used there.
    """
    return merchants.name_similarity(
        merchants.normalize_name(left), merchants.normalize_name(right)
    ) >= FUZZY_CANDIDATE_THRESHOLD


def merge_stores(primary: Store, duplicate: Store, *, reason: str) -> None:
    """Fold `duplicate` into `primary`. Returns nothing; caller flushes.

    Kept as an explicit, separate operation rather than something ingestion can
    trigger. Merging is a human decision, it changes every URL that referenced
    the duplicate, and `StoreStatus.merged` plus `merged_into_id` is what keeps
    existing coupon rows resolving to a real merchant afterwards.
    """
    duplicate.status = StoreStatus.merged
    duplicate.merged_into_id = str(primary.id)
    duplicate.is_active = False
    aliases = list(primary.aliases or [])
    for alias in [duplicate.name, *(duplicate.aliases or [])]:
        if alias and alias not in aliases:
            aliases.append(alias[:160])
    primary.aliases = aliases[:20]
    logger.info(
        "stores merged",
        extra={
            "primary": primary.slug,
            "duplicate": duplicate.slug,
            "reason": reason,
        },
    )


async def get_store_for_offer(db: AsyncSession, offer: NormalizedOffer) -> Store | None:
    """Convenience for callers that only need the row and cannot create one."""
    if offer.store_domain:
        found = await _by_domain(db, offer.store_domain)
        if found is not None:
            return found
    return await _by_name(db, offer.store_normalized_name or merchants.normalize_name(offer.store_name))


def search_terms_for(store: Store) -> list[str]:
    """Tokens a search should match this merchant on.

    "Nike", "NIKE", "nike.com", "Nike Official Store" must all find the same
    page. Returns the normalized name, the display name, the domain and every
    alias — the search layer matches on whichever it is given.
    """
    terms: list[str] = [store.name]
    if store.normalized_name:
        terms.append(store.normalized_name)
    if store.normalized_domain:
        terms.append(store.normalized_domain)
    terms.extend(store.aliases or [])
    return [term for term in dict.fromkeys(terms) if term]
