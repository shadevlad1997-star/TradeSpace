from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator


HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
PIXEL_RE = re.compile(r"^[0-9]{1,3}px$")


class BrandConfig(BaseModel):
    brand_id: str = "tradespace"
    brand_name: str = "TradeSpace"
    brand_short_name: str = "TradeSpace"
    product_name: str = "TradeSpace"
    legal_name: str = "TradeSpace"
    page_title: str = "TradeSpace"
    meta_description: str = "Secure TradeSpace cabinet"
    logo_light: str = "/static/tradespace/brand-mark.svg"
    logo_dark: str = "/static/tradespace/brand-mark.svg"
    logo_compact: str = "/static/tradespace/brand-mark.svg"
    favicon: str = "/static/tradespace/brand-mark.svg"
    login_background: str = "/static/tradespace/brand-mark.svg"
    support_email: str = "support@example.com"
    support_url: str = ""
    support_telegram: str = ""
    website_url: str = ""
    copyright_text: str = "TradeSpace"
    primary_color: str = "#1f7a8c"
    secondary_color: str = "#0b1320"
    accent_color: str = "#f4d35e"
    background_color: str = "#07111f"
    surface_color: str = "#0f2133"
    text_color: str = "#f4f8fb"
    muted_text_color: str = "#a9bac8"
    success_color: str = "#2fbf71"
    warning_color: str = "#f4a261"
    danger_color: str = "#e45757"
    border_radius: str = "12px"
    font_family: str = 'system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif'
    table_density: str = "comfortable"
    sidebar_style: str = "compact"
    login_layout: str = "split"
    email_sender_name: str = "TradeSpace"
    email_sender_address: str = "support@example.com"
    api_docs_title: str = "TradeSpace Merchant API"
    api_docs_description: str = "TradeSpace Merchant API"
    company_footer: str = "TradeSpace"
    terms_url: str = ""
    privacy_url: str = ""

    @field_validator(
        "brand_id",
        "brand_name",
        "brand_short_name",
        "product_name",
        "legal_name",
        "page_title",
        "meta_description",
        "support_email",
        "copyright_text",
        "table_density",
        "sidebar_style",
        "login_layout",
        "email_sender_name",
        "email_sender_address",
        "api_docs_title",
        "api_docs_description",
        "company_footer",
    )
    @classmethod
    def safe_text(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        lowered = cleaned.lower()
        if any(token in lowered for token in ("<script", "javascript:", "</")):
            raise ValueError("branding text contains unsafe markup")
        if any(char in cleaned for char in ("<", ">")):
            raise ValueError("branding text must not contain HTML")
        return cleaned

    @field_validator(
        "primary_color",
        "secondary_color",
        "accent_color",
        "background_color",
        "surface_color",
        "text_color",
        "muted_text_color",
        "success_color",
        "warning_color",
        "danger_color",
    )
    @classmethod
    def hex_color(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not HEX_COLOR_RE.match(cleaned):
            raise ValueError("branding colors must be #RRGGBB values")
        return cleaned

    @field_validator("border_radius")
    @classmethod
    def pixel_radius(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not PIXEL_RE.match(cleaned):
            raise ValueError("border_radius must be a px value")
        return cleaned

    @field_validator("font_family")
    @classmethod
    def safe_font_family(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if any(char in cleaned for char in ("<", ">", "{", "}")):
            raise ValueError("font_family contains unsafe characters")
        return cleaned

    @field_validator("logo_light", "logo_dark", "logo_compact", "favicon", "login_background")
    @classmethod
    def static_asset_path(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            return cleaned
        if not cleaned.startswith("/static/"):
            raise ValueError("branding asset paths must start with /static/")
        if ".." in cleaned or "\\" in cleaned or "://" in cleaned:
            raise ValueError("branding asset path is unsafe")
        return cleaned

    @field_validator("support_url", "support_telegram", "website_url", "terms_url", "privacy_url")
    @classmethod
    def safe_optional_url(cls, value: str) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            return ""
        if not cleaned.startswith("https://"):
            raise ValueError("branding URLs must be https:// URLs")
        if any(token in cleaned.lower() for token in ("javascript:", "<", ">", "\\")):
            raise ValueError("branding URL is unsafe")
        return cleaned

    def css_variables(self) -> dict[str, str]:
        return {
            "--wl-primary": self.primary_color,
            "--wl-secondary": self.secondary_color,
            "--wl-accent": self.accent_color,
            "--wl-bg": self.background_color,
            "--wl-surface": self.surface_color,
            "--wl-text": self.text_color,
            "--wl-muted": self.muted_text_color,
            "--wl-success": self.success_color,
            "--wl-warning": self.warning_color,
            "--wl-danger": self.danger_color,
            "--wl-radius": self.border_radius,
            "--wl-font": self.font_family,
        }
