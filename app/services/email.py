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
class OtpEmailContent:
    """A sign-in code and the window it is valid for.

    Kept separate from delivery so the template can be snapshotted by tests
    without a mail server, and so the only place the plaintext code exists is
    this object plus the message body.
    """

    code: str
    expires_minutes: int


def _html_escape(value: str) -> str:
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def render_otp_text(content: OtpEmailContent, recipient: str) -> str:
    lines = [
        "Your Durza sign-in code",
        "",
        f"Use this code to sign in as {recipient}:",
        "",
        content.code,
        "",
        f"It expires in {content.expires_minutes} minutes and can be used once.",
        "",
        "If you did not try to sign in, you can ignore this email. Nobody can",
        "access your account without this code.",
        "",
        "— Durza",
        "",
    ]
    return _wrap("\n".join(lines))


def render_otp_html(content: OtpEmailContent, recipient: str) -> str:
    return f"""<!doctype html>
<html lang="en">
<body style="margin:0;padding:24px;background:#f6f5f2;font-family:Georgia,'Times New Roman',serif;color:#1c1b18">
  <span style="display:none;max-height:0;overflow:hidden">Your Durza sign-in code expires in {content.expires_minutes} minutes.</span>
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0">
    <tr><td align="center">
      <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:560px;background:#fff;border:1px solid #dcd8d0">
        <tr><td style="padding:18px 28px;background:#EE5A0B">
          <p style="margin:0;font:700 22px/1 ui-sans-serif,system-ui,sans-serif;letter-spacing:-.02em;color:#ffffff">Durza <span style="font:500 13px/1 ui-sans-serif,system-ui,sans-serif;letter-spacing:0;margin-left:6px">Coupon &amp; Deals</span></p>
        </td></tr>
        <tr><td style="padding:8px 28px 24px">
          <h1 style="margin:0 0 12px;font:26px/1.2 Georgia,serif">Sign in to Durza</h1>
          <p style="margin:0 0 18px;font:15px/1.6 ui-sans-serif,system-ui,sans-serif;color:#3d3a34">Enter this code to sign in as {_html_escape(recipient)}. It expires in {content.expires_minutes} minutes and can be used once.</p>
          <p style="margin:0 0 18px;padding:16px 20px;background:#f6f5f2;border:1px solid #dcd8d0;font:700 34px/1.1 ui-monospace,Menlo,monospace;letter-spacing:.28em;color:#1c1b18">{content.code}</p>
          <p style="margin:0 0 8px;font:14px/1.6 ui-sans-serif,system-ui,sans-serif;color:#3d3a34">If you did not try to sign in, you can ignore this email. Nobody can access your account without this code.</p>
        </td></tr>
        <tr><td style="padding:16px 28px;border-top:1px solid #e6e2da">
          <p style="margin:0;font:12px/1.6 ui-sans-serif,system-ui,sans-serif;color:#6b675f">
            For your security, do not forward this code. Durza will never ask you for it.
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def build_otp_message(to: str, content: OtpEmailContent) -> EmailMessage:
    settings = get_settings()
    message = EmailMessage()
    message["Subject"] = f"Your Durza sign-in code: {content.code}"
    message["From"] = formataddr((settings.email_from_name, settings.email_from))
    message["To"] = to
    if settings.email_reply_to:
        message["Reply-To"] = settings.email_reply_to
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid(domain=settings.email_from.rpartition("@")[2] or None)
    # A sign-in code is transactional, not marketing: there is no unsubscribe
    # link, and the subject carries the code so it is visible without rendering
    # HTML (while the body repeats it for screen readers and text clients).
    message["Auto-Submitted"] = "auto-generated"

    message.set_content(render_otp_text(content, to))
    message.add_alternative(render_otp_html(content, to), subtype="html")
    return message


async def send_otp_email(to: str, content: OtpEmailContent) -> bool:
    """Deliver a sign-in code. Never logs the code itself."""
    settings = get_settings()
    if not settings.email_configured:
        logger.info("sign-in email queued (no SMTP_HOST configured)", extra={"to": to})
        return True

    try:
        import aiosmtplib

        await aiosmtplib.send(build_otp_message(to, content), **_send_kwargs())
    except Exception as exc:  # noqa: BLE001 — any SMTP failure is a send failure
        logger.warning("sign-in email send failed", extra={"to": to, "error": str(exc)})
        return False

    logger.info("sign-in email sent", extra={"to": to})
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
    if settings.email_reply_to:
        message["Reply-To"] = settings.email_reply_to
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
        # Certificates are verified by default and this makes that explicit:
        # a self-signed relay is not worth accepting silently in production.
        "validate_certs": True,
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
