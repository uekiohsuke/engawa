"""環境変数（.env）からの設定読み込み。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Settings:
    llm_backend: str = "mock"
    llm_url: str = "http://192.168.0.178:11434"
    llm_model: str = "gemma4:12b"
    core_host: str = "127.0.0.1"
    core_port: int = 8765
    data_dir: Path = PROJECT_ROOT / "data"
    history_window: int = 20

    @property
    def db_path(self) -> Path:
        return self.data_dir / "engawa.db"

    @property
    def core_base_url(self) -> str:
        return f"http://{self.core_host}:{self.core_port}"


def load_settings() -> Settings:
    load_dotenv(PROJECT_ROOT / ".env")
    defaults = Settings()
    data_dir = Path(os.getenv("ENGAWA_DATA_DIR", str(defaults.data_dir)))
    if not data_dir.is_absolute():
        data_dir = PROJECT_ROOT / data_dir
    return Settings(
        llm_backend=os.getenv("ENGAWA_LLM_BACKEND", defaults.llm_backend),
        llm_url=os.getenv("ENGAWA_LLM_URL", defaults.llm_url).rstrip("/"),
        llm_model=os.getenv("ENGAWA_LLM_MODEL", defaults.llm_model),
        core_host=os.getenv("ENGAWA_CORE_HOST", defaults.core_host),
        core_port=int(os.getenv("ENGAWA_CORE_PORT", defaults.core_port)),
        data_dir=data_dir,
        history_window=int(os.getenv("ENGAWA_HISTORY_WINDOW", defaults.history_window)),
    )
