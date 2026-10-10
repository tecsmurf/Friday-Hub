# Engineering Log

## 2026-10-10 — Core audit and stabilization

### Investigated
Every module under `friday/`, both configs, tests, README/CONTRIBUTING. Ran the test
suite, every CLI command, and the real agent CLIs available in the audit environment
(Claude Code 2.1.296, Codex CLI 0.162.1, OpenClaw 2026.6.35) to check their actual
non-interactive interfaces.

### Baseline (before changes)
- `pytest`: 57 passed, 1 failed (`test_conversation_truncation` asserted the wrong index).
- Tests wrote into the developer's real `data/memory` ChromaDB store.
- `friday status` with three agents that each take 0.3 s per call: **4.35 s**.

### Defects found and fixed
| Severity | Defect | Root cause | Fix |
|---|---|---|---|
| Critical | Codex adapter never worked | `codex "<prompt>"` starts the TUI; fails with "stdin is not a terminal" | `codex exec -`, prompt on stdin |
| Critical | OpenClaw adapter never worked, yet reported success | `openclaw run` does not exist and exits 0 | `openclaw agent --agent main --message-file <tmp>` |
| High | Command injection through legacy `execute_pattern` configs | prompt (incl. git branch names from project context) put inside a shell string with only `"` escaped | patterns are tokenised with `shlex` and run without a shell |
| High | Windows: agents installed by npm (`claude.cmd`) could not be launched, long prompts hit the 8 KB cmd.exe limit | bare command name passed to `CreateProcess`; prompt passed as argv through `cmd.exe` | resolve full path with `shutil.which`; send prompt on stdin / temp file |
| High | Health checks always "Healthy"; version guard could never mark an agent incompatible | health command errors swallowed; Claude's `--doctor` flag doesn't exist | failures now report unhealthy; smoke tests are `--help` of the execute subcommand |
| High | Incompatible agents still received tasks, and incompatibility was forgotten on the next run | guard result never consulted by Hermes; same-version check returned "ok" | Hermes skips incompatible agents; guard re-tests and can recover |
| High | After every agent failed, the primary agent ran a **second** time | `_execute_with_fallback` re-executed it to get an error | each agent runs at most once; first error returned with attempts list |
| Medium | Router could pick an agent that isn't installed | default agent returned without checking availability | falls back to best-priority available agent |
| Medium | `--agent X` silently ran a different agent on failure | fallback applied to forced agent | forced agent never falls back |
| Medium | Agent output with `[...]` mangled or crashed the CLI (Rich markup) | output passed to Rich as markup | output rendered as literal `Text`; all dynamic strings escaped |
| Medium | CLI exited 0 on failed tasks and errors | exceptions only printed | exit codes 1 (failure) / 2 (usage) |
| Medium | `OLLAMA_HOST` from `.env` ignored | read at import before `.env` loaded | read lazily; `host`, `host:port`, `0.0.0.0` normalised |
| Medium | "No LLM providers available" hid the real reason (e.g. model not pulled) | provider errors swallowed | error lists each provider's reason |
| Medium | OpenRouter "available" for any non-empty key | checked public `/models` | checks authenticated `/key`; chat no longer pre-pings |
| Medium | Agent processes outlived Friday on timeout/Ctrl-C | only top process killed, not on cancellation | process group killed on timeout and cancellation |
| Medium | Real tasks timed out at 120 s | hard-coded | per-agent `timeout`, default 600 s |
| Low | Feedback records polluted routing context; past failures looked like successes | same collection, no outcome in context | task-only search; context shows outcome and user rating |
| Low | Manual feedback promised but unreachable | no command | `friday feedback <task-id> --good/--bad` |
| Low | Context scan walked all of `$HOME` and hidden dirs | unbounded walk | bounded, hidden/egg-info skipped |
| Low | ChromaDB sent anonymous telemetry | default setting | disabled |
| Low | Task IDs could address files outside the log dir | unvalidated path join | ID validated |

### Performance (measured)
`friday status`, three fake agents at 0.3 s per call: **4.35 s → 1.70 s**. Detection,
guard, and health checks now run concurrently and the version command runs once per agent
instead of three times. Planner and router LLM calls now run concurrently on `friday run`.

### Tests
- `pytest -q`: **90 passed** (58 existing + 32 new regression tests in `tests/test_hardening.py`).
  26 of the 32 new tests fail on the pre-fix code.
- `ruff check friday tests`: clean.
- `python -m build`: wheel builds and contains `friday/defaults/friday.yaml`.
- End to end: `friday run "Reply with exactly the single word pong"` with real Claude Code →
  `pong` in 5.9 s, logged; `friday feedback` and `friday memory` verified against it.
- Not tested end to end: Codex and OpenClaw task execution (no credentials / gateway in the
  audit environment; their CLI interfaces were verified with `--help` and error output),
  Ollama/OpenRouter inference (no server or key available), Windows (covered by CI matrix).

### Security notes
- `pip-audit`: chromadb 1.5.9 has advisories (PYSEC-2026-311/3813/3814/3815) affecting its
  HTTP **server** and RBAC. Friday uses the embedded `PersistentClient` only, so they are not
  reachable; no fixed version exists yet.

### Remaining / next
- Multi-step plan execution (planner output is used only as a routing hint).
- Short-term memory is per-process, so it does not carry over between CLI invocations.
- Product direction changed to a Jarvis-style personal assistant (tasks, automations,
  appointments); see the next entry.

## 2026-10-10 — Personal assistant layer (new product direction)

### Why
ADitya set a new vision: Friday should work like Jarvis — do tasks, automate daily
routines, and schedule appointments. The agent orchestrator stays as the "do real work"
capability underneath.

### Built (`friday/assistant/`)
- SQLite store (WAL, survives restarts, safe for CLI + daemon at once) for todos,
  appointments, reminders and automations.
- Offline natural-language time parsing and recurrence rules.
- Scheduling with overlap detection, next-free-slot suggestions, automatic reminders,
  cancellation, daily briefing, RFC 5545 `.ics` export.
- `friday ask`: rules first, local LLM second (its times are re-parsed locally), agent task
  last.
- Daemon: exactly-once reminder delivery (conditional updates), automations claimed before
  running so crashes or two daemons never double-run, missed work runs once, failures
  recorded without stopping the loop. Windows log-on install via Task Scheduler.
- Desktop notifications with no dependencies; text passed via environment, not scripts.

### Tests
- `pytest -q`: **157 passed** (67 new in `tests/test_assistant.py`).
- Manual: full CLI flow (book, clash refused, remind, todos, automations, agenda, free slot,
  `.ics` export) and `python -m friday daemon run` delivering a due reminder and a due
  automation, with the automation rescheduled.
- Not tested: Windows toast and `schtasks` install (no Windows machine here; command
  construction is unit-tested and CI runs the suite on Windows), macOS notifications.

### Not done yet
- Two-way sync with Google Calendar / Outlook (waiting on which calendar to use; needs
  OAuth credentials from the user).
- Email, messaging and voice input are out of scope for this pass.
