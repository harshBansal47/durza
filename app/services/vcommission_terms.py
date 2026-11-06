"""Structured terms from a vCommission campaign description.

The feed's `description` is advertiser-written HTML (about nine tenths of it inline styling)
that follows one template: a "Promotion Methods" block saying which channels the advertiser
allows, "Cookie Duration" and "Validation Period" lines, and a numbered "Detailed KPI" list.
The API's own structured fields (`payouts`, `model`, `countries`) are reliable for what they
cover, but they are blind to the things that matter most for what we may promote:

  * a payout the API shows as one number may be a GMV slab, or may differ by user type;
  * some advertisers pay less, or pay only for certain codes, when discount codes are used;
  * channels (Telegram, email, SMS, coupons, paid search) are allowed or banned per advertiser.

This module reads those from the text. It works from the RAW HTML at import time: the
stored `description` has been flattened to one line by `_plain_text`, which destroys the line
breaks the labels depend on, so parsing the stored copy later is not possible.

Principles, because a wrong "allowed" can cost an affiliate account:
  * never trust a field: anything not found is None, never a default;
  * fail closed: `can_promote` says no unless the advertiser said yes in terms we understood;
  * say why: `review_reasons` lists what a person should look at before we promote;
  * never raise: a malformed description yields an "unparsed" result, not a failed import.

Standard library only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from html.parser import HTMLParser

TERMS_VERSION = 1

# Channel statuses.
ALLOWED = "allowed"
CONDITIONAL = "conditional"  # allowed, with a qualifier we must honour, e.g. "(Only generic)"
NOT_ALLOWED = "not_allowed"
UNAVAILABLE = "unavailable"  # the advertiser says the option does not exist ("Not Available")

# Reasons that mean a person should look before we promote this campaign: we could not
# establish what the advertiser allows, or the terms contain something that changes what is
# safe to do. `payout_text_differs_from_api` is deliberately NOT here: it says the API's payout
# number is not the whole story (relevant to ranking), not that promoting is unsafe, and
# counting it made about half of all campaigns "need review", which defeats a review queue.
_NEEDS_REVIEW = frozenset(
    {
        "description_missing",
        "parse_error",
        "no_promotion_methods",
        "no_detailed_kpi",
        "code_payout_rule",
        "payout_change_upcoming",
    }
)

_CHANNEL_KEYS = {
    "content": "content",
    "coupon": "coupon",
    "cashback": "cashback",
    "mobileapp": "mobile_app",
    "email": "email",
    "sms": "sms",
    "telegram": "telegram",
    "influencer": "influencer",
    "software": "software",
    "mediabuyingsearch": "search",
    "mediabuyingsocial": "social",
    "mediabuyingnative": "native",
    "mediabuyingdisplay": "display",
    "subnetwork": "sub_network",
    "pop": "pop",
    "push": "push",
    "video": "video",
    "reseller": "reseller",
    "incent": "incent",
    "smartlink": "smartlink",
}

_BLOCK_TAGS = {
    "p",
    "br",
    "li",
    "div",
    "tr",
    "td",
    "th",
    "h1",
    "h2",
    "h3",
    "h4",
    "ul",
    "ol",
    "table",
}

# "<name> - <verdict>". The verdict must START with a known verb, which is what lets a name
# that itself contains a hyphen ("Sub-Network - Allowed") split in the right place.
_CHANNEL_RE = re.compile(
    r"^(?P<name>.+?)\s*[-–—]\s*(?P<value>(?:not\s+allowed|not\s+available|allowed|available)\b.*)$",
    re.IGNORECASE,
)

_SUBPOINT_RE = re.compile(r"^[a-z]\s*[.)]\s*")

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1
    )
}


class _Lines(HTMLParser):
    """HTML to text lines: block elements and <br> start a new line, entities are decoded."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_lines(raw: str) -> list[str]:
    """Non-empty, whitespace-normalised text lines from advertiser HTML."""
    parser = _Lines()
    parser.feed(raw)
    parser.close()
    lines = []
    for line in "".join(parser.parts).replace("\xa0", " ").split("\n"):
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            lines.append(line)
    return lines


@dataclass
class Channel:
    status: str
    note: str | None = None  # the advertiser's qualifier, e.g. "Only Generic KWs"


@dataclass
class Rule:
    kind: str
    text: str  # advertiser wording: internal/admin use, never shown to shoppers as-is


@dataclass
class CampaignTerms:
    version: int = TERMS_VERSION
    conversion_type: str | None = None
    cookie_days: float | None = None
    validation_days: int | None = None
    channels: dict[str, Channel] = field(default_factory=dict)
    rules: list[Rule] = field(default_factory=list)
    geo: str | None = None
    brand_bidding_banned: bool = False
    other_affiliate_code_voids_sale: bool = False
    # A rule about which discount codes affect payout, e.g. "exclusive codes pay 7%" or "only
    # website coupon codes are payable". Either way a person should read it before we promote
    # codes for this advertiser.
    code_payout_rule: str | None = None
    # Discount codes the advertiser itself invites affiliates to use. Preferred over our own
    # exclusive codes where an advertiser pays less (or voids the sale) for those.
    approved_codes: list[str] = field(default_factory=list)
    # "api_only": the text makes no payout claim, so the API's payout stands. Anything else
    # means the text describes a structure the API's single number does not capture.
    payout_structure: str = "api_only"
    payout_effective_from: str | None = None
    review_reasons: list[str] = field(default_factory=list)

    @property
    def needs_review(self) -> bool:
        return any(reason in _NEEDS_REVIEW for reason in self.review_reasons)

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["needs_review"] = self.needs_review
        return data


def _channel_key(name: str) -> str:
    squashed = re.sub(r"[^a-z0-9]", "", name.lower())
    return _CHANNEL_KEYS.get(squashed, re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_"))


def _channel_status(value: str) -> Channel:
    text = value.strip()
    lowered = text.lower()
    if lowered.startswith("not allowed"):
        return Channel(NOT_ALLOWED)
    if lowered.startswith("not available"):
        return Channel(UNAVAILABLE)
    # "Allowed", "Allowed (Only generic)", "Allowed(Only Generic KWs Allowed)"
    qualifier = re.sub(r"^(allowed|available)\s*", "", text, flags=re.IGNORECASE).strip(" ()")
    return Channel(CONDITIONAL, qualifier) if qualifier else Channel(ALLOWED)


def _parse_channels(lines: list[str]) -> dict[str, Channel]:
    start = next(
        (
            i
            for i, line in enumerate(lines)
            if re.match(r"promotion methods\s*:?\s*$", line, re.IGNORECASE)
        ),
        None,
    )
    if start is None:
        return {}
    channels: dict[str, Channel] = {}
    for line in lines[start + 1 :]:
        match = _CHANNEL_RE.match(line)
        if not match:
            break  # the block ends at the first line that is not "<channel> - <verdict>"
        channels[_channel_key(match.group("name"))] = _channel_status(match.group("value"))
    return channels


def _first(lines: list[str], pattern: str) -> re.Match[str] | None:
    compiled = re.compile(pattern, re.IGNORECASE)
    for line in lines:
        match = compiled.search(line)
        if match:
            return match
    return None


def _cookie_days(lines: list[str]) -> float | None:
    match = _first(
        lines, r"cookie\s+(?:duration|period)\s*[-:–]\s*(\d+(?:\.\d+)?)\s*(hours?|hrs?|days?)"
    )
    if not match:
        return None
    value = float(match.group(1))
    days = value / 24 if match.group(2).lower().startswith("h") else value
    return round(days, 4) if 0 < days <= 3650 else None


def _validation_days(lines: list[str]) -> int | None:
    match = _first(lines, r"validation\s+period\s*[-:–]\s*(\d+)\s*(days?|weeks?|months?)")
    if not match:
        return None
    unit = match.group(2).lower()
    days = int(match.group(1)) * (7 if unit.startswith("w") else 30 if unit.startswith("m") else 1)
    return days if 0 < days <= 730 else None


def _effective_date(text: str) -> str | None:
    match = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+([A-Za-z]{3,9})\.?,?\s*(\d{4})", text)
    if not match:
        return None
    month = _MONTHS.get(match.group(2)[:3].lower())
    if month is None:
        return None
    try:
        return date(int(match.group(3)), month, int(match.group(1))).isoformat()
    except ValueError:
        return None


def _payout_structure(lines: list[str]) -> tuple[str, str | None]:
    line = next((ln for ln in lines if re.match(r"payout\s*:", ln, re.IGNORECASE)), None)
    if line is None:
        return "api_only", None
    lowered = line.lower()
    if "slab" in lowered or "gmv" in lowered:
        structure = "slab"
    elif re.search(r"user[\s-]*(?:type[\s-]*)?wise|user\s+type|new user|repeat", lowered):
        structure = "user_type"
    elif re.search(r"category", lowered):
        structure = "category"
    elif re.search(r"wise|below|tier|condition|goal|subscription", lowered):
        structure = "other"
    else:
        # "Payout: 23.1% of order sale amount": a flat statement; the API number stands.
        structure = "api_only"
    return structure, _effective_date(line)


def _rule_kind(text: str) -> str:
    lowered = text.lower()
    if re.search(r"exclusive\s+(?:discount\s+)?(?:coupon|code)|revised payout", lowered):
        return "code_payout_rule"
    if re.search(r"discount\s+code", lowered) and re.search(
        r"\b(?:can|may)\s+use\b|\buse\s+(?:a\s+)?discount", lowered
    ):
        return "approved_discount_code"
    if re.search(r"cancel|return|refund", lowered) and re.search(
        r"(?:not|n't)\s+(?:be\s+|get\s+)?(?:payable|paid|eligible|consider\w*|validated|counted)"
        r"|non[-\s]?payable|deduct",
        lowered,
    ):
        return "cancelled_not_payable"
    if re.search(r"\bbrand", lowered) and re.search(
        r"not\s+(?:allowed|approved|permitted)|\bno\b|prohibit|restrict|\bban", lowered
    ):
        return "brand_bidding_banned"
    if "attribut" in lowered and re.search(
        r"another|other\s+(?:affiliate|channel|partner)|affiliate/partner", lowered
    ):
        return "other_affiliate_attribution"
    if lowered.startswith("geo"):
        return "geo"
    return "other"


def _parse_rules(lines: list[str]) -> list[Rule] | None:
    start = next(
        (
            i
            for i, line in enumerate(lines)
            if re.match(r"detailed\s+kpi\s*:?\s*$", line, re.IGNORECASE)
        ),
        None,
    )
    if start is None:
        return None
    items: list[str] = []
    for line in lines[start + 1 :]:
        numbered = re.match(r"^\d+\s*[.)]\s*(.+)$", line)
        if numbered:
            items.append(numbered.group(1))
        elif items:
            # "a. ..." sub-points and table rows belong to the rule above them
            continuation = _SUBPOINT_RE.sub("", line)
            items[-1] = f"{items[-1]} {continuation}"
    return [Rule(_rule_kind(text), text) for text in items]


def parse_campaign_terms(
    raw_description: object,
    *,
    payouts: object = None,
    model: object = None,
    today: date | None = None,
) -> CampaignTerms:
    """Parse one campaign. Never raises; unknown stays None and is listed in `review_reasons`.

    `today` only decides whether a dated payout change is upcoming (worth a look) or already
    in force (the API figure should reflect it). It defaults to the current date.
    """
    terms = CampaignTerms()
    if not isinstance(raw_description, str) or not raw_description.strip():
        terms.review_reasons.append("description_missing")
        return terms
    try:
        lines = html_to_lines(raw_description)
        reasons = terms.review_reasons

        terms.channels = _parse_channels(lines)
        if not terms.channels:
            reasons.append("no_promotion_methods")

        terms.cookie_days = _cookie_days(lines)
        if terms.cookie_days is None:
            reasons.append("cookie_missing")
        terms.validation_days = _validation_days(lines)
        if terms.validation_days is None:
            reasons.append("validation_missing")

        declared = _first(lines, r"conversion\s+type\s*:\s*([A-Za-z]+)")
        terms.conversion_type = (
            declared.group(1).upper()
            if declared
            else (str(model).upper() if isinstance(model, str) and model.strip() else None)
        )

        rules = _parse_rules(lines)
        if rules is None:
            reasons.append("no_detailed_kpi")
        else:
            terms.rules = rules
            geo = next((r.text for r in rules if r.kind == "geo"), None)
            terms.geo = geo.split(":", 1)[1].strip() if geo and ":" in geo else geo
            kinds = {r.kind for r in rules}
            terms.brand_bidding_banned = "brand_bidding_banned" in kinds
            terms.other_affiliate_code_voids_sale = "other_affiliate_attribution" in kinds
            for rule in rules:
                if rule.kind == "approved_discount_code":
                    found = re.findall(
                        r"discount\s+code\s*[-:–]?\s*([A-Za-z0-9_-]{3,})", rule.text, re.IGNORECASE
                    )
                    terms.approved_codes.extend(code.upper() for code in found)
            penalty = next((r.text for r in rules if r.kind == "code_payout_rule"), None)
            if penalty:
                terms.code_payout_rule = penalty
                reasons.append("code_payout_rule")

        search = terms.channels.get("search")
        note = (search.note if search else None) or ""
        if (
            search
            and search.status == CONDITIONAL
            and "generic" in note.lower()
            and "brand" not in note.lower()
        ):
            # "Allowed (Only generic)" means brand keywords are not. "Allowed (Brand + Generic)"
            # says the opposite, so a note that names brand is left alone.
            terms.brand_bidding_banned = True

        terms.payout_structure, terms.payout_effective_from = _payout_structure(lines)
        if terms.payout_structure != "api_only":
            reasons.append("payout_text_differs_from_api")
        if terms.payout_effective_from and date.fromisoformat(terms.payout_effective_from) > (
            today or datetime.now(UTC).date()
        ):
            reasons.append("payout_change_upcoming")
    except Exception:  # noqa: BLE001 - a malformed description must never fail an import
        return CampaignTerms(review_reasons=["parse_error"])
    return terms


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


def can_promote(
    terms: Mapping[str, object] | None, channel: str, *, accept_conditions: bool = False
) -> Decision:
    """May we promote this campaign on `channel`? Fails closed.

    `terms` is the persisted dict form. Unknown campaign, missing channel matrix, an
    unrecognised channel, "Not Allowed" and "Not Available" are all a no. A conditional
    ("Allowed (Only generic)") is a no unless the caller says it will honour the condition.
    """
    if not terms:
        return Decision(False, "no parsed terms for this campaign")
    channels = terms.get("channels")
    if not isinstance(channels, Mapping) or not channels:
        return Decision(False, "advertiser channel rules were not found")
    entry = channels.get(channel)
    if not isinstance(entry, Mapping):
        return Decision(False, f"no rule found for channel '{channel}'")
    status = entry.get("status")
    if status == ALLOWED:
        return Decision(True, "allowed by the advertiser")
    if status == CONDITIONAL:
        note = entry.get("note") or "see advertiser terms"
        if accept_conditions:
            return Decision(True, f"allowed with a condition: {note}")
        return Decision(False, f"allowed only with a condition: {note}")
    return Decision(False, f"the advertiser does not allow '{channel}'")
