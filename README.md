# LightStickEr

A Telegram bot that lets multiple people contribute stickers to shared, named sticker sets.

Telegram's Bot API only lets a bot add stickers to a set *that the bot itself created*
(and bots can't message other bots, so relaying through `@Stickers` isn't possible).
This bot works around that the standard way: it creates and owns the shared set(s)
itself, and adds any sticker a user sends it straight into the set via the API.

## How it works

1. Send the bot any sticker.
2. If you already admin any packs, it shows them as buttons to pick from —
   or you can type a title instead, whether for one of those or a brand new one.
   Titles are looked up by a slugified version, so "Cat Memes" and "cat memes"
   are the same pack.
   - If a pack with that name already exists, the sticker is added to it
     (assuming you have admin rights there - see below).
   - If not, the bot asks you to confirm before creating a brand new pack —
     this catches the common mistake of mistyping an existing pack's name and
     accidentally starting a duplicate instead of adding to it. Creating a
     pack this way makes you its first admin.

Static, animated, and video stickers can't share one set, so each pack keeps one
active set per format (created lazily) and automatically starts a new "part" of
a set once the current one hits Telegram's size limit (120 for static, 50 for
animated/video).

**Sticker/set state isn't stored anywhere** - set names are always derivable
from the pack title, so the bot just calls `getStickerSet` on Telegram to check
what already exists, whether a sticker was already added, and whether a part is
full, before adding or creating a set. The trade-off is one extra Telegram API
call per submission (and a short scan across parts for `/pack`) - cheap at the
scale this bot is meant for.

**Pack admin rights are the one thing that does need real storage** (a
Firestore database - see Setup below), since that's bookkeeping only this bot
knows about; Telegram has no concept of it. `OWNER_USER_ID` is always an
implicit admin of every pack. Anyone else only gets admin rights on a pack
by being granted them (see `/grantadmin` below) or by being the first person
to add to a pack that has no admins yet (i.e. a brand new pack, or one created
before this feature existed).

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token it gives you.
2. Get your own numeric Telegram user id from [@userinfobot](https://t.me/userinfobot),
   and make sure that user has sent this bot a `/start` at least once
   (Telegram requires the "owner" id to be a user the bot has seen before).
3. Create a Firestore database (Native mode) in your GCP project, if you
   haven't already - one-time setup, e.g.:

   ```bash
   gcloud firestore databases create --location=us-central1 --type=firestore-native
   ```

4. Copy `.env.example` to `.env` and fill in `BOT_TOKEN`, `OWNER_USER_ID`, and
   `GOOGLE_CLOUD_PROJECT`. Then run `gcloud auth application-default login`
   once so the Firestore client can authenticate locally the same way it
   authenticates via the Cloud Run service account in production.
5. Install dependencies (using the existing Python installation):

   ```bash
   python -m venv .venv
   .venv/Scripts/activate   # on Windows
   pip install -r requirements.txt
   ```

6. Run the bot:

   ```bash
   python bot.py
   ```

## Usage

- Send the bot any sticker, then pick a pack (or type a title) when it asks.
- `/delete` then send a sticker to remove it from its pack. The sticker must
  actually be from a pack this bot created (checked via its `set_name`
  suffix, which Telegram always sets to `_by_<bot_username>`) - otherwise
  you'll get an error instead of a delete prompt. You'll be asked to confirm,
  and the pack's title is named in the confirmation.
- Adding to or deleting from an existing pack requires admin rights on it
  (see "How it works" above for how those are granted/claimed).
- `/grantadmin` / `/revokeadmin` - only usable if you already admin at least
  one pack (or are the bot owner). Forward a message from the person you
  want to add/remove, or send their numeric ID directly (get your own with
  `/whoami`), then pick which of your packs it applies to from the buttons shown.
- `/mypacks` lists the packs you currently admin.
- `/pack <title>` lists links to an existing pack's set(s).
- A "Cancel" button is attached to every prompt along the way; `/cancel`
  works too if you'd rather type it.
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
   via Secret Manager rather than `--set-env-vars`. `GOOGLE_CLOUD_PROJECT`
   doesn't need to be set here - Cloud Run injects it automatically:

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

   The service's runtime service account also needs access to Firestore
   (grant this once per project):

   ```bash
   gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
     --member="serviceAccount:YOUR_PROJECT_NUMBER-compute@developer.gserviceaccount.com" \
     --role="roles/datastore.user"
   ```

   (that's the default Compute Engine service account Cloud Run uses unless
   you configured a custom one with `--service-account`).

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
