# Rank diff — `RANKING_VERSION` v1 vs v2

**Written:** 2026-10-22, against `main` = `44aace7`.
**Produced by:** `scripts/rank_diff.py` (the generated section at the end).
**Databases touched while writing this:** none in production. The only
database used was the **disposable local `diag3`** instance in the `pgdiag`
container, for the three-row demonstration of how Postgres orders a NULL
column.

## The answer to "was it ever produced?"

**No.** There is no rank-diff report anywhere in the three repositories, and
there never was one. The search is unambiguous — no file, script, doc or
commit mentions a rank diff:

```
grep -rniE "rank.?diff|rank_diff|ranking_diff" --include='*.py' --include='*.md' \
    --include='*.sh' --include='*.yml' .
# (no matches, in any repository)
```

## Why it was never produced: v2 is a column, not a formula

`RANKING_VERSION` does not select an algorithm. It selects **which stored
column the ranking sorts by**:

| | Column | Written by | Formula |
|---|---|---|---|
| v1 | `coupons.quality_score` | `app/ingestion/pipeline.py:1001` (`score_for_coupon`), rewritten with EPC by `app/services/conversion_sync.py:484` | `app/services/quality.py` — one formula, weights in `QualityWeights` |
| v2 | `coupons.quality_score_v2` | **nobody** | none exists |

- The setting: `app/core/config.py:186` — `ranking_version: str = "v1"`.
- The column: `app/models/coupon.py:168` — nullable, indexed, added by
  `alembic/versions/a2b68934f2c5_add_quality_score_v2.py:17`, and indexed by
  the reconciliation migration `b1c2d3e4f5a6` (`alembic/versions/b1c2d3e4f5a6_reconcile_schema_drift.py:32`).
- The two readers: `app/services/coupon_service.py:69` (the exploration
  sampler) and `app/services/coupon_service.py:198` (the `recommended` sort).
- The writers of v2: **none**. A search for any assignment to the column in
  the whole repository returns nothing:

```
grep -rn "quality_score_v2\s*=" --include='*.py' .   # (no matches)
```

So there is no "v2 ranking" to diff against v1. There is an empty column
waiting for one.

## What switching to v2 today would actually do

The `recommended` sort is `ORDER BY <score> DESC, created_at DESC`. In
Postgres a NULL sorts **first** in a descending order. With the v2 column
empty, every coupon ties at NULL and the listing becomes `created_at DESC` —
newest first, with no ranking in it at all.

Demonstrated on the disposable local database (three rows, all-NULL v2):

```
 key   | v1 | v2_position | v1_position
--------+----+-------------+-------------
 newest | 40 |           1 |           1
 middle | 40 |           2 |           2
 oldest | 40 |           3 |           3
```

And with a populated v2 column, for contrast, the order is driven entirely by
the v2 score and has nothing to do with v1:

```
 key   | v2_position | v1_position
--------+-------------+-------------
 middle |           1 |           2
 oldest |           2 |           3
 newest |           3 |           1
```

**So the switch is not "a slightly different ranking". It is the difference
between a ranking and no ranking.**

## The diff itself

The best data available without touching production is the repository's own
researched offers — `scripts/data/researched_offers.json`, 44 real offers
with a store, a discount, an expiry and a verification status. They are
scored with the shipped formula (`app.services.quality.compute_quality`, the
default weights) and ordered exactly as the API orders them.

## The generated diff

The section below is the output of `scripts/rank_diff.py` on the repository
dataset, verbatim. Its headline: **the two
orders are identical, because the v1 scores of a freshly researched catalogue
are identical too** — every one of these offers is unverified with no votes
and no clicks, so each scores exactly 40. The shipped weights are 70% trust
(verification status + community votes) and 30% EPC; a brand-new offer has
none of those, so v1's own order is chronological as well.

That is worth knowing before any comparison: a diff between two orders that
are both chronological says more about the data than about the algorithms.

## The headline

**Every v2 score is NULL.** `coupons.quality_score_v2` has no writer anywhere in the codebase — it was added by the migration `a2b68934f2c5_add_quality_score_v2` and nothing has ever written to it. `RANKING_VERSION=v2` does not select a different formula; it selects a different column, and that column is empty.

In Postgres a NULL sorts **first** in a descending order, so `ORDER BY quality_score_v2 DESC` puts every coupon at the same score and the listing degenerates into `created_at DESC` — newest first, with no ranking in it. Turning the version over today would not produce a worse ranking; it would produce no ranking.

## The two orders

- Top 24 overlap: **24 of 24**
- Kendall's tau between the two orders: **1.000**
- Candidates that change position: **0 of 44**

| # | v1 (score) | v2 (score) | moved |
|---|---|---|---|
| 1 | Croma: 7.5% instant discount on HDFC Bank & YES Bank (40) | Croma: 7.5% instant discount on HDFC Bank & YES Bank (NULL) |  |
| 2 | Croma: 2,000-15,000 rupee instant discount with IDFC (40) | Croma: 2,000-15,000 rupee instant discount with IDFC (NULL) |  |
| 3 | Reliance Digital: up to 6,000 rupee instant discount (40) | Reliance Digital: up to 6,000 rupee instant discount (NULL) |  |
| 4 | Reliance Digital: 5% cashback or 5X reward points wi (40) | Reliance Digital: 5% cashback or 5X reward points wi (NULL) |  |
| 5 | Reliance Digital: up to 10% bank cashback or 6-month (40) | Reliance Digital: up to 10% bank cashback or 6-month (NULL) |  |
| 6 | Myntra: extra discount on selected styles with code  (40) | Myntra: extra discount on selected styles with code  (NULL) |  |
| 7 | Myntra: extra discount on selected styles with code  (40) | Myntra: extra discount on selected styles with code  (NULL) |  |
| 8 | Myntra: extra discount on selected styles with code  (40) | Myntra: extra discount on selected styles with code  (NULL) |  |
| 9 | Myntra: extra discount on selected styles with code  (40) | Myntra: extra discount on selected styles with code  (NULL) |  |
| 10 | Myntra: up to 500 rupees off your first order with c (40) | Myntra: up to 500 rupees off your first order with c (NULL) |  |
| 11 | Myntra: extra discount on selected styles with code  (40) | Myntra: extra discount on selected styles with code  (NULL) |  |
| 12 | Myntra: extra discount on selected styles with code  (40) | Myntra: extra discount on selected styles with code  (NULL) |  |
| 13 | Myntra: extra discount on selected styles with code  (40) | Myntra: extra discount on selected styles with code  (NULL) |  |
| 14 | Lenskart: up to 60% off on your first order with cod (40) | Lenskart: up to 60% off on your first order with cod (NULL) |  |
| 15 | Lenskart: Buy 1 Get 1 Free with Lenskart Gold Member (40) | Lenskart: Buy 1 Get 1 Free with Lenskart Gold Member (NULL) |  |
| 16 | Lenskart: flat 60% off flash coupon JJFLASH (40) | Lenskart: flat 60% off flash coupon JJFLASH (NULL) |  |
| 17 | Lenskart: frames at 600 rupees with free lenses usin (40) | Lenskart: frames at 600 rupees with free lenses usin (NULL) |  |
| 18 | Lenskart: sunglasses at 800 rupees with code SINGLE (40) | Lenskart: sunglasses at 800 rupees with code SINGLE (NULL) |  |
| 19 | Nykaa: 20% off with HSBC Taj credit cards on orders  (40) | Nykaa: 20% off with HSBC Taj credit cards on orders  (NULL) |  |
| 20 | Nykaa: extra 20% off your first app order (40) | Nykaa: extra 20% off your first app order (NULL) |  |
| 21 | Nykaa: Buy 1 Get 1 on sheet masks (40) | Nykaa: Buy 1 Get 1 on sheet masks (NULL) |  |
| 22 | Nykaa: up to 50% off on bestsellers (40) | Nykaa: up to 50% off on bestsellers (NULL) |  |
| 23 | Nykaa: minimum 20% off on Kay Beauty bestsellers (40) | Nykaa: minimum 20% off on Kay Beauty bestsellers (NULL) |  |
| 24 | Nykaa: up to 15% off on COSRX bestsellers (40) | Nykaa: up to 15% off on COSRX bestsellers (NULL) |  |
| 25 | Nykaa: free gift on orders above 1,099 rupees (Festi (40) | Nykaa: free gift on orders above 1,099 rupees (Festi (NULL) |  |
| 26 | Swiggy One: no delivery fee on Instamart orders abov (40) | Swiggy One: no delivery fee on Instamart orders abov (NULL) |  |
| 27 | Swiggy One: two 150-rupee Dineout coupons every mont (40) | Swiggy One: two 150-rupee Dineout coupons every mont (NULL) |  |
| 28 | Zomato Gold: free delivery within 7 km on orders abo (40) | Zomato Gold: free delivery within 7 km on orders abo (NULL) |  |
| 29 | Zomato Gold: up to 30% extra off at 20,000+ partner  (40) | Zomato Gold: up to 30% extra off at 20,000+ partner  (NULL) |  |
| 30 | MakeMyTrip: up to 15% instant discount with ICICI Ba (40) | MakeMyTrip: up to 15% instant discount with ICICI Ba (NULL) |  |
| 31 | ixigo: up to 6,000 rupees off flights with code MONE (40) | ixigo: up to 6,000 rupees off flights with code MONE (NULL) |  |
| 32 | ixigo: up to 5,000 rupees off international flights  (40) | ixigo: up to 5,000 rupees off international flights  (NULL) |  |
| 33 | ixigo: up to 25% off flights with code INSTANT (40) | ixigo: up to 25% off flights with code INSTANT (NULL) |  |
| 34 | ixigo: flat 12% off your first flight booking with c (40) | ixigo: flat 12% off your first flight booking with c (NULL) |  |
| 35 | ixigo assured: free cancellation and vouchers worth  (40) | ixigo assured: free cancellation and vouchers worth  (NULL) |  |
| 36 | ixigo: exclusive discounts on select OYO properties (40) | ixigo: exclusive discounts on select OYO properties (NULL) |  |
| 37 | ixigo: Great Victory Sale - flat 80% off hotels with (40) | ixigo: Great Victory Sale - flat 80% off hotels with (NULL) |  |
| 38 | ixigo: flat 10% off domestic hotels with RBL Bank cr (40) | ixigo: flat 10% off domestic hotels with RBL Bank cr (NULL) |  |
| 39 | ixigo: flat 10% off up to 2,500 rupees on internatio (40) | ixigo: flat 10% off up to 2,500 rupees on internatio (NULL) |  |
| 40 | ixigo: flat 12% off domestic flights & hotels with I (40) | ixigo: flat 12% off domestic flights & hotels with I (NULL) |  |
| 41 | ixigo: flat 12% off flights & hotels with Federal Ba (40) | ixigo: flat 12% off flights & hotels with Federal Ba (NULL) |  |
| 42 | ixigo: 10% discount up to 1,000 rupees with ICICI Ba (40) | ixigo: 10% discount up to 1,000 rupees with ICICI Ba (NULL) |  |
| 43 | ixigo: 12% discount on flights with ICICI Bank credi (40) | ixigo: 12% discount on flights with ICICI Bank credi (NULL) |  |
| 44 | FirstCry: 30% off clothes & shoes with a Mamy Poko d (40) | FirstCry: 30% off clothes & shoes with a Mamy Poko d (NULL) |  |

## What the v1 order itself rests on

The v1 scores of this dataset take **1 distinct value(s)** across 44 candidates (all 40).

So the v1 order is chronological, not a ranking: the shipped weights are 70% trust (verification status + community votes) and 30% EPC, and a freshly researched offer has neither — no verification, no votes, no clicks. Every such offer scores the same, and the tie-break is `created_at DESC`. That is worth knowing before comparing it with anything: a diff between two orders that are both chronological shows less about the algorithms than about the data.

## What a v2 would have to decide

1. **The formula.** There is no second one in the code. The weights are
   settings, so a v2 could be a weight change rather than new code — but that
   decision has not been made or written down anywhere.
2. **The backfill.** Whatever v2 is, it must be computed for every coupon
   before the switch, or the column stays NULL and the listing degenerates as
   described above. The backfill has to be re-runnable, because the scores
   change as verification, votes and clicks arrive.
3. **One writer.** Today `quality_score` has two writers — the ingestion
   pipeline and the conversion sync (with EPC) — so the same column holds
   slightly different scores depending on which one ran last. A v2 column
   should have a single owner, or the same ambiguity follows it.
4. **A shadow column is not free.** It is an indexed column written by nobody;
   every row in it is storage and index weight for nothing.

Until those are done, `RANKING_VERSION` should stay `v1`: v1 is the column
the pipeline actually fills.

## Reproducing this against production (read-only, for the operator)

The script's database mode issues **SELECTs only** — it reads the two score
columns and lets Postgres do the ordering, so the NULL semantics come from
the database itself:

```bash
uv run python scripts/rank_diff.py \
    --database-url "postgresql+asyncpg://USER:PASSWORD@HOST/DB" \
    --out docs/RANK_DIFF_v1_v2.md
```

It opens a connection, reads `coupons` and writes a markdown file. It never
writes to the database, never changes the schema and never runs a migration.
If you would rather not hand credentials to a script, the equivalent by hand
is two SELECTs:

```sql
-- v1 order
SELECT slug, quality_score, quality_score_v2
FROM coupons
WHERE is_active AND (expires_at IS NULL OR expires_at > now())
ORDER BY quality_score DESC, created_at DESC
LIMIT 24;

-- v2 order
SELECT slug, quality_score, quality_score_v2
FROM coupons
WHERE is_active AND (expires_at IS NULL OR expires_at > now())
ORDER BY quality_score_v2 DESC, created_at DESC
LIMIT 24;

-- how many rows even carry a v2 score
SELECT count(*) AS total, count(quality_score_v2) AS with_v2 FROM coupons;
```

That last query is the one that settles the question: if `with_v2` is 0, the
shadow column is empty in production too, and the switch is not ready.
