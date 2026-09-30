from __future__ import annotations

from functools import lru_cache
from typing import Literal

from fm_common.config import CommonSettings


class IdentitySettings(CommonSettings):
    refresh_token_ttl_days: int = 7
    refresh_cookie_name: str = "fm_refresh"
    refresh_cookie_path: str = "/api/v1/auth"
    cookie_secure: bool = False
    cookie_samesite: Literal["lax", "strict", "none"] = "strict"

    oidc_enabled: bool = False  # no longer needed: Login with Gmail turns on when the client id + secret are set
    oidc_provider_name: str = "google"
    oidc_issuer: str = "https://accounts.google.com"
    oidc_client_id: str = ""
    oidc_client_secret: str = ""
    oidc_redirect_uri: str = "http://localhost:8080/api/v1/auth/oidc/callback"
    oidc_scopes: str = "openid email profile"
    oidc_post_login_redirect: str = "http://localhost:8080/dashboard"

    @property
    def sso_ready(self) -> bool:
        """Login with Gmail is on as soon as the Google client id + secret are in .env (OIDC_ENABLED=true is not needed —
        a copied .env.example left it false, which silently switched it off)."""
        return bool(self.oidc_client_id.strip() and self.oidc_client_secret.strip())

    @property
    def login_page(self) -> str:
        return self.oidc_post_login_redirect.rsplit("/", 1)[0] + "/login"


@lru_cache
def get_settings() -> IdentitySettings:
    return IdentitySettings()


settings = get_settings()
