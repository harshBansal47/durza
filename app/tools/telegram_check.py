"""Check the Telegram setup before relying on it.

    podman-compose run --rm --no-deps -T app python -m app.tools.telegram_check [--send-test]

Verifies, in order: the token is valid, the channel exists, and the bot is an
administrator who is allowed to post. Each failure says what to do about it,
because "403 Forbidden" on its own does not tell anyone to add the bot as an admin.

`--send-test` posts one short message so you can see it arrive. It is not recorded
in the ledger and is not an offer.
"""

import argparse
import asyncio

from app.core.config import get_settings
from app.integrations.telegram import TelegramClient, TelegramConfigError, TelegramError


async def check(*, send_test: bool) -> int:
    settings = get_settings()
    if not settings.telegram_bot_token or not settings.telegram_channel_id:
        print("FAIL  TELEGRAM_BOT_TOKEN and TELEGRAM_CHANNEL_ID must both be set.")
        return 1
    async with TelegramClient(settings.telegram_bot_token, settings.telegram_channel_id) as tg:
        try:
            me = await tg.get_me()
            print(f"ok    token is valid: bot @{me.get('username')} (id {me['id']})")
            chat = await tg.get_chat()
            print(f"ok    channel found: {chat.get('title')!r} ({chat.get('type')})")
            member = await tg.get_chat_member(int(me["id"]))
        except TelegramConfigError as exc:
            print(f"FAIL  {exc}")
            print("      Check the token, that TELEGRAM_CHANNEL_ID is '@name' or the -100... id,")
            print("      and that the bot has been added to the channel as an administrator.")
            return 1
        except TelegramError as exc:
            print(f"FAIL  {exc}")
            return 1

        status = member.get("status")
        if status not in {"administrator", "creator"}:
            print(f"FAIL  the bot is only a {status!r} of the channel. Make it an administrator.")
            return 1
        if status == "administrator" and member.get("can_post_messages") is False:
            print("FAIL  the bot is an administrator but may not post. Enable 'Post messages'.")
            return 1
        print("ok    the bot is an administrator and can post")

        if send_test:
            try:
                message_id = await tg.send_message("✅ Durza bot is connected.", silent=True)
            except TelegramError as exc:
                print(f"FAIL  test message was refused: {exc}")
                return 1
            print(f"ok    test message sent (id {message_id})")
    print("All good.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--send-test", action="store_true", help="post one test message")
    return asyncio.run(check(send_test=parser.parse_args().send_test))


if __name__ == "__main__":
    raise SystemExit(main())
