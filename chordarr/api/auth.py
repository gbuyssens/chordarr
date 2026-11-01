"""Authentication.

* ``/api/*`` requires the API key (header ``X-Api-Key`` or query ``apikey``), like every
  *arr app, except from loopback/private addresses when ``auth_disabled_for_local`` is on
  and no password is set.
* The UI is open by default: it is designed to sit behind a reverse proxy with SSO. Setting
  ``CHORDARR_UI_PASSWORD`` turns on a cookie login instead.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress

from fastapi import HTTPException, Request

from chordarr.config import settings

COOKIE = "chordarr_session"


def _is_local(request: Request) -> bool:
    host = request.client.host if request.client else ""
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


def session_token() -> str:
    return hmac.new(settings.api_key.encode(), b"ui-session:" + settings.ui_password.encode(), hashlib.sha256).hexdigest()


def ui_authenticated(request: Request) -> bool:
    if not settings.ui_password:
        return True
    return hmac.compare_digest(request.cookies.get(COOKIE, ""), session_token())


def require_api_key(request: Request) -> None:
    supplied = request.headers.get("X-Api-Key") or request.query_params.get("apikey") or ""
    if supplied and hmac.compare_digest(supplied, settings.api_key):
        return
    if ui_authenticated(request) and request.cookies.get(COOKIE):
        return
    if not supplied and not settings.ui_password and settings.auth_disabled_for_local and _is_local(request):
        return
    raise HTTPException(status_code=401, detail="Unauthorized")
