"""Second, hook-independent restore path: if a future Anki keeps a hook's name
and signature but stops firing it (sync_will_start, profile_will_close), the
cards we hide must still never be hidden while the collection is synced or
closed. So the collection methods that actually do that are wrapped (on the
class, chaining-safe): before Collection.close / close_for_full_sync /
sync_collection runs on the engine's own collection, everything the add-on
hid is put back. Normally the hooks have already done so and this finds an
empty ledger (no write). Re-applying after a sync stays with sync_did_finish.

No aqt imports (tested headless). compat.SAFETY_POINTS is checked by the
static self-check, so a missing point means "unavailable", not a silent gap.
"""

from __future__ import annotations

import functools
import traceback
from typing import Any, Callable

POINTS = ("close", "close_for_full_sync", "sync_collection")


def install(get_engine: Callable[[], Any], log: Callable[[str], None]) -> list[tuple[str, Any]]:
    """Wrap the points. Returns [(name, previous)] for uninstall()."""
    from anki.collection import Collection

    done = []
    for name in POINTS:
        prev = getattr(Collection, name)
        if getattr(prev, "_brb_safety", False):
            continue

        def make(name: str, prev: Callable) -> Callable:
            @functools.wraps(prev)
            def wrapper(self: Any, *args: Any, **kwargs: Any) -> Any:
                try:
                    eng = get_engine()
                    if eng is not None and getattr(eng, "col", None) is self and \
                            (eng.state.get("hidden") or eng.state.get("pulled")):
                        n = len(eng.state.get("hidden") or {})
                        eng.restore_all()
                        log(f"safety net: put back {n} hidden card(s) before Collection.{name}"
                            " (the hook that should have done it did not run)")
                except Exception:
                    log(f"safety net before Collection.{name} failed:\n{traceback.format_exc()}")
                return prev(self, *args, **kwargs)

            wrapper._brb_safety = True  # type: ignore[attr-defined]
            return wrapper

        setattr(Collection, name, make(name, prev))
        done.append((name, prev))
    return done


def uninstall(done: list[tuple[str, Any]]) -> None:
    from anki.collection import Collection

    for name, prev in done:
        if getattr(getattr(Collection, name), "_brb_safety", False):
            setattr(Collection, name, prev)
