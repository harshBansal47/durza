"""Turn messy source data into canonical form before it touches the database.

Two jobs live here, and the split matters:

* **Syntax** — a provider's date format becomes a `datetime`, its discount string
  becomes a type and a number. Pure and total: it either produces a
  `NormalizedOffer` or raises `NormalizeError`. No database, no network.
* **Semantics** — what the values *mean*. A code becomes its canonical identity
  form, a merchant name becomes a domain and a comparable name, a currency
  becomes an ISO code. This is where providers' disagreements are absorbed.

Keeping them separate is what lets the second be tested exhaustively against
recorded provider payloads without a fixture database, and lets a normalization
bug be distinguished from a parsing bug in a log line.
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from slugify import slugify

from app.ingestion import merchants
from app.ingestion.base import RawOffer
from app.models.coupon import DiscountType

# Formats seen across the networks, most specific first. Ordered rather than
# tried in a dict because "%d/%m/%Y" and "%m/%d/%Y" are both valid and produce
# different dates for the same string — an ordering bug here is a silently wrong
# expiry date, which is worse than a rejected one.
_EXPIRY_FORMATS = (
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%d-%m-%Y",
    "%d %b %Y",
    "%d %B %Y",
    "%Y/%m/%d",
    "%d.%m.%Y",
    "%Y%m%d",
)

# Currency symbols to ISO codes. Providers send both a symbol and a code
# depending on the endpoint; this is for the symbol side. Anything absent here
# is passed through unchanged rather than guessed, because a wrong currency code
# renders "₹200 off" as "$200 off", which is worse than showing no currency.
_CURRENCY_SYMBOLS = {
    "₹": "INR",
    "rs": "INR",
    "inr": "INR",
    "$": "USD",
    "usd": "USD",
    "€": "EUR",
    "eur": "EUR",
    "£": "GBP",
    "gbp": "GBP",
    "¥": "JPY",
    "jpy": "JPY",
    "₩": "KRW",
    "₽": "RUB",
}

# Countries a provider may name. Normalized to ISO 3166-1 alpha-2, uppercased.
# Unknown values pass through rather than being dropped — a new market in a
# provider's feed is data we want to see in the admin, not an error.
_COUNTRY_NAMES = {
    "united states": "US",
    "usa": "US",
    "us": "US",
    "united kingdom": "GB",
    "uk": "GB",
    "great britain": "GB",
    "germany": "DE",
    "deutschland": "DE",
    "france": "FR",
    "canada": "CA",
    "australia": "AU",
    "india": "IN",
    "japan": "JP",
    "brazil": "BR",
    "mexico": "MX",
    "netherlands": "NL",
    "spain": "ES",
    "italy": "IT",
    "ireland": "IE",
    "poland": "PL",
    "sweden": "SE",
    "norway": "NO",
    "denmark": "DK",
    "finland": "FI",
    "singapore": "SG",
    "south korea": "KR",
    "korea": "KR",
    "russia": "RU",
    "turkey": "TR",
    "south africa": "ZA",
    "new zealand": "NZ",
}


# Trailing "Z" is legal ISO-8601 but `fromisoformat` only learned to read
# it in 3.11, so it is expanded explicitly rather than relying on the
# interpreter version a deployment happens to run.
_ZULU = re.compile(r"Z$")


class NormalizeError(ValueError):
    """A record cannot be made canonical. Counted, logged, and skipped.

    Raising rather than returning a half-filled offer is deliberate. A coupon
    with a silently-defaulted expiry or a missing discount is worse than an
    absent one: it renders as confident and wrong.
    """


@dataclass
class NormalizedOffer:
    """One offer, in canonical form. Provider-agnostic by construction.

    Nothing here names a network. That is the property the whole architecture
    rests on — a fifth source can be added by writing an adapter and nothing
    downstream has to change, because nothing downstream can tell which of the
    four wrote this row.
    """

    title: str
    code: str | None
    description: str | None
    discount_type: DiscountType
    discount_value: float | None
    currency: str | None

    # Merchant identity. `store_slug` is the historical column name on the
    # canonical Store row; `store_domain` and `store_normalized_name` are the
    # matching signals.
    store_slug: str
    store_name: str
    store_domain: str | None
    store_normalized_name: str

    category_slug: str | None
    category_name: str | None

    destination_url: str
    starts_at: datetime | None
    expires_at: datetime | None
    external_id: str | None
    country: str | None

    # Provider-claimed conditions we cannot derive and must not guess at.
    restrictions: dict[str, Any] = field(default_factory=dict)
    source_categories: list[str] = field(default_factory=list)
    store_website: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def _to_datetime(value: datetime | str | float | None) -> datetime | None:
    """Parse whatever a provider calls a date.

    Accepts an ISO string, a datetime, or a Unix timestamp (seconds or
    milliseconds — the two are distinguished by magnitude, since no plausible
    expiry date in seconds is above 10^11 until the year 5138). Returns an
    aware datetime in UTC, because a naive expiry compared against an aware `now`
    raises rather than silently deciding which one is right.
    """
    if value is None or value == "":
        return None

    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    if isinstance(value, (int, float)):
        # 10^11 seconds is 5138; 10^11 milliseconds is 1973. Everything a
        # provider plausibly sends falls on one side or the other.
        seconds = value / 1000 if abs(value) > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise NormalizeError(f"unparseable timestamp: {value!r}") from exc

    text = str(value).strip()

    # "in 5 days" — a relative expiry some feeds use. Resolved against now rather
    # than rejected: it is a real provider format and it carries real meaning.
    relative = re.fullmatch(r"in (\d+) days?", text, flags=re.IGNORECASE)
    if relative:
        return datetime.now(UTC) + timedelta(days=int(relative.group(1)))

    try:
        parsed = datetime.fromisoformat(_ZULU.sub("+00:00", text))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        pass

    for fmt in _EXPIRY_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue

    raise NormalizeError(f"unparseable date: {value!r}")


def _parse_expires(value: Any) -> datetime | None:
    """Backwards-compatible wrapper for the previous single-purpose parser."""
    return _to_datetime(value)


def normalize_currency(value: str | None) -> str | None:
    """Map a currency symbol, name or code to ISO 4217. Unknown passes through.

    Returning the input unchanged is the honest behaviour: a currency we do not
    recognise is more likely a new one than a typo, and dropping it would lose
    the only clue about which symbol the fixed discount was in.
    """
    if not value:
        return None
    text = value.strip().lower()
    mapped = _CURRENCY_SYMBOLS.get(text)
    if mapped:
        return mapped
    upper = value.strip().upper()
    # A 3-letter code is assumed to be ISO 4217 already.
    return upper if len(upper) == 3 and upper.isalpha() else value.strip()


def normalize_country(value: str | None) -> str | None:
    """Map a country name or code to ISO 3166-1 alpha-2, uppercased."""
    if not value:
        return None
    text = str(value).strip()
    mapped = _COUNTRY_NAMES.get(text.lower())
    if mapped:
        return mapped
    upper = text.upper()
    if len(upper) == 2 and upper.isalpha():
        return upper
    return text or None


def parse_discount(discount_text: str | None) -> tuple[DiscountType, float | None] | None:
    """Extract a type and value from a human discount phrase.

    Order matters and is deliberate:

    * "Deal"-shaped phrases first. "free shipping" and "BOGO" carry no number,
      so a numeric-first order would fall through every branch and report
      "unparseable" for an offer that is perfectly well specified.
    * Percentage before fixed-amount. "20% off" also matches a loose
      currency-amount pattern, and reporting a 20% discount as a 20-unit cash
      discount is wrong in a way that renders as a confident real number.

    Returns None only when there is genuinely nothing to extract, which the
    caller turns into `discount_type = deal` rather than a rejection.
    """
    if not discount_text:
        return None
    text = discount_text.strip().lower()

    if "free shipping" in text or "free delivery" in text:
        return DiscountType.deal, None
    if any(phrase in text for phrase in ("bogo", "buy one", "buy 1 get 1", "buy one get one")):
        return DiscountType.deal, None

    percent = re.search(r"(\d+(?:[.,]\d+)?)\s*%", text)
    if percent:
        return DiscountType.percentage, _to_float(percent.group(1))

    # Currency symbol/code immediately before the amount: "₹200 off",
    # "USD 15 off", "€10 rabais".
    amount = re.search(
        r"(?:rs\.?|inr|₹|\$|usd|€|eur|£|gbp|¥|jpy)\s*(\d+(?:[.,]\d+)?)",
        text,
    )
    if amount:
        return DiscountType.fixed, _to_float(amount.group(1))

    # A bare amount followed by the word that makes it a discount. Anchored on
    # both sides so a year ("2026 sale") or a quantity is not read as money.
    bare = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:off|discount|rubais|rabais)\b", text)
    if bare:
        return DiscountType.fixed, _to_float(bare.group(1))

    if re.search(r"\bfree\b", text):
        return DiscountType.deal, None
    return None


def _to_float(value: str | float | Decimal | None) -> float | None:
    """Parse a number that may use a comma as the decimal separator.

    Providers disagree: EU feeds send `"19,90"`, US feeds `"19.90"`, and some
    send `"1.234,56"`. Treating a comma as a decimal separator is safe for the
    values a discount ever takes; treating a dot as a thousands separator is not
    guessable, so only the single-separator cases are handled.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, Decimal):
        return float(value)
    text = str(value).strip().replace(" ", "")
    if not text:
        return None
    if "," in text and "." in text:
        # Whichever separator comes last is the decimal one.
        text = text.replace(".", "").replace(",", ".") if text.rfind(",") > text.rfind(".") else text.replace(",", "")
    else:
        text = text.replace(",", ".")
    try:
        return float(text)
    except (ValueError, InvalidOperation):
        return None


def normalize_offer(raw: RawOffer) -> NormalizedOffer:
    """Canonicalize one provider record.

    Raises `NormalizeError` for anything unusable. Every rejection reason is
    specific, because "normalize failed" on a run of 40,000 records tells an
    operator nothing about whether to fix the adapter or the data.
    """
    title = " ".join(raw.title.split()).strip() if raw.title else ""
    if not title:
        raise NormalizeError("empty title")
    if len(title) > 200:
        raise NormalizeError(f"title too long ({len(title)} chars)")

    code = raw.code.strip().upper() if raw.code and raw.code.strip() else None

    destination_url = raw.destination_url.strip() if raw.destination_url else ""
    if not destination_url:
        raise NormalizeError("missing destination_url")

    discount_type: DiscountType | None = None
    if raw.discount_type:
        try:
            discount_type = DiscountType(str(raw.discount_type).strip().lower())
        except ValueError as exc:
            raise NormalizeError(f"bad discount_type: {raw.discount_type!r}") from exc
    discount_value = _to_float(raw.discount_value)
    if discount_type is None:
        parsed = parse_discount(raw.discount_text)
        if parsed is not None:
            discount_type, parsed_value = parsed
            discount_value = parsed_value
    if discount_type is None:
        # No parseable discount. Treated as a deal rather than rejected: "Free
        # returns on every order" is a perfectly good offer with no number in it,
        # and discarding it would throw away real value.
        discount_type = DiscountType.deal

    # ---- Merchant identity -------------------------------------------------
    # Domain is preferred over the name for the slug when available, because
    # `nike.com` is stable and "Nike Official Store #2" is not.
    domain = merchants.normalize_domain(raw.store_website) or merchants.normalize_domain(
        destination_url
    )

    store_name = (raw.store_name or "").strip()
    if not store_name:
        if domain:
            store_name = merchants.merchant_from_domain(domain)
        elif raw.store_slug:
            store_name = raw.store_slug.replace("-", " ").title()
    if not store_name:
        raise NormalizeError("offer identifies no merchant (no store_name, store_slug or domain)")

    normalized_name = merchants.normalize_name(store_name)

    store_slug = raw.store_slug or (slugify(domain) if domain else "") or slugify(store_name)
    if not store_slug:
        raise NormalizeError("offer has no usable store identity")

    category_slug = raw.category_slug or (
        slugify(raw.category_name) if raw.category_name else None
    )

    currency = normalize_currency((raw.extra or {}).get("currency"))
    country = normalize_country((raw.extra or {}).get("country"))
    restrictions = _extract_restrictions(raw.extra or {})
    source_categories = _extract_categories(raw.extra or {})

    return NormalizedOffer(
        title=title,
        code=code,
        description=raw.description.strip() if raw.description else None,
        discount_type=discount_type,
        discount_value=discount_value,
        currency=currency,
        store_slug=store_slug,
        store_name=store_name,
        store_domain=domain,
        store_normalized_name=normalized_name,
        category_slug=category_slug,
        category_name=raw.category_name,
        destination_url=destination_url,
        starts_at=_to_datetime((raw.extra or {}).get("starts_at")),
        expires_at=_to_datetime(raw.expires_at),
        external_id=(str(raw.external_id) if raw.external_id is not None else None),
        country=country,
        restrictions=restrictions,
        source_categories=source_categories,
        store_website=raw.store_website,
        extra=raw.extra or {},
    )


def _extract_restrictions(extra: dict[str, Any]) -> dict[str, Any]:
    """Provider-claimed conditions, kept verbatim rather than interpreted.

    Minimum order value, new-customers-only, eligible and excluded SKUs: these
    are frequently stated and frequently *wrong* on feeds, and they are the
    details a shopper most needs to see before trying a code. Normalizing them
    into our own fields would mean claiming a confidence we do not have, so they
    are carried as the provider stated them and surfaced as such.
    """
    keys = (
        "minimum_order",
        "minimum_order_value",
        "new_customer_only",
        "eligible_products",
        "excluded_products",
        "exclusions",
        "terms",
        "restrictions",
        "shipping",
        "free_shipping",
    )
    found: dict[str, Any] = {}
    for key in keys:
        if key in extra and extra[key] not in (None, "", [], {}):
            found[key] = extra[key]
    return found


def _extract_categories(extra: dict[str, Any]) -> list[str]:
    """Categories a provider supplied, beyond our own taxonomy.

    Kept separate from `category_slug` because it feeds search and merchant-page
    cross-links, while `category_slug` assigns our canonical category. Collapsing
    them would mean adopting a provider's taxonomy as our own.
    """
    raw = extra.get("categories") or extra.get("category") or []
    if isinstance(raw, str):
        raw = [raw]
    values = [str(item).strip() for item in raw if str(item).strip()]
    # Deduplicated case-insensitively, order preserved: providers return the same
    # category under different casings more often than not.
    seen: set[str] = set()
    unique: list[str] = []
    for value in values:
        key = value.lower()
        if key not in seen:
            seen.add(key)
            unique.append(value[:80])
    return unique


def offer_from_dict(data: dict[str, Any]) -> RawOffer:
    """Build a `RawOffer` from a plain dict (CSV row, static config, API body).

    Unknown keys are preserved in `extra` rather than dropped. A provider field we
    have not modelled yet is then available to `_extract_restrictions` and to the
    stored `raw_data` without needing a code change — which is the difference
    between adding a network and losing its data.
    """
    known = set(RawOffer.__dataclass_fields__) - {"extra"}
    kwargs = {key: value for key, value in data.items() if key in known}
    extra = dict(data.get("extra") or {})
    extra.update({key: value for key, value in data.items() if key not in known})
    return RawOffer(**kwargs, extra=extra)
