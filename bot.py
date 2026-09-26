import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Sticker, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config import load_config
from stickers import MAX_TITLE_LENGTH, add_sticker_for_user, list_current_sets, slugify_title

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

PENDING_STICKER_KEY = "pending_sticker"
PENDING_TITLE_KEY = "pending_title"

CONFIRM_YES = "confirm_new:yes"
CONFIRM_NO = "confirm_new:no"

START_TEXT = (
    "Send me any sticker and I'll ask you which pack to add it to (creating it if it's new).\n\n"
    "Use /pack <title> to get links to an existing pack."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(START_TEXT)


async def pack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /pack <title> — shows links for that pack.")
        return

    title = " ".join(context.args)
    bot_username = context.bot_data["bot_username"]
    results = await list_current_sets(context.bot, slugify_title(title), bot_username)
    if not results:
        await update.message.reply_text(f'No pack named "{title}" exists yet.')
        return

    lines = [f'Pack "{title}":']
    for fmt, parts in results.items():
        lines.append(f"\n{fmt.title()}:")
        for set_name, count in parts:
            lines.append(f"- https://t.me/addstickers/{set_name} ({count} stickers)")
    await update.message.reply_text("\n".join(lines))


async def handle_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data[PENDING_STICKER_KEY] = update.message.sticker
    context.user_data[PENDING_TITLE_KEY] = None
    await update.message.reply_text(
        "What's the title of the pack to add this to? (I'll create it if it doesn't exist yet.)"
    )


async def handle_title_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    sticker: Sticker | None = context.user_data.get(PENDING_STICKER_KEY)
    if sticker is None:
        return  # not currently expecting a title, ignore stray text

    title = update.message.text.strip()
    if not title:
        await update.message.reply_text("Please send a non-empty title.")
        return
    title = title[:MAX_TITLE_LENGTH]

    bot_username = context.bot_data["bot_username"]
    existing = await list_current_sets(context.bot, slugify_title(title), bot_username)

    if not existing:
        # No pack with this name exists yet - confirm before creating one, in
        # case the user actually meant an existing pack but mistyped its name.
        context.user_data[PENDING_TITLE_KEY] = title
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Yes, create it", callback_data=CONFIRM_YES),
                    InlineKeyboardButton("No, let me retype", callback_data=CONFIRM_NO),
                ]
            ]
        )
        await update.message.reply_text(
            f'No pack named "{title}" exists yet — create a new one?',
            reply_markup=keyboard,
        )
        return

    context.user_data[PENDING_STICKER_KEY] = None
    await _add_and_reply(context, update.effective_chat.id, sticker, title)


async def handle_confirm_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    sticker: Sticker | None = context.user_data.get(PENDING_STICKER_KEY)
    title = context.user_data.get(PENDING_TITLE_KEY)
    if sticker is None or title is None:
        await query.edit_message_text("That request expired — send the sticker again.")
        return

    await query.edit_message_reply_markup(reply_markup=None)
    context.user_data[PENDING_TITLE_KEY] = None

    if query.data == CONFIRM_YES:
        context.user_data[PENDING_STICKER_KEY] = None
        await _add_and_reply(context, query.message.chat_id, sticker, title)
    else:
        await context.bot.send_message(query.message.chat_id, "Okay, what's the correct title?")


async def _add_and_reply(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, sticker: Sticker, title: str
) -> None:
    config = context.bot_data["config"]
    bot_username = context.bot_data["bot_username"]

    try:
        set_name = await add_sticker_for_user(
            bot=context.bot,
            owner_user_id=config.owner_user_id,
            base_name=slugify_title(title),
            set_title=title,
            bot_username=bot_username,
            sticker=sticker,
        )
        await context.bot.send_message(
            chat_id, f'Added to "{title}"! View the pack: https://t.me/addstickers/{set_name}'
        )
    except ValueError as exc:
        await context.bot.send_message(chat_id, str(exc))
    except TelegramError as exc:
        logger.exception("Failed to add sticker")
        await context.bot.send_message(chat_id, f"Sorry, couldn't add that sticker: {exc}")


async def on_startup(application: Application) -> None:
    me = await application.bot.get_me()
    application.bot_data["bot_username"] = me.username


def main() -> None:
    config = load_config()

    application = Application.builder().token(config.bot_token).post_init(on_startup).build()
    application.bot_data["config"] = config

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("pack", pack))
    application.add_handler(MessageHandler(filters.Sticker.ALL, handle_sticker))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_title_reply))
    application.add_handler(CallbackQueryHandler(handle_confirm_new, pattern="^confirm_new:"))

    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
