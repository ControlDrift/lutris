"""Local OAuth token handling for optional LLM providers."""

import os
from pathlib import Path

from lutris import settings
from lutris.util.log import logger

LLM_AUTH_DIR = Path(settings.CONFIG_DIR) / "llm"
GEMINI_TOKEN_PATH = LLM_AUTH_DIR / "gemini-token.json"
GEMINI_CLIENT_SECRET_PATH = LLM_AUTH_DIR / "gemini-client-secret.json"
GEMINI_SCOPES = ["https://www.googleapis.com/auth/generative-language.retriever"]


class LLMAuthUnavailable(RuntimeError):
    """Raised when browser OAuth cannot be started in this installation."""


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

    def connect(self) -> bool:
        """Open Google's installed-app OAuth flow and cache the resulting token.

        Google does not publish a generic desktop client secret for embedding in
        third-party apps. The user or package must provide one at
        ``~/.config/lutris/llm/gemini-client-secret.json``.
        """
        if not self.client_secret_path.exists():
            raise LLMAuthUnavailable(
                "Gemini OAuth requires a local desktop OAuth client file at %s" % self.client_secret_path
            )

        try:
            from google_auth_oauthlib.flow import InstalledAppFlow  # type: ignore
        except ImportError as ex:
            raise LLMAuthUnavailable("google-auth-oauthlib is required for Gemini OAuth") from ex

        LLM_AUTH_DIR.mkdir(parents=True, exist_ok=True)
        flow = InstalledAppFlow.from_client_secrets_file(str(self.client_secret_path), GEMINI_SCOPES)
        creds = flow.run_local_server(port=0)
        self.token_path.write_text(creds.to_json(), encoding="utf-8")
        os.chmod(self.token_path, 0o600)
        return True

    def load_credentials(self):
        if not self.token_path.exists():
            return None
        try:
            from google.auth.transport.requests import Request  # type: ignore
            from google.oauth2.credentials import Credentials  # type: ignore
        except ImportError:
            return None

        creds = Credentials.from_authorized_user_file(str(self.token_path), GEMINI_SCOPES)
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                self.token_path.write_text(creds.to_json(), encoding="utf-8")
            except Exception as ex:  # noqa: BLE001 - auth failures should fall back gracefully
                logger.warning("Failed to refresh Gemini OAuth token: %s", ex)
                return None
        return creds if creds and creds.valid else None


DEFAULT_LLM_PROVIDER = GeminiOAuthProvider()
