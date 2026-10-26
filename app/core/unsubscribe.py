"""Signed tokens for the unsubscribe link in alert emails.

The link used to carry a bare address (`?email=...`), and the signed-out page
did nothing with it except say it had worked. A token fixes both halves: the
server can trust what it says (only we could have produced it), so it can act
without a session — which is the whole point of a link in an email.

Tokens do not expire. An unsubscribe link in a two-year-old email must still
work; refusing it is how a mailing list earns spam complaints.
"""

from itsdangerous import BadSignature, URLSafeSerializer

from app.core.config import get_settings

_SALT = "durza-email-unsubscribe"


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(get_settings().secret_key, salt=_SALT)


def make_unsubscribe_token(email: str) -> str:
    return _serializer().dumps(email)


def read_unsubscribe_token(token: str) -> str | None:
    """The address a token was issued for, or None if it is forged or damaged."""
    try:
        value = _serializer().loads(token)
    except BadSignature:
        return None
    return value if isinstance(value, str) and value else None
