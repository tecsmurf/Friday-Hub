"""Tests for the personal-assistant layer (friday.assistant)."""

from datetime import datetime, timedelta

import pytest

from friday.assistant.intent import extract_datetime, parse_rules, understand
from friday.assistant.recurrence import describe, next_run, parse_recurrence
from friday.assistant.service import Assistant, SchedulingConflict, to_ics
from friday.assistant.store import AssistantStore
from friday.assistant.timeparse import TimeParseError, parse_datetime, parse_duration

NOW = datetime(2026, 10, 10, 10, 0)  # a Saturday


@pytest.fixture
def assistant(tmp_path):
    store = AssistantStore(tmp_path / "assistant.db")
    yield Assistant(store)
    store.close()


class TestTimeParse:
    @pytest.mark.parametrize("text,expected", [
        ("in 20 minutes", datetime(2026, 10, 10, 10, 20)),
        ("in 1 hour 30 min", datetime(2026, 10, 10, 11, 30)),
        ("in an hour", datetime(2026, 10, 10, 11, 0)),
        ("in 2 days at 5pm", datetime(2026, 10, 12, 17, 0)),
        ("tomorrow at 3pm", datetime(2026, 10, 11, 15, 0)),
        ("tomorrow", datetime(2026, 10, 11, 9, 0)),
        ("today 18:30", datetime(2026, 10, 10, 18, 30)),
        ("tonight", datetime(2026, 10, 10, 20, 0)),
        ("friday 10am", datetime(2026, 10, 16, 10, 0)),
        ("next monday at 9am", datetime(2026, 10, 12, 9, 0)),
        ("saturday 9am", datetime(2026, 10, 17, 9, 0)),  # today's 9am already passed
        ("saturday 4pm", datetime(2026, 10, 10, 16, 0)),
        ("2026-10-14 14:00", datetime(2026, 10, 14, 14, 0)),
        ("14 oct 3pm", datetime(2026, 10, 14, 15, 0)),
        ("oct 14", datetime(2026, 10, 14, 9, 0)),
        ("jan 5", datetime(2027, 1, 5, 9, 0)),  # past dates roll to next year
        ("at noon", datetime(2026, 10, 10, 12, 0)),
        ("8am", datetime(2026, 10, 11, 8, 0)),  # time already passed today → tomorrow
        ("at 2", datetime(2026, 10, 10, 14, 0)),  # bare 1-7 means pm
        ("12am", datetime(2026, 10, 11, 0, 0)),
    ])
    def test_parse(self, text, expected):
        assert parse_datetime(text, now=NOW) == expected

    @pytest.mark.parametrize("text", ["", "whenever", "the report"])
    def test_unparseable(self, text):
        with pytest.raises(TimeParseError):
            parse_datetime(text, now=NOW)

    @pytest.mark.parametrize("text,minutes", [("45m", 45), ("1h", 60), ("1 hour 30 minutes", 90), ("90", 90)])
    def test_duration(self, text, minutes):
        assert parse_duration(text) == minutes


class TestRecurrence:
    def test_weekdays(self):
        rule = parse_recurrence("every weekday at 8am")
        assert rule == {"kind": "weekly", "days": [0, 1, 2, 3, 4], "time": "08:00"}
        assert next_run(rule, NOW) == datetime(2026, 10, 12, 8, 0)  # Saturday → Monday
        assert describe(rule) == "weekdays at 08:00"

    def test_specific_days(self):
        rule = parse_recurrence("every monday and thursday at 18:00")
        assert rule["days"] == [0, 3]
        assert next_run(rule, datetime(2026, 10, 12, 18, 0)) == datetime(2026, 10, 15, 18, 0)

    def test_daily_later_today(self):
        assert next_run(parse_recurrence("daily at 9pm"), NOW) == datetime(2026, 10, 10, 21, 0)

    def test_interval(self):
        rule = parse_recurrence("every 2 hours")
        assert rule == {"kind": "interval", "minutes": 120}
        assert next_run(rule, NOW) == NOW + timedelta(hours=2)

    def test_interval_too_small(self):
        with pytest.raises(TimeParseError):
            parse_recurrence("every 1 minute")

    def test_nonsense(self):
        with pytest.raises(TimeParseError):
            parse_recurrence("sometimes")


class TestScheduling:
    def test_book_creates_event_and_reminder(self, assistant):
        booked = assistant.schedule("Dentist", datetime(2026, 10, 16, 15, 0), 45, location="Clinic", now=NOW)
        assert booked.event.end == datetime(2026, 10, 16, 15, 45)
        assert booked.reminder_at == datetime(2026, 10, 16, 14, 45)
        reminders = assistant.store.list_reminders()
        assert len(reminders) == 1 and reminders[0].event_id == booked.event.id

    def test_conflict_is_refused_with_suggestion(self, assistant):
        assistant.schedule("Dentist", datetime(2026, 10, 16, 15, 0), 45, now=NOW)
        with pytest.raises(SchedulingConflict) as exc:
            assistant.schedule("Call", datetime(2026, 10, 16, 15, 30), 30, now=NOW)
        assert exc.value.suggestion == datetime(2026, 10, 16, 15, 45)
        assert len(assistant.store.events_between(NOW, NOW + timedelta(days=30))) == 1

    def test_back_to_back_is_not_a_conflict(self, assistant):
        assistant.schedule("A", datetime(2026, 10, 16, 15, 0), 30, now=NOW)
        assistant.schedule("B", datetime(2026, 10, 16, 15, 30), 30, now=NOW)

    def test_force_overlap(self, assistant):
        assistant.schedule("A", datetime(2026, 10, 16, 15, 0), 60, now=NOW)
        assistant.schedule("B", datetime(2026, 10, 16, 15, 30), 30, allow_overlap=True, now=NOW)

    def test_past_is_refused(self, assistant):
        with pytest.raises(ValueError, match="past"):
            assistant.schedule("Late", datetime(2026, 10, 9, 9, 0), now=NOW)

    def test_cancel_removes_pending_reminder_and_frees_slot(self, assistant):
        booked = assistant.schedule("A", datetime(2026, 10, 16, 15, 0), 60, now=NOW)
        assert assistant.cancel(booked.event.id)
        assert assistant.store.list_reminders() == []
        assistant.schedule("B", datetime(2026, 10, 16, 15, 0), 60, now=NOW)
        assert not assistant.cancel(booked.event.id)

    def test_free_slot_skips_events_and_respects_workday(self, assistant):
        monday = datetime(2026, 10, 12, 0, 0)
        assistant.schedule("Busy", datetime(2026, 10, 12, 9, 0), 120, now=NOW)
        assert assistant.find_free_slot(60, after=monday, now=NOW) == datetime(2026, 10, 12, 11, 0)
        assistant.schedule("All day", datetime(2026, 10, 12, 11, 0), 7 * 60, now=NOW)
        assert assistant.find_free_slot(60, after=monday, now=NOW) == datetime(2026, 10, 13, 9, 0)

    def test_briefing(self, assistant):
        assistant.schedule("Dentist", datetime(2026, 10, 10, 15, 0), 30, now=NOW)
        assistant.add_todo("Overdue thing", due=datetime(2026, 10, 8, 9, 0))
        assistant.add_todo("Someday")
        text = assistant.briefing(now=NOW)
        assert text.startswith("Today, Saturday 10 October 2026")
        assert "15:00-15:30" in text and "Dentist" in text
        assert "Overdue thing (overdue)" in text
        assert "Other open todos: 1" in text

    def test_ics_export_is_rfc5545(self, assistant):
        booked = assistant.schedule("Lunch; with, friends\nand more " + "x" * 80,
                                    datetime(2026, 10, 16, 12, 0), 60, now=NOW)
        ics = to_ics([booked.event])
        assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
        assert "SUMMARY:Lunch\\; with\\, friends\\nand more" in ics
        assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))


class TestStore:
    def test_todo_lifecycle(self, assistant):
        todo = assistant.add_todo("Buy milk", priority=1)
        assert assistant.store.complete_todo(todo.id)
        assert not assistant.store.complete_todo(todo.id)
        assert assistant.store.list_todos() == []
        assert len(assistant.store.list_todos(include_done=True)) == 1

    def test_validation(self, assistant):
        with pytest.raises(ValueError):
            assistant.add_todo("  ")
        with pytest.raises(ValueError):
            assistant.store.add_automation("x", "daily", {}, "rm -rf", None, NOW)
        with pytest.raises(ValueError):
            assistant.automate("every day at 9", "notify", "")

    def test_data_survives_reopen(self, tmp_path):
        with AssistantStore(tmp_path / "a.db") as store:
            store.add_todo("Persist me")
        with AssistantStore(tmp_path / "a.db") as store:
            assert [t.title for t in store.list_todos()] == ["Persist me"]


class TestDaemon:
    @pytest.mark.asyncio
    async def test_reminder_delivered_once(self, assistant, monkeypatch):
        from friday.assistant import daemon

        sent = []
        monkeypatch.setattr(daemon, "notify", lambda title, body, desktop=True: sent.append(body))
        assistant.remind("stretch", datetime(2026, 10, 10, 10, 30), now=NOW)

        assert (await daemon.tick(assistant, now=datetime(2026, 10, 10, 10, 29)))["reminders"] == 0
        assert (await daemon.tick(assistant, now=datetime(2026, 10, 10, 10, 30)))["reminders"] == 1
        assert (await daemon.tick(assistant, now=datetime(2026, 10, 10, 10, 31)))["reminders"] == 0
        assert sent == ["stretch"]

    @pytest.mark.asyncio
    async def test_repeating_reminder_reschedules(self, assistant, monkeypatch):
        from friday.assistant import daemon

        monkeypatch.setattr(daemon, "notify", lambda *a, **k: None)
        r = assistant.remind("water", datetime(2026, 10, 10, 11, 0), repeat="every 2 hours", now=NOW)
        await daemon.tick(assistant, now=datetime(2026, 10, 10, 11, 0))
        assert assistant.store.get_reminder(r.id).due == datetime(2026, 10, 10, 13, 0)
        assert assistant.store.get_reminder(r.id).delivered_at is None

    @pytest.mark.asyncio
    async def test_late_reminder_says_when_it_was_due(self, assistant, monkeypatch):
        from friday.assistant import daemon

        sent = []
        monkeypatch.setattr(daemon, "notify", lambda title, body, desktop=True: sent.append(body))
        assistant.remind("pay rent", datetime(2026, 10, 10, 10, 30), now=NOW)
        await daemon.tick(assistant, now=datetime(2026, 10, 11, 8, 0))  # PC was off overnight
        assert sent == ["pay rent (was due yesterday 10:30)"]

    @pytest.mark.asyncio
    async def test_automation_runs_once_per_slot_and_records_result(self, assistant, monkeypatch, tmp_path):
        from friday.assistant import daemon

        monkeypatch.setattr(daemon, "notify", lambda *a, **k: None)
        monkeypatch.setattr(daemon, "BRIEFING_DIR", tmp_path / "briefings")
        auto = assistant.automate("every day at 9pm", "briefing", now=NOW)
        run_at = datetime(2026, 10, 10, 21, 0)

        assert (await daemon.tick(assistant, now=run_at))["automations"] == 1
        assert (await daemon.tick(assistant, now=run_at))["automations"] == 0
        stored = assistant.store.get_automation(auto.id)
        assert stored.last_status == "ok"
        assert stored.next_run == datetime(2026, 10, 11, 21, 0)
        assert len(list((tmp_path / "briefings").iterdir())) == 1

    @pytest.mark.asyncio
    async def test_missed_runs_fire_once_not_per_occurrence(self, assistant, monkeypatch):
        from friday.assistant import daemon

        calls = []

        async def fake_agent(prompt):
            calls.append(prompt)
            return True, "done"

        monkeypatch.setattr(daemon, "notify", lambda *a, **k: None)
        monkeypatch.setattr(daemon, "_run_agent_task", fake_agent)
        assistant.automate("every 2 hours", "agent_task", "check inbox", now=NOW)
        await daemon.tick(assistant, now=NOW + timedelta(days=2))  # 24 runs were missed
        assert calls == ["check inbox"]

    @pytest.mark.asyncio
    async def test_failing_automation_does_not_stop_daemon(self, assistant, monkeypatch):
        from friday.assistant import daemon

        async def boom(prompt):
            raise RuntimeError("agent exploded")

        monkeypatch.setattr(daemon, "notify", lambda *a, **k: None)
        monkeypatch.setattr(daemon, "_run_agent_task", boom)
        auto = assistant.automate("every 2 hours", "agent_task", "x", now=NOW)
        assistant.remind("still delivered", NOW + timedelta(hours=2), now=NOW)
        counts = await daemon.tick(assistant, now=NOW + timedelta(hours=2))
        assert counts == {"reminders": 1, "automations": 1, "synced": 0}
        stored = assistant.store.get_automation(auto.id)
        assert stored.last_status == "failed" and "exploded" in stored.last_output

    def test_windows_install_command(self):
        from friday.assistant.daemon import TASK_NAME, windows_install_command

        cmd = windows_install_command()
        assert cmd[:4] == ["schtasks", "/Create", "/SC", "ONLOGON"]
        assert TASK_NAME in cmd and cmd[-1] == "/F"
        assert "-m friday daemon run" in cmd[cmd.index("/TR") + 1]


class TestIntent:
    @pytest.mark.parametrize("text,kind,title,when", [
        ("remind me to call mom tomorrow at 6pm", "remind", "call mom", datetime(2026, 10, 11, 18, 0)),
        ("remind me in 20 minutes to check the oven", "remind", "check the oven", datetime(2026, 10, 10, 10, 20)),
        ("book dentist appointment friday 3pm for 45 minutes", "schedule", "Dentist", datetime(2026, 10, 16, 15, 0)),
        ("schedule a meeting with Rahul next monday at 11am", "schedule", "Meeting with Rahul",
         datetime(2026, 10, 12, 11, 0)),
        ("add todo finish report by friday", "todo", "finish report", datetime(2026, 10, 16, 9, 0)),
        ("todo: buy milk", "todo", "buy milk", None),
        ("show my todos", "list_todos", "", None),
    ])
    def test_rules(self, text, kind, title, when):
        intent = parse_rules(text, now=NOW)
        assert (intent.kind, intent.title, intent.when) == (kind, title, when)

    def test_schedule_extracts_duration_and_location(self):
        intent = parse_rules("book dentist friday 3pm for 45 minutes at Smile Clinic", now=NOW)
        assert (intent.duration, intent.location) == (45, "Smile Clinic")

    def test_automation_rules(self):
        brief = parse_rules("every weekday at 8am brief me", now=NOW)
        assert (brief.kind, brief.action, brief.repeat) == ("automate", "briefing", "every weekday at 8am")
        task = parse_rules("every monday at 10am summarize my github notifications", now=NOW)
        assert (task.action, task.title) == ("agent_task", "summarize my github notifications")

    def test_repeating_reminder(self):
        intent = parse_rules("remind me to drink water every 2 hours", now=NOW)
        assert (intent.kind, intent.title, intent.repeat) == ("remind", "drink water", "every 2 hours")

    def test_extract_datetime_keeps_the_rest(self):
        when, rest = extract_datetime("call the bank tomorrow at 10am please", now=NOW)
        assert when == datetime(2026, 10, 11, 10, 0) and rest == "call the bank please"

    @pytest.mark.asyncio
    async def test_unknown_request_falls_back_to_agent_task_without_llm(self):
        intent = await understand("refactor the auth module", now=NOW, use_llm=False)
        assert intent.kind == "agent_task" and intent.title == "refactor the auth module"

    @pytest.mark.asyncio
    async def test_llm_classification_times_parsed_locally(self, monkeypatch):
        from friday.models import router

        async def chat(**kwargs):
            return {"content": '{"kind": "schedule", "title": "Haircut", "when": "sunday 11am", '
                               '"duration": "1 hour", "location": "", "repeat": "", "action": ""}'}

        monkeypatch.setattr(router, "chat", chat)
        intent = await understand("could you get me in for a haircut sunday 11am, an hour", now=NOW)
        assert (intent.kind, intent.title, intent.when, intent.duration, intent.source) == (
            "schedule", "Haircut", datetime(2026, 10, 11, 11, 0), 60, "llm")

    @pytest.mark.asyncio
    async def test_llm_garbage_falls_back(self, monkeypatch):
        from friday.models import router

        async def chat(**kwargs):
            return {"content": '{"kind": "launch_missiles"}'}

        monkeypatch.setattr(router, "chat", chat)
        intent = await understand("hmm do the thing", now=NOW)
        assert intent.kind == "agent_task"


class TestAssistantCli:
    def test_end_to_end_commands(self, tmp_path, monkeypatch):
        from typer.testing import CliRunner

        from friday.assistant import cli as acli
        from friday.cli import app

        store = AssistantStore(tmp_path / "cli.db")
        monkeypatch.setattr(acli, "_assistant", lambda: Assistant(store))
        runner = CliRunner()

        r = runner.invoke(app, ["schedule", "Dentist", "--at", "tomorrow 3pm", "--for", "45m"])
        assert r.exit_code == 0, r.output
        r = runner.invoke(app, ["schedule", "Clash", "--at", "tomorrow 3:15pm"])
        assert r.exit_code == 1 and "overlaps" in r.output
        r = runner.invoke(app, ["schedule", "Bad", "--at", "whenever"])
        assert r.exit_code == 2
        r = runner.invoke(app, ["ask", "--no-llm", "remind", "me", "to", "stretch", "in", "30", "minutes"])
        assert r.exit_code == 0 and "stretch" in r.output
        r = runner.invoke(app, ["todo", "add", "Buy milk", "--high"])
        assert r.exit_code == 0
        r = runner.invoke(app, ["agenda", "tomorrow"])
        assert r.exit_code == 0 and "Dentist" in r.output
        r = runner.invoke(app, ["automate", "add", "every weekday at 8am", "--brief"])
        assert r.exit_code == 0
        r = runner.invoke(app, ["automate", "add", "every day", "--brief", "--notify", "x"])
        assert r.exit_code == 2
        r = runner.invoke(app, ["calendar", "export", str(tmp_path / "out.ics")])
        assert r.exit_code == 0 and "BEGIN:VEVENT" in (tmp_path / "out.ics").read_text()
        store.close()
