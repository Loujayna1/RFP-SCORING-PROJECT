"""
Small asyncio helpers shared across services.

loop_semaphore() fixes a class of crash seen under Celery: a module-level
``asyncio.Semaphore`` binds itself to the first event loop that uses it and
then raises ``<Semaphore ...> is bound to a different event loop`` the moment
it is awaited from another loop. The worker runs every task in its own
``asyncio.run()`` loop, so a process-global semaphore works for a child's
first task and crashes on the second. Keying the semaphore by the running loop
(and rebuilding it when the loop changes) gives one real semaphore per loop
while preserving the same concurrency limit.

Only the loop binding changes; the limit is unchanged, so this does not alter
scoring, prompts or retrieval behaviour.
"""
from __future__ import annotations

import asyncio

# key -> (semaphore, owning_loop)
_sems: dict[str, tuple[asyncio.Semaphore, asyncio.AbstractEventLoop]] = {}


def loop_semaphore(key: str, value: int) -> asyncio.Semaphore:
    """Return a Semaphore for ``key`` bound to the current running loop.

    All callers that pass the same ``key`` within the same event loop share one
    semaphore (so the concurrency limit is enforced exactly as before). When the
    running loop differs from the one the semaphore was created on, a fresh
    semaphore is created for the new loop. The check-and-store runs without an
    ``await`` in between, so it is atomic on a single-threaded event loop.
    """
    loop = asyncio.get_running_loop()
    entry = _sems.get(key)
    if entry is None or entry[1] is not loop or entry[1].is_closed():
        sem = asyncio.Semaphore(value)
        _sems[key] = (sem, loop)
        return sem
    return entry[0]
