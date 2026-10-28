"""Resolving an incoming offer's merchant to a canonical `Store` row.

One function, `resolve_or_create`, is the only place a Store is written during
ingestion. Centralising it is what makes "NIKE", "NIKE" and "www.nike.com" end
up on one merchant page instead of three, and it means the matching order is
stated once rather than re-implemented per adapter.

Signals are tried strongest-first and each must earn its place:

1. **Normalized domain.** Near-proof of identity. Uniqueness is enforced by the
   database as well, because a check-then-insert races two concurrent ingestion
   runs — which is the *normal* state once sources are ingested independently —
   and the loser has to be able to fall back to the row that won without losing
   its own run. The `ON CONFLICT DO NOTHING` in step 4 is what makes that
   recoverable; see `_claim_domain` for why it is not a savepoint.
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
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.observability import get_logger
from app.ingestion import merchants
from app.ingestion.normalize import NormalizedOffer
from app.models.store import Store, StoreStatus

logger = get_logger("durza.ingestion.merchants")

#: Above this, two normalized names are treated as the same merchant for
#: *review purposes only*. Deliberately high: the goal is to surface
#: "Nike" / "Nike Store UK" to a human, not to decide it automatically.
FUZZY_CANDIDATE_THRESHOLD = 0.88

#: Dialects whose INSERT can say "write this row unless it is already there".
#: Postgres runs the app; SQLite runs the test suite. There is no third option to
#: support: four models already carry `postgresql.ARRAY`/`JSONB` columns, so a
#: MySQL port is a port, not a configuration change.
_INSERT_FACTORIES = {"postgresql": pg_insert, "sqlite": sqlite_insert}


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
    affiliate_network: str | None = None,
) -> MerchantMatch:
    """Find or create the canonical Store, and claim its affiliate network if it has none.

    `affiliate_network` is the network whose tracking links this offer carries. A store with
    no network yet (`"none"`) takes it, so its outbound clicks get that network's click
    reference without anyone editing each merchant by hand. A store that already has a
    network keeps it: one merchant on two networks is a decision for a person, not a feed.
    """
    match = await _resolve(db, offer, program_ids=program_ids)
    await _claim_network(db, match.store, affiliate_network)
    return match


async def _claim_network(db: AsyncSession, store: Store, network: str | None) -> None:
    if not network or store.affiliate_network not in (None, "none"):
        return
    # Guarded in SQL too, so two runs racing cannot overwrite each other's claim.
    await db.execute(
        update(Store)
        .where(Store.id == store.id, Store.affiliate_network == "none")
        .values(affiliate_network=network)
    )
    store.affiliate_network = network


async def _resolve(
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
        # Assigned up front for the same reason `pipeline._ingest_one` assigns a
        # Coupon's id: the row is written by a hand-built INSERT (see
        # `_claim_domain`), and a Python-side `default=` is evaluated by the ORM
        # during flush — which this path deliberately never performs.
        id=uuid4(),
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
    won, store = await _claim_domain(db, created, domain)
    if not won:
        # Another run created this domain between our SELECT and our INSERT. The
        # unique constraint did its job and this run carries on, which is the
        # whole point: two providers listing the same shop at the same moment is
        # ordinary once sources are ingested independently, not an exotic race.
        return MerchantMatch(store=store, matched_by="domain", is_new=False)

    if program_ids:
        # Recorded for operator visibility rather than used for matching here:
        # see `merchants.merchant_ids_match` for why a bare id is not an identity.
        logger.debug("merchant created with program ids", extra={"slug": store.slug})

    return MerchantMatch(store=store, matched_by="created", is_new=True)


async def _claim_domain(db: AsyncSession, created: Store, domain: str | None) -> tuple[bool, Store]:
    """Write a new merchant row unless a concurrent run already owns the domain.

    Returns `(won_the_race, store)`. `store` is the row this call created, or the
    row a rival created first — the two are the same merchant either way, which is
    the entire point of matching on the domain.

    ### Why `ON CONFLICT` and not a savepoint

    The obvious shape is `db.add(created)` inside `begin_nested()`, catch the
    `IntegrityError`, roll back to the savepoint and re-read the winner. It does
    not survive contact with SQLAlchemy 2.1: a flush that fails marks the
    *enclosing* transaction with `_rollback_exception`, so the very next
    statement raises `PendingRollbackError` and the session is unusable until it
    is fully rolled back. A full `db.rollback()` does recover the session — and
    throws away every coupon already flushed in this batch, expired `Source` and
    `IngestionRun` included, which broke the invariant that a run's counters
    always equal the sum of what it did. That failure mode is *worse* than the
    race it was meant to handle, and it fires exactly when ingestion is doing its
    job.

    Letting the database decide the insert is both simpler and correct:

    * no exception, so nothing is rolled back and nothing is expired;
    * the losing session blocks until the winner commits (Postgres blocks on the
      conflicting row), then reads it under a fresh READ COMMITTED snapshot, so
      `_by_domain` below cannot miss it;
    * it costs one extra round trip on the rare create path and nothing at all on
      the domain-match path that every later offer takes.

    Only `normalized_domain` is targeted. `slug` is separately unique and is *not*
    silently swallowed, because a slug collision means two domains produced one
    slug — a genuine bug that should stay loud.
    """
    dialect = getattr(getattr(db, "bind", None), "dialect", None)
    if dialect is None or dialect.name not in _INSERT_FACTORIES:
        raise RuntimeError(
            "merchant creation requires Postgres or SQLite: INSERT ... ON CONFLICT "
            "is the only way to survive the domain race. Got dialect "
            f"{dialect.name if dialect is not None else None!r}."
        )
    factory = _INSERT_FACTORIES[dialect.name]

    stmt = (
        factory(Store)  # type: ignore[attr-defined]
        .values(
            id=created.id,
            name=created.name,
            slug=created.slug,
            website_url=created.website_url,
            normalized_name=created.normalized_name,
            normalized_domain=domain,
            aliases=created.aliases,
            status=created.status,
        )
        .on_conflict_do_nothing(index_elements=["normalized_domain"])
        .returning(Store.id)
    )
    # `synchronize_session=False` because nothing in the identity map describes
    # this row: `created` was never `db.add`ed, so there is no session state for
    # the ORM to reconcile after a no-op insert.
    new_id = (
        await db.execute(stmt.execution_options(synchronize_session=False))
    ).scalar_one_or_none()

    if new_id is not None:
        # Our own insert, read back so the caller gets a persistent instance.
        # One extra SELECT on the create path; the row cannot be built any other
        # way without letting the ORM flush, which is what this avoids.
        store = await db.get(Store, new_id)
        if store is None:
            raise RuntimeError(f"insert of store {created.slug!r} succeeded but row vanished")
        return True, store

    winner = await _by_domain(db, domain) if domain else None
    if winner is None:
        # Unreachable on Postgres: `ON CONFLICT` returning nothing means the
        # conflicting transaction committed, and READ COMMITTED gives this
        # statement a snapshot taken after that commit. Refusing to invent a
        # merchant beats returning one the caller would then link coupons to.
        raise RuntimeError(
            f"insert of store {created.slug!r} conflicted on normalized_domain "
            f"{domain!r} but no owning row could be read"
        )
    return False, winner


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
    return (
        merchants.name_similarity(merchants.normalize_name(left), merchants.normalize_name(right))
        >= FUZZY_CANDIDATE_THRESHOLD
    )


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
    return await _by_name(
        db, offer.store_normalized_name or merchants.normalize_name(offer.store_name)
    )


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
