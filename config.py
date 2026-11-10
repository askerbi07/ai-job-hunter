"""Конфигурация приложения: .env, пути, профиль кандидата (profile.yaml)."""
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

import yaml
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _parse_cron(expr: str) -> tuple[int, int]:
    """Разбирает cron 'M H * * *' в (минута, час). Валидные значения только они."""
    parts = (expr or "").split()
    if len(parts) < 2:
        return 0, 7
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return 0, 7


@dataclass(frozen=True)
class Settings:
    ollama_base_url: str
    ollama_model: str
    ollama_api_key: str
    hh_user_agent: str
    score_threshold: int
    run_cron: tuple[int, int]
    db_path: Path
    profile_path: Path
    scheduler_enabled: bool


def _settings() -> Settings:
    return Settings(
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1").rstrip("/"),
        ollama_model=os.getenv("OLLAMA_MODEL", "qwen2.5:14b-instruct"),
        ollama_api_key=os.getenv("OLLAMA_API_KEY", "ollama"),
        hh_user_agent=os.getenv("HH_USER_AGENT", "AIJobHunter/1.0 (local)"),
        score_threshold=int(os.getenv("SCORE_THRESHOLD", "7")),
        run_cron=_parse_cron(os.getenv("RUN_CRON", "0 7 * * *")),
        db_path=(BASE_DIR / os.getenv("DB_PATH", "data/jobs.db")).resolve(),
        profile_path=(BASE_DIR / os.getenv("PROFILE_PATH", "profile.yaml")).resolve(),
        scheduler_enabled=os.getenv("SCHEDULER_ENABLED", "1").lower() not in ("0", "false", "no"),
    )


settings = _settings()


def load_profile() -> dict:
    """Загружает профиль кандидата. Если profile.yaml нет — берёт profile.example.yaml."""
    path = settings.profile_path
    if not path.exists() and path.name == "profile.yaml":
        candidate = BASE_DIR / "profile.example.yaml"
        if candidate.exists():
            path = candidate
    if not path.exists():
        raise FileNotFoundError(
            f"Не найден профиль: {settings.profile_path}. "
            "Скопируйте profile.example.yaml в profile.yaml и заполните."
        )
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError("profile.yaml должен быть сопоставлением ключ-значение (маппингом)")
    return data


def profile_hash(profile: dict) -> str:
    """Хэш профиля: если профиль изменился — LLM-оценка перезапускается (кэш в БД)."""
    canonical = json.dumps(profile, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
