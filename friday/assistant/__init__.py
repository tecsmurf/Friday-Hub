"""Friday Hub — Personal assistant package.

Jarvis-style assistant features on top of the agent orchestrator:

    store       SQLite persistence for todos, appointments, reminders, automations
    timeparse   Offline natural-language date/time parsing
    recurrence  Recurring schedules ("every weekday at 8am")
    service     Scheduling with conflict checks, briefings, .ics export
    intent      Natural-language request understanding (rules + local LLM)
    daemon      Background loop delivering reminders and running automations
    notify      Desktop notifications (Windows/macOS/Linux)
"""
