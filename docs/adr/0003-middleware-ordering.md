# 0003 — Middleware registration order is the reverse of CLAUDE.md §7's list

- Status: Accepted
- Date: 2026-09-17

## Context

CLAUDE.md §7 specifies a 9-step middleware order and says plainly: "order is behaviour,
not style." Before wiring `app_factory.create_app()`, I verified empirically (rather
than from memory) how Starlette actually applies `app.add_middleware()` calls, since
getting this backwards would silently invert the entire order and be easy to miss in
review.

## Evidence

Against `starlette==1.6.0` / `fastapi==0.141.1` (the versions this project pins), a
three-middleware probe recording entry/exit order showed:

```
app.add_middleware(A)
app.add_middleware(B)
app.add_middleware(C)
# request comes in →
# C-in, B-in, A-in, A-out, B-out, C-out
```

The middleware added **last** wraps everything added before it, and therefore runs
**first** on the way in. Starlette's stack is LIFO, not FIFO.

## Decision

`app_factory.py` calls `add_middleware` in the exact reverse of CLAUDE.md §7's listed
order — `IdempotencyMiddleware` first, `RequestIDMiddleware` last — so that execution
order on an incoming request matches §7 exactly: RequestID runs first, Idempotency runs
last (closest to the route). The module docstring in `app_factory.py` states this
explicitly so the reversal isn't mistaken for a mistake during review.

## Consequences

- Any future middleware added to the stack must be inserted at the correct point in the
  *reversed* call order, not the point matching its position in CLAUDE.md §7's prose
  list. Get this wrong and the bug is silent — no error, just the wrong middleware
  seeing a request first.
- This is specific to Starlette's current implementation. If a future Starlette major
  version changes stack construction, re-verify before assuming this still holds.
