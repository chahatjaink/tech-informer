from __future__ import annotations

import os
from pathlib import Path


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_dotenv() -> None:
    candidates = [Path.cwd() / ".env", Path(__file__).resolve().parents[1] / ".env"]
    for path in candidates:
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)
        return


def find_root() -> Path:
    override = os.environ.get("BRIEF_ROOT")
    if override:
        return Path(override)
    here = Path(__file__).resolve()
    for candidate in (Path.cwd(), here.parents[1]):
        if (candidate / "extension" / "sidepanel.html").exists():
            return candidate
    return here.parents[1]


class Settings:
    def __init__(self) -> None:
        self.root = find_root()
        self.data_dir = Path(os.environ.get("BRIEF_DATA_DIR", self.root / "data"))
        self.db_path = self.data_dir / "brief.sqlite"
        self.master_key_path = self.data_dir / "master.key"
        self.api_token = os.environ.get("BRIEF_API_TOKEN", "dev-token")
        self.auth = os.environ.get("BRIEF_AUTH", "dev")
        self.allowed_email = os.environ.get("BRIEF_ALLOWED_EMAIL", "").strip().lower()
        self.fake_providers = env_bool("BRIEF_FAKE_PROVIDERS", False)
        self.extension_dir = self.root / "extension"


def get_settings() -> Settings:
    load_dotenv()
    return Settings()
