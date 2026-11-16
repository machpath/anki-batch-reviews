"""Batch Reviews engine. No aqt imports: testable headless with the `anki` package.

Mechanism: cards that should be out of sight are put in queue -2 (scheduler-buried)
with a raw SQL write that leaves `mod` and `usn` untouched, so they are never marked
as changed for sync. A per-day ledger records each card's original queue/mod/usn;
a card is only ever restored if it is still exactly in the state we left it in.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable

from anki.collection import Collection

try:
    from . import compat, recover
except ImportError:  # imported as a top-level module (headless tests)
    import compat  # type: ignore[no-redef]
    import recover  # type: ignore[no-redef]

HIDDEN_QUEUE = -2  # QUEUE_TYPE_SIBLING_BURIED
STATE_VERSION = 3

NOT_STARTED, IN_PROGRESS, COMPLETE = "todo", "doing", "done"


def _ids(cids) -> str:
    return "(" + ",".join(str(int(c)) for c in cids) + ")"


def _chunks(seq: list, n: int) -> list[list]:
    return [seq[i : i + n] for i in range(0, len(seq), n)]


def _entry(fn: Callable) -> Callable:
    """Public engine calls: deck names/kinds are read once per outermost call
    (two backend calls) instead of several lookups per deck."""
    def wrapper(self: "BatchEngine", *args: Any, **kwargs: Any) -> Any:
        if self._depth == 0:
            self._info = None
        self._depth += 1
        try:
            return fn(self, *args, **kwargs)
        finally:
            self._depth -= 1

    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def default_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "enabled": False,  # off until the user clicks the toolbar link
        "day": None,
        "batch_size": None,
        # str(did) -> node, for every deck (any level) with batches today:
        # {"name", "top": top-level did, "batches": [[cid]], "current": idx,
        #  "finished": its pool is used up, "size", "pause": pause screen pending,
        #  "touched": (on the top-level node) the tree was studied here today}
        "decks": {},
        "live": {},  # str(top-level did) -> did of the tree's live deck
        "answered": [],  # cards answered here today (box colours)
        "hidden": {},  # str(cid) -> [orig_queue, mod, usn]   (ledger for `day`)
        "pulled": {},  # legacy (state versions 1-2 could pull redos forward): restored, never written now
        "ui": {"minimized": False, "expanded": []},
    }


def _check_state(state: dict) -> None:
    """Raise if the loaded state does not have the expected shape."""
    assert isinstance(state["enabled"], bool)
    for key, deck in state["decks"].items():
        assert all(isinstance(c, int) for b in deck["batches"] for c in b)
        assert 0 <= deck["current"] < len(deck["batches"])
        deck.setdefault("top", int(key))
        assert isinstance(deck["top"], int)
        deck.setdefault("touched", False)
        deck.setdefault("finished", False)
        deck.setdefault("size", state.get("batch_size") or 25)
        deck.setdefault("pause", False)
    for v in list(state["hidden"].values()) + list(state["pulled"].values()):
        assert isinstance(v, list) and all(isinstance(x, int) for x in v)
    assert isinstance(state["live"], dict) and all(isinstance(v, int) for v in state["live"].values())
    assert isinstance(state["answered"], list) and all(isinstance(c, int) for c in state["answered"])
    state["ui"].setdefault("minimized", False)
    state["ui"].setdefault("expanded", [])
    assert isinstance(state["ui"]["expanded"], list)


def _migrate_v1(state: dict) -> dict:
    """State version 1: decks had a "started" list instead of the
    touched/answered/finished fields. Everything else is compatible (stale
    hidden-ledger entries are pruned by hidden_now())."""
    for deck in state.get("decks", {}).values():
        started = deck.pop("started", [])
        deck["touched"] = bool(started)
        deck["answered"] = list(started)
        deck["finished"] = False
    state["version"] = 2
    return state


def _migrate_v2(state: dict, col: Collection) -> dict:
    """State version 2 kept batches for top-level decks only, and
    which batch indexes had an answer. Keep today's batches and progress; the
    per-card record is taken from today's review log for those batches; the
    subdeck lists are added at the next apply (needs_subdecks)."""
    answered: set[int] = set()
    try:
        since = (col.sched.day_cutoff - 86400) * 1000
        for deck in state.get("decks", {}).values():
            cids = [c for i in deck.get("answered", []) if i < len(deck["batches"]) for c in deck["batches"][i]]
            if cids:
                answered |= set(col.db.list(
                    f"select distinct cid from revlog where id>=? and cid in {_ids(cids)}", since))
    except Exception:
        pass
    for key, deck in state.get("decks", {}).items():
        deck.pop("answered", None)
        deck["top"] = int(key)
    state["live"] = {key: int(key) for key in state.get("decks", {})}
    state["answered"] = sorted(answered)
    state["needs_subdecks"] = bool(state.get("decks"))
    ui = state.setdefault("ui", {})
    ui.pop("collapsed", None)
    ui.setdefault("expanded", [])
    state["version"] = STATE_VERSION
    return state


class BatchEngine:
    def __init__(
        self,
        col: Collection,
        state_path: str,
        batch_size: int = 25,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.col = col
        self.state_path = state_path
        self.batch_size = max(1, int(batch_size))
        self.log = log or (lambda msg: None)
        self.notice: str | None = None  # a message the GUI shows once (lost ledger)
        self._ledger_last: tuple | None = None  # what the ledger file holds
        self.state = self._load()
        # in memory only: cards treated as done while preparing a batch switch,
        # the switch prepared for the answer in progress, and the last answer
        self._assume_done: set[int] = set()
        self._prepared: dict | None = None
        self._pre: tuple[int, int] | None = None  # (card id, queue before the answer)
        self._undo: list[dict] = []  # recent answers, newest last, for after_undo()
        self.pause_enabled = True  # config "pause_between_batches"
        # does this Anki bury siblings when a card is answered? (probe.py finds
        # out per Anki build; 2.1.58's v3 does not bury review siblings)
        self.sibling_bury = True
        # the hand-built waiting redo: shown early only if the probe found it is
        # built exactly as Anki's own queue entry ("redo_ok"), and where the
        # FSRS context values come from on this build ("card" / "computed")
        self.redo_ok = True
        self.redo_ok_fsrs = True
        self.redo_context = "card"
        self._info: dict | None = None  # deck names/kinds, see _deck_info()
        self._depth = 0

    # Persistence
    ##########################################################################

    def _set_notice(self, msg: str) -> None:
        self.notice = msg

    def _load(self) -> dict[str, Any]:
        if not os.path.exists(self.state_path):
            # no state, but a ledger file may still name cards a crash left hidden
            if recover.read_ledger(self.state_path)[1] != "none":
                recover.recover_unreadable(self.col, self.state_path, None, self.log, self._set_notice)
            return default_state()
        loaded = None
        text = None
        try:
            with open(self.state_path, encoding="utf8", errors="replace") as f:
                text = f.read()
            loaded = json.loads(text)
            state = default_state()
            if loaded.get("version") == 1:
                loaded = _migrate_v1(loaded)
            if loaded.get("version") == 2:
                loaded = _migrate_v2(loaded, self.col)
            if loaded.get("version") != STATE_VERSION:
                raise ValueError(f"state version {loaded.get('version')}")
            state.update(loaded)
            _check_state(state)
            return state
        except Exception as e:
            self.log(f"unreadable state file {self.state_path} ({e!r}); starting fresh (off)")
            try:  # but first put back whatever its ledger (or the ledger file) says we hid
                recover.recover_unreadable(self.col, self.state_path, loaded, self.log, self._set_notice, text)
            except Exception as e2:
                self.log(f"recovery from the unreadable state failed: {e2!r}")
            try:
                os.replace(self.state_path, self.state_path + ".corrupt")
            except OSError:
                pass
            return default_state()

    def save(self) -> bool:
        """Write the state (and the ledger file). False if the folder is gone:
        the add-on was deleted or its files replaced while running. The folder
        is deliberately not recreated (that would only leave litter), and
        _hide() then hides nothing, so no card is hidden without a record."""
        if not os.path.isdir(os.path.dirname(self.state_path) or "."):
            return False
        tmp = self.state_path + ".tmp"
        data = json.dumps(self.state, separators=(",", ":"))  # (C encoder; json.dump to a file is not)
        try:
            with open(tmp, "w", encoding="utf8") as f:
                f.write(data)
            os.replace(tmp, self.state_path)
            self._sync_ledger()
        except FileNotFoundError:  # (the folder went away just now)
            return False
        return True

    def _sync_ledger(self) -> None:
        """Keep the write-ahead ledger file (recover.write_ledger) equal to the
        state's ledger. Called from save(), which _hide() calls before it writes
        to the collection; written only when the ledger changed."""
        key = (self.state["day"], self.state["hidden"], self.state["pulled"])
        if self._ledger_last is not None and key == self._ledger_last:
            return
        if self._ledger_last is None and not (self.state["hidden"] or self.state["pulled"]) \
                and recover.read_ledger(self.state_path)[1] == "none":
            self._ledger_last = (key[0], {}, {})
            return  # nothing hidden and no file yet: nothing to write
        recover.write_ledger(self.state_path, *key)
        self._ledger_last = (key[0], dict(key[1]), dict(key[2]))

    # Raw writes
    ##########################################################################

    def _write(self, sql: str, *args: Any) -> None:
        self.col.db.execute(sql, *args)
        self._commit()

    def _write_many(self, sql: str, args: list) -> None:
        self.col.db.executemany(sql, args)
        self._commit()

    def _set_queue(self, groups: dict[int, list[int]], only_queue: str) -> None:
        """One UPDATE per target queue (`only_queue`: the guard on the current
        queue, e.g. "queue=-2"). A row-wise executemany costs a commit per row
        on Anki 23.10+ (autocommit); one IN (...) statement is one commit."""
        for queue, cids in groups.items():
            for part in _chunks(sorted(cids), 20000):
                self.col.db.execute(f"update cards set queue={int(queue)} where {only_queue} and id in {_ids(part)}")
        if groups:
            self._commit()

    def _commit(self) -> None:
        """Anki <= 2.1.66 keeps a long-running transaction and commits Python's
        writes at the next save; commit ours right away, so a crash can't undo a
        restore that the ledger already forgot. (From 23.10 every write is
        committed at once and save() is a no-op.)"""
        if hasattr(self.col.db, "modified_in_python"):
            self.col.save()

    # Basic queries
    ##########################################################################

    @property
    def enabled(self) -> bool:
        return bool(self.state["enabled"])

    @property
    def decks(self) -> dict[str, dict]:
        return self.state["decks"]

    def _deck_info(self) -> dict:
        """{"names": did -> full name, "ids": name -> did, "filtered": dids}."""
        if self._info is None:
            every = self.col.decks.all_names_and_ids(include_filtered=True)
            normal = {d.id for d in self.col.decks.all_names_and_ids(include_filtered=False)}
            names = {d.id: d.name for d in every}
            self._info = {"names": names, "ids": {n: i for i, n in names.items()},
                          "filtered": set(names) - normal}
        return self._info

    def _tree_set(self, did: int) -> set[int]:
        """The deck and every deck below it, by current name (cached per call):
        what Anki shows when this deck is studied, after any move or rename."""
        info = self._deck_info()
        trees = info.setdefault("trees", {})
        if did not in trees:
            name = info["names"].get(int(did))
            trees[did] = {int(did)} if name is None else {
                d for d, n in info["names"].items() if n == name or n.startswith(name + "::")}
        return trees[did]

    def top_level_id(self, did: int) -> int:
        info = self._deck_info()
        name = info["names"].get(int(did))
        if not name:
            return int(did)
        return int(info["ids"].get(name.split("::")[0], did))

    def _normal_deck(self, did: int) -> bool:
        info = self._deck_info()
        return int(did) in info["names"] and int(did) not in info["filtered"]

    def _usable(self, did: int) -> bool:
        """Deck still exists, is a normal deck and is still in the tree its
        batches were made for (a top-level node is still top-level)."""
        node = self.decks.get(str(did))
        if node is None or not self._normal_deck(did):
            return False
        name = self._deck_info()["names"][int(did)]
        if int(did) == node["top"]:
            return "::" not in name
        return self.top_level_id(did) == node["top"] and self._usable(node["top"])

    @_entry
    def tops(self) -> list[int]:
        """Top-level decks with batches today (whose node is usable)."""
        return [int(k) for k, n in self.decks.items() if n["top"] == int(k) and self._usable(int(k))]

    def nodes_of(self, top: int) -> list[int]:
        return [int(k) for k, n in self.decks.items() if n["top"] == top]

    @_entry
    def live_deck(self, top: int) -> int:
        """The deck whose current batch is the visible one in `top`'s tree."""
        did = int(self.state["live"].get(str(top), top))
        return did if did != top and self._usable(did) else top

    def _tree(self, did: int) -> list[int]:
        return [int(d) for d in self.col.decks.deck_and_child_ids(did)]

    @_entry
    def hidden_now(self) -> set[int]:
        """Cards the add-on hid that are still in queue -2.

        Ledger entries whose card is gone or no longer in queue -2 (suspended,
        user-buried, reset...) are dropped: such cards are never touched.

        Reading col.sched.today first makes the backend run its own day-rollover
        unbury (sched_timing_today -> unbury_if_day_rolled_over) before we look.
        A ledger from another day is not dropped blindly: entries still in -2 with
        exactly the recorded mod/usn are restored first (a no-op when Anki already
        unburied them; needed when "today" moved backwards and Anki did not). A
        card Anki buried on the new day has a new mod, so it is never touched."""
        today = self.col.sched.today
        ledger = self.state["hidden"]
        if ledger and self.state["day"] != today:
            rows = self.col.db.all(
                f"select id, mod, usn from cards where queue={HIDDEN_QUEUE} and id in {_ids(ledger)}"
            )
            groups: dict[int, list[int]] = {}
            for c, mod, usn in rows:
                if ledger[str(c)][1:] == [mod, usn]:
                    groups.setdefault(ledger[str(c)][0], []).append(c)
            self._set_queue(groups, f"queue={HIDDEN_QUEUE}")
            n = sum(len(v) for v in groups.values())
            self.log(f"day changed: restored {n} of {len(ledger)} entries from day {self.state['day']}")
            self.state["hidden"] = ledger = {}
            self.save()
        if not ledger:
            return set()
        live = set(self.col.db.list(
            f"select id from cards where queue={HIDDEN_QUEUE} and id in {_ids(ledger)}"
        ))
        for key in [k for k in ledger if int(k) not in live]:
            del ledger[key]
        return live

    def _pending(self, cids, hidden: set[int], dids: set[int] | None = None,
                 did_of: dict | None = None) -> set[int]:
        """Cards of `cids` that still need studying today. Deleted cards are done.
        With `dids` (a deck's tree), a card whose deck is no longer in it (the
        deck was moved, the card moved) is done for that deck: studying it can
        never show that card. `did_of` (if given) is filled with card -> deck."""
        if not cids:
            return set()
        today = self.col.sched.today
        cutoff = self.col.sched.day_cutoff
        rows = self.col.db.all(
            f"select id, queue, due, odid, did from cards where id in {_ids(cids)}"
        )
        out = set()
        for cid, queue, due, odid, did in rows:
            if did_of is not None:
                did_of[cid] = did
            if cid in self._assume_done:
                continue
            if dids is not None and did not in dids:
                continue
            if cid in hidden:
                out.add(cid)
            elif odid:
                continue  # moved into a filtered deck: not ours any more
            elif queue == 2 and due <= today:
                out.add(cid)
            elif queue == 1 and due < cutoff:
                out.add(cid)
            elif queue == 3 and due <= today:
                out.add(cid)
        return out

    def _status(self, cids: list[int], pending: set[int], answered: set[int]) -> tuple[str, int, int]:
        done = len(cids) - len(pending.intersection(cids))
        if done == len(cids):
            return COMPLETE, done, len(cids)
        if done > 0 or answered.intersection(cids):
            return IN_PROGRESS, done, len(cids)
        return NOT_STARTED, done, len(cids)

    @_entry
    def batch_status(self, did: int, idx: int, hidden: set[int] | None = None) -> tuple[str, int, int]:
        """(status, done, total) for one batch: gray (todo) = nothing done or
        answered here today, light blue (doing) = some, dark blue (done) = all."""
        if hidden is None:
            hidden = self.hidden_now()
        cids = self.decks[str(did)]["batches"][idx]
        return self._status(cids, self._pending(cids, hidden, self._tree_set(did)), set(self.state["answered"]))

    def _complete(self, did: int, idx: int, hidden: set[int]) -> bool:
        return not self._pending(self.decks[str(did)]["batches"][idx], hidden, self._tree_set(did))

    def _scoped(self, pending: set[int], did_of: dict, did: int) -> set[int]:
        """`pending` (of a whole tree) restricted to cards now in deck `did`'s tree."""
        tree = self._tree_set(did)
        return {c for c in pending if did_of.get(c) in tree}

    @_entry
    def grid(self, only_did: int | None = None) -> list[dict]:
        """Rows for the UI: one per deck (any level) with batches today, in deck
        list order; `parent` is the nearest ancestor row, `depth` its level."""
        if not self.enabled:
            return []
        hidden = self.hidden_now()
        keys = [k for k in self.decks if (only_did is None or int(k) == only_did) and self._usable(int(k))]
        if not keys:
            return []
        names = {k: self._deck_info()["names"][int(k)] for k in keys}
        did_of: dict = {}
        pending_all = self._pending({c for k in keys for b in self.decks[k]["batches"] for c in b}, hidden,
                                    did_of=did_of)
        answered = set(self.state["answered"])
        by_name = {names[k]: int(k) for k in keys}
        rows = []
        for key in sorted(keys, key=lambda k: [p.lower() for p in names[k].split("::")]):
            deck, did, name = self.decks[key], int(key), names[key]
            parts = name.split("::")
            parent = next((by_name[n] for n in ("::".join(parts[:i]) for i in range(len(parts) - 1, 0, -1))
                           if n in by_name), None)
            pending = self._scoped(pending_all, did_of, did)  # (a card moved out of this deck is done here)
            boxes = [self._status(b, pending, answered) for b in deck["batches"]]
            rows.append(
                {
                    "did": did,
                    "name": name,
                    "label": parts[-1],
                    "top": deck["top"],
                    "parent": parent,
                    "depth": len(parts) - 1,
                    "live": self.live_deck(deck["top"]) == did,
                    "boxes": boxes,  # [(status, done, total)]
                    "current": deck["current"],
                    "done": boxes[deck["current"]][1],
                    "total": boxes[deck["current"]][2],
                }
            )
        for row in rows:
            row["children"] = any(r["parent"] == row["did"] for r in rows)
        return rows

    # Hiding and restoring
    ##########################################################################

    def _hide(self, cids: set[int]) -> None:
        if not cids:
            return
        rows = self.col.db.all(
            f"select id, queue, mod, usn from cards where queue in (1,2,3) and id in {_ids(cids)}"
        )
        if not rows:
            return
        # write-ahead: record before touching the collection
        by_queue: dict[int, list[int]] = {}
        for cid, queue, mod, usn in rows:
            self.state["hidden"][str(cid)] = [queue, mod, usn]
            by_queue.setdefault(queue, []).append(cid)
        if not self.save():
            # the record could not be written (folder gone): hide nothing
            for cid, queue, mod, usn in rows:
                self.state["hidden"].pop(str(cid), None)
            return
        # one statement per original queue, guarded on it (the rows were read
        # just above, in this same call: nothing else writes in between)
        for queue, cids in by_queue.items():
            for part in _chunks(sorted(cids), 20000):
                self.col.db.execute(f"update cards set queue={HIDDEN_QUEUE} where queue={int(queue)} and id in {_ids(part)}")
        self._commit()

    def _unhide(self, cids: set[int]) -> None:
        if not cids:
            return
        ledger = self.state["hidden"]
        groups: dict[int, list[int]] = {}
        for cid, ctype, due, mod, usn in self.col.db.all(
            f"select id, type, due, mod, usn from cards where queue={HIDDEN_QUEUE} and id in {_ids(cids)}"
        ):
            orig_queue, orig_mod, orig_usn = ledger[str(cid)]
            if (mod, usn) == (orig_mod, orig_usn):
                queue = orig_queue
            else:
                # changed while hidden (flag, reschedule, other add-on): derive the
                # queue like rslib Card::restore_queue_from_type (used by unbury)
                queue = {0: 0, 2: 2}.get(ctype, 1 if due > 1_000_000_000 else 3)
            groups.setdefault(queue, []).append(cid)
        self._set_queue(groups, f"queue={HIDDEN_QUEUE}")
        for cid in cids:
            ledger.pop(str(cid), None)
        self.save()

    def _unpull(self) -> None:
        pulled = self.state["pulled"]
        if not pulled:
            return
        live = self.col.db.all(
            f"select id, due, mod, usn from cards where queue=1 and id in {_ids(pulled)}"
        )
        args = [
            (pulled[str(cid)][0], cid)
            for cid, due, mod, usn in live
            if pulled[str(cid)][1:] == [due, mod, usn]
        ]
        if args:
            self._write_many("update cards set due=? where id=?", args)
        self.state["pulled"] = {}
        self.save()

    @_entry
    def restore_all(self) -> None:
        """Put back everything the add-on changed. Batches are kept. Afterwards
        the hidden ledger is empty, so cards that arrive buried (e.g. from a
        sync) are never touched."""
        self._unhide(self.hidden_now())
        self._unpull()

    # Building batches
    ##########################################################################

    def _review_pool(self, did: int) -> list[int]:
        """Review cards the scheduler would give today for deck `did`, in order.

        get_queued_cards() only works on the current deck. If `did` is not the
        current deck, it is switched with a raw write to the config table (no
        usn/mtime change, so nothing syncs) and switched back afterwards."""
        if self.col.decks.get_current_id() == did:
            restore = None
        else:
            row = self.col.db.first("select val from config where key='curDeck'")
            if row is None:
                prev = self.col.decks.get_current_id()
                self.col.decks.set_current(did)
                restore = lambda: self.col.decks.set_current(prev)
            else:
                orig = bytes(row[0]).decode()
                self._write(
                    "update config set val=cast(? as blob) where key='curDeck'", str(int(did))
                )
                restore = lambda: self._write(
                    "update config set val=cast(? as blob) where key='curDeck'", orig
                )
        try:
            queued = self.col.sched.get_queued_cards(fetch_limit=100000)
            # (a filtered deck inside the tree is part of Anki's session for it, but
            # cards in a filtered deck are the user's explicit choice: not batched)
            return [qc.card.id for qc in queued.cards if qc.card.queue == 2 and not qc.card.original_deck_id]
        finally:
            if restore:
                restore()

    def _new_deck(self, did: int, name: str, pool: list[int], top: int) -> dict:
        return {
            "name": name,
            "top": top,
            "batches": _chunks(pool, self.batch_size),
            "current": 0,
            "touched": False,  # (top-level node) a card answered in the tree today or a box clicked
            "finished": False,
            "size": self.batch_size,  # batch size these batches were built with
            "pause": False,  # pause screen pending before this deck's current batch
        }

    def _subdeck_lists(self, top: int, pool: list[int]) -> dict[int, list[int]]:
        """Each subdeck's list for today: its cards of the top-level deck's pool,
        in that order (each card goes to its deck and every ancestor below the
        top). One query, no scheduler calls.

        This is exactly what the subdeck's own session gives as long as no limit
        cut the top-level pool. Where a limit did, a subdeck studied directly
        may be allowed more: those come as extension batches when it is the live
        deck and its batches are done (or, for a deck with no share at all, as
        its own list when it is opened: set_live)."""
        info = self._deck_info()
        names, ids, filtered = info["names"], info["ids"], info["filtered"]
        out: dict[int, list[int]] = {}
        if not pool:
            return out
        chains: dict[int, list[int]] = {}
        for cid, did in self.col.db.all(f"select id, did from cards where id in {_ids(pool)}"):
            if did not in chains:
                parts = names.get(did, "").split("::")
                chain = [ids.get("::".join(parts[:i])) for i in range(len(parts), 1, -1)]
                chains[did] = [d for d in chain if d is not None and d not in filtered and d != top]
            for d in chains[did]:
                out.setdefault(d, []).append(cid)
        order = {c: i for i, c in enumerate(pool)}
        for lst in out.values():
            lst.sort(key=order.__getitem__)
        return out

    def _add_tree(self, decks: dict, top: int, name: str, pool: list[int]) -> None:
        decks[str(top)] = self._new_deck(top, name, pool, top)
        names = self._deck_info()["names"]
        for did, sub in self._subdeck_lists(top, pool).items():
            if sub:
                decks[str(did)] = self._new_deck(did, names[did], sub, top)
        self.state["live"][str(top)] = top

    def _due_in_tree(self, did: int) -> int:  # (tests and diagnostics)
        return self.col.db.scalar(
            f"select count() from cards where odid=0 and queue=2 and due<=? and did in {_ids(self._tree(did))}",
            self.col.sched.today)

    def build(self, keep_touched: bool = False) -> None:
        """Restore everything, then split today's reviews of every deck, at every
        level, into batches.

        With keep_touched, trees studied on this device today keep their
        batches exactly (only cleared 'finished' so leftovers are looked for)."""
        self.restore_all()
        if self.state["day"] != self.col.sched.today:
            keep_touched = False
            self.state["answered"] = []
        old = self.decks
        decks: dict[str, dict] = {}
        live = self.state["live"]
        self.state["live"] = {}
        for node in self.col.sched.deck_due_tree().children:
            key = str(node.deck_id)
            if keep_touched and key in old and old[key]["touched"]:
                for k, n in old.items():
                    if n["top"] == node.deck_id:
                        decks[k] = dict(n, finished=False)  # a pause survives a sync
                self.state["live"][key] = live.get(key, node.deck_id)
                continue
            if node.review_count <= 0 or not self._normal_deck(node.deck_id):
                continue
            pool = self._review_pool(node.deck_id)
            if pool:
                self._add_tree(decks, node.deck_id, node.name, pool)
        kept = {k for k in decks if k in old and decks[k]["batches"] is old[k]["batches"]}
        self._undo = [u for u in self._undo if str(u.get("top")) in kept]  # positions still valid
        self.state.pop("needs_subdecks", None)
        self.state["no_pool"] = []  # decks found with nothing to give today (set_live)
        self.state.update(day=self.col.sched.today, batch_size=self.batch_size, decks=decks)
        self.save()

    def _add_missing_subdecks(self) -> None:
        """State migrated from a build with top-level batches only: give each
        subdeck its list as the in-order subsequence of the top-level deck's
        batches, today's progress untouched."""
        self.state.pop("needs_subdecks", None)
        for top in [int(k) for k, n in list(self.decks.items()) if n["top"] == int(k)]:
            node = self.decks[str(top)]
            pool = [c for b in node["batches"] for c in b]
            if not self._normal_deck(top):
                continue
            for did, sub in self._subdeck_lists(top, pool).items():
                if sub and str(did) not in self.decks:
                    new = self._new_deck(did, self._deck_info()["names"][did], sub, top)
                    new["batches"] = _chunks(sub, node.get("size") or self.batch_size)
                    self.decks[str(did)] = new
        self.save()

    def _extend(self, did: int, hidden: set[int]) -> int | None:
        """All batches of `did` are complete: turn any reviews the scheduler
        still gives today into new batches. Returns the first new index."""
        deck = self.decks[str(did)]
        in_batches = {c for b in deck["batches"] for c in b}
        tree = set(self.col.db.list(f"select id from cards where did in {_ids(self._tree(did))}"))
        mine = {c for c in hidden if c in tree or c in in_batches}
        self._unhide(mine)
        hidden -= mine
        full = self._review_pool(did)
        if self._assume_done:
            # called before an answer: the answer still has to count against the
            # review limit. If the limit is what cuts the pool (fewer cards than
            # are due), the pool after the answer is one card shorter.
            cid = next(iter(self._assume_done))
            due = self.col.db.scalar(
                f"select count() from cards where odid=0 and queue=2 and due<=? "
                f"and did in {_ids(self._tree(did))}", self.col.sched.today)
            counts = self.col.db.scalar("select queue in (2,3) from cards where id=?", cid)
            if counts and len(full) < due:
                full = [c for c in full if c != cid][: len(full) - 1]
        pool = [c for c in full if c not in in_batches]
        if not pool:
            return None
        first = len(deck["batches"])
        deck["batches"].extend(_chunks(pool, deck.get("size") or self.batch_size))
        self._distribute_extension(did, pool)
        return first

    def _distribute_extension(self, did: int, pool: list[int]) -> None:
        """The new cards of an extension also join every other row they belong
        to (the decks between them and the top-level deck), as the morning
        lists are built: each such row gets them as new batches, in pool order."""
        top = self.decks[str(did)]["top"]
        info = self._deck_info()
        names, ids, filtered = info["names"], info["ids"], info["filtered"]
        per: dict[int, list[int]] = {}
        for cid, cdid in self.col.db.all(f"select id, did from cards where id in {_ids(pool)}"):
            parts = names.get(cdid, "").split("::")
            for i in range(len(parts), 0, -1):
                d = ids.get("::".join(parts[:i]))
                if d is not None and d != did and d not in filtered and self.top_level_id(d) == top:
                    per.setdefault(d, []).append(cid)
        order = {c: i for i, c in enumerate(pool)}
        for d, cids in per.items():
            cids.sort(key=order.__getitem__)
            node = self.decks.get(str(d))
            if node is None:
                self.decks[str(d)] = self._new_deck(d, names[d], cids, top)
                continue
            have = {c for b in node["batches"] for c in b}
            cids = [c for c in cids if c not in have]
            if cids:
                node["batches"].extend(_chunks(cids, node.get("size") or self.batch_size))
                node["finished"] = False

    # Main entry points
    ##########################################################################

    @_entry
    def ensure(self) -> bool:
        """Bring the collection in line with the state. Returns True if cards changed."""
        if not self.enabled:
            had = bool(self.hidden_now() or self.state["pulled"])
            self.restore_all()
            return had
        if self.state["day"] != self.col.sched.today or self.state["batch_size"] is None:
            self.build()
        return self._apply()

    @_entry
    def after_sync(self) -> bool:
        """After a sync: rebuild trees not studied here today from the synced
        collection, keep the others' batches exactly, then re-apply."""
        if not self.enabled or self.state["day"] != self.col.sched.today or self.state["batch_size"] is None:
            return self.ensure()
        self.build(keep_touched=True)
        self._apply()
        # a kept pause only stands while there is a next batch to show
        hidden = self.hidden_now()
        for key, deck in self.decks.items():
            if deck.get("pause") and (deck["finished"] or self._complete(int(key), deck["current"], hidden)):
                deck["pause"] = False
        self.save()
        return True

    def _day_stale(self) -> bool:
        return self.state["day"] != self.col.sched.today or self.state["batch_size"] is None

    def _advance_live(self, top: int, hidden: set[int], pending: set[int], did_of: dict) -> int | None:
        """Make sure the live deck of `top`'s tree has an incomplete current batch:
        move on to its next unfinished batch, else turn leftovers into extension
        batches, else fall back to the top-level deck. Returns the live deck, or
        None if the whole tree is finished for today."""
        did = self.live_deck(top)
        while True:
            deck = self.decks[str(did)]
            batches = deck["batches"]
            mine = self._scoped(pending, did_of, did)
            if mine.intersection(batches[deck["current"]]):
                break
            nxt = self._next_unfinished_in(deck, mine)
            if nxt is None and not deck["finished"]:
                nxt = self._extend(did, hidden)
                deck["finished"] = nxt is None
                if nxt is not None:
                    new = {c for n in self.nodes_of(top) for b in self.decks[str(n)]["batches"] for c in b}
                    pending |= self._pending(new, hidden, self._tree_set(top), did_of)
            if nxt is not None:
                deck["current"] = nxt
                break
            if did == top:
                self.state["live"][str(top)] = top
                return None
            did = top  # the live deck has nothing left: back to the top-level deck
        self.state["live"][str(top)] = did
        return did

    def _next_unfinished_in(self, deck: dict, pending: set[int]) -> int | None:
        n = len(deck["batches"])
        for step in range(1, n):  # the others, after the current one
            idx = (deck["current"] + step) % n
            if pending.intersection(deck["batches"][idx]):
                return idx
        return None

    def _apply(self) -> bool:
        if self.decks and not self._assume_done and not self._day_stale() and \
                not any(self._normal_deck(int(k)) for k in self.decks):
            self.log("none of the state's decks exist in this collection: rebuilding")
            self.state["batch_size"] = None  # (-> build() below)
        if self.state.get("needs_subdecks") and not self._day_stale():
            self._add_missing_subdecks()
        if self._day_stale() and not self._assume_done:
            self.build()  # every path into _apply sees today's batches
        hidden = self.hidden_now()
        hidden_before = set(hidden)
        desired: set[int] = set()
        today = self.col.sched.today
        for top in self.tops():
            nodes = [n for n in self.nodes_of(top) if self._usable(n)]
            in_batches = {c for n in nodes for b in self.decks[str(n)]["batches"] for c in b}
            did_of: dict = {}
            # only cards still in this tree: a card whose deck was moved elsewhere
            # is neither waited for nor hidden by this tree's batches
            pending = self._pending(in_batches, hidden, self._tree_set(top), did_of)
            live = self._advance_live(top, hidden, pending, did_of)
            nodes = [n for n in self.nodes_of(top) if self._usable(n)]  # (extension may add rows)
            for n in nodes:  # other rows: their current box moves off a done batch
                deck = self.decks[str(n)]
                mine = self._scoped(pending, did_of, n)
                if n != live and not mine.intersection(deck["batches"][deck["current"]]):
                    nxt = self._next_unfinished_in(deck, mine)
                    if nxt is not None:
                        deck["current"] = nxt
            if live is None:
                continue  # finished: un-hide everything, Anki's limits apply
            deck = self.decks[str(live)]
            current = set(deck["batches"][deck["current"]])
            in_batches = {c for n in nodes for b in self.decks[str(n)]["batches"] for c in b}
            tree_due = self.col.db.list(
                f"select id from cards where odid=0 and queue in (2,{HIDDEN_QUEUE}) "
                f"and due<=? and did in {_ids(self._tree(top))}",
                today,
            )
            desired |= set(tree_due) - current
            desired |= self._pending(in_batches - current, hidden, self._tree_set(top))
        # `desired` may include cards the user or Anki buried: only ever hide
        # cards that are visible now, and keep the ones we already hid.
        # never hide the card being answered (or a visible sibling it will bury);
        # siblings we already hide stay hidden
        desired = (desired - self._assume_done) | (self._assume_done & hidden)
        to_hide = self._visible(desired - hidden)
        to_unhide = hidden - desired
        self._unhide(to_unhide)
        self._hide(to_hide)
        self.save()
        return bool(to_hide or to_unhide or hidden != hidden_before)

    def _visible(self, cids: set[int]) -> set[int]:
        if not cids:
            return set()
        return set(
            self.col.db.list(f"select id from cards where queue in (1,2,3) and id in {_ids(cids)}")
        )

    def _next_unfinished(self, did: int, hidden: set[int]) -> int | None:
        deck = self.decks[str(did)]
        return self._next_unfinished_in(deck, self._pending({c for b in deck["batches"] for c in b}, hidden))

    @_entry
    def set_enabled(self, on: bool) -> None:
        self.clear_pause()
        self.state["enabled"] = bool(on)
        if on:
            sizes = {d.get("size") for d in self.decks.values()} | {self.state["batch_size"]}
            if self.state["day"] != self.col.sched.today or sizes - {self.batch_size}:
                self.state["batch_size"] = None  # forces a rebuild in ensure()
            for deck in self.decks.values():
                deck["finished"] = False  # look for leftovers again
        self.save()
        self.ensure()

    @_entry
    def disable_after_error(self) -> None:
        """Fail open: put everything back and switch batching off."""
        self.state["enabled"] = False
        try:
            self.restore_all()
        finally:
            self.save()

    def _clear_tree_pause(self, top: int) -> None:
        for n in self.nodes_of(top):
            self.decks[str(n)]["pause"] = False

    @_entry
    def set_live(self, did: int) -> bool:
        """The user opened deck `did` to study (overview or review). If it has
        batches today, its current batch becomes the visible one in its tree.
        A deck without batches today (or a filtered deck) changes nothing; a
        deck whose batches are all done does not take over (recheck_finished()
        looks for leftovers when its review starts). Returns True if cards changed."""
        if not self.enabled:
            return False
        if self._day_stale():
            return self.ensure()
        deck = self.decks.get(str(did))
        if deck is None:
            return self._live_on_demand(int(did))
        if deck["finished"] or not self._usable(did):
            return False
        top = deck["top"]
        if self.live_deck(top) == did:
            return False
        self.state["live"][str(top)] = did
        self._clear_tree_pause(top)
        return self._apply()

    def _live_on_demand(self, did: int) -> bool:
        """A deck with no batches (no share of its top-level deck's pool, because
        a limit cut that pool) is opened to study: if Anki would give it reviews
        when studied directly, those become its own batches and it goes live.
        One scheduler call for this deck only; decks with nothing are remembered
        for the day."""
        top = self.top_level_id(did)
        if top == did or str(top) not in self.decks or not self._usable(top) or not self._normal_deck(did):
            return False
        if did in self.state.setdefault("no_pool", []):
            return False
        tree = self._tree(did)
        due = self.col.db.scalar(
            f"select count() from cards where odid=0 and queue in (2,{HIDDEN_QUEUE}) and due<=? and did in {_ids(tree)}",
            self.col.sched.today)
        if not due:
            self.state["no_pool"].append(did)
            self.save()
            return False
        hidden = self.hidden_now()
        mine = set(self.col.db.list(f"select id from cards where queue={HIDDEN_QUEUE} and did in {_ids(tree)}")) & hidden
        self._unhide(mine)  # the scheduler only gives what is not hidden
        pool = self._review_pool(did)
        if not pool:
            self.state["no_pool"].append(did)
            self._apply()
            return bool(mine)
        self.decks[str(did)] = self._new_deck(did, self._deck_info()["names"][did], pool, top)
        self.state["live"][str(top)] = did
        self._clear_tree_pause(top)
        self._apply()
        return True

    @_entry
    def select_batch(self, did: int, idx: int) -> bool:
        """Make batch `idx` of deck `did` (any level) current and that deck the
        live one of its tree. False if not possible."""
        if self.enabled and self._day_stale():
            self.ensure()  # a click on yesterday's grid: rebuild instead
            return False
        deck = self.decks.get(str(did))
        if not self.enabled or deck is None or not self._usable(did):
            return False
        if not 0 <= idx < len(deck["batches"]) or self._complete(did, idx, self.hidden_now()):
            return False
        top = deck["top"]
        deck["current"] = idx
        self.decks[str(top)]["touched"] = True
        self.state["live"][str(top)] = did
        self._clear_tree_pause(top)
        self._apply()
        return True

    @_entry
    def recheck_finished(self, current_deck_id: int) -> bool:
        """When studying a finished deck directly, look for reviews that became
        due since (e.g. a raised limit); if there are, it becomes the live deck."""
        if self.enabled and self._day_stale():
            return self.ensure()
        deck = self.decks.get(str(current_deck_id))
        if not self.enabled or deck is None or not deck["finished"] or not self._usable(current_deck_id):
            return False
        deck["finished"] = False
        self.state["live"][str(deck["top"])] = int(current_deck_id)
        return self._apply()

    def _positions(self, top: int) -> dict:
        return {str(n): [self.decks[str(n)]["current"], self.decks[str(n)]["finished"]] for n in self.nodes_of(top)}

    def _set_positions(self, pos: dict) -> None:
        for key, (cur, fin) in pos.items():
            deck = self.decks.get(key)
            if deck is not None and cur < len(deck["batches"]):
                deck["current"], deck["finished"] = cur, fin

    @_entry
    def prepare_answer(self, cid: int, current_deck_id: int, finishes: bool) -> bool:
        """Called just before an answer is applied. If it is the last pending card
        of the live batch and the answer will finish it for today, show the
        next batch now (or the top-level deck's, or un-hide the tree if nothing
        is left), so that no collection write is needed after the answer and it
        stays undoable. The positions only move in after_answer(). Returns True
        if cards changed."""
        self._prepared = None
        self._pre = (cid, self.col.db.scalar("select queue from cards where id=?", cid))
        if not (self.enabled and finishes) or self.state["day"] != self.col.sched.today:
            return False
        top = self.top_level_id(current_deck_id)
        if str(top) not in self.decks or not self._usable(top):
            return False
        live = self.live_deck(top)
        deck = self.decks[str(live)]
        if cid not in deck["batches"][deck["current"]]:
            return False
        pos, live_before = self._positions(top), self.state["live"].get(str(top), top)
        sizes = {str(n): len(self.decks[str(n)]["batches"]) for n in self.nodes_of(top)}
        nodes_before = set(self.decks)
        # siblings this answer will bury count as done too
        self._assume_done = {cid} | self._siblings_buried_by(cid, self._pre[1])
        changed = applied = False
        try:
            if not self._complete(live, deck["current"], self.hidden_now()):
                return False  # other cards of the batch are still pending
            applied = True
            changed = self._apply()
            self._prepared = {"top": top, "cid": cid, "live": self.live_deck(top),
                              "pos": self._positions(top),
                              "ext": {k: n for k, n in sizes.items()
                                      if k in self.decks and len(self.decks[k]["batches"]) > n},
                              "new_nodes": [k for k in self.decks if k not in nodes_before]}
            # pool lookups may have rebuilt the backend queue with other cards on
            # top; answer_card() would then refuse this card ("not at top of
            # queue"). Any non-select statement makes the backend drop its queues.
            self._write("update cards set queue=queue where 0")
        finally:
            self._assume_done = set()
            if applied:  # (positions only moved inside this call otherwise)
                self._set_positions(pos)
                self.state["live"][str(top)] = live_before
                self.save()
        return changed

    @_entry
    def after_answer(self, cid: int, current_deck_id: int) -> bool:
        """Called after each answer, before the reviewer fetches the next card.

        Moves on to the next unfinished batch of the live deck when its current
        one (redos included) is done, falling back to the top-level deck when
        the live deck has nothing left. If prepare_answer() already did the
        switch, this only updates the state (no collection write, so the answer
        stays undoable). Returns True if cards changed."""
        prepared, self._prepared = self._prepared, None
        if not self.enabled:
            return False
        if self.state["day"] != self.col.sched.today:
            return self.ensure()
        top = self.top_level_id(current_deck_id)
        last = {"top": None, "cid": cid, "live": None, "pos": None, "answered": False,
                "reps": None, "prepared": False, "siblings": {}}
        if cid:
            # siblings first: the answer may come from a deck without batches
            # (a filtered deck, another top-level deck) and still bury ours
            last["reps"] = self.col.db.scalar("select reps from cards where id=?", cid)
            pre_queue = self._pre[1] if self._pre and self._pre[0] == cid else 2
            last["siblings"] = self._take_buried_siblings(cid, pre_queue)
        if str(top) not in self.decks or not self._usable(top):
            if last["siblings"]:
                self._undo = self._undo[-19:] + [last]
                self.save()  # no collection write here: the answer stays undoable
            return False
        live = self.live_deck(top)
        deck = self.decks[str(live)]
        last.update(top=top, live=self.state["live"].get(str(top), top), pos=self._positions(top))
        cur_before = deck["current"]
        if cid:
            self.decks[str(top)]["touched"] = True
            if cid not in self.state["answered"] and any(
                    cid in b for n in self.nodes_of(top) for b in self.decks[str(n)]["batches"]):
                self.state["answered"].append(cid)
                last["answered"] = True
        changed = False
        if self._complete(live, deck["current"], self.hidden_now()):
            if prepared and prepared["top"] == top and prepared["cid"] == cid:
                self._set_positions(prepared["pos"])
                self.state["live"][str(top)] = prepared["live"]
                last["prepared"] = True
                new_live = prepared["live"]
                ext = prepared["ext"].get(str(new_live))
                if ext is not None and not self._extension_ok(new_live):
                    # the review limit cut the pool differently: redo it now (in
                    # every row the extension went to)
                    for k, n in prepared["ext"].items():
                        if k in self.decks:
                            del self.decks[k]["batches"][n:]
                    for k in prepared.get("new_nodes", []):
                        self.decks.pop(k, None)
                    self._set_positions(last["pos"])
                    self.state["live"][str(top)] = last["live"]
                    self.decks[str(new_live)]["finished"] = False
            changed = self._apply()  # a no-op write-wise when prepared correctly
        elif prepared:
            changed = self._apply()  # predicted wrongly: hide the next batch again
        else:
            self.save()
        if self.live_deck(top) == live and deck["current"] != cur_before and not deck["finished"]:
            # moved on to another batch of the same deck inside the reviewer: hold
            # it behind the pause screen (a display gate only; it is already shown)
            deck["pause"] = self.pause_enabled
            self.save()
        if cid:
            self._undo = self._undo[-19:] + [last]
        return changed

    def _live_node(self, deck_id: int) -> dict | None:
        top = self.top_level_id(deck_id)
        if str(top) not in self.decks or not self._usable(top):
            return None
        return self.decks[str(self.live_deck(top))]

    @_entry
    def pause_pending(self, deck_id: int) -> bool:
        deck = self._live_node(deck_id)
        return bool(self.enabled and self.pause_enabled and deck and deck.get("pause"))

    @_entry
    def clear_pause(self, deck_id: int | None = None) -> None:
        """Move on past the pause screen (deck_id's tree, or all)."""
        if deck_id is None:
            keys = list(self.decks)
        else:
            top = self.top_level_id(deck_id)
            keys = [str(n) for n in self.nodes_of(top)]
        changed = False
        for key in keys:
            if self.decks.get(key, {}).get("pause"):
                self.decks[key]["pause"] = False
                changed = True
        if changed:
            self.save()

    @_entry
    def advance_if_complete(self, deck_id: int) -> bool:
        """The reviewer is about to fetch a card: if the live batch got done
        some other way (bury/suspend from the reviewer, redo), move on now, as
        after an answer. Returns True if cards changed."""
        top = self.top_level_id(deck_id)
        if not self.enabled or str(top) not in self.decks or not self._usable(top) or self._day_stale():
            return False
        live = self.live_deck(top)
        deck = self.decks[str(live)]
        if deck["finished"] or not self._complete(live, deck["current"], self.hidden_now()):
            return False
        return self.after_answer(0, deck_id)

    def _siblings_buried_by(self, cid: int, queue_before: int | None) -> set[int]:
        """Siblings that answering `cid` buries in Anki (rslib maybe_bury_siblings /
        all_siblings_for_bury), counting cards we hide by their original queue."""
        row = self.col.db.first("select nid, did, odid from cards where id=?", cid)
        if row is None or not self.sibling_bury:
            return set()
        conf = self.col.decks.config_dict_for_deck_id(row[2] or row[1])
        order = {0: 3, 2: 2, 3: 1}.get(queue_before, 0)  # gather order: learn 0, daylearn 1, review 2, new 3
        queues = {q for q, on in ((0, conf["new"].get("bury")),
                                  (2, conf["rev"].get("bury") and order <= 2),
                                  (3, conf.get("buryInterdayLearning") and order <= 1)) if on}
        if not queues:
            return set()
        ledger = self.state["hidden"]
        out = set()
        for sid, queue in self.col.db.all("select id, queue from cards where nid=? and id!=?", row[0], cid):
            if queue == HIDDEN_QUEUE and str(sid) in ledger:
                queue = ledger[str(sid)][0]
            if queue in queues:
                out.add(sid)
        return out

    def _take_buried_siblings(self, cid: int, queue_before: int | None) -> dict:
        """Anki skips cards we hide when it buries siblings on an answer. Treat
        those as buried for today, as vanilla Anki would: drop them from the
        ledger (they stay in -2; Anki's day-rollover unbury releases them without
        marking them modified) and from the batches. Returns what undo needs."""
        hidden = self.hidden_now()
        taken = {}
        for sid in self._siblings_buried_by(cid, queue_before) & hidden:
            places = []
            for key, deck in self.decks.items():
                for bi, batch in enumerate(deck["batches"]):
                    if sid in batch:
                        places.append((key, bi, batch.index(sid)))
                        batch.remove(sid)
            taken[str(sid)] = {"ledger": self.state["hidden"].pop(str(sid)), "places": places}
        if taken:
            self.save()
        return taken

    def _return_siblings(self, taken: dict) -> None:
        """Undo of _take_buried_siblings (Anki's undo un-buries siblings too)."""
        for sid, info in taken.items():
            if self.col.db.scalar("select queue from cards where id=?", int(sid)) != HIDDEN_QUEUE:
                continue
            self.state["hidden"][sid] = info["ledger"]
            for key, bi, pos in sorted(info["places"], key=lambda p: p[2]):
                deck = self.decks.get(key)
                if deck is not None and bi < len(deck["batches"]):
                    deck["batches"][bi].insert(pos, int(sid))

    def _extension_ok(self, did: int) -> bool:
        """After a prepared answer that created new batches: does the scheduler
        give exactly the new current batch? (Read-only check.)"""
        deck = self.decks[str(did)]
        node = self.col.decks.find_deck_in_tree(self.col.sched.deck_due_tree(), did)
        visible = len(self._pending(deck["batches"][deck["current"]], self.hidden_now()))
        return node is not None and node.review_count == visible

    @_entry
    def after_undo(self) -> bool:
        """Called when an undo has finished, before the screens refresh. If it
        took back the last answer, put the batch bookkeeping back too (the live
        deck and the current batches, if that answer had moved on). Returns
        True if cards changed."""
        if not self.enabled or not self._undo:
            return False
        last = self._undo[-1]
        reps = self.col.db.scalar("select reps from cards where id=?", last["cid"])
        if reps is None or reps >= last["reps"]:
            return False  # something else was undone
        self._undo.pop()
        if last["answered"] and last["cid"] in self.state["answered"]:
            self.state["answered"].remove(last["cid"])
        self._return_siblings(last["siblings"])
        top = last["top"]
        if top is None or str(top) not in self.decks:  # answered outside the batches
            return self._apply()
        self._set_positions(last["pos"])
        self.state["live"][str(top)] = last["live"]
        self._clear_tree_pause(top)
        changed = self._apply()
        if last["prepared"] and not changed:
            # that answer was made with no backend queue (prepare_answer dropped
            # it), so the backend's undo does not requeue the card: drop the
            # queue that was built after the answer so the card comes back
            self._write("update cards set queue=queue where 0")
            changed = True
        return changed

    @staticmethod
    def finishes_today(state: Any, now: int, cutoff: int) -> bool:
        """Will a card answered into scheduler state `state` (a SchedulingState,
        the reviewer's chosen next state) be done for today?"""
        if compat.state_kind(state) != "normal":
            return False
        kind = compat.normal_kind(state.normal)
        if kind == "review":
            return True
        if kind == "learning":
            return now + state.normal.learning.scheduled_secs >= cutoff
        if kind == "relearning":
            return now + state.normal.relearning.learning.scheduled_secs >= cutoff
        return False

    @_entry
    def redos_not_shown_yet(self, deck_id: int) -> list[int]:
        """redos_waiting() that Anki itself will not show yet: due later than now
        plus the learn-ahead limit (config "collapseTime", rslib LearnAheadSecs,
        default 1200 s). Those are the ones Anki's Learn column does not count."""
        waiting = self.redos_waiting(deck_id) if self.early_redo() else []
        if not waiting:
            return []
        limit = int(time.time()) + int(self.col.get_config("collapseTime", 1200))
        return self.col.db.list(f"select id from cards where due>? and id in {_ids(waiting)}", limit)

    @_entry
    def early_redo(self) -> bool:
        """May a waiting redo be shown before Anki would show it? Only where
        the probe found the hand-built entry identical to Anki's own, for the
        collection's FSRS setting."""
        fsrs = bool(self.col.get_config("fsrs", False))
        return bool(self.redo_ok_fsrs if fsrs else self.redo_ok)

    @_entry
    def redo_to_show(self, current_deck_id: int) -> int | None:
        """The reviewer found nothing to show. If the current batch still has a
        (re)learning card due later today (beyond the learn-ahead window), return
        it so the reviewer can show it now. Nothing is written: the backend
        accepts answering any card of its intraday learning queue early."""
        if not self.early_redo():
            return None  # this Anki: the redo comes when Anki itself shows it
        waiting = self.redos_waiting(current_deck_id)
        return waiting[0] if waiting else None

    @_entry
    def redos_waiting(self, deck_id: int) -> list[int]:
        """Pending (re)learning cards of the live batch of deck_id's tree, in
        deck_id's own tree, due later today; earliest first. Read-only."""
        if not self.enabled or self._day_stale():
            return []
        top = self.top_level_id(deck_id)
        if str(top) not in self.decks or not self._usable(top):
            return []
        live = self.live_deck(top)
        deck = self.decks[str(live)]
        batch = deck["batches"][deck["current"]]
        if self._complete(live, deck["current"], self.hidden_now()):
            return []
        return self.col.db.list(
            f"select id from cards where queue=1 and odid=0 and due<? and id in {_ids(batch)} "
            f"and did in {_ids(self._tree(deck_id))} order by due",
            self.col.sched.day_cutoff,
        )

    @_entry
    def queued_redo(self, current_deck_id: int) -> Any:
        """redo_to_show() packed as the QueuedCards the reviewer expects, or None."""
        cid = self.redo_to_show(current_deck_id)
        if not cid:
            return None
        from anki.scheduler_pb2 import QueuedCards

        card = self.col._backend.get_card(cid)
        seed = ((((cid << 8) | (cid >> 56)) & (2**64 - 1)) + card.reps) & (2**64 - 1)  # Card::review_seed
        entry = compat.make_queued_card(
            card, QueuedCards.LEARNING, self.col._backend.get_scheduling_states(cid),
            self.col.decks.name(card.original_deck_id or card.deck_id), seed,
            compat.fsrs_context_values(self.col, card, self.redo_context))
        return QueuedCards(cards=[entry], learning_count=1)

    # UI state
    ##########################################################################

    def toggle_expanded(self, did: int) -> None:
        expanded = self.state["ui"]["expanded"]
        if did in expanded:
            expanded.remove(did)
        else:
            expanded.append(did)
        self.save()

    def toggle_minimized(self) -> None:
        self.state["ui"]["minimized"] = not self.state["ui"]["minimized"]
        self.save()
