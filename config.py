import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_user_id: int


def load_config() -> Config:
    bot_token = os.environ["BOT_TOKEN"]
    owner_user_id = int(os.environ["OWNER_USER_ID"])
    return Config(bot_token=bot_token, owner_user_id=owner_user_id)
