"""A small Telegram Bot API client: just what posting to one channel needs.

Two things shape it.

**The bot token is in the URL.** Telegram puts it in the request path
(`/bot<token>/sendMessage`), so anything that prints the URL prints the token:
an httpx INFO line, `str()` of an httpx error, a traceback chained to one. So this
module never raises with the original exception attached (`from None`), never
calls `raise_for_status`, and its errors carry only Telegram's own description.

**Callers need to know whether the message might have been delivered.** "Did it
send?" decides whether retrying is safe, and a bare HTTP error cannot say. So
failures are sorted by what they imply, not by status code:

* `TelegramConfigError`  - token, chat or permissions are wrong. Nothing will
  work until a person fixes it; retrying is pointless.
* `TelegramRateLimited`  - not delivered; wait `retry_after` seconds.
* `TelegramTemporary`    - Telegram-side 5xx or no connection. Not delivered.
* `TelegramRejected`     - this message itself was refused (bad HTML, bad URL).
* `TelegramUncertain`    - the request went out and the answer never came back.
  It may have been delivered, so it must not be blindly re-sent.
"""

from types import TracebackType
from typing import Any, Self

import httpx

_API = "https://api.telegram.org"


class TelegramError(Exception):
    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class TelegramConfigError(TelegramError):
    pass


class TelegramRateLimited(TelegramError):
    def __init__(self, message: str, *, retry_after: int) -> None:
        super().__init__(message, code=429)
        self.retry_after = retry_after


class TelegramTemporary(TelegramError):
    pass


class TelegramRejected(TelegramError):
    pass


class TelegramUncertain(TelegramError):
    pass


def _raise_for(response: httpx.Response) -> Any:
    """The `result` of a successful call, or the right error for a failed one."""
    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if response.status_code == 200 and body.get("ok"):
        return body.get("result")

    code = int(body.get("error_code") or response.status_code)
    description = str(body.get("description") or f"HTTP {code}")[:200]
    retry_after = (body.get("parameters") or {}).get("retry_after")

    if code == 429:
        raise TelegramRateLimited(description, retry_after=int(retry_after or 5))
    if code in (401, 403, 404) or (code == 400 and "chat not found" in description.lower()):
        raise TelegramConfigError(description, code=code)
    if code >= 500:
        raise TelegramTemporary(description, code=code)
    raise TelegramRejected(description, code=code)


class TelegramClient:
    """`async with TelegramClient(token, chat_id) as tg: await tg.send_message(...)`."""

    def __init__(self, token: str, chat_id: str, *, timeout: float = 15.0) -> None:
        self._chat_id = chat_id
        self._base_url = f"{_API}/bot{token}"
        self._timeout = httpx.Timeout(timeout, connect=5.0)
        self._http: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        self._http = httpx.AsyncClient(base_url=self._base_url, timeout=self._timeout)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        if self._http is None:
            raise RuntimeError("use TelegramClient as an async context manager")
        try:
            response = await self._http.post(f"/{method}", json=payload or {})
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            # The request never left, so nothing was delivered.
            raise TelegramTemporary("could not connect to Telegram") from None
        except httpx.HTTPError as exc:
            # Sent, but no usable answer. It may have been delivered.
            raise TelegramUncertain(f"no answer from Telegram ({type(exc).__name__})") from None
        return _raise_for(response)

    async def send_message(
        self,
        text: str,
        *,
        button: tuple[str, str] | None = None,
        silent: bool = False,
    ) -> int:
        """Post `text` (HTML) to the channel. Returns Telegram's message id."""
        payload: dict[str, Any] = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": True},
            "disable_notification": silent,
        }
        if button is not None:
            label, url = button
            payload["reply_markup"] = {"inline_keyboard": [[{"text": label, "url": url}]]}
        result = await self._call("sendMessage", payload)
        return int(result["message_id"])

    async def get_me(self) -> dict[str, Any]:
        return dict(await self._call("getMe"))

    async def get_chat(self) -> dict[str, Any]:
        return dict(await self._call("getChat", {"chat_id": self._chat_id}))

    async def get_chat_member(self, user_id: int) -> dict[str, Any]:
        return dict(
            await self._call("getChatMember", {"chat_id": self._chat_id, "user_id": user_id})
        )
