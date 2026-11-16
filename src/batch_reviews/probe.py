"""Behavioural probe of the raw-database contract the add-on relies on, run on a
throwaway collection in a temp dir (never the user's collection). It drives the
real engine, so it tests exactly the code that will run. No aqt imports.

run_probe() -> {"ok": bool, "problems": [...], "sibling_bury": bool}
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
from typing import Any

from anki.collection import Collection

try:
    from . import compat
    from .engine import HIDDEN_QUEUE, BatchEngine
except ImportError:  # top-level import (headless tests)
    import compat  # type: ignore[no-redef]
    from engine import HIDDEN_QUEUE, BatchEngine  # type: ignore[no-redef]

MOD, USN = 1700000000, 3
PROBE_VERSION = 3  # bump when the probe checks more: cached verdicts are redone


def _open(path: str) -> Any:
    col = Collection(path)
    if compat.v3_problem(col) and callable(getattr(col, "set_v3_scheduler", None)):
        col.set_v3_scheduler(True)  # 2.1.58: a new collection starts on v2
    return col


def _seed(col: Any) -> tuple[int, int, list[int]]:
    model = col.models.by_name("Basic")
    did = col.decks.id("Probe")
    for i in range(7):
        note = col.new_note(model)
        note["Front"] = f"p{i}"
        col.add_note(note, did)
    sib_did = col.decks.id("ProbeSiblings")
    note = col.new_note(col.models.by_name("Basic (and reversed card)"))
    note["Front"], note["Back"] = "s", "t"
    col.add_note(note, sib_did)
    conf_id = col.decks.add_config_returning_id("probe-bury")
    conf = col.decks.get_config(conf_id)
    conf["rev"]["bury"] = True
    col.decks.update_config(conf)
    deck = col.decks.get(sib_did)
    deck["conf"] = conf_id
    col.decks.save(deck)
    col.db.execute("update cards set type=2, queue=2, ivl=10, due=?, factor=2500, mod=?, usn=?",
                   col.sched.today, MOD, USN)
    if hasattr(col.db, "modified_in_python"):
        col.save()
    return did, sib_did, col.db.list("select id from cards where did=? order by id", did)


def _seed_tree(col: Any) -> tuple[int, int, int, list[int]]:
    """ProbeTree::Sub (5 reviews) and ProbeTree::Other (2): a subdeck that
    becomes the live deck. Returns the ids and Sub's own session order."""
    model = col.models.by_name("Basic")
    top, sub, other = col.decks.id("ProbeTree"), col.decks.id("ProbeTree::Sub"), col.decks.id("ProbeTree::Other")
    for d, n in ((sub, 5), (other, 2)):
        for i in range(n):
            note = col.new_note(model)
            note["Front"] = f"t{d}{i}"
            col.add_note(note, d)
    ids = col.db.list(f"select id from cards where did in ({sub},{other})")
    col.db.execute(f"update cards set type=2, queue=2, ivl=10, due=?, factor=2500, mod=?, usn=? "
                   f"where id in ({','.join(map(str, ids))})", col.sched.today, MOD, USN)
    if hasattr(col.db, "modified_in_python"):
        col.save()
    col.decks.select(sub)
    order = [qc.card.id for qc in col.sched.get_queued_cards(fetch_limit=50).cards]
    return top, sub, other, order


def _answer(col: Any, eng: BatchEngine | None, ease: int = 3) -> int:
    from anki.cards import Card
    from anki.scheduler.v3 import CardAnswer

    top = col.sched.get_queued_cards().cards[0]
    states = compat.queued_states(top)
    card = Card(col, backend_card=top.card)
    card.start_timer()
    choice = (states.again, states.hard, states.good, states.easy)[ease - 1]
    if eng is not None:
        eng.prepare_answer(card.id, col.decks.get_current_id(),
                           eng.finishes_today(choice, int(time.time()), col.sched.day_cutoff))
    rating = {1: CardAnswer.AGAIN, 3: CardAnswer.GOOD}[ease]
    col.sched.answer_card(col.sched.build_answer(card=card, states=states, rating=rating))
    if eng is not None:
        eng.after_answer(card.id, col.decks.get_current_id())
    return card.id


def _probe_redo(tmp: str) -> dict:
    """Is the hand-built waiting redo exactly Anki's own queue entry here, with
    FSRS off and with FSRS on? Builds a redo in a throwaway collection, lets
    Anki itself show it (large learn-ahead) and compares context, states and
    card. Returns {"redo_ok", "redo_ok_fsrs", "redo_context", "redo_problem"}."""
    from anki.scheduler.v3 import CardAnswer

    out: dict = {"redo_ok": False, "redo_ok_fsrs": False, "redo_context": None, "redo_problem": None}
    for fsrs in (False, True):
        key = "redo_ok_fsrs" if fsrs else "redo_ok"
        if fsrs and "memory_state" not in compat.CARD_FIELDS:
            out[key] = None  # no FSRS in this Anki
            continue
        col = _open(os.path.join(tmp, f"redo{int(fsrs)}.anki2"))
        try:
            if fsrs:
                col.set_config("fsrs", True)
            did = col.decks.id("ProbeRedo")
            for i in range(3):
                note = col.new_note(col.models.by_name("Basic"))
                note["Front"] = f"r{i}"
                col.add_note(note, did)
            col.db.execute("update cards set type=2, queue=2, ivl=12, due=?, factor=2500", col.sched.today)
            conf = col.decks.config_dict_for_deck_id(did)
            conf["lapse"]["delays"] = [90.0]
            col.decks.update_config(conf)
            col.decks.select(did)
            for rating in (CardAnswer.GOOD, CardAnswer.AGAIN):  # the second becomes a redo
                cid = _answer_with(col, rating)
            card = col._backend.get_card(cid)
            states = col._backend.get_scheduling_states(cid)
            col.set_config("collapseTime", 6 * 3600)  # Anki itself shows the redo now
            col.db.execute("update cards set queue=queue where 0")  # (rebuild the queue; throwaway collection)
            theirs = next((q for q in col.sched.get_queued_cards(fetch_limit=50).cards if q.card.id == cid), None)
            if theirs is None:
                out["redo_problem"] = f"Anki did not queue the redo (fsrs={fsrs})"
                continue
            if not compat.states_match(compat.queued_states(theirs), states):
                out["redo_problem"] = f"scheduling states differ from Anki's own (fsrs={fsrs})"
                continue
            seed = ((((cid << 8) | (cid >> 56)) & (2**64 - 1)) + card.reps) & (2**64 - 1)
            for source in ("card", "computed"):
                c = col._backend.get_card(cid)
                ours = compat.make_queued_card(c, theirs.queue, states, col.decks.name(did), seed,
                                               compat.fsrs_context_values(col, c, source))
                if (not compat.QUEUED_HAS_CONTEXT or ours.context == theirs.context) and ours.card == theirs.card:
                    out[key] = True
                    if fsrs:
                        out["redo_context"] = source
                    break
            else:
                out["redo_problem"] = f"cannot build the scheduling context as Anki does (fsrs={fsrs})"
        finally:
            col.close()
    return out


def _answer_with(col: Any, rating: Any) -> int:
    from anki.cards import Card

    top = col.sched.get_queued_cards().cards[0]
    card = Card(col, backend_card=top.card)
    card.start_timer()
    col.sched.answer_card(col.sched.build_answer(card=card, states=compat.queued_states(top), rating=rating))
    return card.id


def source_hash(folder: str) -> str:
    """Short hash of the add-on's own .py files: a changed add-on with the same
    PROBE_VERSION never reuses a cached verdict."""
    import hashlib

    h = hashlib.sha1()
    for name in sorted(os.listdir(folder)):
        if name.endswith(".py"):
            with open(os.path.join(folder, name), "rb") as f:
                h.update(name.encode() + b"\0" + f.read())
    return h.hexdigest()[:12]


def run_probe() -> dict:
    problems: list[str] = []
    result = {"ok": False, "problems": problems, "sibling_bury": True}
    try:
        tmp = tempfile.mkdtemp(prefix="batch-reviews-probe-")
    except Exception as e:  # e.g. an unwritable temp folder: not this Anki's fault, so not cached
        problems.append(f"cannot create a temporary folder for the self-test ({e!r})")
        result["transient"] = True
        return result
    path = os.path.join(tmp, "probe.anki2")
    col = None
    try:
        col = _open(path)
        if compat.v3_problem(col):
            problems.append(f"v3 scheduler unavailable on a test collection: {compat.v3_problem(col)}")
            return result
        did, sib_did, cids = _seed(col)
        t_top, t_sub, t_other, sub_order = _seed_tree(col)
        rows = lambda: {r[0]: tuple(r[1:]) for r in col.db.all("select id, queue, mod, usn from cards")}
        cur_deck = lambda: col.db.first("select usn, mtime_secs, val from config where key='curDeck'")
        before_cfg = cur_deck()

        eng = BatchEngine(col, os.path.join(tmp, "state.json"), 3)
        eng.set_enabled(True)  # hide: batches [3,3,1], the rest of the deck buried
        hidden = eng.hidden_now()
        snap = rows()
        if len([c for c in cids if snap[c][0] == HIDDEN_QUEUE]) != 4:
            problems.append("hiding did not bury the expected cards")
        if any(snap[c][1:] != (MOD, USN) for c in cids):
            problems.append("hiding changed mod/usn")
        if cur_deck() != before_cfg:
            problems.append("the temporary current-deck switch changed the config row")
        col.decks.select(did)
        if col.sched.get_queued_cards(fetch_limit=50).review_count != 3:
            problems.append("hidden cards still counted by the scheduler")

        # the batch-completing answer must stay undoable (switch prepared before it)
        _answer(col, eng)
        _answer(col, eng)
        last = _answer(col, eng)
        if eng.decks[str(did)]["current"] != 1:
            problems.append("auto-continue did not move to the next batch")
        if not col.undo_status().undo:
            problems.append("the batch-completing answer is not undoable")
        else:
            col.undo()
            eng.after_undo()
            if col.db.scalar("select reps from cards where id=?", last) != 0 or \
                    col.sched.get_queued_cards().cards[0].card.id != last:
                problems.append("undo did not bring the card back first")
            if eng.decks[str(did)]["current"] != 0:
                problems.append("undo did not roll the batch switch back")

        # a subdeck opened to study: its own batch is the only one visible in
        # its tree, and Anki's deck tree shows that batch on the parent
        if [c for b in eng.decks.get(str(t_sub), {}).get("batches", []) for c in b] != sub_order:
            problems.append("a subdeck's batches differ from its own session order")
        else:
            col.decks.select(t_sub)
            eng.set_live(t_sub)
            snap = rows()
            tree_ids = col.db.list(f"select id from cards where did in ({t_sub},{t_other})")
            visible = {c for c in tree_ids if snap[c][0] == 2}
            if visible != set(sub_order[:3]):
                problems.append("opening a subdeck did not show exactly its own batch")
            if any(snap[c][1:] != (MOD, USN) for c in tree_ids):
                problems.append("switching the live deck changed mod/usn")
            tree = col.sched.deck_due_tree()
            counts = {d: col.decks.find_deck_in_tree(tree, d).review_count for d in (t_top, t_sub, t_other)}
            if counts != {t_top: 3, t_sub: 3, t_other: 0}:
                problems.append(f"deck tree counts with a subdeck live are not as expected ({list(counts.values())})")

        # OFF restores exactly
        eng.set_enabled(False)
        after = rows()
        untouched = [c for c in cids if c in hidden]
        if any(after[c] != (2, MOD, USN) for c in untouched):
            problems.append("restore did not give back queue/mod/usn exactly")
        if col.db.scalar(f"select count() from cards where queue={HIDDEN_QUEUE}"):
            problems.append("cards left buried after OFF")

        # siblings: does this Anki bury a review sibling when its card is answered?
        col.decks.select(sib_did)
        a = _answer(col, None)
        result["sibling_bury"] = col.db.scalar(
            "select queue from cards where did=? and id!=?", sib_did, a) == HIDDEN_QUEUE

        # Anki's own day-rollover unbury releases our cards without marking them modified
        eng.set_enabled(True)
        hidden = eng.hidden_now()
        col.db.execute("update col set crt = crt - 86400")
        col.close()
        col = Collection(path)
        col.sched.deck_due_tree()
        back = rows()
        if any(back[c][0] != 2 or back[c][1:] != (MOD, USN) for c in hidden):
            problems.append("the day-rollover unbury did not release hidden cards unmodified")
        col.close()
        col = None
        result.update(_probe_redo(tmp))  # not a hard failure: only the early redo depends on it
    except Exception as e:
        problems.append(f"probe failed: {e!r}")
    finally:
        try:
            if col is not None and getattr(col, "db", None) is not None:
                col.close()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)
    result["ok"] = not problems
    return result
