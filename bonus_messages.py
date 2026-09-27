import random

from google.cloud import firestore

COLLECTION = "bonus_messages"

_client: firestore.AsyncClient | None = None


def _db() -> firestore.AsyncClient:
    global _client
    if _client is None:
        # No arguments needed: project + credentials resolve automatically via
        # Application Default Credentials (the Cloud Run service account in
        # production, `gcloud auth application-default login` locally).
        _client = firestore.AsyncClient()
    return _client


async def get_message(user_id: int) -> str:
    """Returns one bonus message for this user, or "" if none are configured.

    Doc id is the user's numeric Telegram id (as a string). Prefers messages
    that haven't been shown yet: picks one from the "messages" field and
    moves it to "seen_messages", so it won't repeat until every message has
    been shown once. Once "messages" runs dry, falls back to picking randomly
    from "seen_messages" - duplicates become possible again at that point,
    since there's nothing unseen left to offer.
    """
    doc_ref = _db().collection(COLLECTION).document(str(user_id))
    doc = await doc_ref.get()
    if not doc.exists:
        return ""

    unseen = list(doc.get("messages") or [])
    if unseen:
        message = random.choice(unseen)
        await doc_ref.update(
            {
                "messages": firestore.ArrayRemove([message]),
                "seen_messages": firestore.ArrayUnion([message]),
            }
        )
        return message

    seen = list(doc.get("seen_messages") or [])
    if not seen:
        return ""
    return random.choice(seen)
