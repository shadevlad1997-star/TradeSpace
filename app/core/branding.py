from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.branding_schema import BrandConfig
from app.core.config import settings


PROJECT_ROOT = Path(__file__).absolute().parents[2]


def _parse_scalar(value: str) -> str | bool:
    cleaned = value.strip().strip("'").strip('"')
    lowered = cleaned.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return cleaned


def _load_simple_yaml(path: Path) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if not path.exists():
        return payload
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        payload[key] = _parse_scalar(value)
    return payload


def _resolve_branding_path(path: str | None) -> Path:
    configured = (path or settings.BRAND_CONFIG_PATH or "branding/default.yml").strip()
    candidate = Path(configured)
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    resolved = candidate.absolute()
    root = PROJECT_ROOT.absolute()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("branding config path must stay inside the project root") from exc
    return resolved


def load_branding(path: str | None = None) -> BrandConfig:
    resolved = _resolve_branding_path(path)
    payload = _load_simple_yaml(resolved)
    return BrandConfig(**payload)


@lru_cache(maxsize=1)
def get_branding() -> BrandConfig:
    return load_branding()


def reset_branding_cache() -> None:
    get_branding.cache_clear()


def branding_context() -> dict[str, Any]:
    brand = get_branding()
    return {"brand": brand, "brand_css_variables": brand.css_variables()}
