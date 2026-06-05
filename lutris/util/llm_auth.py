"""Local OAuth token handling for optional LLM providers."""

import json
import os
import queue
import secrets
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

from lutris import settings
from lutris.util import system
from lutris.util.log import logger

LLM_AUTH_DIR = Path(settings.CONFIG_DIR) / "llm"
GEMINI_TOKEN_PATH = LLM_AUTH_DIR / "gemini-token.json"
GEMINI_CLIENT_SECRET_PATH = LLM_AUTH_DIR / "gemini-client-secret.json"
GEMINI_SCOPES = ["https://www.googleapis.com/auth/generative-language.retriever"]
GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
GEMINI_GENERATE_CONTENT_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"


class LLMAuthUnavailable(RuntimeError):
    """Raised when browser OAuth cannot be started in this installation."""


def _validate_gemini_client_secret(path: Path) -> None:
    try:
        with path.open(encoding="utf-8") as secret_file:
            data = json.load(secret_file)
    except (OSError, json.JSONDecodeError) as ex:
        raise LLMAuthUnavailable("The selected Gemini OAuth client file is not valid JSON") from ex

    client_config = data.get("installed")
    if not isinstance(client_config, dict):
        raise LLMAuthUnavailable("The selected Gemini OAuth client file must be a desktop OAuth client")

    if not client_config.get("client_id") or not client_config.get("client_secret"):
        raise LLMAuthUnavailable("The selected Gemini OAuth client file is missing required client credentials")

    if client_config.get("auth_uri") != GOOGLE_AUTH_URI or client_config.get("token_uri") != GOOGLE_TOKEN_URI:
        raise LLMAuthUnavailable("The selected Gemini OAuth client file must use Google's OAuth endpoints")


def _load_json_file(path: Path) -> dict[str, object] | None:
    try:
        with path.open(encoding="utf-8") as input_file:
            data = json.load(input_file)
    except (OSError, json.JSONDecodeError):
        return None

    return data if isinstance(data, dict) else None


def _save_json_file(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    os.chmod(path, 0o600)


def _client_config(path: Path) -> dict[str, str]:
    try:
        with path.open(encoding="utf-8") as secret_file:
            data = json.load(secret_file)
    except (OSError, json.JSONDecodeError) as ex:
        raise LLMAuthUnavailable("The selected Gemini OAuth client file is not valid JSON") from ex

    client_config = data.get("installed")
    if not isinstance(client_config, dict):
        raise LLMAuthUnavailable("The selected Gemini OAuth client file must be a desktop OAuth client")

    if not client_config.get("client_id") or not client_config.get("client_secret"):
        raise LLMAuthUnavailable("The selected Gemini OAuth client file is missing required client credentials")

    if client_config.get("auth_uri") != GOOGLE_AUTH_URI or client_config.get("token_uri") != GOOGLE_TOKEN_URI:
        raise LLMAuthUnavailable("The selected Gemini OAuth client file must use Google's OAuth endpoints")

    return client_config


def _mask_client_id(client_id: str) -> str:
    if len(client_id) <= 12:
        return client_id[:4] + "…" + client_id[-4:]
    return f"{client_id[:6]}…{client_id[-6:]}"


def _client_project_id(path: Path) -> str | None:
    data = _load_json_file(path)
    if not data:
        return None
    # Google puts project_id inside the "installed" object of desktop OAuth client files.
    installed = data.get("installed")
    if isinstance(installed, dict):
        data = installed
    project_id = data.get("project_id")
    return project_id if isinstance(project_id, str) and project_id else None


def _log_client_secret(action: str, client_config: dict[str, str], project_id: str | None) -> None:
    masked_client_id = _mask_client_id(client_config["client_id"])
    if project_id:
        logger.info("%s Gemini OAuth client %s from project %s", action, masked_client_id, project_id)
    else:
        logger.info("%s Gemini OAuth client %s", action, masked_client_id)


def _normalize_token_data(data: dict[str, object]) -> dict[str, object]:
    normalized = dict(data)
    expires_in = normalized.get("expires_in")
    if isinstance(expires_in, (int, float)):
        normalized["expires_at"] = time.time() + float(expires_in)
    elif isinstance(expires_in, str):
        try:
            normalized["expires_at"] = time.time() + float(expires_in)
        except ValueError:
            pass
    return normalized


def _token_is_expired(token_data: dict[str, object]) -> bool:
    expires_at = token_data.get("expires_at")
    if isinstance(expires_at, (int, float)):
        return time.time() >= float(expires_at) - 60
    return False


def _oauth_response_data(response: requests.Response) -> dict[str, object]:
    try:
        response.raise_for_status()
        data = response.json()
    except ValueError as ex:
        raise LLMAuthUnavailable(f"Gemini OAuth request failed: {response.text[:200]}") from ex
    except requests.RequestException as ex:
        raise LLMAuthUnavailable(f"Gemini OAuth request failed: {ex}") from ex

    if isinstance(data, dict) and data.get("error"):
        raise LLMAuthUnavailable(f"Gemini OAuth request failed: {data.get('error_description') or data.get('error')}")
    if not isinstance(data, dict):
        raise LLMAuthUnavailable("Gemini OAuth request failed: invalid JSON response")
    return data


class _OAuthCallbackServer(HTTPServer):
    allow_reuse_address = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _OAuthCallbackHandler)
        self.result_queue: queue.Queue[tuple[str, str | None, str | None]] = queue.Queue(maxsize=1)


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        if parsed.path != "/":
            self.send_error(404)
            return

        params = parse_qs(parsed.query)
        error = params.get("error", [None])[0]
        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]

        if error:
            message = "Authorization failed"
            self.server.result_queue.put(("error", error, state))
        elif code:
            message = "Authorization received"
            self.server.result_queue.put(("code", code, state))
        else:
            message = "Authorization response missing code"
            self.server.result_queue.put(("error", "missing_code", state))

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            f"<html><body><p>{message}. You can close this tab and return to Lutris.</p></body></html>".encode("utf-8")
        )
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def log_message(self, _format, *_args):
        return


def _build_auth_url(client_config: dict[str, str], redirect_uri: str, state: str) -> str:
    query = urlencode(
        {
            "client_id": client_config["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(GEMINI_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
    )
    return f"{client_config['auth_uri']}?{query}"


def _exchange_code_for_token(client_config: dict[str, str], code: str, redirect_uri: str) -> dict[str, object]:
    response = requests.post(
        client_config["token_uri"],
        data={
            "code": code,
            "client_id": client_config["client_id"],
            "client_secret": client_config["client_secret"],
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=30,
    )
    token_data = _oauth_response_data(response)
    return _normalize_token_data(token_data)


def _refresh_access_token(client_config: dict[str, str], token_data: dict[str, object]) -> dict[str, object] | None:
    refresh_token = token_data.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        return None

    response = requests.post(
        client_config["token_uri"],
        data={
            "refresh_token": refresh_token,
            "client_id": client_config["client_id"],
            "client_secret": client_config["client_secret"],
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    refreshed = _oauth_response_data(response)
    refreshed["refresh_token"] = refresh_token
    if "expires_in" not in refreshed and "expires_at" in token_data:
        refreshed["expires_at"] = token_data["expires_at"]
    return _normalize_token_data(refreshed)


class GeminiOAuthProvider:
    id = "gemini"
    name = "Gemini API"
    token_path = GEMINI_TOKEN_PATH
    client_secret_path = GEMINI_CLIENT_SECRET_PATH

    @property
    def credential_files(self) -> list[Path]:
        return [self.token_path]

    def is_authenticated(self) -> bool:
        return self.token_path.exists()

    def disconnect(self) -> None:
        for path in self.credential_files:
            path.unlink(missing_ok=True)

    def import_client_secret(self, source_path: str | os.PathLike[str]) -> None:
        source = Path(source_path)
        _validate_gemini_client_secret(source)
        self.client_secret_path.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != self.client_secret_path.resolve():
            shutil.copyfile(source, self.client_secret_path)
        os.chmod(self.client_secret_path, 0o600)
        client_config = _client_config(self.client_secret_path)
        _log_client_secret("Loaded", client_config, _client_project_id(self.client_secret_path))

    def load_project_id(self) -> str | None:
        return _client_project_id(self.client_secret_path)

    def connect(self) -> bool:
        """Open Google's installed-app OAuth flow and cache the resulting token."""
        if not self.client_secret_path.exists():
            raise LLMAuthUnavailable("Choose a Google Gemini desktop OAuth client JSON file before connecting")
        client_config = _client_config(self.client_secret_path)
        _log_client_secret("Using", client_config, _client_project_id(self.client_secret_path))
        server = _OAuthCallbackServer()
        redirect_uri = f"http://127.0.0.1:{server.server_port}/"
        state = secrets.token_urlsafe(24)
        auth_url = _build_auth_url(client_config, redirect_uri, state)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        try:
            system.spawn(["xdg-open", auth_url], quiet=True)
            try:
                result_type, value, returned_state = server.result_queue.get(timeout=300)
            except queue.Empty as ex:
                raise LLMAuthUnavailable("Timed out waiting for Gemini OAuth sign-in to complete") from ex

            if result_type != "code" or not value:
                raise LLMAuthUnavailable(f"Gemini OAuth sign-in failed: {value or 'unknown error'}")
            if returned_state != state:
                raise LLMAuthUnavailable("Gemini OAuth response did not match the current sign-in request")

            token_data = _exchange_code_for_token(client_config, value, redirect_uri)
            _save_json_file(self.token_path, token_data)
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=5)
        return True

    def load_access_token(self) -> str | None:
        if not self.token_path.exists():
            return None
        token_data = _load_json_file(self.token_path)
        if not token_data:
            return None

        try:
            client_config = _client_config(self.client_secret_path)
        except LLMAuthUnavailable as ex:
            logger.warning("Gemini OAuth client secret is invalid: %s", ex)
            return None
        if _token_is_expired(token_data):
            refreshed = _refresh_access_token(client_config, token_data)
            if not refreshed:
                logger.warning("Failed to refresh Gemini OAuth token: missing refresh token")
                return None
            token_data = refreshed
            _save_json_file(self.token_path, token_data)

        access_token = token_data.get("access_token")
        return access_token if isinstance(access_token, str) and access_token else None

    def load_credentials(self):
        return self.load_access_token()


DEFAULT_LLM_PROVIDER = GeminiOAuthProvider()
