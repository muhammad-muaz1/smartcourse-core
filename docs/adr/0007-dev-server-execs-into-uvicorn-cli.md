# 0007 — `python -m src.main` execs into the uvicorn CLI rather than calling uvicorn.run() in-process

- Status: Accepted
- Date: 2026-09-18

## Context

`make dev` needed to (a) read host/port/reload from Settings instead of a hardcoded
CLI flag, while (b) still producing the same clean startup banner
(`Uvicorn running on http://...`, `Will watch for changes...`) and reload behavior as
running `uvicorn src.main:app --reload` by hand.

The first attempt called `uvicorn.run("src.main:app", reload=True, ...)` directly from
inside `src/main.py`'s `__main__` block. This silently broke the startup banner: those
three lines simply stopped appearing, with no error.

## Root cause

`src/main.py` does `app = create_app()` at module level — required so `uvicorn` can
import `src.main:app`. Running `python -m src.main` therefore executes
`configure_logging()` (via `create_app()`) *before* reaching the `uvicorn.run(...)`
call. `uvicorn.run()` then constructs a `Config`, whose `__init__` calls uvicorn's own
`configure_logging()` — which runs `logging.config.dictConfig(...)` and resets the
`uvicorn` / `uvicorn.error` / `uvicorn.access` loggers to uvicorn's own defaults, in the
*same process* that our config just set up. The reload supervisor's own log lines then
go through that half-reset logging state and never surface.

This doesn't happen when uvicorn is invoked as a CLI (`uvicorn src.main:app --reload`):
the CLI's reload supervisor process never imports `src.main` at all — only the child
worker it later spawns does — so our `configure_logging()` never runs in the supervisor
process to begin with, and uvicorn's own default formatting stays intact for its banner.

## Decision

`python -m src.main` execs (`os.execvp`) into `python -m uvicorn src.main:app --host ...
--port ... [--reload --reload-dir src]`, with the flags built from `Settings`. This
fully replaces the process image, so the supervisor is the real uvicorn CLI process —
identical behavior to typing the command by hand — while the actual command a developer
types stays `make dev`, with host/port/reload sourced from `.env`.

## Consequences

- `app = create_app()` still runs once in the brief pre-exec process purely to load
  Settings; that work is thrown away when `execvp` replaces the process. Harmless, but
  worth knowing if `create_app()` ever grows an expensive side effect.
- `os.execvp` is POSIX-only. Fine for this project's Linux dev/deploy targets; would
  need a `subprocess.run` fallback if Windows-native (non-WSL) support is ever needed.
- Verified manually: banner lines and reload-triggered restarts both appear exactly as
  they did before this change, confirmed by touching a watched file and observing the
  worker process restart with a new PID while the supervisor's PID stays constant.
