from __future__ import annotations

import logging
import os
from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger("config")


def _strip_env(v: str) -> str:
    if not isinstance(v, str):
        return v
    s = v.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"":
        s = s[1:-1].strip()
    return s


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8-sig",
        extra="ignore",
    )

    app_name: str = "Закупки"
    secret_key: str = "change-me-in-production"
    app_password: str = "zakupki"
    data_dir: str = "/data"
    host: str = "0.0.0.0"
    port: int = 3000

    sender_name: str = "Отдел закупок"
    sender_company: str = ""

    mail_smtp_host: str = "smtp.yandex.ru"
    mail_smtp_port: int = 465
    mail_imap_host: str = "imap.yandex.ru"
    mail_imap_port: int = 993
    mail_user: str = ""
    mail_password: str = ""
    mail_from: str = ""
    mail_imap_folder: str = "INBOX"
    mail_poll_enabled: bool = True
    mail_poll_minutes: int = 3

    ai_api_key: str = ""
    ai_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    ai_model: str = "gemini-flash-latest"
    ai_fallback_model: str = "gemini-flash-lite-latest"
    ai_enabled: bool = True

    timezone: str = "Europe/Moscow"

    @field_validator(
        "app_name",
        "app_password",
        "secret_key",
        "sender_name",
        "sender_company",
        "mail_user",
        "mail_password",
        "mail_from",
        "mail_imap_folder",
        "mail_smtp_host",
        "mail_imap_host",
        "ai_api_key",
        "ai_base_url",
        "ai_model",
        "ai_fallback_model",
        "data_dir",
        mode="before",
    )
    @classmethod
    def _clean_str(cls, v):
        if isinstance(v, str):
            return _strip_env(v)
        return v

    @field_validator("mail_password", mode="after")
    @classmethod
    def _clean_mail_password(cls, v: str) -> str:
        return (v or "").replace(" ", "").replace("-", "")

    @property
    def mail_ready(self) -> bool:
        return bool(self.mail_user.strip() and self.mail_password.strip())

    @property
    def mail_from_addr(self) -> str:
        return (self.mail_from or self.mail_user or "").strip()

    @property
    def ai_ready(self) -> bool:
        return bool(self.ai_enabled and self.ai_api_key.strip())

    @property
    def data_root(self) -> Path:
        """Directory for SQLite + attachments. On Amvera must stay on the /data volume."""
        root = Path(self.data_dir).expanduser()
        # Never silently fall back when DATA_DIR is set or path is the Amvera volume —
        # otherwise DB/files land in ephemeral /app/data and disappear after rebuild.
        forced = bool(os.environ.get("DATA_DIR")) or root.as_posix() in ("/data", "/data/")
        try:
            root.mkdir(parents=True, exist_ok=True)
            test = root / ".write_test"
            test.write_text("ok", encoding="utf-8")
            test.unlink(missing_ok=True)
        except OSError as exc:
            if forced:
                raise RuntimeError(
                    f"Каталог данных недоступен для записи: {root} ({exc}). "
                    "На Amvera нужен persistenceMount=/data и переменная DATA_DIR=/data."
                ) from exc
            fallback = Path(__file__).resolve().parent.parent / "data"
            fallback.mkdir(parents=True, exist_ok=True)
            log.warning("DATA_DIR %s not writable (%s), using %s", root, exc, fallback)
            root = fallback
        return root

    @property
    def db_path(self) -> Path:
        return self.data_root / "zakupki.db"

    @property
    def attachments_dir(self) -> Path:
        path = self.data_root / "attachments"
        path.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache
def get_settings() -> Settings:
    return Settings()
