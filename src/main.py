"""Entrypoint. All wiring lives in app.create_app; `app` is what a process manager
(uvicorn/gunicorn in production) imports directly.

Running this file as a script (`python -m src.main`, which `make dev` uses) execs into
the real `uvicorn` CLI with host/port/reload read from Settings, instead of calling
uvicorn.run(reload=True) in-process. That distinction matters: uvicorn's Config
construction calls its own configure_logging() as a side effect, which resets the
"uvicorn"/"uvicorn.error"/"uvicorn.access" loggers to uvicorn's own default formatter.
Called in-process, that happens in the same process that already ran *our*
configure_logging() when `app = create_app()` executed a few lines above — so uvicorn's
own reset wins, and the reload supervisor's own messages ("Uvicorn running on...",
"Will watch for changes...") silently stop appearing. Exec'ing into the CLI keeps that
supervisor a genuinely separate process that never imports this module, exactly like
running `uvicorn src.main:app --reload` by hand — the behavior this replaces.
"""

import os
import sys

from src.app import create_app

app = create_app()

if __name__ == "__main__":
    from src.config import get_settings

    settings = get_settings()
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "src.main:app",
        "--host",
        settings.app.host,
        "--port",
        str(settings.app.port),
    ]
    if settings.app.reload:
        command += ["--reload", "--reload-dir", "src"]

    os.execvp(command[0], command)
