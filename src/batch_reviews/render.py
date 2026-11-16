"""HTML for the grids (deck list and reviewer bar). No aqt imports, so the
deck-list render can be timed headless (tests/perf_targets.py)."""

from __future__ import annotations

import html

try:
    from .engine import COMPLETE, BatchEngine
except ImportError:  # imported as a top-level module (headless timing)
    from engine import COMPLETE, BatchEngine  # type: ignore[no-redef]

CMD = "batchrev:"


CSS = """
<style>
#brb-grid{margin:.6em auto 0;display:inline-block;text-align:start;font-size:90%}
#brb-grid .brb-head{cursor:pointer;opacity:.75;user-select:none}
#brb-grid.brb-min .brb-body{display:none}
.brb-row{margin:2px 0;display:flex;align-items:flex-start}
.brb-row.brb-hide{display:none}
.brb-name{user-select:none;flex:none;box-sizing:border-box;width:15em;margin-inline-end:.4em;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:20px}
.brb-name.brb-kids{cursor:pointer}
.brb-row.brb-live .brb-name{font-weight:600}
.brb-mark{display:inline-block;width:1em;opacity:.7}
.brb-boxes{flex:1 1 auto;max-width:42em;white-space:normal;line-height:0}
.brb-wait{margin-inline-start:.6em;color:var(--state-learn);font-size:90%;line-height:20px;white-space:nowrap}
.brb-wait:empty{display:none}
.brb-box{display:inline-block;width:16px;height:16px;margin:2px 3px;border-radius:3px;
  vertical-align:middle;box-sizing:border-box;cursor:pointer;--brb-dark:#1d4ed8}
.brb-box.todo{background:#c9c9c9}
.brb-box.doing{background:#93c5fd}
.brb-box.done{background:var(--brb-dark)}
.brb-box.cur{outline:2px solid var(--brb-dark);outline-offset:1px}
.brb-row:not(.brb-live) .brb-box.cur{outline-style:dotted}
.nightMode .brb-box{--brb-dark:#2357d9}
.nightMode .brb-box.todo{background:#6b6b6b}
.nightMode .brb-box.doing{background:#7cb4f0}
#brb-rev{font-size:12px;line-height:16px;padding-top:3px;white-space:nowrap;overflow:hidden}
#brb-rev:empty{display:none}
#brb-rev .brb-box{width:12px;height:12px;margin:1px 2px}
</style>
"""

JS = """
<script>
function brbMin(){document.getElementById('brb-grid').classList.toggle('brb-min');pycmd('batchrev:min');return false;}
function brbShow(){var rows=document.querySelectorAll('#brb-grid .brb-row');var vis={};
 rows.forEach(function(r){var p=r.getAttribute('data-parent');
  var v=!p||(vis[p]&&document.getElementById('brb-row-'+p).classList.contains('brb-open'));
  vis[r.getAttribute('data-did')]=v;r.classList.toggle('brb-hide',!v);});}
function brbTog(did){var r=document.getElementById('brb-row-'+did);r.classList.toggle('brb-open');
 var m=r.querySelector('.brb-mark');if(m)m.textContent=r.classList.contains('brb-open')?'\u25be':'\u25b8';
 brbShow();pycmd('batchrev:exp:'+did);return false;}
</script>
"""


# Updates the existing box elements (class, tooltip, click action) instead of
# replacing them, so a click in progress (e.g. the second click of a double-click)
# still lands on the same element. Falls back to replacing when the layout changed.
PATCH_JS = """
window.brbPatch = window.brbPatch || function(old, html) {
  var t = document.createElement('div'); t.innerHTML = html;
  var src = t.querySelector('#' + old.id) || t;
  var ids = function(e){ return Array.from(e.querySelectorAll('.brb-row')).map(function(r){return r.id}).join(); };
  var ob = old.querySelectorAll('.brb-box'), nb = src.querySelectorAll('.brb-box');
  var os = old.querySelectorAll('.brb-wait'), ns = src.querySelectorAll('.brb-wait');
  if (ob.length !== nb.length || os.length !== ns.length || ids(old) !== ids(src)) return false;
  for (var i = 0; i < ob.length; i++) {
    ob[i].className = nb[i].className; ob[i].title = nb[i].title;
    ob[i].setAttribute('onclick', nb[i].getAttribute('onclick'));
    ob[i].setAttribute('ondblclick', nb[i].getAttribute('ondblclick'));
  }
  for (var j = 0; j < os.length; j++) os[j].textContent = ns[j].textContent;
  var orows = old.querySelectorAll('.brb-row'), nrows = src.querySelectorAll('.brb-row');
  for (var k = 0; k < orows.length; k++) orows[k].classList.toggle('brb-live', nrows[k].classList.contains('brb-live'));
  return true;
};
"""


def _boxes(row: dict, cmd: str) -> str:
    out = []
    for i, (status, done, total) in enumerate(row["boxes"]):
        cls = status + (" cur" if i == row["current"] and status != COMPLETE else "")
        if status == COMPLETE:
            title, act = f"Batch {i + 1} \u00b7 completed \u00b7 click to browse", "browse"
        elif i == row["current"]:
            title, act = f"Batch {i + 1} \u00b7 {done} / {total} done \u00b7 click to browse", "browse"
        else:
            verb = "select" if cmd == "select" else "switch"
            title, act = f"Batch {i + 1} \u00b7 {done} / {total} done \u00b7 click to {verb}", cmd
        click = (
            f" onclick=\"pycmd('{CMD}{act}:{row['did']}:{i}');return false;\""
            # some input paths deliver one click + dblclick: browse on dblclick too
            f" ondblclick=\"pycmd('{CMD}dbl:{row['did']}:{i}');return false;\""
        )
        out.append(f'<span class="brb-box {cls}" title="{title}"{click}></span>')
    return "".join(out)


def _waiting_text(eng: BatchEngine, did: int, label: str) -> str:
    """Anki's Learn column does not count a redo whose step is beyond the
    learn-ahead window, so the deck can look finished. Say that it is not."""
    n = len(eng.redos_not_shown_yet(did))
    return f"\u21bb {n} to redo \u00b7 open {label} and Study Now" if n else ""


def deck_grid_html(eng: BatchEngine) -> str:
    """The deck-list grid: a collapsible tree of the decks with batches today.
    Top-level rows show; a deck's name shows/hides its direct children's rows
    (expansion persists per profile). Every row is in the page (hidden ones
    with brb-hide), so in-place updates keep the layout and the scroll position."""
    rows = eng.grid()
    if not rows:
        return ""
    ui = eng.state["ui"]
    expanded = set(ui["expanded"])
    visible: dict[int, bool] = {}
    level: dict[int, int] = {}
    body = []
    for row in rows:  # parents come before their children (deck list order)
        did, parent = row["did"], row["parent"]
        visible[did] = parent is None or (visible.get(parent, False) and parent in expanded)
        level[did] = 0 if parent is None else level.get(parent, 0) + 1
        cls = "brb-row" + (" brb-open" if did in expanded and row["children"] else "") \
            + (" brb-hide" if not visible[did] else "") + (" brb-live" if row["live"] else "")
        if row["children"]:
            mark = "\u25be" if did in expanded else "\u25b8"
            name = (f'<span class="brb-name brb-kids" style="padding-inline-start:{level[did]}em" '
                    f'title="{html.escape(row["name"])}" onclick="return brbTog({did});">'
                    f'<span class="brb-mark">{mark}</span>{html.escape(row["label"])}</span>')
        else:
            name = (f'<span class="brb-name" style="padding-inline-start:{level[did]}em" '
                    f'title="{html.escape(row["name"])}"><span class="brb-mark"></span>{html.escape(row["label"])}</span>')
        wait = html.escape(_waiting_text(eng, did, row["label"])) if row["live"] else ""
        body.append(
            f'<div class="{cls}" id="brb-row-{did}" data-did="{did}" data-parent="{parent or ""}">'
            f'{name}<span class="brb-boxes">{_boxes(row, "select")}</span>'
            f'<span class="brb-wait">{wait}</span></div>'
        )
    minimized = " brb-min" if ui["minimized"] else ""
    return (
        f'<div id="brb-grid" class="{minimized}">{CSS}{JS}'
        f'<div class="brb-head" onclick="return brbMin();">Batches</div>'
        f'<div class="brb-body">{"".join(body)}</div></div>'
    )


