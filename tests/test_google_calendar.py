"""Tests for Google Calendar sync (no network: httpx.MockTransport and fakes)."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from friday.assistant.google_calendar import GoogleCalendar, GoogleCalendarError, RemoteEvent
from friday.assistant.service import Assistant, SchedulingConflict
from friday.assistant.store import AssistantStore

NOW = datetime(2026, 10, 10, 10, 0)


class FakeCalendar:
    """In-memory stand-in for GoogleCalendar."""

    def __init__(self, events=(), fail=False):
        self.events = {e.id: e for e in events}
        self.fail = fail
        self.created, self.deleted = [], []

    def list_events(self, start, end):
        if self.fail:
            raise GoogleCalendarError("offline")
        return [e for e in self.events.values() if e.start < end and e.end > start]

    def create_event(self, event):
        if self.fail:
            raise GoogleCalendarError("offline")
        gid = f"g{event.id}"
        self.events[gid] = RemoteEvent(gid, event.title, event.start, event.end, friday_id=event.id)
        self.created.append(event.title)
        return gid

    def delete_event(self, gid):
        if self.fail:
            raise GoogleCalendarError("offline")
        self.events.pop(gid, None)
        self.deleted.append(gid)


@pytest.fixture
def store(tmp_path):
    s = AssistantStore(tmp_path / "a.db")
    yield s
    s.close()


class TestSync:
    def test_booking_is_created_in_google(self, store):
        cal = FakeCalendar()
        booked = Assistant(store, calendar=cal).schedule("Dentist", datetime(2026, 10, 16, 15, 0), 45, now=NOW)
        assert cal.created == ["Dentist"]
        assert booked.event.external_id == f"g{booked.event.id}" and booked.event.provider == "google"

    def test_google_events_block_time(self, store):
        cal = FakeCalendar([RemoteEvent("x1", "Team standup", datetime(2026, 10, 16, 15, 0), datetime(2026, 10, 16, 16, 0))])
        assistant = Assistant(store, calendar=cal)
        with pytest.raises(SchedulingConflict) as exc:
            assistant.schedule("Dentist", datetime(2026, 10, 16, 15, 30), 30, now=NOW)
        assert "Team standup" in str(exc.value)
        assert exc.value.suggestion == datetime(2026, 10, 16, 16, 0)

    def test_fridays_own_google_copy_is_not_a_conflict_with_itself(self, store):
        cal = FakeCalendar()
        assistant = Assistant(store, calendar=cal)
        assistant.schedule("A", datetime(2026, 10, 16, 15, 0), 30, now=NOW)
        # Only one busy entry for the slot, not the local event plus its Google copy.
        assert len(assistant.busy(datetime(2026, 10, 16, 15, 0), datetime(2026, 10, 16, 15, 30))) == 1

    def test_offline_booking_saved_locally_then_synced(self, store):
        cal = FakeCalendar(fail=True)
        assistant = Assistant(store, calendar=cal)
        booked = assistant.schedule("Gym", datetime(2026, 10, 16, 7, 0), 60, now=NOW)
        assert booked.event.external_id is None
        assert any("not yet in Google" in w for w in assistant.warnings)
        cal.fail = False
        assert assistant.sync_pending(now=NOW) == 1
        assert store.get_event(booked.event.id).external_id == f"g{booked.event.id}"
        assert assistant.sync_pending(now=NOW) == 0  # nothing left, no duplicates

    def test_cancel_deletes_from_google(self, store):
        cal = FakeCalendar()
        assistant = Assistant(store, calendar=cal)
        booked = assistant.schedule("A", datetime(2026, 10, 16, 15, 0), 30, now=NOW)
        assert assistant.cancel(booked.event.id)
        assert cal.deleted == [booked.event.external_id]

    def test_briefing_shows_google_events(self, store):
        cal = FakeCalendar([RemoteEvent("x1", "Standup", datetime(2026, 10, 10, 11, 0), datetime(2026, 10, 10, 11, 15))])
        text = Assistant(store, calendar=cal).briefing(now=NOW)
        assert "11:00-11:15  (Google) Standup" in text

    def test_google_down_does_not_block_local_use(self, store):
        assistant = Assistant(store, calendar=FakeCalendar(fail=True))
        assert "Appointments" in assistant.briefing(now=NOW) or "No appointments" in assistant.briefing(now=NOW)
        assert assistant.find_free_slot(30, now=NOW) is not None

    @pytest.mark.asyncio
    async def test_daemon_retries_pending_sync(self, store, monkeypatch):
        from friday.assistant import daemon

        cal = FakeCalendar(fail=True)
        assistant = Assistant(store, calendar=cal)
        assistant.schedule("Gym", datetime(2026, 10, 16, 7, 0), 60, now=NOW)
        cal.fail = False
        assistant.warnings.clear()
        monkeypatch.setattr(daemon, "notify", lambda *a, **k: None)
        counts = await daemon.tick(assistant, now=NOW)
        assert counts["synced"] == 1 and cal.created == ["Gym"]


class TestApiClient:
    def _client(self, handler):
        return GoogleCalendar("primary", token_provider=lambda: "tok", transport=httpx.MockTransport(handler))

    def test_list_events_parses_and_paginates(self):
        pages = {
            None: {"items": [
                {"id": "a", "summary": "Standup", "start": {"dateTime": "2026-10-16T09:00:00Z"},
                 "end": {"dateTime": "2026-10-16T09:15:00Z"}},
                {"id": "b", "summary": "Free time", "transparency": "transparent",
                 "start": {"dateTime": "2026-10-16T10:00:00Z"}, "end": {"dateTime": "2026-10-16T11:00:00Z"}},
            ], "nextPageToken": "p2"},
            "p2": {"items": [
                {"id": "c", "summary": "Holiday", "start": {"date": "2026-10-17"}, "end": {"date": "2026-10-18"}},
                {"id": "d", "summary": "Friday booked", "start": {"dateTime": "2026-10-16T12:00:00Z"},
                 "end": {"dateTime": "2026-10-16T12:30:00Z"},
                 "extendedProperties": {"private": {"fridayEventId": "7"}}},
            ]},
        }
        seen = []

        def handler(request):
            assert request.headers["Authorization"] == "Bearer tok"
            assert request.url.path == "/calendar/v3/calendars/primary/events"
            assert request.url.params["singleEvents"] == "true"
            seen.append(request.url.params.get("pageToken"))
            return httpx.Response(200, json=pages[request.url.params.get("pageToken")])

        events = self._client(handler).list_events(NOW, NOW + timedelta(days=7))
        assert seen == [None, "p2"]
        assert [e.id for e in events] == ["a", "c", "d"]  # transparent event skipped
        assert events[0].start == datetime.fromisoformat("2026-10-16T09:00:00+00:00").astimezone().replace(tzinfo=None)
        assert events[1].start == datetime(2026, 10, 17) and events[2].friday_id == 7

    def test_create_event_body(self):
        captured = {}

        def handler(request):
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"id": "new123"})

        class E:
            id, title, location, notes = 5, "Dentist", "Clinic", None
            start, end = datetime(2026, 10, 16, 15, 0), datetime(2026, 10, 16, 15, 45)

        assert self._client(handler).create_event(E) == "new123"
        assert captured["summary"] == "Dentist" and captured["location"] == "Clinic"
        assert captured["extendedProperties"]["private"]["fridayEventId"] == "5"
        assert datetime.fromisoformat(captured["start"]["dateTime"]).replace(tzinfo=None) == E.start

    def test_errors_are_readable(self):
        def handler(request):
            return httpx.Response(403, json={"error": {"message": "Calendar API has not been used in project"}})

        with pytest.raises(GoogleCalendarError, match="403: Calendar API has not been used"):
            self._client(handler).list_events(NOW, NOW + timedelta(days=1))

    def test_delete_already_gone_is_ok(self):
        self._client(lambda r: httpx.Response(410))._request("DELETE", "/events/x")

    def test_calendar_id_is_url_encoded(self):
        def handler(request):
            assert request.url.raw_path.startswith(b"/calendar/v3/calendars/team%40group.calendar.google.com/")
            return httpx.Response(200, json={"items": []})

        GoogleCalendar("team@group.calendar.google.com", token_provider=lambda: "t",
                       transport=httpx.MockTransport(handler)).list_events(NOW, NOW)


class TestConnect:
    def test_rejects_web_client_json(self, tmp_path):
        from friday.assistant import google_calendar as gc

        bad = tmp_path / "creds.json"
        bad.write_text(json.dumps({"web": {"client_id": "x"}}))
        with pytest.raises(GoogleCalendarError, match="Desktop app"):
            gc.connect(bad, open_browser=False)

    def test_missing_file(self, tmp_path):
        from friday.assistant import google_calendar as gc

        with pytest.raises(GoogleCalendarError, match="not found"):
            gc.connect(tmp_path / "nope.json", open_browser=False)

    def test_not_connected_by_default(self):
        from friday.assistant import google_calendar as gc

        assert gc.get_provider() is None
        with pytest.raises(GoogleCalendarError, match="not connected"):
            gc._load_access_token(gc.TOKEN_PATH)

    def test_token_refresh_is_saved(self, tmp_path, monkeypatch):
        """An expired token is refreshed and re-written (0600) so the next run doesn't re-prompt."""
        from google.oauth2.credentials import Credentials

        from friday.assistant import google_calendar as gc

        token = tmp_path / "token.json"
        token.write_text(json.dumps({"token": "old", "refresh_token": "r", "client_id": "c", "client_secret": "s",
                                     "token_uri": "https://oauth2.googleapis.com/token",
                                     "expiry": "2020-01-01T00:00:00Z"}))

        def fake_refresh(self, request):
            self.token = "new"
            self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)

        monkeypatch.setattr(Credentials, "refresh", fake_refresh)
        assert gc._load_access_token(token) == "new"
        assert json.loads(token.read_text())["token"] == "new"
