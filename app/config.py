from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

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
    mail_poll_minutes: int = 10

    ai_api_key: str = ""
    ai_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai/"
    ai_model: str = "gemini-2.5-flash"
    ai_enabled: bool = True

    timezone: str = "Europe/Moscow"

    @property
    def mail_ready(self) -> bool:
        user = self.mail_user.strip()
        password = self.mail_password.strip()
        return bool(user and password)

    @property
    def mail_from_addr(self) -> str:
        return (self.mail_from or self.mail_user or "").strip()

    @property
    def ai_ready(self) -> bool:
        return bool(self.ai_enabled and self.ai_api_key.strip())

    @property
    def db_path(self) -> Path:
        root = Path(self.data_dir)
        if not root.exists():
            try:
                root.mkdir(parents=True, exist_ok=True)
            except OSError:
                root = Path(__file__).resolve().parent.parent / "data"
                root.mkdir(parents=True, exist_ok=True)
        try:
            test = root / ".write_test"
            test.write_text("ok", encoding="utf-8")
            test.unlink(missing_ok=True)
        except OSError:
            root = Path(__file__).resolve().parent.parent / "data"
            root.mkdir(parents=True, exist_ok=True)
        return root / "zakupki.db"


@lru_cache
def get_settings() -> Settings:
    return Settings()
