# LightStickEr

A Telegram bot that lets multiple people contribute stickers to shared, named sticker sets.

Telegram's Bot API only lets a bot add stickers to a set *that the bot itself created*
(and bots can't message other bots, so relaying through `@Stickers` isn't possible).
This bot works around that the standard way: it creates and owns the shared set(s)
itself, and adds any sticker a user sends it straight into the set via the API.

## How it works

1. Send the bot any sticker.
2. It asks you for a pack title (e.g. "Cat Memes").
3. Reply with the title. Packs are looked up by a slugified version of the
   title, so "Cat Memes" and "cat memes" are the same pack:
   - If a pack with that name already exists, the sticker is added to it right away.
   - If not, the bot asks you to confirm before creating a brand new pack —
     this catches the common mistake of mistyping an existing pack's name and
     accidentally starting a duplicate instead of adding to it.

You have to already know a pack's title to add to it or look it up with
`/pack <title>` — Telegram's Bot API has no way to list the sticker sets a bot
has created, so there's no built-in "show me all packs" picker. Doing that
would need a separate always-on store (e.g. a small database on a real server)
to remember pack titles across restarts, which is a later-version feature.

Static, animated, and video stickers can't share one set, so each pack keeps one
active set per format (created lazily) and automatically starts a new "part" of
a set once the current one hits Telegram's size limit (120 for static, 50 for
animated/video).

The bot keeps no local state (no database): set names are always derivable from
the pack title, so it just calls `getStickerSet` on Telegram to check what
already exists, whether a sticker was already added, and whether a part is full,
before adding or creating a set. The trade-off is one extra Telegram API call
per submission (and a short scan across parts for `/pack`) — cheap at the scale
this bot is meant for. It also means there's no record of *who* submitted which
sticker, since Telegram doesn't expose that.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token it gives you.
2. Get your own numeric Telegram user id from [@userinfobot](https://t.me/userinfobot),
   and make sure that user has sent this bot a `/start` at least once
   (Telegram requires the "owner" id to be a user the bot has seen before).
3. Copy `.env.example` to `.env` and fill in `BOT_TOKEN` and `OWNER_USER_ID`.
4. Install dependencies (using the existing Python installation):

   ```bash
   python -m venv .venv
   .venv/Scripts/activate   # on Windows
   pip install -r requirements.txt
   ```

5. Run the bot:

   ```bash
   python bot.py
   ```

## Usage

- Send the bot any sticker, then reply with the pack title when it asks.
- `/pack <title>` lists links to an existing pack's set(s).
- `/start` shows a short intro.

## Deploying to Cloud Run

Locally the bot polls Telegram for updates (`RUN_MODE=polling`). Cloud Run
bills per request and can scale to zero, which doesn't mix well with a
process that holds an outbound long-poll connection open forever — so in
production the bot instead runs a small HTTP server and Telegram pushes
updates to it (`RUN_MODE=webhook`, handled by `webhook_server.py`).

The container never calls `setWebhook` itself — it doesn't know its own
public URL until Cloud Run assigns one, and guessing wrong would crash it
before it could even start listening. So registering the webhook with
Telegram is a separate, explicit step you do once you know the URL.

1. Generate a webhook secret (used to reject requests that aren't really
   from Telegram):

   ```bash
   python -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

2. Deploy the container (Cloud Build builds it from source, no local Docker
   needed). `BOT_TOKEN` and `WEBHOOK_SECRET` hold real secrets, so pass them
   via Secret Manager rather than `--set-env-vars`:

   ```bash
   gcloud secrets create bot-token --data-file=- <<< "your-bot-token"
   gcloud secrets create webhook-secret --data-file=- <<< "the-secret-from-step-1"

   gcloud run deploy lightsticker \
     --source . \
     --region us-central1 \
     --allow-unauthenticated \
     --set-env-vars RUN_MODE=webhook,OWNER_USER_ID=123456789 \
     --set-secrets BOT_TOKEN=bot-token:latest,WEBHOOK_SECRET=webhook-secret:latest
   ```

   `--allow-unauthenticated` is required so Telegram's servers can reach the
   webhook endpoint. Note the URL the command prints when it finishes
   (`https://lightsticker-xxxxx.a.run.app`).

3. Register that URL with Telegram (path is the bot token, matching what
   `webhook_server.py` listens on):

   ```bash
   curl "https://api.telegram.org/bot<your-bot-token>/setWebhook" \
     -d "url=https://lightsticker-xxxxx.a.run.app/<your-bot-token>" \
     -d "secret_token=the-secret-from-step-1"
   ```

Send the bot a sticker to confirm it responds. If you ever redeploy to a
different URL (new service name, region, or custom domain), just repeat
step 3 with the new URL — `GET https://api.telegram.org/bot<token>/getWebhookInfo`
shows what Telegram currently has registered, useful for checking it's live
and see the `pending_update_count`/`last_error_message` fields if something's
wrong.
