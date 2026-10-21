"""The analytics consent cookie.

Analytics is collected by a browser, so the consent decision has to reach the
server somehow. A client-side flag does not: anything can post to
`/analytics/event` with the flag off. So the decision is carried by a cookie
the server signs, and the server only records an event when it can verify the
signature.

The cookie is:

* **HttpOnly** — JavaScript cannot read it, so a page cannot forge the choice
  and cannot read the value either;
* **signed** with the secret key, so a hand-crafted value is rejected;
* **scoped to the parent domain**, so the cookie the storefront sets on
  `durza.in` is also sent to `api.durza.in` and the two agree;
* **SameSite=Lax** (the default), which is what a same-site request from the
  storefront to the API is.

Withdrawing consent deletes the cookie, and the next request stops being
recorded.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

#: The value stored in the cookie when consent was given.
ACCEPTED = "accepted"


@dataclass(frozen=True)
class ConsentCookie:
    """A signed consent cookie, and the attributes it is set with."""

    value: str
    max_age: int
    domain: str | None


def parent_domain(site_url: str) -> str | None:
    """The parent domain of a site URL, as a cookie domain.

    `https://www.durza.in` and `https://api.durza.in` both give `.durza.in`,
    so a cookie set by one is sent to the other. A host that is already
    registrable (`https://durza.in`) has no parent to share, and gets `None`:
    the cookie stays host-only.
    """
    host = (urlsplit(site_url).hostname or "").lower()
    if not host:
        return None
    labels = host.split(".")
    if len(labels) < 3 or host == "localhost":
        # `durza.in` is already the registrable domain, and `localhost` has
        # no parent. There is nothing to share it with.
        return None
    return "." + ".".join(labels[-2:])


def _signature(payload: str, secret: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def issue_consent_cookie(
    *,
    secret: str,
    max_age_days: int,
    site_url: str,
    issued_at: int | None = None,
) -> ConsentCookie:
    """A signed consent cookie for the accepted choice."""
    issued = int(issued_at if issued_at is not None else time.time())
    payload = f"{ACCEPTED}.{issued}"
    value = f"{payload}.{_signature(payload, secret)}"
    return ConsentCookie(
        value=value,
        max_age=max_age_days * 86400,
        domain=parent_domain(site_url),
    )


def has_valid_consent(
    cookie_value: str | None,
    *,
    secret: str,
    max_age_days: int,
    now: int | None = None,
) -> bool:
    """Whether a cookie value proves that consent was given.

    A tampered value, a value for another choice, and an expired value are all
    "no consent" — the caller stores nothing.
    """
    if not cookie_value:
        return False
    parts = cookie_value.split(".")
    if len(parts) != 3:
        return False
    choice, issued_raw, signature = parts
    if choice != ACCEPTED:
        return False
    expected = _signature(f"{choice}.{issued_raw}", secret)
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        issued = int(issued_raw)
    except ValueError:
        return False
    current = int(now if now is not None else time.time())
    if issued > current + 60:
        return False
    return current - issued <= max_age_days * 86400
