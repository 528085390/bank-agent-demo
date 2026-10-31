"""The one event loop the process uses.

psycopg's async connections refuse Windows' default Proactor loop, so the selector loop has
to be selected before any loop is created.

Two entry points need this, and they need it in different shapes:

- `configure_event_loop()` installs the *policy*, which is what `asyncio.run()` and pytest
  consult when they build a loop.
- `selector_loop()` is a zero-argument *loop factory*, which is what uvicorn wants. uvicorn
  0.53 ignores the policy: on Windows it passes `asyncio.ProactorEventLoop` explicitly, so
  installing the policy alone is not enough and the server dies during startup. For a loop
  given as an import string, uvicorn calls it with no arguments and expects a new loop back.

Both are the same choice, so they live here together.
"""

import asyncio
import sys


def configure_event_loop() -> None:
    if sys.platform != "win32":
        return
    policy = getattr(asyncio, "WindowsSelectorEventLoopPolicy", None)
    if policy is None:
        return
    if not isinstance(asyncio.get_event_loop_policy(), policy):
        asyncio.set_event_loop_policy(policy())


def selector_loop() -> asyncio.AbstractEventLoop:
    """Build the loop uvicorn should run on.

    `configure_event_loop()` is not enough on its own: uvicorn 0.53 hard-codes
    `asyncio.ProactorEventLoop` on Windows and never consults the policy. Use this as
    `loop="app.core.eventloop:selector_loop"`, or let `scripts/serve.py` do it.
    """

    return asyncio.SelectorEventLoop()
