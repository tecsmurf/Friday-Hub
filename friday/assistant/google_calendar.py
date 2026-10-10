"""
Google Calendar sync.

Friday keeps its own local calendar (works offline) and mirrors it to Google
Calendar once you connect:

* appointments Friday books are created in Google Calendar,
* cancelling in Friday deletes the Google event,
* your existing Google events are treated as busy time for clash checks and
  free-slot search, and appear in the agenda/briefing.

Setup (once): create an OAuth client of type "Desktop app" in Google Cloud
Console with the Google Calendar API enabled, download its JSON, then run
``friday calendar connect --credentials <file>``. A browser opens for you to
approve access; the resulting token is stored only on this computer.

Only the ``calendar.events`` scope is requested: Friday can read and write
events, but not change calendar settings or sharing.

Requires the optional dependencies: ``pip install "friday-hub[google]"``.
"""

import json
import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import httpx

from friday.config import ROOT_DIR, config

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
API = "https://www.googleapis.com/calendar/v3"
_cfg = (config.get("assistant", {}) or {}).get("google_calendar", {}) or {}
GOOGLE_DIR = ROOT_DIR / "data" / "google"
CREDENTIALS_PATH = GOOGLE_DIR / "credentials.json"
TOKEN_PATH = GOOGLE_DIR / "token.json"
CALENDAR_ID = str(_cfg.get("calendar_id", "primary"))
ENABLED = bool(_cfg.get("enabled", True))
_TIMEOUT = 15.0


class GoogleCalendarError(RuntimeError):
    """Google Calendar is not set up or a request failed."""


class MissingDependency(GoogleCalendarError):
    def __init__(self):
        super().__init__('Google Calendar support is not installed. Run: pip install "friday-hub[google]"')


@dataclass
class RemoteEvent:
    id: str
    title: str
    start: datetime
    end: datetime
    location: str | None = None
    friday_id: int | None = None  # set when Friday created it


def _write_private(path: Path, text: str) -> None:
    """Write a secret file readable only by the current user (POSIX)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def _local(dt_str: str) -> datetime:
    """RFC 3339 (or all-day date) → naive local datetime."""
    if len(dt_str) == 10:  # all-day event: YYYY-MM-DD
        return datetime.fromisoformat(dt_str)
    return datetime.fromisoformat(dt_str.replace("Z", "+00:00")).astimezone().replace(tzinfo=None)


def _rfc3339(dt: datetime) -> str:
    """Naive local datetime → RFC 3339 with the local UTC offset."""
    return dt.astimezone().isoformat(timespec="seconds")


# ── Authorisation ──────────────────────────────────────────────────────


def is_connected(token_path: Path | None = None) -> bool:
    return ENABLED and (token_path or TOKEN_PATH).exists()


def connect(credentials_file: Path, open_browser: bool = True) -> str:
    """Run Google's installed-app OAuth flow and store the token. Returns the account email if known."""
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise MissingDependency() from exc

    credentials_file = Path(credentials_file).expanduser()
    if not credentials_file.exists():
        raise GoogleCalendarError(f"Credentials file not found: {credentials_file}")
    try:
        client = json.loads(credentials_file.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise GoogleCalendarError(f"{credentials_file} is not valid JSON") from exc
    if "installed" not in client:
        raise GoogleCalendarError(
            'That file is not a "Desktop app" OAuth client. In Google Cloud Console create '
            "Credentials → OAuth client ID → Application type: Desktop app, then download its JSON."
        )

    GOOGLE_DIR.mkdir(parents=True, exist_ok=True)
    if credentials_file.resolve() != CREDENTIALS_PATH.resolve():
        shutil.copyfile(credentials_file, CREDENTIALS_PATH)
        os.chmod(CREDENTIALS_PATH, 0o600)

    flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
    creds = flow.run_local_server(port=0, open_browser=open_browser,
                                  authorization_prompt_message="Approve Friday in your browser: {url}")
    _write_private(TOKEN_PATH, creds.to_json())
    return getattr(creds, "account", "") or ""


def disconnect() -> bool:
    """Forget the stored token (the Google events themselves are kept)."""
    if TOKEN_PATH.exists():
        TOKEN_PATH.unlink()
        return True
    return False


def _load_access_token(token_path: Path) -> str:
    """Return a valid access token, refreshing and re-saving it when expired."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
    except ImportError as exc:
        raise MissingDependency() from exc

    if not token_path.exists():
        raise GoogleCalendarError("Google Calendar is not connected. Run: friday calendar connect --credentials <file>")
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds.valid:
        if not creds.refresh_token:
            raise GoogleCalendarError("Google sign-in expired. Run: friday calendar connect")
        try:
            creds.refresh(Request())
        except Exception as exc:
            raise GoogleCalendarError(f"Could not refresh Google sign-in ({exc}). Run: friday calendar connect") from exc
        _write_private(token_path, creds.to_json())
    return creds.token


# ── API client ─────────────────────────────────────────────────────────


class GoogleCalendar:
    """Minimal Calendar API v3 client over httpx.

    ``token_provider`` and ``transport`` are injectable for tests.
    """

    def __init__(self, calendar_id: str = CALENDAR_ID, token_provider=None, transport=None):
        self.calendar_id = calendar_id
        self._token_provider = token_provider or (lambda: _load_access_token(TOKEN_PATH))
        self._transport = transport

    def _request(self, method: str, path: str, **kwargs) -> dict:
        headers = {"Authorization": f"Bearer {self._token_provider()}"}
        url = f"{API}/calendars/{quote(self.calendar_id, safe='')}{path}"
        try:
            with httpx.Client(timeout=_TIMEOUT, transport=self._transport) as client:
                response = client.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise GoogleCalendarError(f"Could not reach Google Calendar: {exc}") from exc
        if response.status_code == 410 and method == "DELETE":
            return {}  # already deleted
        if response.status_code >= 400:
            try:
                message = response.json().get("error", {}).get("message")
            except ValueError:
                message = None
            raise GoogleCalendarError(f"Google Calendar error {response.status_code}: {message or response.reason_phrase}")
        return response.json() if response.content else {}

    def list_events(self, start: datetime, end: datetime) -> list[RemoteEvent]:
        """Confirmed/tentative events overlapping [start, end), recurring ones expanded."""
        events: list[RemoteEvent] = []
        params = {
            "timeMin": _rfc3339(start),
            "timeMax": _rfc3339(end),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": "250",
        }
        while True:
            data = self._request("GET", "/events", params=params)
            for item in data.get("items", []):
                if item.get("status") == "cancelled" or item.get("transparency") == "transparent":
                    continue  # "free" events don't block time
                s, e = item.get("start", {}), item.get("end", {})
                start_s = s.get("dateTime") or s.get("date")
                end_s = e.get("dateTime") or e.get("date")
                if not start_s or not end_s:
                    continue
                private = (item.get("extendedProperties") or {}).get("private") or {}
                friday_id = private.get("fridayEventId")
                events.append(RemoteEvent(
                    id=item["id"],
                    title=item.get("summary") or "(busy)",
                    start=_local(start_s),
                    end=_local(end_s),
                    location=item.get("location"),
                    friday_id=int(friday_id) if str(friday_id or "").isdigit() else None,
                ))
            token = data.get("nextPageToken")
            if not token:
                return events
            params["pageToken"] = token

    def create_event(self, event) -> str:
        """Create a Google event for a Friday Event; returns its Google id."""
        body = {
            "summary": event.title,
            "start": {"dateTime": _rfc3339(event.start)},
            "end": {"dateTime": _rfc3339(event.end)},
            "extendedProperties": {"private": {"fridayEventId": str(event.id)}},
            "reminders": {"useDefault": True},
        }
        if event.location:
            body["location"] = event.location
        if event.notes:
            body["description"] = event.notes
        return self._request("POST", "/events", json=body)["id"]

    def delete_event(self, google_id: str) -> None:
        self._request("DELETE", f"/events/{quote(google_id, safe='')}")


def get_provider() -> GoogleCalendar | None:
    """The Google Calendar client when connected and enabled, else None."""
    return GoogleCalendar() if is_connected() else None
