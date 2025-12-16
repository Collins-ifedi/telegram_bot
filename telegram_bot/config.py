# config.py
"""
Production-grade configuration management.
Implements the 12-Factor App methodology using Pydantic V2.

Priority of Settings (Highest to Lowest):
1. Environment Variables (e.g., set in Docker/Render)
2. .env File (Local development)
3. config.yaml (Legacy/Complex configuration)
4. Default Values (Hardcoded fallbacks)
"""

import os
import logging
from pathlib import Path
from typing import Dict, List, Optional, Any
from functools import lru_cache

import yaml
from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict, PydanticBaseSettingsSource

# Setup basic logging for config loading
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("config")

BASE_DIR = Path(__file__).resolve().parent
YAML_CONFIG_PATH = BASE_DIR / "config.yaml"


# ------------------------------------------------------------------------------
# Custom YAML Source Loader
# ------------------------------------------------------------------------------
class YamlConfigSettingsSource(PydanticBaseSettingsSource):
    """
    Custom Pydantic source to load settings from a YAML file.
    It acts as a fallback for Environment Variables.
    """
    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        encoding = self.config.get('env_file_encoding')
        file_content_json = {}
        if YAML_CONFIG_PATH.exists():
            try:
                with open(YAML_CONFIG_PATH, "r", encoding=encoding) as f:
                    file_content_json = yaml.safe_load(f) or {}
            except Exception as e:
                logger.warning(f"Failed to load YAML config: {e}")
        
        field_value = file_content_json.get(field_name)
        return field_value, field_name, False

    def prepare_field_value(self, field_name: str, field: Any, value: Any, value_is_complex: bool) -> Any:
        return value

    def __call__(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {}
        if YAML_CONFIG_PATH.exists():
            with open(YAML_CONFIG_PATH, encoding='utf-8') as f:
                d = yaml.safe_load(f) or {}
        return d


# ------------------------------------------------------------------------------
# Main Settings Class
# ------------------------------------------------------------------------------
class Settings(BaseSettings):
    """
    Application Settings with strict typing and validation.
    """

    # --- Core Application ---
    APP_NAME: str = Field(default="Telegram Digital Product Bot")
    ENV: str = Field(default="production")
    
    # --- Telegram ---
    # SecretStr hides the value in logs (prints as '**********')
    TELEGRAM_TOKEN: SecretStr = Field(..., description="Main Bot API Token from @BotFather")
    
    # Critical for support functionality
    ADMIN_CHAT_ID: Optional[int] = Field(
        None, 
        description="Telegram Chat ID for Admin/Support notifications"
    )

    # --- Database ---
    DATABASE_URL: str = Field(
        default=f"postgresql+asyncpg://telegram_bot_uypn_user:o3RrL8uwLVBaoXWtP2aYOv3QNKcxOPKt@dpg-d50g0pf5r7bs739dlk00-a/telegram_bot_uypn",
        description="Connection string for SQLAlchemy"
    )
    
    # Database Tuning (Advanced)
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_ECHO: bool = False

    # --- Security ---
    # A random key is generated if not provided (safe for stateless, but better to set one)
    SECRET_KEY: SecretStr = Field(default_factory=lambda: SecretStr(os.urandom(32).hex()))
    
    # --- Languages ---
    DEFAULT_LANGUAGE: str = "en"
    SUPPORTED_LANGUAGES: Dict[str, str] = Field(
        default={
            "en": "English",
            "ru": "Русский",
            "ar": "العربية"
        }
    )

    # --- Payments ---
    PAYMENT_MODE: str = "manual"
    SUPPORTED_PAYMENTS: List[str] = Field(
        default=["binance_pay", "bybit_pay", "usdt_trc20", "usdt_erc20"]
    )
    
    # Payment Wallets (Mapped by payment method key)
    # These can be set in ENV like: WALLETS__USDT_TRC20="Tx..."
    WALLETS: Dict[str, str] = Field(
        default={
            "binance_pay": "NOT_SET",
            "bybit_pay": "NOT_SET",
            "usdt_trc20": "NOT_SET",
        }
    )

    # --- Admin Access ---
    SUPER_ADMIN_IDS: List[int] = Field(default=[])

    # --- Logging ---
    LOG_LEVEL: str = "INFO"
    LOG_FILE: str = str(BASE_DIR / "bot.log")

    # --- Pydantic Configuration ---
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,  # e.g., APP_NAME differs from app_name
        extra="ignore"        # Ignore extra keys in .env
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """
        Define the priority order of settings sources.
        1. Init arguments (Highest)
        2. Environment variables
        3. .env file
        4. YAML config file (Custom)
        5. Defaults (Lowest)
        """
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            YamlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )


# ------------------------------------------------------------------------------
# Singleton Export
# ------------------------------------------------------------------------------
@lru_cache()
def get_settings() -> Settings:
    """
    Creates and caches the settings object.
    Fails fast if critical config is missing.
    """
    try:
        return Settings()
    except ValidationError as e:
        logger.critical(f"🚨 CONFIGURATION ERROR: {e}")
        raise RuntimeError("Application failed to start due to invalid configuration.") from e

# Export the settings instance for direct use
settings = get_settings()

# ------------------------------------------------------------------------------
# Debug / Validation Script
# ------------------------------------------------------------------------------
if __name__ == "__main__":
    print(f"✅ Configuration Loaded: {settings.APP_NAME}")
    print(f"🌍 Environment: {settings.ENV}")
    print(f"📡 Database: {settings.DATABASE_URL}")
    print(f"🗣  Languages: {list(settings.SUPPORTED_LANGUAGES.keys())}")
    
    # Example of safely accessing a secret
    print(f"🔑 Token Set: {'Yes' if settings.TELEGRAM_TOKEN.get_secret_value() else 'No'}")
    
    if not settings.ADMIN_CHAT_ID:
        print("⚠ WARNING: ADMIN_CHAT_ID is not set. 'Contact' feature may fail.")