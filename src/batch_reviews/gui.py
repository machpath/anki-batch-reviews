"""Anki GUI glue for Batch Reviews: hooks, toolbar link and the two grids.

Every hook body runs through `_guarded`: an exception is logged to
user_files/batch_reviews.log, everything the add-on hid is restored and batching
is switched off (fail open)."""

from __future__ import annotations

import html
import json
import os
import re
import time
import traceback
from typing import Any, Callable

import aqt
from aqt import gui_hooks, mw
from aqt.reviewer import ReviewerBottomBar
from aqt.utils import tooltip

from . import compat, recover, safety
from .engine import COMPLETE, BatchEngine

ADDON_DIR = os.path.dirname(__file__)
MODULE = __name__.split(".")[0]
USER_FILES = os.path.join(ADDON_DIR, "user_files")
LOG_PATH = os.path.join(USER_FILES, "batch_reviews.log")
LINK_ID = "batch_reviews_toggle"
CMD = "batchrev:"

engine: BatchEngine | None = None
_last_browse: tuple = (None, None, 0.0)
closing = False


# Safety
##########################################################################


def _log(msg: str) -> None:
    try:
        if not os.path.isdir(ADDON_DIR):
            return  # the add-on was deleted while running: leave no litter behind
        os.makedirs(USER_FILES, exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1_000_000:
            os.replace(LOG_PATH, LOG_PATH + ".old")
        with open(LOG_PATH, "a", encoding="utf8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _ui(fn: Callable[[], Any]) -> None:
    """The threading rule for this add-on. Some entry points run on a worker
    thread (Anki installs AnkiWeb downloads and syncs in the background, and
    the add-on manager / collection wrappers run wherever they are called):
    collection restores and file writes stay synchronous on the calling thread,
    but every Qt / UI touch (web views, tooltips, menus, mw.reset) goes through
    here, which runs it now on the main thread and otherwise posts it to the
    main thread. Never raises into the caller."""
    import threading

    def safe() -> None:
        try:
            fn()
        except Exception:
            _log(f"UI update failed:\n{traceback.format_exc()}")

    try:
        if threading.current_thread() is threading.main_thread():
            safe()
        else:
            mw.taskman.run_on_main(safe)
    except Exception:
        _log(f"could not schedule a UI update:\n{traceback.format_exc()}")


def _show_tip(msg: str, period: int = 6000) -> None:
    _ui(lambda: tooltip(msg, period=period))


def _fail_open(where: str) -> None:
    _log(f"error in {where}:\n{traceback.format_exc()}")
    try:
        if engine is not None and mw.col is not None:
            engine.disable_after_error()
    except Exception:
        _log(f"restore after error failed:\n{traceback.format_exc()}")
    _update_toolbar_label()
    _show_tip("Batch Reviews ran into a problem and switched itself off; every card it had hidden is back. "
              "Details: Tools > Add-ons > Batch Reviews > View Files > user_files > batch_reviews.log", 10000)


def _guarded(fn: Callable) -> Callable:
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except Exception:
            _fail_open(fn.__name__)
            return None

    wrapper.__name__ = fn.__name__
    return wrapper


# set when Anki is about to replace ("install") or delete ("delete") this add-on's
# folder: everything was put back; nothing more is done until Anki restarts
_suspended: str | None = None


def _active() -> BatchEngine | None:
    if engine is None or closing or mw.col is None or unavailable or _suspended:
        return None
    engine.col = mw.col
    return engine


def _resume_if_close_cancelled() -> None:
    """profile_will_close fired but the close was cancelled (e.g. Add Cards ->
    "Keep editing"): the collection is still open, so take over again."""
    global closing
    if closing and engine is not None and mw.col is not None and not _suspended:
        closing = False
        engine.col = mw.col
        engine.ensure()
        _after_db_change()
        _update_toolbar_label()


def _after_db_change() -> None:
    # raw writes discard the backend's undo queue; keep the Edit menu honest
    _ui(mw.update_undo_actions)


def _batch_size() -> int:
    conf = mw.addonManager.getConfig(MODULE) or {}
    try:
        return max(1, int(conf.get("batch_size", 25)))
    except (TypeError, ValueError):
        return 25


def _state_path(col: Any = None) -> str:
    if os.path.isdir(ADDON_DIR):  # (never recreate the folder of a deleted add-on)
        os.makedirs(USER_FILES, exist_ok=True)
    return recover.state_path_for(USER_FILES, mw.pm.name, _log, col=col or mw.col,
                                  other_profiles=mw.pm.profiles())


# Lifecycle
##########################################################################


# Availability: static self-check at load, collection checks, behavioural probe
##########################################################################

unavailable: str | None = None  # reason batching cannot run here; nothing is written then
_logged_once: set = set()


def _log_once(key: str, msg: str) -> None:
    if key not in _logged_once:
        _logged_once.add(key)
        _log(msg)


_told: set = set()  # unavailable reasons already shown this session


def _set_unavailable(reason: str | None, by_probe: bool = False) -> None:
    global unavailable, _unavailable_by_probe
    unavailable = reason
    _unavailable_by_probe = bool(reason) and by_probe
    if reason and reason not in _told:  # once per session (and again only on a click)
        _told.add(reason)
        _log(f"unavailable: {reason}")
        _show_tip(_tip())
    _update_toolbar_label()


def _notify(msg: str) -> None:
    """A one-off message (e.g. hidden cards that cannot be identified)."""
    _log(msg)
    _show_tip(msg, 12000)


def _collection_problem(col) -> str | None:
    problem = compat.v3_problem(col)
    if problem:
        return problem
    for name in ("get_card", "get_scheduling_states"):
        if not callable(getattr(col._backend, name, None)):
            return f"this Anki's backend has no {name}"
    if not callable(getattr(col.decks, "find_deck_in_tree", None)):
        return "this Anki has no decks.find_deck_in_tree"
    return None


PROBE_CACHE = os.path.join(USER_FILES, "probe-cache.json")


_SOURCE_HASH: str | None = None
_unavailable_by_probe = False
_force_probe = False  # the user clicked while unavailable by the probe: run it again
_last_probe_run = 0.0
PROBE_RECHECK_SECONDS = 30  # a click re-runs the probe at most this often


def _build_key() -> str:
    """Anki build + probe version + a hash of this add-on's own source, so a
    changed add-on never reuses a cached verdict."""
    global _SOURCE_HASH
    from anki.buildinfo import buildhash, version

    from .probe import PROBE_VERSION, source_hash

    if _SOURCE_HASH is None:
        _SOURCE_HASH = source_hash(ADDON_DIR)
    return f"{version}+{buildhash}+probe{PROBE_VERSION}+{_SOURCE_HASH}"


def _probe_verdict(eng: BatchEngine) -> str | None:
    """Run the throwaway-collection probe once per Anki build (cached in
    user_files) and apply what it found. Returns a problem or None."""
    from .probe import run_probe

    global _force_probe, _last_probe_run
    try:
        with open(PROBE_CACHE, encoding="utf8") as f:
            cache = json.load(f)
    except (OSError, ValueError):
        cache = {}
    if not isinstance(cache, dict):  # valid JSON but not ours: start over
        cache = {}
    key = _build_key()
    verdict = None if _force_probe else cache.get(key)
    _force_probe = False
    if not isinstance(verdict, dict) or "ok" not in verdict:
        verdict = run_probe()
        _last_probe_run = time.time()
        _log(f"probe on Anki {key}: {verdict}")
        if verdict.get("transient"):  # (e.g. no temp folder): try again next time
            cache.pop(key, None)
        else:
            cache[key] = verdict
        try:
            os.makedirs(USER_FILES, exist_ok=True)
            with open(PROBE_CACHE + ".tmp", "w", encoding="utf8") as f:
                json.dump(cache, f)
            os.replace(PROBE_CACHE + ".tmp", PROBE_CACHE)
        except OSError:
            pass
    eng.sibling_bury = bool(verdict.get("sibling_bury", True))
    eng.redo_ok = bool(verdict.get("redo_ok", False))
    eng.redo_ok_fsrs = bool(verdict.get("redo_ok_fsrs", False))
    eng.redo_context = verdict.get("redo_context") or "card"
    if verdict.get("redo_problem"):
        _log_once("redo", f"waiting redos are not shown early on this Anki in some cases: {verdict['redo_problem']}")
    if not verdict.get("ok"):
        return "self-test failed on this Anki version: " + "; ".join(str(p) for p in (verdict.get("problems") or ["unknown"]))
    return None


@_guarded
def on_collection_did_load(col) -> None:
    global engine, closing
    closing = False
    engine = None
    if _suspended:  # (replaced or deleted this session: nothing until Anki restarts)
        _update_toolbar_label()
        return
    _set_unavailable(_collection_problem(col))
    if unavailable:
        # nothing else read or written: only cards an earlier run left hidden
        # (crash, earlier build) are put back, by the ledger's guarded rule
        recover.recover_file(col, _state_path(col), _log, unavailable, notify=_notify)
        return
    engine = BatchEngine(col, _state_path(col), _batch_size(), log=_log)
    if engine.notice:
        _notify(engine.notice)
        engine.notice = None
    if engine.enabled:
        _set_unavailable(_probe_verdict(engine), by_probe=True)
        if unavailable:
            engine = None
            recover.recover_file(col, _state_path(col), _log, unavailable, notify=_notify)
            return
    # run our answer filter after every other add-on's, so the ease we see is final
    gui_hooks.reviewer_will_answer_card.remove(on_reviewer_will_answer_card)
    gui_hooks.reviewer_will_answer_card.append(on_reviewer_will_answer_card)
    global _paused_view
    _paused_view = False
    _patch_reviewer_and_overview()
    engine.pause_enabled = _pause_config()
    engine.clear_pause()  # closed while paused: the next batch simply starts
    engine.restore_all()  # crash recovery: anything left hidden by a previous run
    engine.ensure()
    _after_db_change()
    _update_toolbar_label()


def _suspend(why: str) -> None:
    """Put back every hidden card and save (state + ledger file) while the folder
    still exists, then do nothing more this session. The state still says ON,
    so after the restart the new (or no) add-on takes it from there."""
    global _suspended
    if _suspended:
        return
    try:
        if engine is not None and mw.col is not None and not unavailable:
            engine.col = mw.col
            engine.restore_all()
            engine.clear_pause()
            engine.save()
    except Exception:
        _log(f"restore before {why} failed:\n{traceback.format_exc()}")
    _suspended = why
    _log(f"suspended until Anki restarts: add-on {why}")

    def ui() -> None:
        _update_toolbar_label()
        if mw.state == "review":
            mw.reset()  # the reviewer goes on without batches

    _ui(ui)  # (an AnkiWeb update calls this on a background thread)


def on_addon_manager_will_install_addon(manager: Any, module: str) -> None:
    try:
        if module == MODULE:  # this add-on is being replaced (an update)
            _suspend("install")
    except Exception:
        _log(f"before install:\n{traceback.format_exc()}")  # never into Anki's installer


def _watch_disable() -> None:
    """Anki has no hook for "disable add-on" (it takes effect at the restart):
    wrap the add-on manager's toggleEnabled (chaining-safe, on the instance) so
    that disabling this add-on puts everything back right away, as a delete does.
    Then nothing depends on this add-on ever loading again."""
    am = mw.addonManager

    def make(inner: Callable) -> Callable:
        def toggleEnabled(module: str, enable: Any = None) -> Any:
            out = inner(module, enable)
            try:
                if module == MODULE and not am.isEnabled(MODULE):
                    _suspend("disable")
            except Exception:
                _log(f"after disabling:\n{traceback.format_exc()}")
            return out

        return toggleEnabled

    if compat.has_params(getattr(am, "toggleEnabled", None), "module", "enable") and callable(getattr(am, "isEnabled", None)):
        _wrap(am, "toggleEnabled", make)
    else:
        _log("degraded on this Anki: disabling the add-on is noticed only at the next close")


def on_addons_dialog_will_delete_addons(dialog: Any, ids: list[str]) -> None:
    try:
        if MODULE in (ids or []):  # this add-on is being deleted
            _suspend("delete")
    except Exception:
        _log(f"before delete:\n{traceback.format_exc()}")


@_guarded
def on_profile_will_close() -> None:
    global closing
    closing = True  # the engine is kept: the close can still be cancelled
    if engine is not None and mw.col is not None and not unavailable and not _suspended:
        engine.col = mw.col
        engine.restore_all()


@_guarded
def on_sync_will_start() -> None:
    if eng := _active():
        eng.restore_all()


@_guarded
def on_sync_did_finish() -> None:
    # mw.reset() runs right after this hook and redraws the current screen
    if eng := _active():
        eng.after_sync()  # rebuilds decks not yet studied here today
        _after_db_change()


@_guarded
def on_will_temporarily_close(*_args: Any) -> None:
    # colpkg export closes the collection: export it without anything hidden
    if eng := _active():
        eng.restore_all()


@_guarded
def on_did_temporarily_close(*_args: Any) -> None:
    if eng := _active():
        eng.ensure()
        _after_db_change()


def on_exporter_will_export(options: Any, exporter: Any) -> Any:
    # .apkg/.colpkg exports with scheduling must not carry our hidden state
    on_will_temporarily_close()
    return options


@_guarded
def on_day_did_change() -> None:
    if eng := _active():
        if eng.ensure():
            _after_db_change()
        mw.reset()


@_guarded
def on_config_updated(conf: dict) -> None:
    if engine is not None:
        try:
            engine.batch_size = max(1, int(conf.get("batch_size", 25)))
        except (TypeError, ValueError):
            pass
        engine.pause_enabled = bool(conf.get("pause_between_batches", True))
        if not engine.pause_enabled and _paused_view and mw.state == "review":
            engine.clear_pause()
            mw.reviewer.nextCard()  # switched off while the pause screen shows


def _pause_config() -> bool:
    return bool((mw.addonManager.getConfig(MODULE) or {}).get("pause_between_batches", True))


# Study flow
##########################################################################


@_guarded
def on_state_will_change(new_state: str, old_state: str) -> None:
    global _paused_view
    _resume_if_close_cancelled()
    if old_state == "review" and new_state != "review":
        _paused_view = False  # left the reviewer (also via timebox "Finish"):
        if engine is not None:  # the next batch starts directly next time
            engine.clear_pause()
    if new_state in ("overview", "review") and engine is not None and not unavailable:
        _patch_reviewer_and_overview()  # still ours? (another add-on may have replaced it)
    eng = _active()
    if eng is None or new_state not in ("deckBrowser", "overview", "review"):
        return
    changed = False
    did = mw.col.decks.get_current_id()
    if new_state in ("overview", "review"):
        # the deck opened to study: its own current batch is the visible one
        # (no-op if it already is, has no batches today, or is a filtered deck)
        changed = eng.set_live(did)
    changed = eng.ensure() or changed
    if new_state == "review":
        changed = eng.recheck_finished(did) or changed
    if changed:
        _after_db_change()


def on_reviewer_will_answer_card(ease_tuple, reviewer, card):
    # Filter run in Reviewer._answerCard() before the answer op starts. If this
    # answer completes the batch, the next batch is un-hidden now, so nothing is
    # written after the answer and Ctrl+Z can still undo it. Always returns the
    # tuple unchanged (registered last, so the ease is final).
    try:
        proceed, ease = ease_tuple
        eng = _active()
        if proceed and eng is not None:
            try:
                states = reviewer._v3.states
                state = (states.again, states.hard, states.good, states.easy)[ease - 1]
                finishes = eng.finishes_today(state, int(time.time()), mw.col.sched.day_cutoff)
            except Exception:
                finishes = ease > 1 and card.type == 2  # review card not failed
            if eng.prepare_answer(card.id, mw.col.decks.get_current_id(), finishes):
                _after_db_change()
    except Exception:
        _fail_open("on_reviewer_will_answer_card")
    return ease_tuple


@_guarded
def on_state_did_undo(changes) -> None:
    # Fires in the undo op's success callback, before operation_did_execute makes
    # the reviewer fetch its next card: the undone card's batch is current again.
    if eng := _active():
        if eng.after_undo():
            _after_db_change()


@_guarded
def on_reviewer_did_answer_card(reviewer, card, ease) -> None:
    # Runs in Reviewer._after_answering() before nextCard(), so a batch switch
    # is in place before the reviewer looks for a card.
    if eng := _active():
        if eng.after_answer(card.id, mw.col.decks.get_current_id()):
            _after_db_change()


@_guarded
def on_state_did_change(new_state: str, old_state: str) -> None:
    # Safety net for the reviewer running dry without an answer (e.g. the last
    # card of a batch was buried/suspended from the review screen).
    eng = _active()
    if eng is None or new_state != "overview" or old_state != "review":
        return
    if mw.col.sched.get_queued_cards().cards:
        return  # user left with cards remaining
    if eng.after_answer(0, mw.col.decks.get_current_id()):
        _after_db_change()
        if mw.col.sched.get_queued_cards().cards:
            mw.progress.single_shot(0, lambda: mw.state == "overview" and mw.moveToState("review"))


# Reviewer redo and overview Unbury (instance patches on Anki 26.09.4 objects)
##########################################################################

_reapply_after_unbury = False


def _wrap(obj: Any, name: str, make: Callable[[Callable], Callable]) -> None:
    """Install make(inner) as obj.<name>, where inner is whatever is there when
    it is called: an instance attribute set by another add-on before us, or else
    the class's method looked up at call time (so class patches keep working).
    Re-run at screen changes: if another add-on replaced our instance attribute
    without chaining to it, we wrap theirs."""
    cur = obj.__dict__.get(name)
    if getattr(cur, "_brb", False):
        return

    def inner(*args: Any, **kwargs: Any) -> Any:
        if cur is not None:
            return cur(*args, **kwargs)
        return getattr(type(obj), name)(obj, *args, **kwargs)

    wrapper = make(inner)
    wrapper._brb = True
    setattr(obj, name, wrapper)


def _patch_reviewer_and_overview() -> None:
    r, ov = mw.reviewer, mw.overview

    def next_card(inner: Callable) -> Callable:
        def get_next_v3_card() -> Any:
            out = inner()
            if r.card is None:
                _show_waiting_redo(r)
            return out

        return get_next_v3_card

    def unbury(inner: Callable) -> Callable:
        return lambda: _unbury_safely(inner)

    def finished(inner: Callable) -> Callable:
        return lambda: _overview_instead_of_congrats(inner)

    _wrap(r, "_get_next_v3_card", next_card)
    _wrap(r, "nextCard", lambda inner: lambda: _next_card(r, inner))
    _wrap(r, "refresh_if_needed", lambda inner: lambda: _refresh_if_needed(r, inner))
    # the "u" shortcut, the bottom-bar button and the congrats page link all
    # call self.on_unbury
    _wrap(ov, "on_unbury", unbury)
    ov.onUnbury = ov.on_unbury
    _wrap(ov, "_show_finished_screen", finished)


def _overview_instead_of_congrats(show_congrats: Callable) -> Any:
    """Anki thinks the deck is finished, but a redo of the current batch is only
    waiting for its relearning step: show the normal overview (Study Now brings
    the redo up) with a note, instead of the congratulations page."""
    try:
        eng = _active()
        waiting = eng.redos_waiting(mw.col.decks.get_current_id()) if eng and eng.early_redo() else []
        if not waiting:
            return show_congrats()
        from aqt.overview import OverviewContent

        ov = mw.overview
        deck = mw.col.decks.current()
        note = (f'<div class="descfont descmid" style="margin-top:.5em">Batch Reviews: '
                f'{len(waiting)} card{"s" if len(waiting) > 1 else ""} of the current batch to redo '
                f'(Anki would wait for the relearning step). Study Now shows {"them" if len(waiting) > 1 else "it"}.</div>')
        content = OverviewContent(deck=deck["name"], shareLink="", desc=ov._desc(deck) + note, table=ov._table())
        gui_hooks.overview_will_render_content(ov, content)
        content.deck = html.escape(content.deck)
        ov.web.stdHtml(ov._body % content.__dict__, css=["css/overview.css"],
                       js=["js/vendor/jquery.min.js"], context=ov)
        return None
    except Exception:
        _fail_open("_overview_instead_of_congrats")
        return show_congrats()


@_guarded
def _show_waiting_redo(r) -> None:
    """The backend queue is empty but a redo of the current batch waits beyond the
    learn-ahead window: show it now, without writing anything (the answer that
    made it a redo stays undoable)."""
    from anki.cards import Card
    from aqt.reviewer import V3CardInfo

    eng = _active()
    queued = eng.queued_redo(mw.col.decks.get_current_id()) if eng else None
    if queued is not None:
        r._v3 = V3CardInfo.from_queue(queued)
        r.card = Card(mw.col, backend_card=r._v3.top_card().card)
        r.card.start_timer()


def _unbury_safely(orig_unbury: Callable) -> Any:
    """Anki's unbury_deck would unbury (and mark modified) the cards we hide. Put
    ours back first (raw, no mod change), let Anki unbury what is still buried
    (the user's own and sibling burials), then hide ours again."""
    global _reapply_after_unbury
    try:
        eng = _active()
        if eng is not None:
            eng.restore_all()
            _after_db_change()
            if not mw.col.sched.have_buried():
                eng.ensure()
                _after_db_change()
                mw.overview.refresh()
                return
            _reapply_after_unbury = True
        out = orig_unbury()
        if _reapply_after_unbury and not mw._background_op_count:
            on_unbury_op_done()  # the user cancelled Anki's choice dialog
        return out
    except Exception:
        _fail_open("_unbury_safely")
        return None


@_guarded
def on_unbury_op_done(*_args: Any) -> None:
    global _reapply_after_unbury
    if not _reapply_after_unbury:
        return
    _reapply_after_unbury = False
    if eng := _active():
        if eng.ensure():
            _after_db_change()
        if mw.state == "overview":
            mw.overview.refresh()


def on_overview_will_render_bottom(link_handler, links):
    """Hide Anki's Unbury button when the only buried cards are ours."""
    try:
        eng = _active()
        if eng is not None and eng.enabled:
            ours = eng.hidden_now()
            dids = eng._tree(mw.col.decks.get_current_id())
            others = mw.col.db.list(
                f"select id from cards where queue in (-2,-3) and did in ({','.join(map(str, dids))})"
            )
            if not set(others) - ours:
                links[:] = [link for link in links if link[1] != "unbury"]
    except Exception:
        _fail_open("on_overview_will_render_bottom")
    return link_handler


# Pause screen between batches
##########################################################################
# A display gate only: the next batch was already un-hidden before the answer
# (prepare_answer), so neither showing the pause nor moving on writes anything,
# and Ctrl+Z still undoes the batch-completing answer.

_paused_view = False

PAUSE_TEXT = "Batch is done. Press Space or Enter to move to the next batch."


def _next_card(r, inner: Callable) -> Any:
    try:
        eng = _active()
        if eng is not None:
            did = mw.col.decks.get_current_id()
            if eng.advance_if_complete(did):  # done by bury/suspend/redo etc.
                _after_db_change()
            if eng.pause_pending(did):
                if getattr(r, "_reps", None) is None:  # set in show() on older Anki
                    eng.clear_pause(did)  # (re)entering review: start directly
                else:
                    _show_pause(r)
                    return
    except Exception:
        _fail_open("nextCard")
    if _paused_view:
        _leave_pause_view(r)
    return inner()


def _refresh_if_needed(r, inner: Callable) -> Any:
    from aqt.reviewer import RefreshNeeded

    if _paused_view and r._refresh_needed in (RefreshNeeded.NOTE_TEXT, RefreshNeeded.FLAG):
        r._refresh_needed = None  # no card on screen to redraw
        return None
    return inner()


def _show_pause(r) -> None:
    global _paused_view
    from aqt.qt import Qt
    from aqt.sound import av_player

    if r.card is not None:
        r.previous_card = r.card
    r.card, r._v3, r.state = None, None, None  # not "question"/"answer": nothing acts on it
    if hasattr(r, "_clear_auto_advance_timers"):  # no auto-advance before 23.12
        r._clear_auto_advance_timers()
    av_player.stop_and_clear_queue()
    # only Space/Enter (and undo) while paused; set directly, so no add-on shortcut
    # that acts on mw.reviewer.card is active. applyShortcuts() turns auto-repeat
    # off, here and for the reviewer's own Space afterwards, so a held key
    # cannot also reveal the answer.
    mw.clearStateShortcuts()
    mw.stateShortcuts = mw.applyShortcuts(
        [(Qt.Key.Key_Space, _move_on_from_pause), (Qt.Key.Key_Return, _move_on_from_pause),
         (Qt.Key.Key_Enter, _move_on_from_pause), ("u", mw.undo)]
    )
    msg = (f'<div id="brb-pause" onclick="pycmd(\'{CMD}next\')" style="cursor:pointer;margin-top:30vh;'
           f'text-align:center;font-size:1.4em">{PAUSE_TEXT}</div>')
    r.web.eval(
        f"(function(){{var q=document.getElementById('qa');if(q){{q.innerHTML={json.dumps(msg)};q.style.opacity=1;}}"
        "['_flag','_mark'].forEach(function(i){var e=document.getElementById(i);if(e)e.hidden=true;});})();"
    )
    button = (f'<table cellpadding=0><tr><td class=stat2 align=center><button id="brb-next" '
              f'onclick="pycmd(\'{CMD}next\')">Next batch</button></td></tr></table>')
    r.bottom.web.eval(
        "document.querySelectorAll('td.stat button').forEach(function(b){b.style.visibility='hidden';});"
        f"if(window.showAnswer)showAnswer({json.dumps(button)}, true);"
    )
    _paused_view = True
    on_reviewer_did_show(None)  # grid: finished batch dark blue, ring on the next


def _leave_pause_view(r) -> None:
    global _paused_view
    _paused_view = False
    if mw.state == "review":
        mw.clearStateShortcuts()
        mw.setStateShortcuts(r._shortcutKeys())
        r.bottom.web.eval(
            "document.querySelectorAll('td.stat button').forEach(function(b){b.style.visibility='';});"
        )


@_guarded
def _move_on_from_pause() -> None:
    if not _paused_view or mw.state != "review":
        return
    if eng := _active():
        eng.clear_pause(mw.col.decks.get_current_id())
    mw.reviewer.nextCard()


# Toolbar link
##########################################################################


def _label() -> str:
    if _suspended:
        return "Batch: restart Anki"
    if STATIC_PROBLEM or unavailable:
        return "Batch: unavailable"
    return "Batch: ON" if engine is not None and engine.enabled else "Batch: OFF"


def _tip() -> str:
    if _suspended:
        # true whether or not Anki's operation then succeeded (it may still fail
        # after this point, e.g. a locked folder), and after re-enabling
        return ("Batch Reviews paused itself because Anki was updating, removing or disabling it; "
                "every card it had hidden is back. Restart Anki to continue.")
    reason = STATIC_PROBLEM or unavailable
    if not reason:
        return "Batch Reviews: click to turn batches on or off"
    if STATIC_PROBLEM:
        return f"Batch Reviews is unavailable on this version of Anki ({reason})."
    again = " Click to check again." if (unavailable and _unavailable_by_probe) else ""
    return f"Batch Reviews is unavailable: {reason}.{again}"


def _update_toolbar_label() -> None:
    def update() -> None:
        js = (f"(function(){{var e=document.getElementById({json.dumps(LINK_ID)});"
              f"if(e){{e.textContent={json.dumps(_label())};e.title={json.dumps(_tip())};}}}})();")
        mw.toolbar.web.eval(js)

    _ui(update)


@_guarded
def on_toggle() -> None:
    global _force_probe
    if _suspended:
        _show_tip(_tip())
        return
    _resume_if_close_cancelled()
    rechecked = False
    if unavailable and _unavailable_by_probe and time.time() - _last_probe_run >= PROBE_RECHECK_SECONDS:
        _force_probe = True  # the click re-runs the self-test (not more often than every 30 s)
    if mw.col is not None and (unavailable or engine is None):
        on_collection_did_load(mw.col)  # re-check: e.g. the v3 scheduler was switched on
        rechecked = True
    if unavailable:
        _show_tip(_tip())
        return
    eng = _active()
    if eng is None:
        return
    if rechecked and eng.enabled:
        # it was on before it became unavailable: the re-check resumed it, and
        # the click that made it available must not switch it off again
        _update_toolbar_label()
        mw.reset()
        return
    if not eng.enabled:
        _set_unavailable(_probe_verdict(eng), by_probe=True)  # first time ON under this Anki build
        if unavailable:
            recover.recover_file(mw.col, _state_path(), _log, unavailable, notify=_notify)
            return
    eng.batch_size = _batch_size()
    eng.set_enabled(not eng.enabled)
    _after_db_change()
    _update_toolbar_label()
    mw.reset()


def on_top_toolbar_did_init_links(links: list[str], toolbar) -> None:
    try:
        links.append(
            toolbar.create_link(
                "batch_reviews_toggle", _label(), _on_unavailable_click if STATIC_PROBLEM else on_toggle,
                tip=_tip(), id=LINK_ID,
            )
        )
    except Exception:
        _log(f"toolbar link failed:\n{traceback.format_exc()}")


# Grids
##########################################################################

from .render import CSS, JS, PATCH_JS, _boxes, _waiting_text, deck_grid_html  # noqa: E402


@_guarded
def on_deck_browser_will_render_content(deck_browser, content) -> None:
    eng = _active()
    if eng is None:
        return
    grid = deck_grid_html(eng)
    if not grid:
        return
    stats = content.stats
    start = stats.find('id="studiedToday"')
    end = stats.find("</div>", start) if start >= 0 else -1
    if end >= 0:
        end += len("</div>")
        content.stats = stats[:end] + grid + stats[end:]
    else:  # markup anchor gone: don't draw the grid rather than guess a place
        _log_once("deck-anchor", "deck list: no #studiedToday anchor, grid not drawn")


def reviewer_grid_html(eng: BatchEngine) -> str:
    """The row of the deck being studied, whatever its level."""
    rows = eng.grid(only_did=mw.col.decks.get_current_id())
    if not rows:
        return ""
    return _boxes(rows[0], "switch")  # bottom bar: switches right away


@_guarded
def on_webview_will_set_content(web_content, context) -> None:
    if not isinstance(context, ReviewerBottomBar):
        return
    eng = _active()
    inner = reviewer_grid_html(eng) if eng else ""
    if "<center id=outer>" not in web_content.body:  # markup anchor gone: no grid
        _log_once("bottom-anchor", "reviewer bottom bar: no <center id=outer> anchor, grid not drawn")
        return
    web_content.head += CSS
    web_content.body = web_content.body.replace(
        "<center id=outer>", f'<center id=outer><div id="brb-rev">{inner}</div>', 1
    )


@_guarded
def on_reviewer_did_show(card) -> None:
    eng = _active()
    inner = reviewer_grid_html(eng) if eng else ""
    web = mw.reviewer.bottom.web
    web.eval(
        PATCH_JS
        + f"(function(){{var e=document.getElementById('brb-rev');if(e&&!brbPatch(e,{json.dumps(inner)}))e.innerHTML={json.dumps(inner)};}})();"
    )
    web.adjustHeightToFit()


def on_js_message(handled: tuple[bool, Any], message: str, context: Any) -> tuple[bool, Any]:
    if not message.startswith(CMD):
        if handled[0]:
            return handled  # another add-on already took it
        if _paused_view and isinstance(context, (aqt.reviewer.Reviewer, ReviewerBottomBar)) \
                and message not in ("statesMutated", "updateToolbar", "repaintNeeded"):
            return (True, None)  # pause screen: no card to answer, edit, play...
        return handled
    _handle_cmd(message[len(CMD):])
    return (True, None)


@_guarded
def _handle_cmd(cmd: str) -> None:
    eng = _active()
    if eng is None:
        return
    parts = cmd.split(":")
    if parts[0] == "min":
        eng.toggle_minimized()
    elif parts[0] == "exp":  # deck list: a deck's name shows/hides its children
        eng.toggle_expanded(int(parts[1]))
    elif parts[0] == "next":  # pause screen: Enter / button / click
        _move_on_from_pause()
    elif parts[0] == "reload":  # in-place update not possible: plain refresh
        mw.deckBrowser.refresh()
    elif parts[0] == "dbl":  # double-click: make sure it is current, then browse
        did, idx = int(parts[1]), int(parts[2])
        deck = eng.decks.get(str(did))
        if deck is None or not 0 <= idx < len(deck["batches"]):
            return
        if deck["current"] != idx and eng.batch_status(did, idx)[0] != COMPLETE:
            _handle_cmd(("select" if mw.state == "deckBrowser" else "switch") + f":{did}:{idx}")
        _browse_batch(eng, did, idx)
    elif parts[0] == "browse":
        _browse_batch(eng, int(parts[1]), int(parts[2]))
    elif parts[0] == "select":  # deck list: make it current, stay on the deck list
        did, idx = int(parts[1]), int(parts[2])
        deck = eng.decks.get(str(did))
        if deck is not None and deck["current"] == idx:
            # already current: the second click of a double-click that arrived
            # before the grid was updated, or a click on the current box
            _browse_batch(eng, did, idx)
            return
        if deck is None or not eng.select_batch(did, idx):
            return
        _after_db_change()
        if mw.state == "deckBrowser":
            _refresh_deck_list_in_place(eng)
    elif parts[0] == "switch":  # reviewer bottom bar: switch and continue right away
        did, idx = int(parts[1]), int(parts[2])
        deck = eng.decks.get(str(did))
        if deck is not None and deck["current"] == idx:
            _browse_batch(eng, did, idx)  # current batch's box: browse, review stays
            return
        if not eng.select_batch(did, idx):
            return
        _after_db_change()
        if mw.state == "review":
            mw.reviewer.nextCard()


def _refresh_deck_list_in_place(eng: BatchEngine) -> None:
    """Update the deck rows (Due column) and the grid without reloading the page,
    so the scroll position stays where it is. DeckBrowser.refresh() would reload
    the page and jump to the top (it only restores the offset for reuse=True,
    which re-uses the old counts). Other add-ons' deck-list hooks still run."""
    from aqt.deckbrowser import DeckBrowserContent

    db = mw.deckBrowser
    tree = mw.col.sched.deck_due_tree()
    if hasattr(db, "_render_data"):
        db._render_data.tree = tree
    else:  # Anki <= 23.10 keeps the tree in _dueTree
        db._dueTree = tree
    content = DeckBrowserContent(tree=db._renderDeckTree(tree), stats=db._renderStats())
    gui_hooks.deck_browser_will_render_content(db, content)
    js = (
        "(function(t,g){var tb=document.querySelector('center > table');"
        "var old=document.getElementById('brb-grid');"
        "if(!tb||!old){pycmd('batchrev:reload');return;}"
        "tb.innerHTML=t; if(!brbPatch(old,g)) old.outerHTML=g; if(window.init) init();})(%s,%s);"
        % (json.dumps(content.tree), json.dumps(deck_grid_html(eng)))
    )
    db.web.eval(PATCH_JS + js)


def _browser_open() -> bool:
    return aqt.dialogs._dialogs.get("Browser", [None, None])[1] is not None


def _browse_batch(eng: BatchEngine, did: int, idx: int) -> None:
    """Open the Browser on exactly the cards of one batch. Read-only: no state
    or collection change, the reviewer stays where it is behind the window.
    (In the Browser's notes mode, the notes of those cards are listed.)"""
    global _last_browse
    deck = eng.decks.get(str(did))
    if deck is None or not 0 <= idx < len(deck["batches"]):
        return
    if _last_browse[:2] == (did, idx) and time.time() - _last_browse[2] < 1.5 and _browser_open():
        return  # same batch just opened by the other half of a double-click
    _last_browse = (did, idx, time.time())
    cids = ",".join(str(c) for c in deck["batches"][idx])
    aqt.dialogs.open("Browser", mw, search=(f"cid:{cids}",))


# Registration
##########################################################################


STATIC_PROBLEM: str | None = None  # set by setup() when this Anki lacks what we need


def _on_unavailable_click() -> None:
    _show_tip(_tip())


def _static_failure(reason: str) -> None:
    """Unavailable for the whole session: only the toolbar link that says why
    and the recovery of cards an earlier run left hidden; never a collection
    write besides that."""
    global STATIC_PROBLEM
    STATIC_PROBLEM = reason
    _log(f"not available on this Anki: {STATIC_PROBLEM}")
    try:  # still put back anything an earlier run left hidden
        recover.install_fallback(ADDON_DIR, _log, "unavailable: " + STATIC_PROBLEM)
    except Exception:
        _log(f"could not install recovery:\n{traceback.format_exc()}")
    try:
        hook = getattr(gui_hooks, "top_toolbar_did_init_links", None)
        if hook is not None and compat.params(type(hook).__call__) == ["links", "top_toolbar"]:
            hook.append(on_top_toolbar_did_init_links)
    except Exception:
        _log(f"no toolbar link:\n{traceback.format_exc()}")
    try:
        from aqt.qt import QTimer

        QTimer.singleShot(4000, _on_unavailable_click)  # the one message
    except Exception:
        _log(f"could not show the unavailable notice:\n{traceback.format_exc()}")


def setup() -> None:
    """Self-check first; then register every hook, or (if anything is missing or
    changed, or a registration fails partway) only the toolbar link that says
    why, and never touch a collection except to put back what we hid."""
    try:
        problems, degraded = compat.gui_problems(mw)
    except Exception as e:
        problems, degraded = [f"self-check failed: {e!r}"], {}
    if problems:
        _static_failure("; ".join(problems))
        return
    for name, why in degraded.items():
        _log(f"degraded on this Anki: {name}: {why}")
    try:
        _register_hooks()
    except Exception as e:
        _log(f"hook registration failed, everything already registered was removed:\n{traceback.format_exc()}")
        _static_failure(f"registering with Anki failed ({e})")


def _register_hooks() -> None:
    """All or nothing: on any failure, everything already added is removed and
    the exception is passed on (setup() then takes the unavailable path)."""
    added: list = []
    wrapped: list = []

    def add(name: str, fn: Callable) -> None:
        hook = getattr(gui_hooks, name)
        hook.append(fn)
        added.append((hook, fn))

    try:
        add("collection_did_load", on_collection_did_load)
        add("profile_will_close", on_profile_will_close)
        add("sync_will_start", on_sync_will_start)
        add("sync_did_finish", on_sync_did_finish)
        add("collection_will_temporarily_close", on_will_temporarily_close)
        add("collection_did_temporarily_close", on_did_temporarily_close)
        add("exporter_will_export", on_exporter_will_export)
        add("exporter_did_export", on_did_temporarily_close)
        if hasattr(gui_hooks, "day_did_change"):  # else noticed at the next screen change
            add("day_did_change", on_day_did_change)
        # (optional) before Anki replaces or deletes our folder: put everything back
        if compat.optional_hook_ok("addon_manager_will_install_addon"):
            add("addon_manager_will_install_addon", on_addon_manager_will_install_addon)
        if compat.optional_hook_ok("addons_dialog_will_delete_addons"):
            add("addons_dialog_will_delete_addons", on_addons_dialog_will_delete_addons)
        add("state_will_change", on_state_will_change)
        add("state_did_change", on_state_did_change)
        add("reviewer_will_answer_card", on_reviewer_will_answer_card)
        add("reviewer_did_answer_card", on_reviewer_did_answer_card)
        add("state_did_undo", on_state_did_undo)
        add("operation_did_execute", on_unbury_op_done)
        add("overview_will_render_bottom", on_overview_will_render_bottom)
        add("reviewer_did_show_question", on_reviewer_did_show)
        add("reviewer_did_show_answer", on_reviewer_did_show)
        add("deck_browser_will_render_content", on_deck_browser_will_render_content)
        add("top_toolbar_did_init_links", on_top_toolbar_did_init_links)
        add("webview_will_set_content", on_webview_will_set_content)
        add("webview_did_receive_js_message", on_js_message)
        # hook-independent second restore path before close / full-sync close / sync
        wrapped = safety.install(lambda: engine, _log)
        _watch_disable()
        mw.addonManager.setConfigUpdatedAction(MODULE, on_config_updated)
    except Exception:
        for hook, fn in reversed(added):
            try:
                hook.remove(fn)
            except Exception:
                pass
        try:
            safety.uninstall(wrapped)
        except Exception:
            pass
        raise
