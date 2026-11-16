"""Everything that differs between Anki versions (supported: 26.05 - 26.09.x;
older branches are kept as feature detection, not as a support promise),
resolved once by feature detection: protobuf field/oneof names, optional hooks,
optional private helpers. The only version-number check is the floor of the
supported range (MIN_POINT_VERSION); a future version is judged by what it
actually provides.

Importable without aqt (the engine and the headless tests use the anki part).
"""

from __future__ import annotations

import inspect
from typing import Any, Callable

from anki import cards_pb2, scheduler_pb2

# Protobuf shapes
##########################################################################


def _fields(message: Any) -> set[str]:
    return set(message.DESCRIPTOR.fields_by_name)


def _oneof_with(message: Any, field: str) -> str | None:
    """Name of the (real) oneof that contains `field`: "kind" from 2.1.66 on,
    "value" before; proto3 'optional' fields add synthetic oneofs we skip."""
    for oneof in message.DESCRIPTOR.oneofs:
        if field in [f.name for f in oneof.fields]:
            return oneof.name
    return None


QueuedCard = scheduler_pb2.QueuedCards.QueuedCard
SchedulingState = scheduler_pb2.SchedulingState
SchedulingContext = getattr(scheduler_pb2, "SchedulingContext", None)  # 2.1.66+

STATES_FIELD = "states" if "states" in _fields(QueuedCard) else "next_states"
QUEUED_HAS_CONTEXT = "context" in _fields(QueuedCard)
STATE_ONEOF = _oneof_with(SchedulingState, "normal")
NORMAL_ONEOF = _oneof_with(SchedulingState.Normal, "review")
CONTEXT_FIELDS = _fields(SchedulingContext) if SchedulingContext is not None else set()
CARD_FIELDS = _fields(cards_pb2.Card)


def queued_states(queued_card: Any) -> Any:
    """The next-state choices of a QueuedCards.QueuedCard."""
    return getattr(queued_card, STATES_FIELD)


def state_kind(state: Any) -> str | None:
    return state.WhichOneof(STATE_ONEOF)


def normal_kind(normal: Any) -> str | None:
    return normal.WhichOneof(NORMAL_ONEOF)


def states_match(a: Any, b: Any) -> bool:
    """Scheduling states equal apart from elapsed_secs (seconds since the last
    answer: it grows while a card is on screen, in Anki's own entries too)."""
    strip = lambda m: "\n".join(l for l in str(m).splitlines() if "elapsed_secs" not in l)
    return strip(a) == strip(b)


CONTEXT_HAS_FSRS = {"decay", "desired_retention"} <= CONTEXT_FIELDS  # 26.05+


def fsrs_context_values(col: Any, card: Any, source: str) -> dict:
    """The FSRS fields Anki's own queue puts in SchedulingContext for `card`
    (only where the context has them, and only for a card with FSRS data).
    Which source reproduces Anki's own value differs between versions (26.05
    copies the card's stored, rounded values; 26.09 uses the preset's exact
    ones, as compute_memory_state() returns them): probe.py finds out per
    build. Read-only."""
    if not CONTEXT_HAS_FSRS or "decay" not in CARD_FIELDS or not card.HasField("decay"):
        return {}
    if source == "computed":
        r = col._backend.compute_memory_state(card.id)
        return {"decay": r.decay, "desired_retention": r.desired_retention}
    return {k: getattr(card, k) for k in ("decay", "desired_retention") if card.HasField(k)}


def make_queued_card(card: Any, queue: int, states: Any, deck_name: str, seed: int,
                     fsrs: dict | None = None) -> Any:
    """Build a QueuedCard by hand (a waiting redo shown early), with whatever
    fields this version's message has; `fsrs` from fsrs_context_values(). The
    card carries the same values as the context, as in Anki's own entry."""
    kwargs = {"card": card, "queue": queue, STATES_FIELD: states}
    if QUEUED_HAS_CONTEXT and SchedulingContext is not None:
        ctx = SchedulingContext(**{k: v for k, v in (("deck_name", deck_name), ("seed", seed)) if k in CONTEXT_FIELDS})
        for name, value in (fsrs or {}).items():
            if name in CONTEXT_FIELDS:  # never a field this version does not have
                setattr(ctx, name, value)
                if name in CARD_FIELDS:
                    setattr(card, name, value)
        kwargs["context"] = ctx
    return QueuedCard(**kwargs)


# Scheduler
##########################################################################


def v3_problem(col: Any) -> str | None:
    """None if the v3 scheduler is active, else a one-line reason. Reads only."""
    try:
        v3 = getattr(col, "v3_scheduler", None)
        if v3 is None or not v3():
            return "it needs the v3 scheduler, which this collection is not using"
        sched = col.sched
        for name in ("get_queued_cards", "build_answer", "answer_card", "deck_due_tree"):
            if not callable(getattr(sched, name, None)):
                return f"the scheduler has no {name}()"
        if not hasattr(type(sched), "day_cutoff") or not hasattr(type(sched), "today"):
            return "the scheduler has no today/day_cutoff"
    except Exception as e:  # pragma: no cover - defensive
        return f"scheduler check failed ({e!r})"
    return None


# Signature checks (used by the GUI self-check)
##########################################################################


def params(func: Callable) -> list[str] | None:
    """Positional/keyword parameter names of a callable (without self), or None."""
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        return None
    return [p.name for p in sig.parameters.values()
            if p.name != "self" and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD)]


def has_params(func: Callable, *names: str) -> bool:
    found = params(func)
    return found is not None and all(n in found for n in names)


def accepts(func: Callable, *names: str) -> bool:
    """has_params(), or a pass-through wrapper (*args) another add-on put there."""
    if has_params(func, *names):
        return True
    try:
        return any(p.kind == p.VAR_POSITIONAL for p in inspect.signature(func).parameters.values())
    except (TypeError, ValueError):
        return False


# collection methods where safety.py puts hidden cards back whatever the hooks do
SAFETY_POINTS = {"close": ["downgrade"], "close_for_full_sync": [], "sync_collection": ["auth", "sync_media"]}


# Static self-check of the Anki GUI (run before any hook is registered)
##########################################################################

# hook name -> parameter names of its __call__, as in every tested version
REQUIRED_HOOKS = {
    "collection_did_load": ["col"],
    "profile_will_close": [],
    "sync_will_start": [],
    "sync_did_finish": [],
    "collection_will_temporarily_close": ["col"],
    "collection_did_temporarily_close": ["col"],
    "exporter_will_export": ["export_options", "exporter"],
    "exporter_did_export": ["export_options", "exporter"],
    "state_will_change": ["new_state", "old_state"],
    "state_did_change": ["new_state", "old_state"],
    "reviewer_will_answer_card": ["ease_tuple", "reviewer", "card"],
    "reviewer_did_answer_card": ["reviewer", "card", "ease"],
    "state_did_undo": ["changes"],
    "operation_did_execute": ["changes", "handler"],
    "overview_will_render_bottom": ["link_handler", "links"],
    "overview_will_render_content": ["overview", "content"],
    "reviewer_did_show_question": ["card"],
    "reviewer_did_show_answer": ["card"],
    "deck_browser_will_render_content": ["deck_browser", "content"],
    "top_toolbar_did_init_links": ["links", "top_toolbar"],
    "webview_will_set_content": ["web_content", "context"],
    "webview_did_receive_js_message": ["handled", "message", "context"],
}
# optional hooks: missing = degraded, present with another shape = fatal
OPTIONAL_HOOKS = {
    "day_did_change": [],  # 25.07+; without it the day change is noticed at the next screen change
    # put everything back before Anki replaces or deletes this add-on's folder
    "addon_manager_will_install_addon": ["manager", "module"],
    "addons_dialog_will_delete_addons": ["dialog", "ids"],
}
# an optional hook that is missing or changed is a degraded feature, never "unavailable"
FATAL_IF_CHANGED = {"day_did_change"}


def optional_hook_ok(name: str) -> bool:
    """Present with the parameters we call it with (else not registered)."""
    from aqt import gui_hooks

    hook = getattr(gui_hooks, name, None)
    return hook is not None and params(type(hook).__call__) == OPTIONAL_HOOKS[name]

# (object path, attribute, required parameter names or None for "exists")
REQUIRED_ATTRS = [
    ("mw.reviewer", "_get_next_v3_card", []),
    ("mw.reviewer", "nextCard", []),
    ("mw.reviewer", "refresh_if_needed", []),
    ("mw.reviewer", "_shortcutKeys", []),
    ("mw.reviewer", "_answerCard", ["ease"]),
    ("mw.reviewer", "_v3", None),
    ("mw.reviewer", "_refresh_needed", None),
    ("mw.reviewer", "previous_card", None),
    ("mw.reviewer", "card", None),
    ("mw.reviewer", "state", None),
    ("mw.reviewer", "web", None),
    ("mw.reviewer", "bottom", None),
    ("mw.overview", "on_unbury", []),
    ("mw.overview", "_show_finished_screen", []),
    ("mw.overview", "_desc", ["deck"]),
    ("mw.overview", "_table", []),
    ("mw.overview", "_body", None),
    ("mw.overview", "refresh", []),
    ("mw.overview", "web", None),
    ("mw.deckBrowser", "refresh", []),
    ("mw.deckBrowser", "_renderDeckTree", ["top"]),
    ("mw.deckBrowser", "_renderStats", []),
    ("mw.deckBrowser", "web", None),
    ("mw", "clearStateShortcuts", []),
    ("mw", "applyShortcuts", ["shortcuts"]),
    ("mw", "setStateShortcuts", ["shortcuts"]),
    ("mw", "stateShortcuts", None),
    ("mw", "undo", []),
    ("mw", "update_undo_actions", []),
    ("mw", "reset", []),
    ("mw", "moveToState", ["state"]),
    ("mw.progress", "single_shot", ["ms", "func"]),
    ("mw.toolbar", "create_link", ["cmd", "label", "func", "tip", "id"]),
    ("mw.addonManager", "getConfig", ["module"]),
    ("mw.addonManager", "setConfigUpdatedAction", ["module", "fn"]),
]


def _resolve(root: Any, path: str) -> Any:
    obj = root
    for part in path.split(".")[1:]:
        obj = getattr(obj, part)
    return obj


MIN_POINT_VERSION = 260500  # Anki 26.05: the oldest supported release (as in manifest.json)


def anki_point_version() -> int | None:
    try:
        from anki.utils import point_version

        return int(point_version())
    except Exception:
        try:
            from anki.utils import pointVersion  # type: ignore[attr-defined]

            return int(pointVersion())
        except Exception:
            return None


def gui_problems(mw: Any) -> tuple[list[str], dict[str, str]]:
    """(fatal problems, degraded features) for this Anki GUI. Reads only."""
    import aqt
    from aqt import gui_hooks

    problems: list[str] = []
    degraded: dict[str, str] = {}
    pv = anki_point_version()
    if pv is None or pv < MIN_POINT_VERSION:
        # the manifest already keeps older Anki from loading us; this covers a
        # forced load (a copied folder, an edited manifest)
        problems.append(f"this Anki ({pv}) is older than the oldest supported release, 26.05")
    for name, expected in REQUIRED_HOOKS.items():
        hook = getattr(gui_hooks, name, None)
        if hook is None:
            problems.append(f"hook {name} missing")
        elif params(type(hook).__call__) != expected:
            problems.append(f"hook {name} changed: {params(type(hook).__call__)}")
    for name, expected in OPTIONAL_HOOKS.items():
        hook = getattr(gui_hooks, name, None)
        if hook is None:
            degraded[name] = "hook missing"
        elif params(type(hook).__call__) != expected:
            if name in FATAL_IF_CHANGED:
                problems.append(f"hook {name} changed: {params(type(hook).__call__)}")
            else:
                degraded[name] = f"hook changed: {params(type(hook).__call__)} (not used)"
    try:
        from anki.collection import Collection

        for name, expected in SAFETY_POINTS.items():
            fn = getattr(Collection, name, None)
            if not callable(fn):
                problems.append(f"Collection.{name} missing")
            elif not accepts(fn, *expected):
                problems.append(f"Collection.{name}{tuple(expected)} changed: {params(fn)}")
    except Exception as e:
        problems.append(f"Collection check failed: {e!r}")
    for path, attr, expected in REQUIRED_ATTRS:
        try:
            obj = _resolve(mw, path)
        except AttributeError:
            problems.append(f"{path} missing")
            continue
        if not hasattr(obj, attr):
            problems.append(f"{path}.{attr} missing")
        elif expected is not None and not has_params(getattr(obj, attr), *expected):
            problems.append(f"{path}.{attr}{tuple(expected)} changed: {params(getattr(obj, attr))}")
    try:
        from aqt import reviewer, overview, deckbrowser

        if not callable(getattr(reviewer.V3CardInfo, "from_queue", None)):
            problems.append("V3CardInfo.from_queue missing")
        for name in ("QUEUES", "NOTE_TEXT"):
            if not hasattr(reviewer.RefreshNeeded, name):
                problems.append(f"RefreshNeeded.{name} missing")
        if not hasattr(reviewer, "ReviewerBottomBar"):
            problems.append("ReviewerBottomBar missing")
        if not {"deck", "shareLink", "desc", "table"} <= set(getattr(overview.OverviewContent, "__dataclass_fields__", {})):
            problems.append("OverviewContent fields changed")
        body = getattr(mw.overview, "_body", "")
        if not all(k in body for k in ("%(deck)s", "%(desc)s", "%(table)s", "%(shareLink)s")):
            problems.append("Overview._body template changed")
        if not {"tree", "stats"} <= set(getattr(deckbrowser.DeckBrowserContent, "__dataclass_fields__", {})):
            problems.append("DeckBrowserContent fields changed")
        if not callable(getattr(aqt.dialogs, "open", None)):
            problems.append("aqt.dialogs.open missing")
        from aqt.sound import av_player

        if not callable(getattr(av_player, "stop_and_clear_queue", None)):
            problems.append("av_player.stop_and_clear_queue missing")
        if not hasattr(mw.reviewer, "_clear_auto_advance_timers"):
            degraded["auto_advance"] = "no auto-advance in this Anki: nothing to stop on the pause screen"
        if not (hasattr(mw.deckBrowser, "_render_data") or hasattr(deckbrowser, "RenderData")):
            degraded["deck_list_in_place"] = "deck list keeps its tree in _dueTree (older Anki)"
    except Exception as e:
        problems.append(f"GUI check failed: {e!r}")
    problems += engine_problems()
    return problems, degraded


def engine_problems() -> list[str]:
    """Protobuf/backend shapes the engine needs."""
    out = []
    if STATE_ONEOF is None or NORMAL_ONEOF is None:
        out.append("SchedulingState layout changed")
    for name in ("learning", "relearning", "review"):
        if name not in _fields(SchedulingState.Normal):
            out.append(f"SchedulingState.Normal.{name} missing")
    if "scheduled_secs" not in _fields(SchedulingState.Learning):
        out.append("SchedulingState.Learning.scheduled_secs missing")
    if "learning" not in _fields(SchedulingState.Relearning):
        out.append("SchedulingState.Relearning.learning missing")
    if not {"card", "queue", STATES_FIELD} <= _fields(QueuedCard):
        out.append("QueuedCards.QueuedCard layout changed")
    if not hasattr(scheduler_pb2.QueuedCards, "LEARNING"):
        out.append("QueuedCards.LEARNING missing")
    for name in ("id", "deck_id", "original_deck_id", "reps", "queue"):
        if name not in CARD_FIELDS:
            out.append(f"Card.{name} missing")
    return out
