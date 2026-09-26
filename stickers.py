import logging
import re

from telegram import Bot, InputSticker, Sticker
from telegram.error import BadRequest

logger = logging.getLogger(__name__)

MAX_TITLE_LENGTH = 64

# Kept short so there's always room left in the 64-char set-name budget for
# the format/part suffixes and the "_by_<bot_username>" tail.
MAX_SLUG_LENGTH = 40

# Telegram limits for "regular" sticker sets (Bot API 7.x).
SET_LIMITS = {
    "static": 120,
    "animated": 50,
    "video": 50,
}

FORMAT_SUFFIX = {
    "static": "",
    "animated": "anim",
    "video": "vid",
}

DEFAULT_EMOJI = "\U0001F642"  # slightly smiling face

# Safety cap on how many "part N" sets we'll scan/create per format.
MAX_PARTS = 50

# Substrings (checked case-insensitively) Telegram uses when a set name doesn't exist yet.
_MISSING_SET_ERRORS = ("STICKERSET_INVALID", "STICKER_SET_INVALID", "STICKERSET_NOT_FOUND")

# Substrings Telegram uses when a set is already at its size limit.
_FULL_SET_ERRORS = ("STICKERS_TOO_MUCH", "TOO_MUCH_STICKERS")


def slugify_title(title: str) -> str:
    """Turns a freeform user-supplied title into a safe Telegram set-name slug.

    Lower-cased so that e.g. "Cat Memes" and "cat memes" resolve to the same
    underlying pack.
    """
    slug = re.sub(r"[^a-z0-9_]", "", title.lower())
    if not slug or not slug[0].isalpha():
        slug = "pack" + slug
    return slug[:MAX_SLUG_LENGTH]


def get_format(sticker: Sticker) -> str:
    if sticker.is_video:
        return "video"
    if sticker.is_animated:
        return "animated"
    return "static"


def _build_set_name(base: str, fmt: str, part: int, bot_username: str) -> str:
    suffix = FORMAT_SUFFIX[fmt]
    part_suffix = "" if part == 1 else f"p{part}"
    tail = f"_by_{bot_username}"
    head = f"{base}{suffix}{part_suffix}"
    max_head_len = 64 - len(tail)
    return head[:max_head_len] + tail


def _matches(exc: BadRequest, codes: tuple[str, ...]) -> bool:
    message = str(exc).upper()
    return any(code in message for code in codes)


async def add_sticker_for_user(
    bot: Bot,
    owner_user_id: int,
    base_name: str,
    set_title: str,
    bot_username: str,
    sticker: Sticker,
) -> str:
    """Adds `sticker` to the shared set, creating/rolling over sets as needed.

    Telegram itself is treated as the source of truth: no local state is kept.
    Returns the name of the set the sticker ended up in.
    Raises ValueError if the sticker was already added before.
    """
    fmt = get_format(sticker)
    emoji_list = [sticker.emoji] if sticker.emoji else [DEFAULT_EMOJI]
    input_sticker = InputSticker(sticker=sticker.file_id, emoji_list=emoji_list, format=fmt)

    for part in range(1, MAX_PARTS + 1):
        name = _build_set_name(base_name, fmt, part, bot_username)

        try:
            existing = await bot.get_sticker_set(name)
        except BadRequest as exc:
            if _matches(exc, _MISSING_SET_ERRORS):
                title = set_title if part == 1 else f"{set_title} {part}"
                await bot.create_new_sticker_set(
                    user_id=owner_user_id, name=name, title=title, stickers=[input_sticker]
                )
                return name
            raise

        if any(s.file_unique_id == sticker.file_unique_id for s in existing.stickers):
            raise ValueError("This sticker has already been added to the pack.")

        if len(existing.stickers) >= SET_LIMITS[fmt]:
            continue  # this part is full, try the next one

        try:
            await bot.add_sticker_to_set(user_id=owner_user_id, name=name, sticker=input_sticker)
            return name
        except BadRequest as exc:
            if _matches(exc, _FULL_SET_ERRORS):
                continue  # filled up concurrently, try the next part
            raise

    raise RuntimeError(f"Too many '{fmt}' pack parts (limit {MAX_PARTS}).")


async def list_current_sets(
    bot: Bot, base_name: str, bot_username: str
) -> dict[str, list[tuple[str, int]]]:
    """Queries Telegram for every existing set part, per format."""
    results: dict[str, list[tuple[str, int]]] = {}
    for fmt in SET_LIMITS:
        parts: list[tuple[str, int]] = []
        for part in range(1, MAX_PARTS + 1):
            name = _build_set_name(base_name, fmt, part, bot_username)
            try:
                existing = await bot.get_sticker_set(name)
            except BadRequest as exc:
                if _matches(exc, _MISSING_SET_ERRORS):
                    break
                raise
            parts.append((name, len(existing.stickers)))
        if parts:
            results[fmt] = parts
    return results
