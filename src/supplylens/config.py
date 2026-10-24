from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "sqlite:///./runtime/supplylens.db"
    agent_mode: Literal["demo", "cloud"] = "demo"
    embedding_mode: Literal["local", "cloud"] = "local"
    model_api_key: str = ""
    model_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model_name: str = "qwen-plus"
    model_extra_body: dict = Field(default_factory=lambda: {"enable_thinking": False})
    embedding_model: str = "text-embedding-v4"
    embedding_dimensions: int = 1024
    as_of_date: date = date(2026, 10, 24)
    sop_dir: Path = ROOT / "data" / "sop"
    index_dir: Path = Path("runtime/index")
    request_timeout_seconds: float = Field(default=60, gt=0)
    model_timeout_seconds: float = Field(default=20, gt=0)
    max_tool_calls: int = Field(default=8, ge=1, le=32)
    max_model_rounds: int = Field(default=5, ge=1, le=10)
    auto_seed: bool = True
    api_url: str = "http://127.0.0.1:8000"

