"""Email rendering and delivery.

The rendering tests are the ones worth having. Delivery itself is exercised
through its no-SMTP path and its failure path, because that is where the
behaviour that matters lives: a send that fails must return False rather than
raise, or a single bad address rolls back the alert history recording the
attempt.
"""

import json
import re
from email import message_from_string
from urllib.parse import unquote

import pytest

from app.core.config import get_settings
from app.core.unsubscribe import read_unsubscribe_token
from app.services import email as email_service
from app.services.email import (
    AlertContent,
    OtpEmailContent,
    build_message,
    build_otp_message,
    render_html,
    render_otp_html,
    render_otp_text,
    render_text,
    send_otp_email,
)

ALERT = AlertContent(
    subject="Sony WH-1000XM5 dropped to $279.00 USD",
    preheader="Down $40.00 USD",
    headline="A price drop",
    detail="Sony WH-1000XM5 fell from $319.00 USD to $279.00 USD.",
    cta_label="See the price",
    cta_url="https://durza.in/products/sony-wh-1000xm5",
    price_line="$319.00 USD → $279.00 USD after the code",
    code="SAVE20",
)


def test_text_body_carries_the_actionable_facts() -> None:
    body = render_text(ALERT, "shopper@example.com")
    assert "A price drop" in body
    assert "$279.00 USD" in body
    assert "SAVE20" in body
    assert "https://durza.in/products/sony-wh-1000xm5" in body


def test_text_body_always_offers_an_way_out() -> None:
    body = render_text(ALERT, "shopper@example.com")
    # The link carries a signed token, not a bare address: anyone can type an
    # address into a URL, but only the server can mint a token that unsubscribes it.
    match = re.search(r"/account/unsubscribe\?token=(\S+)", body)
    assert match is not None
    assert read_unsubscribe_token(unquote(match.group(1))) == "shopper@example.com"
    assert "shopper%40example.com" not in body and "shopper@example.com?" not in body


def test_text_lines_are_wrapped_to_78_columns() -> None:
    # A long unbroken line is left intact on purpose — see _wrap_line — but a
    # long *sentence* must be wrapped or some clients hard-wrap it themselves,
    # which can break the unsubscribe URL.
    alert = AlertContent(
        subject="s",
        preheader="p",
        headline="h",
        detail="word " * 80,
        cta_label="Go",
        cta_url="https://durza.in/x",
    )
    for line in render_text(alert, "a@example.com").splitlines():
        # A line that is one unbroken token (a URL, which now carries a signed
        # unsubscribe token) is emitted intact on purpose; only prose is wrapped.
        if " " in line.strip():
            assert len(line) <= 78, line


def test_html_escapes_store_supplied_text() -> None:
    # Product names and codes come from scraped feeds. A store named
    # `<script>alert(1)</script>` must not become executable in someone's inbox.
    alert = AlertContent(
        subject="<b>subject</b>",
        preheader="p",
        headline="<img src=x onerror=alert(1)>",
        detail="A & B < C",
        cta_label="Go",
        cta_url="https://durza.in/?a=1&b=2",
        code="<script>bad()</script>",
    )
    html = render_html(alert, "a@example.com")
    assert "<img src=x" not in html
    assert "<script>" not in html
    assert "&lt;img src=x" in html
    assert "A &amp; B &lt; C" in html


def test_message_is_multipart_with_a_text_and_html_part() -> None:
    message = build_message("shopper@example.com", ALERT)
    assert message.is_multipart()
    subtypes = {part.get_content_subtype() for part in message.walk()}
    assert "plain" in subtypes
    assert "html" in subtypes


def test_message_carries_list_unsubscribe_headers() -> None:
    # The one-click unsubscribe header is what makes a Gmail "Unsubscribe"
    # link appear; without it a marketing complaint is a support ticket.
    message = build_message("shopper@example.com", ALERT)
    header = message["List-Unsubscribe"]
    # RFC 8058: the header must point at something that accepts a bare POST, so it
    # is the one-click endpoint, not the confirmation page, and the token in it
    # must be one that unsubscribes this recipient.
    prefix = f"<{get_settings().site_url.rstrip('/')}/account/unsubscribe/one-click?token="
    assert header.startswith(prefix) and header.endswith(">")
    token = unquote(header[len(prefix) : -1])
    assert read_unsubscribe_token(token) == "shopper@example.com"
    assert "List-Unsubscribe=One-Click" in message["List-Unsubscribe-Post"]


@pytest.mark.asyncio
async def test_send_without_smtp_configured_reports_queued() -> None:
    assert get_settings().email_configured is False
    assert await email_service.send_email("shopper@example.com", ALERT) is True


@pytest.mark.asyncio
async def test_send_failure_returns_false_and_does_not_raise(monkeypatch) -> None:
    # A bounced address must not roll back the alert-event row recording the
    # attempt, so this returns False rather than propagating.
    settings = get_settings()
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com", raising=False)

    import aiosmtplib

    async def _boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(aiosmtplib, "send", _boom)

    assert await email_service.send_email("shopper@example.com", ALERT) is False


@pytest.mark.asyncio
async def test_send_uses_starttls_by_default(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com", raising=False)
    monkeypatch.setattr(settings, "smtp_security", "starttls", raising=False)

    captured: dict = {}

    import aiosmtplib

    async def _capture(message, **kwargs):
        captured.update(kwargs)
        captured["message"] = message

    monkeypatch.setattr(aiosmtplib, "send", _capture)

    assert await email_service.send_email("shopper@example.com", ALERT) is True
    assert captured["start_tls"] is True
    assert captured["use_tls"] is False
    assert captured["hostname"] == "smtp.example.com"


@pytest.mark.asyncio
async def test_implicit_tls_does_not_also_request_starttls(monkeypatch) -> None:
    # Conflating implicit TLS and STARTTLS is the classic "works locally, hangs
    # in production" mail bug: port 465 wants use_tls and no start_tls.
    settings = get_settings()
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com", raising=False)
    monkeypatch.setattr(settings, "smtp_security", "ssl", raising=False)

    captured: dict = {}

    import aiosmtplib

    async def _capture(message, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(aiosmtplib, "send", _capture)
    await email_service.send_email("shopper@example.com", ALERT)

    assert captured["use_tls"] is True
    assert captured["start_tls"] is False


def test_message_id_has_a_domain() -> None:
    # Some spam filters discard a Message-ID with no domain part.
    settings = get_settings()
    rendered = message_from_string(build_message("a@example.com", ALERT).as_string())
    _, _, domain = rendered["Message-ID"].partition("@")
    assert domain.strip(">") == settings.email_from.rpartition("@")[2]


def test_rendered_html_is_serialisable_json_safe() -> None:
    # Guards against a stray object sneaking into an f-string in the template.
    assert json.dumps({"html": render_html(ALERT, "a@example.com")}) is not None


# ---- Passwordless email sign-in codes --------------------------------------


OTP = OtpEmailContent(code="482913", expires_minutes=5)


def test_otp_text_body_carries_the_code_and_expiry() -> None:
    body = render_otp_text(OTP, "signer@example.com")
    assert "482913" in body
    assert "5 minutes" in body
    assert "If you did not try to sign in" in body


def test_otp_html_body_carries_the_code_and_has_no_unsubscribe_link() -> None:
    html = render_otp_html(OTP, "signer@example.com")
    assert "482913" in html
    assert "expires in 5 minutes" in html
    assert "unsubscribe" not in html.lower()


def test_otp_html_escapes_the_recipient() -> None:
    html = render_otp_html(OTP, "<script>bad@example.com</script>")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_otp_message_is_multipart_and_addresses_the_recipient() -> None:
    message = build_otp_message("signer@example.com", OTP)
    assert message["Subject"] == "Your Durza sign-in code: 482913"
    assert message["To"] == "signer@example.com"
    assert message.is_multipart()
    subtypes = {part.get_content_subtype() for part in message.walk()}
    assert "plain" in subtypes
    assert "html" in subtypes


def test_otp_message_uses_the_reply_to_when_configured(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "email_reply_to", "hello@durza.in", raising=False)
    message = build_otp_message("signer@example.com", OTP)
    assert message["Reply-To"] == "hello@durza.in"


@pytest.mark.asyncio
async def test_send_otp_without_smtp_configured_reports_queued() -> None:
    assert get_settings().email_configured is False
    assert await send_otp_email("signer@example.com", OTP) is True


@pytest.mark.asyncio
async def test_send_otp_failure_returns_false_and_does_not_raise(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com", raising=False)

    import aiosmtplib

    async def _boom(*args, **kwargs):
        raise OSError("connection refused")

    monkeypatch.setattr(aiosmtplib, "send", _boom)

    assert await send_otp_email("signer@example.com", OTP) is False


@pytest.mark.asyncio
async def test_send_otp_uses_starttls_and_explicit_certificate_validation(monkeypatch) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "smtp_host", "smtp.example.com", raising=False)
    monkeypatch.setattr(settings, "smtp_security", "starttls", raising=False)

    captured: dict = {}

    import aiosmtplib

    async def _capture(message, **kwargs):
        captured.update(kwargs)
        captured["message"] = message

    monkeypatch.setattr(aiosmtplib, "send", _capture)

    assert await send_otp_email("signer@example.com", OTP) is True
    assert captured["start_tls"] is True
    assert captured["use_tls"] is False
    assert captured["validate_certs"] is True
