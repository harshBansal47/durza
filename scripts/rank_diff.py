"""Rank diff: what changes if `RANKING_VERSION` is switched from v1 to v2.

The question this answers is "if we turn the ranking version over, what does
the shopper see?". It is worth asking before the switch and not after,
because the two versions are read from two different stored columns:

* **v1** reads `coupons.quality_score`, written by the ingestion pipeline
  (`score_for_coupon`) and rewritten by the conversion sync with the coupon's
  EPC.
* **v2** reads `coupons.quality_score_v2`, a column added in preparation for a
  second ranking. **Nothing writes it.** There is no second formula in the
  code — "v2" is only a choice of column — so the column stays NULL and the
  v2 order is not a ranking at all.

So the diff is produced from two sources:

* `--dataset` scores a JSON file of offers with the shipped
  `app.services.quality` code, which is the same arithmetic the ingestion
  pipeline performs. This is the baseline: the v1 order of real offers.
* `--database-url` reads a database and asks Postgres for both orders, so the
  NULL semantics of `ORDER BY quality_score_v2 DESC` come from the database
  itself rather than from this script. The queries are SELECTs; the script
  never writes.

Usage:

    # the offers in the repository, scored with the shipped formula
    uv run python scripts/rank_diff.py \
        --dataset scripts/data/researched_offers.json \
        --out docs/RANK_DIFF_v1_v2.md

    # a real database (read-only SELECTs)
    uv run python scripts/rank_diff.py \
        --database-url "postgresql+asyncpg://user:pass@host/db" \
        --out docs/RANK_DIFF_v1_v2.md
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.coupon import VerificationStatus
from app.services.quality import (
    QualityInputs,
    QualityWeights,
    compute_quality,
)

#: How many rows the storefront shows in a "recommended" listing.
PAGE = 24

#: A fixed clock, so the report is reproducible: the scores depend on "now"
#: (recency, expiry urgency), and a report that changes every time it is
#: regenerated cannot be compared with the previous one.
REFERENCE_NOW = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)


@dataclass
class Candidate:
    """One thing that can be ranked."""

    key: str
    title: str
    created_at: datetime | None
    v1: int | None
    v2: int | None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _verification_status(value: str | None) -> VerificationStatus:
    try:
        return VerificationStatus(value or "unverified")
    except ValueError:
        return VerificationStatus.unverified


def candidates_from_dataset(path: Path, *, now: datetime) -> list[Candidate]:
    """Score a JSON file of offers with the shipped formula.

    The inputs are exactly the ones the ingestion pipeline has at the moment a
    coupon is first stored: no community votes, no clicks, no EPC, one source.
    That is the state the ranking has to work with on day one.
    """
    data = json.loads(path.read_text())
    offers = data["offers"] if isinstance(data, dict) else data
    weights = QualityWeights()
    candidates: list[Candidate] = []
    for offer in offers:
        created = _parse_dt(offer.get("checked_on"))
        inputs = QualityInputs(
            verification_status=_verification_status(offer.get("verification")),
            success_rate=None,
            source_count=1,
            source_confidence=0.5,
            expires_at=_parse_dt(offer.get("expires_at")),
            created_at=created,
            last_verified_at=None,
            clicks_count=0,
            success_count=0,
            fail_count=0,
            conversions_count=0,
            merchant_popularity=None,
            discount_value=offer.get("discount_value"),
            discount_type=offer.get("discount_type") or "deal",
            is_deal_without_code=not bool(offer.get("code")),
            expected_revenue=None,
        )
        score, _breakdown = compute_quality(inputs, weights=weights, now=now)
        candidates.append(
            Candidate(
                key=str(offer.get("external_id") or offer.get("title")),
                title=str(offer.get("title") or ""),
                created_at=created,
                v1=score,
                # The shadow column has no writer. In a dataset there is no
                # column at all, so every v2 score is what the database would
                # store: NULL.
                v2=None,
            )
        )
    return candidates


def candidates_from_database(url: str, *, now: datetime) -> list[Candidate]:
    """Read both stored scores from a database and let Postgres do the ordering.

    Read-only: three SELECTs, no writes, no schema changes.
    """
    import asyncio

    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def _read() -> list[Candidate]:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as conn:
                rows = (
                    await conn.execute(
                        text(
                            """
                            SELECT slug,
                                   title,
                                   created_at,
                                   quality_score,
                                   quality_score_v2
                            FROM coupons
                            WHERE is_active
                              AND (expires_at IS NULL OR expires_at > :now)
                            ORDER BY quality_score DESC, created_at DESC
                            LIMIT :limit
                            """
                        ),
                        {"now": now, "limit": PAGE * 5},
                    )
                ).mappings()
                return [
                    Candidate(
                        key=str(row["slug"]),
                        title=str(row["title"] or row["slug"]),
                        created_at=row["created_at"],
                        v1=row["quality_score"],
                        v2=row["quality_score_v2"],
                    )
                    for row in rows
                ]
        finally:
            await engine.dispose()

    return asyncio.run(_read())


def order(candidates: list[Candidate], score: str) -> list[Candidate]:
    """The order the storefront shows, for one version.

    `ORDER BY <score> DESC, created_at DESC` is what the API does. In
    Postgres a NULL sorts **first** in a descending order, so a score column
    that nothing writes puts every row in the same place — and the listing
    becomes "newest first" with no ranking in it at all.
    """
    key = (lambda c: c.v1) if score == "v1" else (lambda c: c.v2)

    def sort_key(candidate: Candidate) -> tuple[int, int, float]:
        value = key(candidate)
        # NULL first, exactly like Postgres DESC.
        null_rank = 0 if value is None else 1
        created = candidate.created_at or datetime.min.replace(tzinfo=UTC)
        return (null_rank, -(value or 0), -created.timestamp())

    return sorted(candidates, key=sort_key)


def kendall_tau(pairs: list[tuple[int, int]]) -> float:
    """Kendall's tau between two orderings of the same items."""
    if len(pairs) < 2:
        return 1.0
    concordant = discordant = 0
    for i in range(len(pairs)):
        for j in range(i + 1, len(pairs)):
            a1, b1 = pairs[i]
            a2, b2 = pairs[j]
            if (a1 < a2 and b1 < b2) or (a1 > a2 and b1 > b2):
                concordant += 1
            else:
                discordant += 1
    total = concordant + discordant
    return (concordant - discordant) / total if total else 1.0


def render(
    candidates: list[Candidate],
    *,
    source: str,
    now: datetime,
    weights: QualityWeights,
) -> str:
    v1_order = order(candidates, "v1")
    v2_order = order(candidates, "v2")

    v1_rank = {c.key: i + 1 for i, c in enumerate(v1_order)}
    v2_rank = {c.key: i + 1 for i, c in enumerate(v2_order)}

    shared = [k for k in v1_rank if k in v2_rank]
    pairs = [(v1_rank[k], v2_rank[k]) for k in shared]
    tau = kendall_tau(pairs)

    v1_top = [c.key for c in v1_order[:PAGE]]
    v2_top = [c.key for c in v2_order[:PAGE]]
    overlap = len(set(v1_top) & set(v2_top))

    scored_v2 = [c for c in candidates if c.v2 is not None]
    missing_v2 = len(candidates) - len(scored_v2)

    moved = [
        (c.key, v1_rank[c.key], v2_rank[c.key])
        for c in candidates
        if c.key in v1_rank and c.key in v2_rank and v1_rank[c.key] != v2_rank[c.key]
    ]

    lines: list[str] = []
    lines.append("# Rank diff — RANKING_VERSION v1 vs v2")
    lines.append("")
    lines.append(
        "**Source:** "
        + source
        + f"  \n**Reference clock:** {now.isoformat()} (fixed, so the report is "
        "reproducible)  \n**Candidates:** " + str(len(candidates))
    )
    lines.append("")
    lines.append(
        "The weights in force: "
        + ", ".join(
            f"`{name}`={value}" for name, value in sorted(weights.__dict__.items()) if value
        )
        + "."
    )
    lines.append("")

    lines.append("## The headline")
    lines.append("")
    if missing_v2 == len(candidates) and candidates:
        lines.append(
            "**Every v2 score is NULL.** `coupons.quality_score_v2` has no writer "
            "anywhere in the codebase — it was added by the migration "
            "`a2b68934f2c5_add_quality_score_v2` and nothing has ever written to "
            "it. `RANKING_VERSION=v2` does not select a different formula; it "
            "selects a different column, and that column is empty."
        )
        lines.append("")
        lines.append(
            "In Postgres a NULL sorts **first** in a descending order, so "
            "`ORDER BY quality_score_v2 DESC` puts every coupon at the same score "
            "and the listing degenerates into `created_at DESC` — newest first, "
            "with no ranking in it. Turning the version over today would not "
            "produce a worse ranking; it would produce no ranking."
        )
    else:
        lines.append(
            f"{scored_v2 and len(scored_v2)} of {len(candidates)} candidates carry a v2 score."
        )
    lines.append("")

    lines.append("## The two orders")
    lines.append("")
    lines.append(
        f"- Top {PAGE} overlap: **{overlap} of {PAGE}**"
        + ("" if len(v1_top) == PAGE else f" (only {len(v1_top)} candidates ranked)")
    )
    lines.append(f"- Kendall's tau between the two orders: **{tau:.3f}**")
    lines.append(f"- Candidates that change position: **{len(moved)} of {len(candidates)}**")
    lines.append("")

    lines.append("| # | v1 (score) | v2 (score) | moved |")
    lines.append("|---|---|---|---|")
    for i in range(max(len(v1_order), len(v2_order))):
        a = v1_order[i] if i < len(v1_order) else None
        b = v2_order[i] if i < len(v2_order) else None
        a_text = f"{a.title[:52]} ({a.v1})" if a else "—"
        b_text = f"{b.title[:52]} ({b.v2 if b.v2 is not None else 'NULL'})" if b else "—"
        moved_text = ""
        if a and b and a.key == b.key and v1_rank[a.key] != v2_rank[b.key]:
            moved_text = f"← {v1_rank[a.key]}"
        lines.append(f"| {i + 1} | {a_text} | {b_text} | {moved_text} |")
    lines.append("")

    v1_scores = [c.v1 for c in candidates if c.v1 is not None]
    if v1_scores:
        distinct = len(set(v1_scores))
        lines.append("## What the v1 order itself rests on")
        lines.append("")
        lines.append(
            f"The v1 scores of this dataset take **{distinct} distinct value(s)** "
            f"across {len(v1_scores)} candidates"
            + (f" (all {v1_scores[0]})." if distinct == 1 else ".")
        )
        lines.append("")
        if distinct == 1:
            lines.append(
                "So the v1 order is chronological, not a ranking: the shipped "
                "weights are 70% trust (verification status + community votes) and "
                "30% EPC, and a freshly researched offer has neither — no "
                "verification, no votes, no clicks. Every such offer scores the "
                "same, and the tie-break is `created_at DESC`. That is worth "
                "knowing before comparing it with anything: a diff between two "
                "orders that are both chronological shows less about the "
                "algorithms than about the data."
            )
            lines.append("")

    lines.append("## What a v2 would have to decide")
    lines.append("")
    lines.append(
        "1. **The formula.** There is no second one in the code. The weights are "
        "settings, so a v2 could be a weight change rather than new code — but "
        "that decision has not been made or written down."
    )
    lines.append(
        "2. **The backfill.** Whatever v2 is, it has to be computed for every "
        "coupon before the switch, or the column stays NULL and the listing "
        "degenerates as described above."
    )
    lines.append(
        "3. **The backfill has to be idempotent and cheap enough to re-run** — "
        "the same column is written by two writers today (ingestion, and the "
        "conversion sync with EPC), so a v2 column needs one owner."
    )
    lines.append("")
    lines.append(
        "Until those are done, `RANKING_VERSION` should stay `v1`: v1 is the "
        "column the pipeline actually fills."
    )
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, help="a JSON file of offers")
    parser.add_argument("--database-url", help="a read-only database URL")
    parser.add_argument("--out", type=Path, help="where to write the markdown")
    parser.add_argument(
        "--now",
        default=REFERENCE_NOW.isoformat(),
        help="the reference clock (default: the fixed reference time)",
    )
    args = parser.parse_args()

    if bool(args.dataset) == bool(args.database_url):
        parser.error("give exactly one of --dataset or --database-url")

    now = datetime.fromisoformat(args.now)
    weights = QualityWeights()

    if args.dataset:
        candidates = candidates_from_dataset(args.dataset, now=now)
        source = f"`{args.dataset}` (repository dataset, scored with the shipped formula)"
    else:
        candidates = candidates_from_database(args.database_url, now=now)
        source = "the database named by `--database-url` (read-only SELECTs)"

    report = render(candidates, source=source, now=now, weights=weights)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report)
        print(f"wrote {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
