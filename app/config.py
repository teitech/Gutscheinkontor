"""Konfiguration aus Umgebungsvariablen."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    # Wird beim Docker-Build gesetzt (Git-Tag und Commit), lokal "dev".
    version: str = _env("APP_VERSION", "dev")
    git_sha: str = _env("GIT_SHA", "")
    # Öffentliche URL hinter dem Reverse Proxy, z. B. https://gutscheine.example.de
    base_url: str = _env("BASE_URL", "http://localhost:8000")
    data_dir: str = _env("DATA_DIR", "./data")
    repo_url: str = "https://github.com/teitech/Gutscheinkontor"


def get_settings() -> Settings:
    return Settings()
