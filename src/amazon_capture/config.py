"""Runtime configuration for standalone Amazon capture invocations."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

PROFILE_ENV = "AMAZON_CAPTURE_PROFILE_DIR"


@dataclass(frozen=True)
class RuntimePaths:
    profile: Path
    output: Path
    screenshots: Path
    logs: Path


def default_profile_dir(
    root: Path | None = None, env: Mapping[str, str] | None = None,
) -> Path:
    base = Path.cwd() if root is None else Path(root)
    values = os.environ if env is None else env
    configured = values.get(PROFILE_ENV)
    return Path(configured).expanduser() if configured else base / "private" / "browser-profiles" / "amazon-field-discovery"


def runtime_paths(
    root: Path | None = None, env: Mapping[str, str] | None = None,
) -> RuntimePaths:
    base = Path.cwd() if root is None else Path(root)
    return RuntimePaths(
        profile=default_profile_dir(base, env),
        output=base / "raw-captures" / "amazon-field-discovery",
        screenshots=base / "screenshots" / "amazon-field-discovery",
        logs=base / "logs" / "amazon-field-discovery",
    )
