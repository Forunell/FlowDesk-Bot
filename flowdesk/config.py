import os
import secrets
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    database: Path
    password: str
    secure_cookie: bool = False
    operator_password: str = ""

    @classmethod
    def load(cls):
        env_path = ROOT / ".env"
        if not env_path.exists() and not os.getenv("FLOWDESK_ADMIN_PASSWORD"):
            password = secrets.token_urlsafe(24)
            with env_path.open("x", encoding="utf-8") as file:
                file.write(f"FLOWDESK_ADMIN_PASSWORD={password}\n")
        load_dotenv(env_path, encoding="utf-8-sig")
        password = os.getenv("FLOWDESK_ADMIN_PASSWORD", "")
        if len(password) < 12:
            raise ValueError("Set FLOWDESK_ADMIN_PASSWORD to at least 12 characters in .env.")
        path = Path(os.getenv("FLOWDESK_DB", "data/flowdesk.sqlite3"))
        operator = os.getenv("FLOWDESK_OPERATOR_PASSWORD", "")
        if operator and (len(operator) < 12 or operator == password):
            raise ValueError("Operator password must be distinct and at least 12 characters.")
        return cls(path if path.is_absolute() else ROOT / path, password,
                   os.getenv("FLOWDESK_COOKIE_SECURE", "false").lower() == "true", operator)
