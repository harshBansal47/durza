# Category resolution

Providers describe categories in their own words ("Electronics & Computers
Accessories", "Consumer Electronics", "Home Appliances"); our categories were chosen
before any provider was connected. The pipeline used to match them exactly and
**drop** whatever did not match. It no longer drops anything: an offer is real
whatever its category wording, and only its *filing* is unsettled.

## How an offer is filed

Each wording on the offer is tried, strongest first. The first four are per wording;
the best-ranked one wins across all of an offer's wordings.

1. **The offer's own slug**, when the provider already speaks our taxonomy (a CSV).
2. **An alias a person set** (Admin > Category Aliases). Always wins over rules.
3. **One of our category names**, exactly, ignoring case and punctuation.
4. **Keyword rules** (`app/ingestion/category_rules.py`), or an alias they previously
   produced. "Computers", "Computer Accessories", "Electronics & COMPUTERS
   ACCESORIES" and "Consumer Electronics" all resolve to *Electronics*.
5. **The source's `default_category_slug`**, if you set one.
6. **The fallback category** (`other`), created on first use.

So `category_id` (which is required on every coupon) is always satisfied, and
nothing is rejected for want of a category.

## The review loop

Every wording the pipeline sees is recorded in **Admin > Category Aliases**:

| Status | Meaning | What to do |
|---|---|---|
| **Needs a category** | nothing matched; its offers sit in "Other" | pick a category |
| **Guessed by a rule** | a keyword rule chose it | glance; override if wrong |
| **Set by a person** | you chose it | nothing; it beats every rule |

Sort by *seen count* to do the biggest wordings first. When you choose a category for
a wording, every later offer with that wording follows it, and the offers already
waiting in "Other" with that wording are moved immediately. You can also add a
wording before any provider sends it.

If the category you need does not exist yet, create it (Admin > Categories) first.
Rules for common ones are already written and switch on by themselves when a category
with a matching slug exists:

`home-kitchen`, `health`, `books-education`, `entertainment`, `finance`, `gifts`,
`automotive`, `kids-baby` (plus your existing seven). Until then, "Home Appliances"
files under Electronics, which is a reasonable home for it.

After adding categories or rules, move what was waiting in "Other":

```
podman-compose run --rm --no-deps -T app python -m app.tools.recategorize --dry-run
podman-compose run --rm --no-deps -T app python -m app.tools.recategorize
```

## Changing the rules

Add a keyword to the right rule in `category_rules.py`. For a single odd wording, do
not edit code: map it in the admin. Keywords match whole words (with plurals), and a
longer phrase outscores a shorter one, so "home appliance" beats "appliance" once a
Home & Kitchen category exists.

## What else changed

- **Admitad:** the adapter never read the `categories` Admitad sends with each coupon,
  so every Admitad offer arrived with no category wording. It now passes the coupon's
  and its programme's category names (and parents) through.
- **Logging:** a run logs "offers filed by category" with a count per method and a few
  new unmapped wordings, so you can see it working without opening the admin.
- **Setting:** `INGESTION_FALLBACK_CATEGORY_SLUG` (default `other`).
- **Concurrency:** wordings are written with `INSERT ... ON CONFLICT`, as merchants are.
  Sources run at the same time and routinely meet the same new wording.

## Notes

- "Other" is a real category, so it appears wherever categories are listed. Rename it
  in the admin (keep the slug) or hide it from navigation if you prefer.
- "Other", "Misc", "General offers"-style wordings carry no information and are not
  recorded.
- Deleting a category sends its wordings back to *Needs a category*; nothing is lost.
