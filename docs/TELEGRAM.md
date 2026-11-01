# Telegram channel

New offers are posted to a public Telegram channel automatically, **each offer at
most once**. Nothing is hooked into coupon creation: a scheduled scan looks at what
is live and new, and the `telegram_posts` table remembers what was already posted.

## One-time setup

1. **Create the channel** in Telegram (New Channel, **Public**) and pick the
   username, e.g. `@durzadeals`. Add the logo, description and a pinned message
   (see "Presenting it" below).
2. **Use your bot.** `TELEGRAM_BOT_TOKEN` is already used for per-user alerts; the
   same bot posts to the channel. If you have no bot, message **@BotFather**,
   send `/newbot`, and copy the token.
3. **Add the bot as an administrator of the channel**: channel > Administrators >
   Add Administrator > the bot, with **Post messages** enabled. A bot that is only
   a member cannot post.
4. **Configure the server** (`.env` on the VM, before deploying):

   ```
   TELEGRAM_BOT_TOKEN=<from BotFather>
   TELEGRAM_CHANNEL_ID=@durzadeals
   TELEGRAM_CHANNEL_ENABLED=true
   ```

   If you use `SCHEDULER_JOBS`, add `post_telegram` to the list. Without it the
   job never runs.
5. **Deploy**, then check the setup from the VM:

   ```
   podman-compose run --rm --no-deps -T app python -m app.tools.telegram_check --send-test
   ```

   It checks the token, the channel and the bot's permission, says what to fix when
   one is wrong, and posts one test message so you can see it arrive.
6. **Watch the first posts.** Admin > **Telegram Posts** lists every offer with its
   status. The first scan runs within a few minutes of boot.

## What gets posted

An offer is announced only if it is publicly live (active, store active, started,
not expired), will not expire in the next 3 hours, was created in the last 24 hours,
is for India, and is not known to be broken. Best-scored first.

| Setting | Default | Meaning |
|---|---|---|
| `TELEGRAM_POST_INTERVAL_MINUTES` | 5 | how often the scan runs |
| `TELEGRAM_MAX_POSTS_PER_RUN` | 6 | per scan |
| `TELEGRAM_MAX_POSTS_PER_DAY` | 40 | per rolling 24 hours |
| `TELEGRAM_SEND_GAP_SECONDS` | 4 | pause between posts (Telegram allows ~20/min per channel) |
| `TELEGRAM_POST_WINDOW_HOURS` | 24 | only offers created this recently |
| `TELEGRAM_MIN_HOURS_TO_EXPIRY` | 3 | skip offers about to lapse |
| `TELEGRAM_COUNTRY` | `IN` | skip other countries; empty means any |
| `TELEGRAM_ONLY_VERIFIED` | false | post only verified offers |
| `TELEGRAM_SILENT_AT_NIGHT` | true | no notification sound 11pm-7am IST |
| `TELEGRAM_FOOTER` | commission disclosure | last line of every post |

The caps are what stop a large import from becoming a firehose: the first big
ingestion is spread over the day, strongest offers first.

Each post links to the site's own offer page (`/coupons/<slug>`), tagged
`utm_source=telegram`, so the click is yours to measure rather than a raw affiliate
link. "Verified" appears only when the offer was actually verified in the last three
days.

## Once only: how it is guaranteed

A ledger row is written *before* the message is sent, and the database refuses a
second row for the same coupon or the same offer. "Same offer" means the same store,
code and discount, so a re-import that creates a new coupon row for an offer already
posted is recognised and skipped.

| Status | Meaning | Re-sent? |
|---|---|---|
| `sent` | Telegram accepted it | no |
| `rejected` | Telegram refused this message (bad markup or URL) | no, it cannot succeed |
| `uncertain` | no answer came back; it may have been delivered | no, it could duplicate |
| `pending` | claimed, send in flight (stuck ones become `uncertain` after 15 minutes) | no |
| `duplicate` | same offer already posted under another coupon | no |

If a send definitely did **not** happen (cannot connect, Telegram is down, rate
limited, bot lacks permission) the claim is removed and the offer is simply tried
again on the next scan. The trade-off is deliberate: a crash in the instant between
sending and recording loses one post rather than posting it twice.

A row stuck at `uncertain`: look in the channel. If the post is there, nothing to do.
If it is not, delete the row (SQL) and it will be posted on the next scan.

## Presenting it

- **Name:** keep it the same as the site. `Durza Deals` with the username
  `@durzadeals` (check availability in Telegram; if taken, `@durza_deals` or
  `@durzadeals_in`). Add "India" or "Coupons" in the description, not the name.
- **Photo:** the site logo, so a post and the website look like one thing.
- **Description:** what the channel is, who runs it, and the disclosure. For example:
  *"Verified coupons and offers for Indian shoppers, from Durza. Posts may contain
  affiliate links; we may earn a commission at no extra cost to you."*
- **Pinned message:** the site link, how often you post, and one line on how offers
  are checked.
- **Avoid:** "loot", "free", "100%" and brand names in the channel name. They read
  as spam, and brand names invite a takedown.

## Safety

- The bot token is in every Telegram request URL, so `httpx` request logging is
  turned down to WARNING (it printed the URL at INFO), and the stored log redacts
  anything shaped like a token.
- A wrong token, chat id or missing admin right stops the run with a clear error in
  the logs and posts nothing; fix it and the pending offers go out.
