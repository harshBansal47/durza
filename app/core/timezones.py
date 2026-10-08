"""Timezone coercion for values that have been through the database.

Every datetime this application writes is UTC-aware. Not every datetime it *reads*
is: a column declared `DateTime(timezone=True)` comes back aware from Postgres and
**naive** from SQLite, which has no timezone type and simply drops the offset. The
test suite runs on in-memory SQLite, so this is not a theoretical concern — it is
the difference between the suite exercising a code path and the suite passing
because it never got there.

The failure mode is always the same and always looks like a mystery: comparing an
aware value with a naive one raises `TypeError: can't compare offset-naive and
offset-aware datetimes`, in the middle of a function that has nothing to do with
timezones.

Assuming a naive datetime is UTC is safe here because that is the only thing we
ever write. A source offering a local-time expiry is converted at the edge, in
`ingestion/normalize.py`, before it becomes a domain value.

This exists because the same three-line check had already been reimplemented in
four places (`services/ad_service`, `services/quality`, `services/price_service`,
`admin/views`). Copies drift: one of them returns the value unchanged, one mutates,
one is typed `Any`. One function with one contract is easier to audit than four
near-identical ones.
"""

from datetime import UTC, datetime


def ensure_utc(value: datetime) -> datetime:
    """Return `value` as an aware UTC datetime, assuming naive means UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def ensure_utc_optional(value: datetime | None) -> datetime | None:
    """`ensure_utc` that passes `None` through — for nullable expiry columns."""
    return None if value is None else ensure_utc(value)
