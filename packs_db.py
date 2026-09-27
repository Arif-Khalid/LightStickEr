from google.cloud import firestore
from google.cloud.firestore import FieldFilter

COLLECTION = "packs"

_client: firestore.AsyncClient | None = None


def _db() -> firestore.AsyncClient:
    global _client
    if _client is None:
        # No arguments needed: project + credentials resolve automatically via
        # Application Default Credentials (the Cloud Run service account in
        # production, `gcloud auth application-default login` locally).
        _client = firestore.AsyncClient()
    return _client


async def is_admin(slug: str, user_id: int, owner_id: int) -> bool:
    if user_id == owner_id:
        return True
    doc = await _db().collection(COLLECTION).document(slug).get()
    if not doc.exists:
        return False
    return user_id in (doc.get("admins") or [])


async def claim_or_check(slug: str, title: str, user_id: int, owner_id: int) -> bool:
    """Returns whether `user_id` may add a sticker to this pack.

    A pack with no admin record yet is unclaimed - the first person to add to
    it becomes its sole admin. Once claimed, only its admins (or the bot
    owner) may add further stickers.
    """
    if user_id == owner_id:
        return True
    doc_ref = _db().collection(COLLECTION).document(slug)
    doc = await doc_ref.get()
    if not doc.exists:
        await doc_ref.set(
            {
                "title": title,
                "admins": [user_id],
                "created_by": user_id,
                "created_at": firestore.SERVER_TIMESTAMP,
            }
        )
        return True
    return user_id in (doc.get("admins") or [])


async def grant(slug: str, user_id: int) -> None:
    await _db().collection(COLLECTION).document(slug).update(
        {"admins": firestore.ArrayUnion([user_id])}
    )


async def revoke(slug: str, user_id: int) -> None:
    await _db().collection(COLLECTION).document(slug).update(
        {"admins": firestore.ArrayRemove([user_id])}
    )


async def get_title(slug: str) -> str | None:
    doc = await _db().collection(COLLECTION).document(slug).get()
    return doc.get("title") if doc.exists else None


async def list_for_admin(user_id: int) -> list[tuple[str, str]]:
    """Returns [(slug, title), ...] for packs where `user_id` is an admin."""
    query = _db().collection(COLLECTION).where(filter=FieldFilter("admins", "array_contains", user_id))
    return [(doc.id, doc.get("title")) async for doc in query.stream()]


async def list_all() -> list[tuple[str, str]]:
    """Returns [(slug, title), ...] for every registered pack."""
    return [(doc.id, doc.get("title")) async for doc in _db().collection(COLLECTION).stream()]
