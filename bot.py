import logging
import random

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, MessageOriginUser, Sticker, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import packs_db
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
ADMIN_ACTION_KEY = "admin_action"  # "grant" or "revoke" while awaiting a target user
PENDING_ADMIN_TARGET_KEY = "pending_admin_target"  # {"user_id", "name"} awaiting a pack choice
MOCK_USER_ID_KEY = "mock_user_id"  # owner-only: pretend to be this user id, for testing

CONFIRM_YES = "confirm_new:yes"
CONFIRM_NO = "confirm_new:no"
CONFIRM_DELETE = "confirm_delete"
CANCEL_ADD = "cancel_add"  # cancels whatever flow is currently pending
PACK_ADD_PREFIX = "pack_add:"
GRANT_PACK_PREFIX = "grant_pack:"
REVOKE_PACK_PREFIX = "revoke_pack:"

START_TEXT = (
    "Send me any sticker and I'll ask you which pack to add it to (creating it if it's new).\n\n"
    "Commands:\n"
    "/pack <title> - get links to an existing pack\n"
    "/mypacks - see packs you admin\n"
    "/delete - remove a sticker from its pack (requires admin rights on that pack)\n"
    "/grantadmin - let someone else add/delete for a pack (requires admin rights on that pack)\n"
    "/revokeadmin - take that back (requires admin rights on that pack)\n"
    "/whoami - get your numeric Telegram ID\n"
    "/cancel - back out of whatever's in progress"
)

OWNER_TEXT = (
    "\n\nOwner commands:\n"
    "/mock <user_id> - act as another user, for testing\n"
    "/unmock - stop acting as another user"
)


def _start_text_for(real_user_id: int, owner_id: int) -> str:
    if real_user_id == owner_id:
        return START_TEXT + OWNER_TEXT
    return START_TEXT


WAIT_MESSAGE = "Please wait a moment..."

CACHE_NOTE = (
    "\n\nTelegram caches sticker packs on your device, so this change might not show up right "
    "away — close the Telegram app completely and reopen it if the pack still looks unchanged."
)

async def _bonus_message(user_id: int) -> str:
    """A little something extra for whichever users have messages configured.

    Looked up from Firestore (`bonus_messages/{user_id}`, field "messages") -
    see packs_db.get_bonus_messages. Returns "" for anyone with none set.
    """
    messages = await packs_db.get_bonus_messages(user_id)
    if not messages:
        return ""
    return "\n\n" + random.choice(messages)


def _effective_user_id(context: ContextTypes.DEFAULT_TYPE, real_user_id: int) -> int:
    """Resolves the id to actually act as - the owner's active /mock target, if any.

    Only ever substitutes for the real owner; anyone else's real id passes
    through unchanged, since only the owner can set a mock in the first place.
    """
    config = context.bot_data["config"]
    if real_user_id == config.owner_user_id:
        mock_id = context.user_data.get(MOCK_USER_ID_KEY)
        if mock_id is not None:
            return mock_id
    return real_user_id


async def mock_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Hidden: silently ignore for anyone but the real owner, so its existence
    # isn't revealed. Always checked against the real id, never a mocked one.
    config = context.bot_data["config"]
    if update.effective_user.id != config.owner_user_id:
        return

    if not context.args or not context.args[0].lstrip("-").isdigit():
        await update.message.reply_text("Usage: /mock <user_id>")
        return

    mock_id = int(context.args[0])
    _clear_pending(context)
    context.user_data[MOCK_USER_ID_KEY] = mock_id
    await update.message.reply_text(f"Now acting as user {mock_id}. Use /unmock to stop.")


async def unmock_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = context.bot_data["config"]
    if update.effective_user.id != config.owner_user_id:
        return  # hidden, same as /mock

    _clear_pending(context)
    context.user_data[MOCK_USER_ID_KEY] = None
    await update.message.reply_text("No longer mocking - back to your own identity.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = context.bot_data["config"]
    await update.message.reply_text(_start_text_for(update.effective_user.id, config.owner_user_id))


async def whoami(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    name = f"@{user.username}" if user.username else user.first_name
    await update.message.reply_text(f"Your Telegram ID is {user.id} ({name}).")


async def pack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /pack <title> — shows links for that pack.")
        return

    title = " ".join(context.args)
    bot_username = context.bot_data["bot_username"]
    await update.message.reply_text(WAIT_MESSAGE)
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


async def mypacks(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = context.bot_data["config"]
    user_id = _effective_user_id(context, update.effective_user.id)
    await update.message.reply_text(WAIT_MESSAGE)
    packs = await packs_db.list_all() if user_id == config.owner_user_id else await packs_db.list_for_admin(user_id)
    if not packs:
        await update.message.reply_text("You're not an admin of any packs yet.")
        return
    lines = ["Packs you admin:"] + [f"- {title}" for _, title in sorted(packs, key=lambda p: p[1].lower())]
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
    context.user_data[ADMIN_ACTION_KEY] = None
    context.user_data[PENDING_ADMIN_TARGET_KEY] = None


async def _remove_prompt_keyboard(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Strips the buttons off the earlier prompt message, if any.

    Called once the user has moved past it (by typing, or by the flow
    otherwise progressing) so it doesn't linger as a stale, still-clickable
    keyboard.
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


async def grant_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _start_admin_action(update, context, mode="grant")


async def revoke_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _start_admin_action(update, context, mode="revoke")


async def _start_admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE, mode: str) -> None:
    config = context.bot_data["config"]
    user_id = _effective_user_id(context, update.effective_user.id)
    if user_id != config.owner_user_id:
        await update.message.reply_text(WAIT_MESSAGE)
        packs = await packs_db.list_for_admin(user_id)
        if not packs:
            await update.message.reply_text("You're not an admin of any packs yet.")
            return

    _clear_pending(context)
    context.user_data[ADMIN_ACTION_KEY] = mode
    match mode:
        case "grant":
            verb = "add"
        case "revoke":
            verb = "remove"
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Cancel", callback_data=CANCEL_ADD)]])
    prompt_message = await update.message.reply_text(
        f"Forward me a message from the person you want to {verb} as an admin, or send their "
        "numeric user ID directly (they can get it by sending /whoami to me).",
        reply_markup=keyboard,
    )
    context.user_data[PROMPT_MESSAGE_ID_KEY] = prompt_message.message_id


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return

    if context.user_data.get(ADMIN_ACTION_KEY):
        await _handle_admin_target(update, context)
        return

    if context.user_data.get(AWAITING_DELETE_KEY):
        if message.sticker:
            await _handle_delete_target(update, context)
        else:
            await message.reply_text("Send me a sticker to delete, or /cancel.")
        return

    if message.sticker:
        await _handle_new_sticker(update, context)
        return

    if message.text and context.user_data.get(PENDING_STICKER_KEY):
        await handle_title_reply(update, context)
        return

    # Nothing pending and nothing recognized - remind them what the bot does.
    config = context.bot_data["config"]
    await message.reply_text(_start_text_for(update.effective_user.id, config.owner_user_id))


async def _handle_new_sticker(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data[PENDING_STICKER_KEY] = update.message.sticker
    context.user_data[PENDING_TITLE_KEY] = None

    config = context.bot_data["config"]
    user_id = _effective_user_id(context, update.effective_user.id)
    await update.message.reply_text(WAIT_MESSAGE)
    packs = await packs_db.list_all() if user_id == config.owner_user_id else await packs_db.list_for_admin(user_id)

    buttons = [
        [InlineKeyboardButton(title, callback_data=f"{PACK_ADD_PREFIX}{slug}")]
        for slug, title in sorted(packs, key=lambda p: p[1].lower())
    ]
    buttons.append([InlineKeyboardButton("Cancel", callback_data=CANCEL_ADD)])

    prompt_text = (
        "Pick a pack to add this to, or type a new title:"
        if packs
        else "What's the title of the pack to add this to? (I'll create it if it doesn't exist yet.)"
    )
    prompt_message = await update.message.reply_text(
        prompt_text, reply_markup=InlineKeyboardMarkup(buttons)
    )
    context.user_data[PROMPT_MESSAGE_ID_KEY] = prompt_message.message_id


async def handle_pack_add_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    sticker: Sticker | None = context.user_data.get(PENDING_STICKER_KEY)
    if sticker is None:
        await query.edit_message_text("That request expired — send the sticker again.")
        return

    slug = query.data[len(PACK_ADD_PREFIX):]
    title = await packs_db.get_title(slug)
    if title is None:
        await query.edit_message_text("That pack no longer exists — send the sticker again.")
        return

    await query.edit_message_reply_markup(reply_markup=None)
    context.user_data[PENDING_STICKER_KEY] = None
    context.user_data[PROMPT_MESSAGE_ID_KEY] = None

    await _add_and_reply(
        context, query.message.chat_id, _effective_user_id(context, query.from_user.id), sticker, title
    )


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

    await update.message.reply_text(WAIT_MESSAGE)
    try:
        sticker_set = await context.bot.get_sticker_set(set_name)
    except TelegramError:
        await update.message.reply_text(not_ours_text)
        return

    config = context.bot_data["config"]
    slug = slugify_title(sticker_set.title)
    user_id = _effective_user_id(context, update.effective_user.id)
    if not await packs_db.is_admin(slug, user_id, config.owner_user_id):
        await update.message.reply_text(
            f'You don\'t have admin rights on "{sticker_set.title}", so I can\'t delete from it.'
        )
        return

    context.user_data[PENDING_DELETE_KEY] = {
        "file_id": sticker.file_id,
        "title": sticker_set.title,
        "slug": slug,
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
    await context.bot.send_message(query.message.chat_id, WAIT_MESSAGE)

    config = context.bot_data["config"]
    user_id = _effective_user_id(context, query.from_user.id)
    if not await packs_db.is_admin(pending["slug"], user_id, config.owner_user_id):
        await context.bot.send_message(
            query.message.chat_id,
            f'You don\'t have admin rights on "{pending["title"]}" anymore, so I can\'t delete from it.',
        )
        return

    try:
        await context.bot.delete_sticker_from_set(sticker=pending["file_id"])
        bonus = await _bonus_message(user_id)
        await context.bot.send_message(
            query.message.chat_id,
            f'Deleted from "{pending["title"]}".{CACHE_NOTE}{bonus}',
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
    await update.message.reply_text(WAIT_MESSAGE)

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
    await _add_and_reply(
        context,
        update.effective_chat.id,
        _effective_user_id(context, update.effective_user.id),
        sticker,
        title,
        already_waited=True,
    )


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
        await _add_and_reply(
            context, query.message.chat_id, _effective_user_id(context, query.from_user.id), sticker, title
        )
    else:
        await context.bot.send_message(query.message.chat_id, "Okay, what's the correct title?")


async def _add_and_reply(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    user_id: int,
    sticker: Sticker,
    title: str,
    already_waited: bool = False,
) -> None:
    config = context.bot_data["config"]
    bot_username = context.bot_data["bot_username"]
    slug = slugify_title(title)

    if not already_waited:
        await context.bot.send_message(chat_id, WAIT_MESSAGE)
    allowed = await packs_db.claim_or_check(slug, title, user_id, config.owner_user_id)
    if not allowed:
        await context.bot.send_message(
            chat_id, f'You don\'t have admin rights on "{title}", so I can\'t add to it.'
        )
        return

    try:
        set_name = await add_sticker_for_user(
            bot=context.bot,
            owner_user_id=config.owner_user_id,
            base_name=slug,
            set_title=title,
            bot_username=bot_username,
            sticker=sticker,
        )
        bonus = await _bonus_message(user_id)
        await context.bot.send_message(
            chat_id,
            f'Added to "{title}"! View the pack: https://t.me/addstickers/{set_name}'
            f"{CACHE_NOTE}{bonus}",
        )
    except ValueError as exc:
        await context.bot.send_message(chat_id, str(exc))
    except TelegramError as exc:
        logger.exception("Failed to add sticker")
        await context.bot.send_message(chat_id, f"Sorry, couldn't add that sticker: {exc}")


async def _handle_admin_target(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    mode = context.user_data.get(ADMIN_ACTION_KEY)

    target_id: int | None = None
    target_name: str | None = None

    origin = message.forward_origin
    if isinstance(origin, MessageOriginUser):
        target_id = origin.sender_user.id
        target_name = (
            f"@{origin.sender_user.username}" if origin.sender_user.username else origin.sender_user.first_name
        )
    elif message.text and message.text.strip().lstrip("-").isdigit():
        target_id = int(message.text.strip())
        target_name = f"user {target_id}"

    if target_id is None:
        await message.reply_text(
            "I couldn't identify who that is. Forward a message from them, or send their "
            "numeric user ID directly (they can get it by sending /whoami to me)."
        )
        return

    await _remove_prompt_keyboard(context, update.effective_chat.id)
    context.user_data[ADMIN_ACTION_KEY] = None
    await message.reply_text(WAIT_MESSAGE)

    config = context.bot_data["config"]
    acting_user_id = _effective_user_id(context, update.effective_user.id)

    if mode == "grant":
        packs = (
            await packs_db.list_all()
            if acting_user_id == config.owner_user_id
            else await packs_db.list_for_admin(acting_user_id)
        )
        prefix = GRANT_PACK_PREFIX
        empty_text = "You don't have admin rights on any packs to grant."
        prompt_verb = "added to"
    else:
        target_packs = await packs_db.list_for_admin(target_id)
        if acting_user_id == config.owner_user_id:
            packs = target_packs
        else:
            acting_slugs = {slug for slug, _ in await packs_db.list_for_admin(acting_user_id)}
            packs = [(slug, title) for slug, title in target_packs if slug in acting_slugs]
        prefix = REVOKE_PACK_PREFIX
        empty_text = f"{target_name} isn't an admin of any packs you control."
        prompt_verb = "removed from"

    if not packs:
        context.user_data[PENDING_ADMIN_TARGET_KEY] = None
        await message.reply_text(empty_text)
        return

    context.user_data[PENDING_ADMIN_TARGET_KEY] = {"user_id": target_id, "name": target_name}
    buttons = [
        [InlineKeyboardButton(title, callback_data=f"{prefix}{slug}")]
        for slug, title in sorted(packs, key=lambda p: p[1].lower())
    ]
    buttons.append([InlineKeyboardButton("Cancel", callback_data=CANCEL_ADD)])
    prompt_message = await message.reply_text(
        f"Which pack should {target_name} be {prompt_verb} as an admin?",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    context.user_data[PROMPT_MESSAGE_ID_KEY] = prompt_message.message_id


async def handle_admin_pack_choice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    target = context.user_data.get(PENDING_ADMIN_TARGET_KEY)
    if target is None:
        await query.edit_message_text("That request expired — start over with /grantadmin or /revokeadmin.")
        return

    is_grant = query.data.startswith(GRANT_PACK_PREFIX)
    slug = query.data[len(GRANT_PACK_PREFIX if is_grant else REVOKE_PACK_PREFIX):]
    await context.bot.send_message(query.message.chat_id, WAIT_MESSAGE)
    title = await packs_db.get_title(slug)

    await query.edit_message_reply_markup(reply_markup=None)
    context.user_data[PENDING_ADMIN_TARGET_KEY] = None

    if title is None:
        await context.bot.send_message(query.message.chat_id, "That pack no longer exists.")
        return

    if is_grant:
        await packs_db.grant(slug, target["user_id"])
        await context.bot.send_message(
            query.message.chat_id, f'Added {target["name"]} as an admin of "{title}".'
        )
    else:
        await packs_db.revoke(slug, target["user_id"])
        await context.bot.send_message(
            query.message.chat_id, f'Removed {target["name"]} as an admin of "{title}".'
        )


async def on_startup(application: Application) -> None:
    me = await application.bot.get_me()
    application.bot_data["bot_username"] = me.username


def main() -> None:
    config = load_config()

    application = Application.builder().token(config.bot_token).post_init(on_startup).build()
    application.bot_data["config"] = config

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("whoami", whoami))
    application.add_handler(CommandHandler("pack", pack))
    application.add_handler(CommandHandler("mypacks", mypacks))
    application.add_handler(CommandHandler("delete", delete_command))
    application.add_handler(CommandHandler("grantadmin", grant_admin_command))
    application.add_handler(CommandHandler("revokeadmin", revoke_admin_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("mock", mock_command))
    application.add_handler(CommandHandler("unmock", unmock_command))
    application.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_confirm_new, pattern="^confirm_new:"))
    application.add_handler(CallbackQueryHandler(handle_confirm_delete, pattern=f"^{CONFIRM_DELETE}:"))
    application.add_handler(CallbackQueryHandler(handle_pack_add_choice, pattern=f"^{PACK_ADD_PREFIX}"))
    application.add_handler(
        CallbackQueryHandler(handle_admin_pack_choice, pattern=f"^({GRANT_PACK_PREFIX}|{REVOKE_PACK_PREFIX})")
    )
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
