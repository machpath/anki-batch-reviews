"""Put back cards that a state file says Batch Reviews hid, without needing
anything else of the add-on: used when the add-on is unavailable on this Anki
(self-check, v3 off, failed probe, import failure) or cannot read its state,
so a crash or an earlier build never leaves cards hidden.

Only the standard library and the collection's own db/sched objects are used.
The rule is the engine's (engine._unhide / hidden_now):
- only cards still in queue -2 whose id is in the ledger are touched;
- a card with the recorded mod/usn gets its recorded queue back;
- a card changed since (flag, reschedule, other add-on) gets the queue Anki's
  own unbury would give (rslib Card::restore_queue_from_type), but only for a
  ledger of today: from another day only exact matches are restored (a card
  Anki buried on the new day has a new mod and is never touched);
- legacy "pulled" entries get their due back only if due/mod/usn are as recorded.
Raw writes, so mod and usn never change.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

HIDDEN_QUEUE = -2

# The ledger is also kept in its own small write-ahead file next to the state
# file (written before any hide, fsynced, the previous good copy rotated to
# ".prev"), so it survives a state file that is lost or unreadable.
LEDGER, LEDGER_PREV = ".ledger", ".ledger.prev"
LOST_MESSAGE = ("Batch Reviews was interrupted and could not read its full record of the cards it "
                "had hidden, so some may still be hidden. They come back by themselves at Anki's next "
                "day rollover. (Unbury on a deck's overview brings them back now, but it also brings "
                "back every card you buried yourself.)")


def _ids(cids) -> str:
    return "(" + ",".join(str(int(c)) for c in cids) + ")"


def _entries(raw: Any, size: int) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    if not isinstance(raw, dict):
        return out
    for key, val in raw.items():
        try:
            if isinstance(val, list) and len(val) == size and all(isinstance(x, int) for x in val):
                out[int(key)] = val
        except (TypeError, ValueError):
            continue
    return out


def _safe_name(profile: str | None) -> str:
    import re

    return re.sub(r"[^\w.-]", "_", profile or "default")


SAFE_NAME_BYTES = 100  # (any legal profile name fits; the hash keeps it unique)


def state_file_name(profile: str | None) -> str:
    """state-<name>-<hash>.json: the name made file-safe (at most 100 bytes) plus
    a short hash of the raw profile name, so profiles whose names sanitise alike
    never share a file and long names stay within file-name limits."""
    import hashlib

    raw = profile or "default"
    safe = _safe_name(raw).encode("utf8")[:SAFE_NAME_BYTES].decode("utf8", "ignore")
    return f"state-{safe}-{hashlib.sha1(raw.encode('utf8')).hexdigest()[:8]}.json"


def _named_ids(base: str) -> tuple[set[int], set[int]]:
    """(deck ids, card ids) a state file or its ledger files name."""
    decks: set[int] = set()
    cards: set[int] = set()
    for path in (base, base + LEDGER, base + LEDGER_PREV):
        try:
            data = _read_json(path)
        except Exception:
            continue
        if isinstance(data, dict):
            for key in (data.get("decks") or {}):
                try:
                    decks.add(int(key))
                except (TypeError, ValueError):
                    pass
            cards |= set(_entries(data.get("hidden"), 3)) | set(_entries(data.get("pulled"), 4))
    return decks, cards


REAL_ID_MIN = 10**12  # ids Anki makes from the clock (ms); the Default deck (1) is in every collection


def _belongs(base: str, col: Any) -> bool:
    """Does the state at `base` name decks or cards of this collection? Only
    clock-made ids count (practically unique between collections); the Default
    deck's id 1, which every collection has, does not."""
    decks, cards = _named_ids(base)
    decks = {i for i in decks if i >= REAL_ID_MIN}
    cards = {i for i in cards if i >= REAL_ID_MIN}
    if decks and col.db.scalar(f"select count() from decks where id in {_ids(decks)}"):
        return True
    return bool(cards and col.db.scalar(f"select count() from cards where id in {_ids(cards)}"))


def state_path_for(user_files: str, profile: str | None, log: Callable[[str], None] = lambda m: None,
                   col: Any = None, other_profiles: Any = ()) -> str:
    """The profile's state file. Earlier versions used state-<name>.json: on
    first use it is renamed (with its ledger files) to the new name, so a state
    with a non-empty ledger is never orphaned.

    - Both exist and the old-name file is newer (an older version ran after this
      one, then this one again): whatever this version's own files name is put
      back first (exact matches only), then they are kept as ".superseded" and
      the old-name file takes over.
    - Another profile's name sanitises to the same old name: the old-name file
      is taken only if it names decks or cards of this collection; otherwise it
      is left for that profile and this one starts fresh."""
    new = os.path.join(user_files, state_file_name(profile))
    old = os.path.join(user_files, f"state-{_safe_name(profile)}.json")  # (uncapped, as written before)
    suffixes = ("", LEDGER, LEDGER_PREV)

    def mtime(base: str) -> float | None:
        times = []
        for x in suffixes:
            try:
                times.append(os.path.getmtime(base + x))
            except OSError:
                pass
        return max(times, default=None)

    old_t, new_t = mtime(old), mtime(new)
    if old_t is None:
        return new
    shared = any(p != profile and _safe_name(p) == _safe_name(profile) for p in (other_profiles or ()))
    if shared and (col is None or not _belongs(old, col)):
        log(f"{os.path.basename(old)} is not this collection's (another profile has a similar name): left alone")
        return new
    if new_t is not None and old_t > new_t:
        if col is not None:  # never set aside a record of cards still hidden
            n = 0
            for data in (_read_or_none(new + x) for x in (LEDGER, LEDGER_PREV, "")):
                if isinstance(data, dict):
                    n += restore_from_state(col, data, log, exact_only=True)
            if n:
                log(f"put back {n} card(s) named by {os.path.basename(new)} before it was set aside")
        for x in suffixes:
            if os.path.exists(new + x):
                os.replace(new + x, new + x + ".superseded")
        log(f"{os.path.basename(old)} is newer than {os.path.basename(new)}: it takes over")
        new_t = None
    if new_t is None:
        for x in suffixes:
            if os.path.exists(old + x):
                try:
                    os.replace(old + x, new + x)
                except OSError as e:
                    log(f"could not move {old + x} to {new + x}: {e!r}")
                    return old  # keep using the old name rather than lose its ledger
        log(f"state file renamed {os.path.basename(old)} -> {os.path.basename(new)}")
    return new


def _read_or_none(path: str) -> Any:
    try:
        return _read_json(path)
    except Exception:
        return None


def _read_json(path: str) -> Any:
    with open(path, encoding="utf8") as f:
        return json.load(f)


def _ledger_ok(data: Any) -> bool:
    return isinstance(data, dict) and data.get("format") == "batch-reviews-ledger" \
        and isinstance(data.get("hidden"), dict)


def write_ledger(state_path: str, day: Any, hidden: dict, pulled: dict) -> None:
    """Atomically write the ledger file (fsynced), first rotating the current
    copy to .prev if it is readable (so .prev is always a good copy)."""
    path = state_path + LEDGER
    tmp = path + ".tmp"
    data = json.dumps({"format": "batch-reviews-ledger", "version": 1, "day": day,
                       "hidden": hidden, "pulled": pulled}, separators=(",", ":"))
    with open(tmp, "w", encoding="utf8") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    if os.path.exists(path):
        try:
            if _ledger_ok(_read_json(path)):
                os.replace(path, state_path + LEDGER_PREV)
        except Exception:
            pass  # an unreadable current copy is not rotated over a good .prev
    os.replace(tmp, path)


def clear_ledger(state_path: str) -> None:
    """After a recovery: an empty ledger, and no older copy that could still
    name cards that are not ours any more."""
    for suffix in (LEDGER_PREV,):
        try:
            os.remove(state_path + suffix)
        except OSError:
            pass
    if os.path.exists(state_path + LEDGER):
        tmp = state_path + LEDGER + ".tmp"
        with open(tmp, "w", encoding="utf8") as f:
            f.write(json.dumps({"format": "batch-reviews-ledger", "version": 1, "day": None,
                                "hidden": {}, "pulled": {}}))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, state_path + LEDGER)


def read_ledger(state_path: str) -> tuple[dict | None, str, bool]:
    """(ledger, status, exact_only): the current ledger copy if readable, else
    the previous one. status: "none" (no ledger files), "ok", or "unreadable"
    (files exist, none readable). The previous copy can still name cards that
    were un-hidden since, so from it only exact matches are restored."""
    found = False
    for suffix, exact_only in ((LEDGER, False), (LEDGER_PREV, True)):
        path = state_path + suffix
        if not os.path.exists(path):
            continue
        found = True
        try:
            data = _read_json(path)
        except Exception:
            continue
        if _ledger_ok(data):
            return data, "ok", exact_only
    return None, ("unreadable" if found else "none"), False


def restore_from_state(col: Any, state: Any, log: Callable[[str], None] = lambda m: None,
                       exact_only: bool = False, stats: dict | None = None) -> int:
    """Restore what the ledger in `state` (any state-file version) says is
    hidden. Returns the number of cards put back. Does not touch the file."""
    if not isinstance(state, dict):
        return 0
    hidden = _entries(state.get("hidden"), 3)
    pulled = _entries(state.get("pulled"), 4)
    if not hidden and not pulled:
        if stats is not None:
            stats.update(named=0, restored=0, left=0)
        return 0
    try:
        today = col.sched.today  # (also lets Anki run its own day-rollover unbury first)
    except Exception:
        today = None  # e.g. no v3 scheduler: only exact matches are restored
    same_day = today is not None and state.get("day") == today and not exact_only
    args = []
    in_queue = col.db.all(
        f"select id, type, due, mod, usn from cards where queue={HIDDEN_QUEUE} and id in {_ids(hidden)}"
    ) if hidden else []
    if hidden:
        for cid, ctype, due, mod, usn in in_queue:
            queue, rec_mod, rec_usn = hidden[cid]
            if (mod, usn) == (rec_mod, rec_usn):
                args.append((queue, cid))
            elif same_day:
                args.append(({0: 0, 2: 2}.get(ctype, 1 if due > 1_000_000_000 else 3), cid))
    groups: dict = {}
    for queue, cid in args:  # one statement per target queue (one commit each on 23.10+)
        groups.setdefault(int(queue), []).append(cid)
    for queue, cids in groups.items():
        col.db.execute(f"update cards set queue={queue} where queue={HIDDEN_QUEUE} and id in {_ids(cids)}")
    pargs = []
    if pulled:
        for cid, due, mod, usn in col.db.all(
            f"select id, due, mod, usn from cards where queue=1 and id in {_ids(pulled)}"
        ):
            if pulled[cid][1:] == [due, mod, usn]:
                pargs.append((pulled[cid][0], cid))
    if pargs:
        col.db.executemany("update cards set due=? where id=?", pargs)
    if (args or pargs) and hasattr(col.db, "modified_in_python"):
        col.save()  # Anki <= 2.1.66: commit now
    if stats is not None:
        stats["named"] = len(hidden)
        stats["restored"] = len(args)
        stats["left"] = len(in_queue) - len(args)  # named, still hidden, not ours to judge any more
    log(f"restored {len(args)} of {len(hidden)} hidden cards"
        + (f" and {len(pargs)} of {len(pulled)} pulled" if pulled else "")
        + f" from the ledger (day {state.get('day')}, today {today})")
    return len(args)


def _mid_session(text: str | None) -> bool:
    """Does the text of an unreadable state file suggest that cards were hidden
    when it was written (batching on, ledger not empty or cut off)? A state
    saved at a clean close has an empty ledger ("hidden":{})."""
    import re

    if not text:
        return False
    if re.search(r'"hidden"\s*:\s*\{\s*"\d', text):
        return True
    return bool(re.search(r'"enabled"\s*:\s*true', text)) and not re.search(r'"hidden"\s*:\s*\{\s*\}', text)


def recover_unreadable(col: Any, state_path: str, parsed: Any, log: Callable[[str], None],
                       notify: Callable[[str], None] | None = None, text: str | None = None) -> int:
    """The state file is missing or unreadable (`parsed`: what could be parsed
    of it, or None; `text`: its raw text). Restore from whatever still names the
    hidden cards (the parsed state, the ledger file), clear the ledger files.

    Tell the user once (notify + log, never guessing which buried cards are
    ours) when scheduler-buried cards remain and there is evidence some may be
    ours: only the older ledger copy could be used, or named cards had to be
    left alone (changed since), or nothing names them although a ledger file
    exists but is unreadable or the state text shows a session with cards
    hidden. A garbage file after a clean close (empty ledger file, or no sign of
    hidden cards) is no reason to alarm anyone."""
    n = 0
    named = partial = False
    left = 0
    if isinstance(parsed, dict) and (_entries(parsed.get("hidden"), 3) or _entries(parsed.get("pulled"), 4)):
        named = True
        st: dict = {}
        n += restore_from_state(col, parsed, log, stats=st)
        left += st.get("left", 0)
    ledger, status, exact_only = read_ledger(state_path)
    if ledger is not None:
        st = {}
        n += restore_from_state(col, ledger, log, exact_only=exact_only, stats=st)
        named = named or bool(st.get("named"))
        left += st.get("left", 0)
        partial = exact_only  # the newest copy was lost: cards hidden since may be missing
        log(f"ledger file used ({'previous copy, exact matches only' if exact_only else 'current copy'})")
    state_unreadable = os.path.exists(state_path) and not isinstance(parsed, dict)
    evidence = status == "unreadable" or (status == "none" and state_unreadable and _mid_session(text))
    remaining = col.db.scalar(f"select count() from cards where queue={HIDDEN_QUEUE}") or 0
    if remaining and (partial or left or (not named and evidence)):
        log(f"some hidden cards may not have been identified (previous copy used: {partial}, named but "
            f"changed since: {left}, ledger readable: {ledger is not None}, scheduler-buried now: "
            f"{remaining}): " + LOST_MESSAGE)
        if notify is not None:
            notify(LOST_MESSAGE)
    if status != "none":
        clear_ledger(state_path)
    return n


def recover_file(col: Any, path: str, log: Callable[[str], None] = lambda m: None, why: str = "",
                 notify: Callable[[str], None] | None = None) -> int:
    """restore_from_state() for a state file, then clear its ledger (the rest
    of the file is kept, so batching can carry on once available) and the
    ledger files. A missing or unreadable state file: recover_unreadable() (the
    file itself is left as it is). Returns the number of cards put back."""
    tag = lambda m: log(f"recovery{' (' + why + ')' if why else ''}: {m}")
    state: Any = None
    text: str | None = None
    readable = False
    if os.path.exists(path):
        try:
            with open(path, encoding="utf8", errors="replace") as f:
                text = f.read()
            state = json.loads(text)
            readable = isinstance(state, dict)
        except Exception as e:
            log(f"recovery: cannot read {path} ({e!r})")
    if not readable:
        return recover_unreadable(col, path, state, tag, notify, text)
    n = 0
    if state.get("hidden") or state.get("pulled"):
        n = restore_from_state(col, state, tag)
        state["hidden"], state["pulled"] = {}, {}
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf8") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    if read_ledger(path)[1] != "none":
        clear_ledger(path)
    return n


def install_fallback(addon_dir: str, log: Callable[[str], None], why: str) -> None:
    """For an add-on that will not run on this Anki: restore any ledger of the
    profile's state file when its collection opens. Uses whichever hook this
    Anki has (any signature), else polls for mw.col for a while."""
    import re

    import aqt

    def _tooltip(msg: str) -> None:
        try:  # (Qt only on the main thread)
            import threading

            from aqt.utils import tooltip

            show = lambda: tooltip(msg, period=12000)
            if threading.current_thread() is threading.main_thread():
                show()
            else:
                aqt.mw.taskman.run_on_main(show)
        except Exception:
            pass

    def run(*_args: Any) -> None:
        try:
            mw = aqt.mw
            if mw is None or mw.col is None:
                return
            path = state_path_for(os.path.join(addon_dir, "user_files"), mw.pm.name, log,
                                  col=mw.col, other_profiles=mw.pm.profiles())
            recover_file(mw.col, path, log, why, notify=_tooltip)
        except Exception as e:  # never break Anki
            log(f"recovery failed: {e!r}")

    hooks = getattr(aqt, "gui_hooks", None)
    for name in ("collection_did_load", "profile_did_open"):
        hook = getattr(hooks, name, None)
        if hook is not None and callable(getattr(hook, "append", None)):
            hook.append(run)
            return
    try:  # no usable hook: look for an open collection for a minute
        from aqt.qt import QTimer

        tries = [0]

        def poll() -> None:
            mw = aqt.mw
            if mw is not None and mw.col is not None:
                run()
            elif tries[0] < 30:
                tries[0] += 1
                QTimer.singleShot(2000, poll)

        QTimer.singleShot(2000, poll)
    except Exception as e:
        log(f"recovery not installed: {e!r}")
