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

import webhook_server
from config import load_config
from stickers import MAX_TITLE_LENGTH, add_sticker_for_user, list_current_sets, slugify_title

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

PENDING_STICKER_KEY = "pending_sticker"
PENDING_TITLE_KEY = "pending_title"
PROMPT_MESSAGE_ID_KEY = "prompt_message_id"
AWAITING_DELETE_KEY = "awaiting_delete"
PENDING_DELETE_KEY = "pending_delete"

CONFIRM_YES = "confirm_new:yes"
CONFIRM_NO = "confirm_new:no"
CONFIRM_DELETE = "confirm_delete"
CANCEL_ADD = "cancel_add"  # cancels whatever flow (add or delete) is currently pending

START_TEXT = (
    "Send me any sticker and I'll ask you which pack to add it to (creating it if it's new).\n\n"
    "Use /pack <title> to get links to an existing pack, /delete to remove a sticker from its "
    "pack, or /cancel to back out of either."
)

CACHE_NOTE = (
    "\n\nTelegram caches sticker packs on your device, so this change might not show up right "
    "away — close the Telegram app completely and reopen it if the pack still looks unchanged."
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


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _remove_prompt_keyboard(context, update.effective_chat.id)
    _clear_pending(context)
    await update.message.reply_text("Cancelled.")


async def handle_cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _clear_pending(context)
    await query.edit_message_text("Cancelled.")


def _clear_pending(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data[PENDING_STICKER_KEY] = None
    context.user_data[PENDING_TITLE_KEY] = None
    context.user_data[PROMPT_MESSAGE_ID_KEY] = None
    context.user_data[AWAITING_DELETE_KEY] = False
    context.user_data[PENDING_DELETE_KEY] = None


async def _remove_prompt_keyboard(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Strips the Cancel button off the earlier title-prompt message, if any.

    Called once the user has moved past it by typing instead of tapping it,
    so it doesn't linger as a stale, still-clickable button.
    """
    message_id = context.user_data.get(PROMPT_MESSAGE_ID_KEY)
    if message_id is None:
        return
    context.user_data[PROMPT_MESSAGE_ID_KEY] = None
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=None
        )
    except TelegramError:
        pass  # message may already be edited/gone - nothing to clean up


async def delete_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _clear_pending(context)
    context.user_data[AWAITING_DELETE_KEY] = True
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data=CANCEL_ADD)]])
    prompt_message = await update.message.reply_text(
        "Send me the sticker you want to delete from its pack.",
        reply_markup=keyboard,
    )
    context.user_data[PROMPT_MESSAGE_ID_KEY] = prompt_message.message_id


async def handle_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get(AWAITING_DELETE_KEY):
        await _handle_delete_target(update, context)
        return

    context.user_data[PENDING_STICKER_KEY] = update.message.sticker
    context.user_data[PENDING_TITLE_KEY] = None
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data=CANCEL_ADD)]])
    prompt_message = await update.message.reply_text(
        "What's the title of the pack to add this to? (I'll create it if it doesn't exist yet.)",
        reply_markup=keyboard,
    )
    context.user_data[PROMPT_MESSAGE_ID_KEY] = prompt_message.message_id


async def _handle_delete_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data[AWAITING_DELETE_KEY] = False
    await _remove_prompt_keyboard(context, update.effective_chat.id)

    sticker = update.message.sticker
    bot_username = context.bot_data["bot_username"]
    set_name = sticker.set_name

    not_ours_text = "That sticker isn't from a pack created by this bot, so I can't delete it."
    if not set_name or not set_name.lower().endswith(f"_by_{bot_username.lower()}"):
        await update.message.reply_text(not_ours_text)
        return

    try:
        sticker_set = await context.bot.get_sticker_set(set_name)
    except TelegramError:
        await update.message.reply_text(not_ours_text)
        return

    context.user_data[PENDING_DELETE_KEY] = {
        "file_id": sticker.file_id,
        "title": sticker_set.title,
    }
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Yes, delete it", callback_data=f"{CONFIRM_DELETE}:yes"),
                InlineKeyboardButton("Cancel", callback_data=CANCEL_ADD),
            ]
        ]
    )
    await update.message.reply_text(
        f'Delete this sticker from "{sticker_set.title}"?',
        reply_markup=keyboard,
    )


async def handle_confirm_delete(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    pending = context.user_data.get(PENDING_DELETE_KEY)
    if pending is None:
        await query.edit_message_text("That request expired — send /delete again.")
        return

    await query.edit_message_reply_markup(reply_markup=None)
    context.user_data[PENDING_DELETE_KEY] = None

    try:
        await context.bot.delete_sticker_from_set(sticker=pending["file_id"])
        await context.bot.send_message(
            query.message.chat_id, f'Deleted from "{pending["title"]}".{CACHE_NOTE}'
        )
    except TelegramError as exc:
        logger.exception("Failed to delete sticker")
        await context.bot.send_message(
            query.message.chat_id, f"Sorry, couldn't delete that sticker: {exc}"
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

    await _remove_prompt_keyboard(context, update.effective_chat.id)

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
                ],
                [InlineKeyboardButton("Cancel", callback_data=CANCEL_ADD)],
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
            chat_id,
            f'Added to "{title}"! View the pack: https://t.me/addstickers/{set_name}{CACHE_NOTE}',
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
    application.add_handler(CommandHandler("delete", delete_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(MessageHandler(filters.Sticker.ALL, handle_sticker))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_title_reply))
    application.add_handler(CallbackQueryHandler(handle_confirm_new, pattern="^confirm_new:"))
    application.add_handler(CallbackQueryHandler(handle_confirm_delete, pattern=f"^{CONFIRM_DELETE}:"))
    application.add_handler(CallbackQueryHandler(handle_cancel_callback, pattern=f"^{CANCEL_ADD}$"))

    if config.run_mode == "webhook":
        webhook_server.run(
            application,
            port=config.port,
            webhook_secret=config.webhook_secret,
            url_path=config.bot_token,
        )
    else:
        application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
