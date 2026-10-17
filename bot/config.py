"""
TeleSeek bot configuration.
Uses Pydantic Settings to validate environment variables.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Telegram Bot
    bot_token: str
    bot_name: str = "TeleSeek"

    # Database
    database_url: str

    # Administrators — comma-separated string: "123456789,987654321"
    # Deliberately str, not List[int]: pydantic-settings v2 tries to JSON-decode
    # List fields before validators run, which breaks the "id1,id2" format
    admin_ids: str = ""

    # Search settings
    search_results_per_page: int = 10
    max_query_length: int = 100

    # PostgreSQL (for docker-compose)
    postgres_db: str = "teleseek_db"
    postgres_user: str = "teleseek"
    postgres_password: str = "password"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    def get_admin_ids(self) -> list[int]:
        """Parse ADMIN_IDS from a comma-separated string."""
        if not self.admin_ids.strip():
            return []
        return [
            int(i.strip())
            for i in self.admin_ids.split(",")
            if i.strip().isdigit()
        ]


# Global settings instance
settings = Settings()
