import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_user_id: int
    run_mode: str
    port: int
    webhook_secret: str | None


def load_config() -> Config:
    bot_token = os.environ["BOT_TOKEN"]
    owner_user_id = int(os.environ["OWNER_USER_ID"])
    run_mode = os.environ.get("RUN_MODE", "polling")
    if run_mode not in ("polling", "webhook"):
        raise ValueError(f"RUN_MODE must be 'polling' or 'webhook', got {run_mode!r}")
    port = int(os.environ.get("PORT", "8080"))
    webhook_secret = os.environ.get("WEBHOOK_SECRET") or None
    return Config(
        bot_token=bot_token,
        owner_user_id=owner_user_id,
        run_mode=run_mode,
        port=port,
        webhook_secret=webhook_secret,
    )
