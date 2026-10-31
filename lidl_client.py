"""
Неофициальный клиент Lidl Plus (только свои чеки).
OAuth PKCE + tickets.lidlplus.com v2/v3.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import requests

CLIENT_ID = "LidlPlusNativeClient"
CLIENT_SECRET_B64 = base64.b64encode(b"LidlPlusNativeClient:secret").decode()
AUTH_API = "https://accounts.lidl.com"
TICKETS_V2 = "https://tickets.lidlplus.com/api/v2"
TICKETS_V3 = "https://tickets.lidlplus.com/api/v3"
REDIRECT_URI = "com.lidlplus.app://callback"
APP_VERSION = "16.43.4"
DEVICE_ID = "a1b2c3d4e5f60718"

DEFAULT_DATA_DIR = Path(__file__).resolve().parent / "data" / "lidl_tokens"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def create_pkce() -> tuple[str, str]:
    """Возвращает (code_verifier, code_challenge)."""
    verifier = secrets.token_urlsafe(64)[:86]
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def build_login_url(country: str = "PL", language: str = "pl") -> tuple[str, str]:
    """
    Собирает URL для ручного логина в браузере.
    Возвращает (url, code_verifier) — verifier нужно сохранить до обмена code.
    """
    country = country.upper()
    language = language.lower()
    verifier, challenge = create_pkce()
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "scope": "openid profile offline_access lpprofile lpapis",
        "redirect_uri": REDIRECT_URI,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "Country": country,
        "language": f"{language}-{country}",
        # Иначе SSO-cookie молча выдаёт code без формы логина
        "force": "true",
        "prompt": "login",
    }
    url = f"{AUTH_API}/connect/authorize?{urlencode(params)}"
    return url, verifier


def extract_code(pasted: str) -> str:
    pasted = pasted.strip()
    if pasted.startswith("http") or "://" in pasted or "code=" in pasted:
        qs = parse_qs(urlparse(pasted).query)
        if "code" in qs and qs["code"]:
            return qs["code"][0]
    # Иногда вставляют только сам code
    if pasted and " " not in pasted and "=" not in pasted:
        return pasted
    raise ValueError("Не удалось извлечь code из вставленной строки")


def exchange_code(code: str, code_verifier: str) -> dict:
    headers = {
        "Authorization": f"Basic {CLIENT_SECRET_B64}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": code_verifier,
    }
    resp = requests.post(f"{AUTH_API}/connect/token", headers=headers, data=data, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Token exchange failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


def refresh_access_token(refresh_token: str) -> dict:
    headers = {
        "Authorization": f"Basic {CLIENT_SECRET_B64}",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    resp = requests.post(f"{AUTH_API}/connect/token", headers=headers, data=data, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"Refresh failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json()


def token_path(account_id: str, data_dir: Optional[Path] = None) -> Path:
    base = data_dir or DEFAULT_DATA_DIR
    return base / f"{account_id}.json"


def load_tokens(account_id: str, data_dir: Optional[Path] = None) -> dict:
    path = token_path(account_id, data_dir)
    if not path.exists():
        raise FileNotFoundError(f"Нет токена: {path}. Сначала выполните login.")
    return json.loads(path.read_text(encoding="utf-8"))


def save_tokens(account_id: str, token_response: dict, data_dir: Optional[Path] = None, *, keep: Optional[dict] = None) -> Path:
    """Сохраняет ответ token endpoint. Критично писать ДО любых API-вызовов (rotation)."""
    path = token_path(account_id, data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(keep or {})
    payload.update({
        "access_token": token_response["access_token"],
        "refresh_token": token_response["refresh_token"],
        "expires_at": time.time() + int(token_response.get("expires_in", 3600)) - 60,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    })
    if "country" not in payload:
        payload["country"] = "PL"
    if "language" not in payload:
        payload["language"] = "pl"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


class LidlClient:
    def __init__(self, account_id: str, data_dir: Optional[Path] = None):
        self.account_id = account_id
        self.data_dir = data_dir or DEFAULT_DATA_DIR
        self._tokens = load_tokens(account_id, self.data_dir)

    @property
    def country(self) -> str:
        return str(self._tokens.get("country", "PL")).upper()

    @property
    def language(self) -> str:
        return str(self._tokens.get("language", "pl")).lower()

    def ensure_access_token(self) -> str:
        if self._tokens.get("access_token") and float(self._tokens.get("expires_at", 0)) > time.time():
            return self._tokens["access_token"]
        return self._refresh()

    def _refresh(self) -> str:
        old = self._tokens["refresh_token"]
        fresh = refresh_access_token(old)
        save_tokens(self.account_id, fresh, self.data_dir, keep={
            "country": self.country,
            "language": self.language,
        })
        self._tokens = load_tokens(self.account_id, self.data_dir)
        return self._tokens["access_token"]

    def _headers(self) -> dict:
        token = self.ensure_access_token()
        now = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        return {
            "Authorization": f"Bearer {token}",
            "App-Version": APP_VERSION,
            "Operating-System": "Android",
            "App": "com.lidl.eci.lidlplus",
            "Accept-Language": self.language[:2],
            "User-Agent": "okhttp/5.3.2",
            "OS-Version": "16",
            "Model": "Pixel 6",
            "Brand": "Google",
            "deviceid": DEVICE_ID,
            "Date": now,
            "Accept": "application/json",
        }

    def _get(self, url: str) -> Any:
        resp = requests.get(url, headers=self._headers(), timeout=30)
        if resp.status_code == 401:
            self._refresh()
            resp = requests.get(url, headers=self._headers(), timeout=30)
        if resp.status_code >= 400:
            raise RuntimeError(f"GET {url} → {resp.status_code}: {resp.text[:800]}")
        return resp.json()

    def list_tickets(self, page: int = 1) -> dict:
        url = (
            f"{TICKETS_V2}/{self.country}/tickets"
            f"?pageNumber={page}&onlyFavorite=false"
        )
        return self._get(url)

    def ticket_detail(self, ticket_id: str) -> dict:
        # Сначала v3 (HTML/items), при ошибке — v2
        url_v3 = f"{TICKETS_V3}/{self.country}/tickets/{ticket_id}"
        try:
            return self._get(url_v3)
        except RuntimeError as e:
            if "404" not in str(e) and "500" not in str(e):
                raise
            url_v2 = f"{TICKETS_V2}/{self.country}/tickets/{ticket_id}"
            return self._get(url_v2)
