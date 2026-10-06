"""Turn a stored destination URL into the URL we actually redirect to.

Affiliate tracking lives here, in one place, so adding a network is a data
change on the Store (network + optional template) rather than a code change or
an edit to every coupon.

Resolution order:
1. The store's `link_template`, if set. Placeholders:
   `{url}`      the destination, URL-encoded (for deeplink wrappers)
   `{raw_url}`  the destination as stored
   `{clickref}` this click's unique reference
2. The network's click-reference query parameter appended to the destination.
3. The destination unchanged (`none`, `direct`, or an unknown network).

The parameter names below are each network's documented sub-ID field; confirm
against the programme's own docs when onboarding, and use `link_template` if a
particular advertiser needs something different.
"""

import enum
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse


class AffiliateNetwork(str, enum.Enum):
    none = "none"
    direct = "direct"
    awin = "awin"
    cuelinks = "cuelinks"
    admitad = "admitad"
    impact = "impact"


CLICKREF_PARAM: dict[str, str] = {
    AffiliateNetwork.awin.value: "clickref",
    AffiliateNetwork.cuelinks.value: "subid1",
    AffiliateNetwork.admitad.value: "subid",
    AffiliateNetwork.impact.value: "subId1",
}


def normalize_network(value: str | None) -> str:
    """Unknown or empty values fall back to `none` rather than failing a redirect."""
    if value and value in {n.value for n in AffiliateNetwork}:
        return value
    return AffiliateNetwork.none.value


def _is_http(url: str) -> bool:
    return urlparse(url).scheme in {"http", "https"}


def build_outbound_url(
    destination_url: str,
    *,
    network: str | None,
    link_template: str | None,
    clickref: str,
) -> str:
    """Return the redirect target. Never raises: a broken template must not
    take down a redirect, so any problem falls back to the plain destination."""
    if link_template:
        built = (
            link_template.replace("{url}", quote(destination_url, safe=""))
            .replace("{raw_url}", destination_url)
            .replace("{clickref}", clickref)
        )
        if _is_http(built):
            return built
        return destination_url

    param = CLICKREF_PARAM.get(normalize_network(network))
    if param is None or not _is_http(destination_url):
        return destination_url

    if normalize_network(network) == AffiliateNetwork.cuelinks.value:
        return _cuelinks_wrap(destination_url, clickref=clickref)

    parts = urlparse(destination_url)
    # Replace any existing value for the parameter rather than adding a second.
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != param]
    query.append((param, clickref))
    return urlunparse(parts._replace(query=urlencode(query)))


def _cuelinks_wrap(destination_url: str, *, clickref: str) -> str:
    """Wrap a destination in our Cuelinks publisher tracker.

    Two parts, both required for the loop to close:
    1. The outbound URL goes through Cuelinks' click-tracking wrapper,
       identified by our publisher id, so the click is registered against our
       account.
    2. The clickref rides along as Cuelinks' `subid1` parameter on the inner
       destination, because Cuelinks hands it back verbatim in the transaction
       report — that is what lets a conversion row be matched to a ClickEvent.

    Without a configured publisher id there is nothing to wrap with, so the
    destination is returned with just the subid1 parameter (same fallback
    behaviour as every other network) rather than producing a broken link.
    """
    from app.core.config import get_settings

    settings = get_settings()
    param = CLICKREF_PARAM[AffiliateNetwork.cuelinks.value]
    parts = urlparse(destination_url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != param]
    query.append((param, clickref))
    tracked = urlunparse(parts._replace(query=urlencode(query)))

    if not settings.cuelinks_publisher_id:
        return tracked
    wrapper = (
        f"{settings.cuelinks_tracker_base}"
        f"?pid={quote(settings.cuelinks_publisher_id, safe='')}"
        f"&url={quote(tracked, safe='')}"
    )
    return wrapper
