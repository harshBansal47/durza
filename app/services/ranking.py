"""The ranking version, and the guard rails around it.

`RANKING_VERSION` selects which stored score the "recommended" sort reads:
`coupons.quality_score` (v1) or `coupons.quality_score_v2` (v2).

The v2 column is a shadow column. It was added by the migration
`a2b68934f2c5_add_quality_score_v2` and nothing in the codebase writes it, so
it is NULL for every row. That matters because a NULL sorts **first** in a
descending order: `ORDER BY quality_score_v2 DESC` puts every coupon at the
same score, and the "recommended" listing quietly becomes "newest first" — a
listing with no ranking in it, which looks like a working site.

Two guard rails, because they guard different moments:

* **At startup** (`assert_ranking_version_ready`): if v2 is selected and any
  active coupon has no v2 score, the application refuses to start. The
  configuration is wrong — the column was never backfilled — and a loud
  failure is better than a silently wrong ranking. The check only refuses
  when it can *prove* the column is not ready: if the query itself fails (the
  database is unreachable, which is its own incident) the application starts
  anyway and the failure is logged, because refusing to start on a transient
  database error would turn a degraded service into an absent one.

* **At query time** (`ranking_score`): the sort falls back to the v1 score for
  a coupon that has no v2 score. The startup check cannot cover every row — a
  coupon ingested after the check has no v2 score either — and a NULL in the
  sort key would put that coupon at the top of the listing.

Neither guard rail backfills anything, and neither switches the version. The
decision to turn v2 on belongs to the operator, and it belongs to them after
the backfill exists — not to a deploy that guesses.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.coupon import Coupon

#: The ranking versions this codebase knows about.
V1 = "v1"
V2 = "v2"


class RankingVersionNotReadyError(RuntimeError):
    """The configured ranking version cannot be served.

    Raised at startup when `RANKING_VERSION` asks for a score column that the
    data does not have. The message says what is missing and what to do, so
    the failure reads as an instruction rather than a stack trace.
    """


async def count_active_coupons_without_v2_score(db: AsyncSession) -> int:
    """How many active coupons have no v2 score.

    Only active coupons matter: an inactive one is not in any listing, so it
    cannot distort an order.
    """
    stmt = (
        select(func.count())
        .select_from(Coupon)
        .where(Coupon.is_active.is_(True), Coupon.quality_score_v2.is_(None))
    )
    return int((await db.execute(stmt)).scalar_one())


async def assert_ranking_version_ready(db: AsyncSession, *, ranking_version: str) -> int:
    """Check that the configured ranking version can be served.

    Returns the number of active coupons missing a v2 score (zero for v1, which
    has nothing to check). Raises `RankingVersionNotReadyError` when the
    version is v2 and that number is not zero.

    A v1 deployment is untouched by this: no query is run and nothing is
    refused, because v1's column is the one the ingestion pipeline fills.
    """
    if ranking_version != V2:
        return 0

    missing = await count_active_coupons_without_v2_score(db)
    if missing:
        raise RankingVersionNotReadyError(
            f"RANKING_VERSION={V2} was selected, but {missing} active coupon(s) have no "
            f"quality_score_v2. That column is a shadow column: nothing writes it, so the "
            f"ranking would order every coupon at the same score and the listing would "
            f"become 'newest first'. Either run the v2 backfill and set "
            f"RANKING_VERSION={V1} again, or leave RANKING_VERSION={V1} until it is done. "
            f"Nothing is backfilled automatically."
        )
    return missing


def ranking_score(ranking_version: str) -> Any:
    """The expression the ranking sorts by.

    v1 reads the score the pipeline fills. v2 reads the shadow column, but
    falls back to the v1 score for a coupon that has no v2 score yet — a NULL
    would sort first and put that coupon at the top of the listing, which is
    the failure this module exists to prevent.

    The two columns are on different scales until a backfill exists, so a
    mixed order is still a mixed order. That is why the startup check refuses
    a half-backfilled v2 outright: the fallback is a safety net for rows that
    arrive after startup, not a way to run v2.
    """
    if ranking_version == V2:
        return func.coalesce(Coupon.quality_score_v2, Coupon.quality_score)
    return Coupon.quality_score
