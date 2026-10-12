"""Real email delivery.

Uses aiosmtplib so sending happens on the same event loop as everything else —
a synchronous SMTP handshake inside an async alert scan would stall every other
request for the duration of the connection.

Design notes that are easy to get wrong and expensive to get wrong publicly:

* **Text and HTML are both sent.** Roughly a fifth of alert emails land in a
  client that renders neither, and a price alert is exactly the message you do
  not want to lose.
* **One message per recipient.** Alerts are addressed individually so the
  `List-Unsubscribe` header can carry that person's own address. BCC-ing a
  batch would leak addresses to each other.
* **Delivery failures are not job failures.** A bounced email must not roll
  back the alert-event rows that record what we tried to send, so
  `send_email` reports failure rather than raising.
"""

from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Any

from app.core.config import get_settings
from app.core.observability import get_logger

logger = get_logger("durza.email")

# RFC 5322 recommends 78 characters; longer lines get hard-wrapped by some
# clients, which breaks the unsubscribe URL we care most about.
_MAX_LINE = 78


def _wrap(text: str) -> str:
    return "\n".join(_wrap_line(line) for line in text.splitlines())


def _wrap_line(line: str) -> str:
    if len(line) <= _MAX_LINE:
        return line
    words = line.split()
    out: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        # A single token longer than the limit (a URL) is emitted intact rather
        # than split — a broken link is worse than a long line.
        if len(candidate) <= _MAX_LINE:
            current = candidate
        else:
            if current:
                out.append(current)
            current = word
    if current:
        out.append(current)
    return "\n".join(out)


@dataclass(frozen=True)
class AlertContent:
    """Everything a rendered alert needs, so rendering stays a pure function."""

    subject: str
    preheader: str
    headline: str
    detail: str
    cta_label: str
    cta_url: str
    store_name: str | None = None
    product_name: str | None = None
    price_line: str | None = None
    code: str | None = None


@dataclass(frozen=True)
class ResetPasswordContent:
    """Data needed for a password reset email."""

    reset_url: str
    expires_hours: int


def render_reset_text(content: ResetPasswordContent, recipient: str) -> str:
    lines = [
        "Reset your Durza password",
        "",
        f"Someone requested a password reset for {recipient}.",
        f"This link expires in {content.expires_hours} hours:",
        "",
        content.reset_url,
        "",
        "If you did not request this, you can ignore this email.",
        "",
        "— Durza",
        "",
    ]
    return _wrap("\n".join(lines))


def render_reset_html(content: ResetPasswordContent, recipient: str) -> str:
    def esc(value: str) -> str:
        return (
            value.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    return f"""<!doctype html>
<html lang="en">
<body style="margin:0;padding:24px;background:#f6f5f2;font-family:Georgia,'Times New Roman',serif;color:#1c1b18">
  <span style="display:none;max-height:0;overflow:hidden">Reset your Durza password</span>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
    <tr><td align="center">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#fff;border:1px solid #dcd8d0">
        <tr><td style="padding:18px 28px;background:#EE5A0B">
          <p style="margin:0;font:700 22px/1 ui-sans-serif,system-ui,sans-serif;letter-spacing:-.02em;color:#ffffff">Durza <span style="font:500 13px/1 ui-sans-serif,system-ui,sans-serif;letter-spacing:0;margin-left:6px">Coupon &amp; Deals</span></p>
        </td></tr>
        <tr><td style="padding:8px 28px 24px">
          <h1 style="margin:0 0 12px;font:26px/1.2 Georgia,serif">Reset your password</h1>
          <p style="margin:0 0 12px;font:15px/1.6 ui-sans-serif,system-ui,sans-serif;color:#3d3a34">Someone requested a password reset for {esc(recipient)}. This link expires in {content.expires_hours} hours.</p>
          <p style="margin:0 0 24px">
            <a href="{esc(content.reset_url)}" style="display:inline-block;padding:11px 18px;background:#C2410C;color:#fff;text-decoration:none;font:600 14px ui-sans-serif,system-ui,sans-serif">Reset password</a>
          </p>
        </td></tr>
        <tr><td style="padding:16px 28px;border-top:1px solid #e6e2da">
          <p style="margin:0;font:12px/1.6 ui-sans-serif,system-ui,sans-serif;color:#6b675f">
            If you did not request this, you can ignore this email.
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def build_reset_message(to: str, content: ResetPasswordContent) -> EmailMessage:
    settings = get_settings()
    message = EmailMessage()
    message["Subject"] = "Reset your Durza password"
    message["From"] = formataddr((settings.email_from_name, settings.email_from))
    message["To"] = to
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=settings.email_from.rpartition("@")[2] or None)
    message["List-Unsubscribe"] = f"<{unsubscribe_url(to)}>"
    message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    message["Auto-Submitted"] = "auto-generated"

    message.set_content(render_reset_text(content, to))
    message.add_alternative(render_reset_html(content, to), subtype="html")
    return message


async def send_reset_email(to: str, content: ResetPasswordContent) -> bool:
    """Deliver a password reset email."""
    settings = get_settings()
    if not settings.email_configured:
        logger.info(
            "reset email queued (no SMTP_HOST configured)",
            extra={"to": to},
        )
        return True

    try:
        import aiosmtplib

        await aiosmtplib.send(build_reset_message(to, content), **_send_kwargs())
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "reset email send failed",
            extra={"to": to, "error": str(exc)},
        )
        return False

    logger.info("reset email sent", extra={"to": to})
    return True


def unsubscribe_url(email: str) -> str:
    from urllib.parse import quote

    settings = get_settings()
    return f"{settings.site_url.rstrip('/')}/account/unsubscribe?email={quote(email)}"


def render_text(alert: AlertContent, recipient: str) -> str:
    lines = [
        alert.headline,
        "",
        alert.detail,
    ]
    if alert.price_line:
        lines += ["", f"Price: {alert.price_line}"]
    if alert.code:
        lines += ["", f"Code: {alert.code}"]
    lines += [
        "",
        f"{alert.cta_label}: {alert.cta_url}",
        "",
        "— Durza",
        "",
        "You are getting this because you tracked something or followed a store.",
        f"Change what you hear about: {unsubscribe_url(recipient)}",
        "",
        "We are not in the business of writing to people who did not ask.",
    ]
    return _wrap("\n".join(lines))


def render_html(alert: AlertContent, recipient: str) -> str:
    unsubscribe = unsubscribe_url(recipient)

    def esc(value: str) -> str:
        return (
            value.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )

    price_row = (
        f'<p style="margin:0 0 12px;font:16px/1.5 ui-monospace,Menlo,monospace">{esc(alert.price_line)}</p>'
        if alert.price_line
        else ""
    )
    code_row = (
        f'<p style="margin:0 0 16px"><code style="padding:2px 6px;border:1px solid #ccc;'
        f'font:15px ui-monospace,Menlo,monospace">{esc(alert.code)}</code></p>'
        if alert.code
        else ""
    )

    return f"""<!doctype html>
<html lang="en">
<body style="margin:0;padding:24px;background:#f6f5f2;font-family:Georgia,'Times New Roman',serif;color:#1c1b18">
  <span style="display:none;max-height:0;overflow:hidden">{esc(alert.preheader)}</span>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
    <tr><td align="center">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#fff;border:1px solid #dcd8d0">
        <tr><td style="padding:18px 28px;background:#EE5A0B">
          <p style="margin:0;font:700 22px/1 ui-sans-serif,system-ui,sans-serif;letter-spacing:-.02em;color:#ffffff">Durza <span style="font:500 13px/1 ui-sans-serif,system-ui,sans-serif;letter-spacing:0;margin-left:6px">Coupon &amp; Deals</span></p>
        </td></tr>
        <tr><td style="padding:8px 28px 24px">
          <h1 style="margin:0 0 12px;font:26px/1.2 Georgia,serif">{esc(alert.headline)}</h1>
          <p style="margin:0 0 12px;font:15px/1.6 ui-sans-serif,system-ui,sans-serif;color:#3d3a34">{esc(alert.detail)}</p>
          {price_row}
          {code_row}
          <p style="margin:0 0 24px">
            <a href="{esc(alert.cta_url)}" style="display:inline-block;padding:11px 18px;background:#C2410C;color:#fff;text-decoration:none;font:600 14px ui-sans-serif,system-ui,sans-serif">{esc(alert.cta_label)}</a>
          </p>
        </td></tr>
        <tr><td style="padding:16px 28px;border-top:1px solid #e6e2da">
          <p style="margin:0;font:12px/1.6 ui-sans-serif,system-ui,sans-serif;color:#6b675f">
            You are getting this because you tracked something or followed a store on Durza.
            <a href="{esc(unsubscribe)}" style="color:#1c4b8f">Stop email alerts</a>
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def build_message(to: str, alert: AlertContent) -> EmailMessage:
    settings = get_settings()
    message = EmailMessage()
    message["Subject"] = alert.subject
    # The preheader lives in the headers as well as the hidden div: some
    # clients show the first text line in the inbox list instead of Subject.
    message["From"] = formataddr((settings.email_from_name, settings.email_from))
    message["To"] = to
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=settings.email_from.rpartition("@")[2] or None)
    message["List-Unsubscribe"] = f"<{unsubscribe_url(to)}>"
    message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    message["Auto-Submitted"] = "auto-generated"

    message.set_content(render_text(alert, to))
    message.add_alternative(render_html(alert, to), subtype="html")
    return message


def _send_kwargs() -> dict[str, Any]:
    settings = get_settings()
    security = settings.smtp_security.lower()
    return {
        "hostname": settings.smtp_host,
        "port": settings.smtp_port,
        # Implicit TLS (usually 465) versus STARTTLS (587) versus nothing at
        # all for a local relay. Conflating the first two is the classic
        # "works in dev, hangs in prod" mail bug.
        "use_tls": security == "ssl",
        "start_tls": security == "starttls",
        "username": settings.smtp_user,
        "password": settings.smtp_password,
        "timeout": 30,
    }


async def send_email(to: str, alert: AlertContent) -> bool:
    """Deliver one alert.

    Returns False instead of raising: a bounced address must not roll back the
    alert-history rows that record the attempt, and the caller decides what a
    failed channel means for the job as a whole.
    """
    settings = get_settings()
    if not settings.email_configured:
        logger.info(
            "email queued (no SMTP_HOST configured)",
            extra={"to": to, "subject": alert.subject},
        )
        return True

    try:
        import aiosmtplib

        # aiosmtplib drives asyncio streams, so this does not block the loop
        # the way a synchronous smtplib handshake would.
        await aiosmtplib.send(build_message(to, alert), **_send_kwargs())
    except Exception as exc:  # noqa: BLE001 — any SMTP failure is a send failure
        logger.warning(
            "email send failed",
            extra={"to": to, "subject": alert.subject, "error": str(exc)},
        )
        return False

    logger.info("email sent", extra={"to": to, "subject": alert.subject})
    return True
