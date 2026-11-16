"""Batch Reviews: study each deck's reviews in fixed batches.

Supported: Anki 26.05 - 26.09.x (v3 scheduler required); later Anki through its self-check. On anything else it checks
what this Anki provides first and stays off; the only thing it then still does is
put back cards that an earlier run left hidden (recover.py)."""

try:
    from aqt import mw
except ImportError:  # imported outside Anki (e.g. headless tests)
    mw = None

_MARKER = "_batch_reviews_active_copy"  # set on mw by the first copy that loads

if mw is not None and getattr(mw, _MARKER, None) not in (None, __name__):
    # a second copy (e.g. one installed from a file, another from AnkiWeb, in
    # different folders): it registers nothing and writes nothing; one notice
    _first = getattr(mw, _MARKER)
    try:
        from aqt.qt import QTimer
        from aqt.utils import tooltip

        QTimer.singleShot(5000, lambda: tooltip(
            f'Batch Reviews is installed twice (add-on folders "{_first}" and "{__name__}"). '
            f'Only the copy in "{_first}" is active. Please remove the other one in Tools > Add-ons.',
            period=12000))
    except Exception:
        pass
    mw = None  # (nothing below runs)

if mw is not None:
    try:
        from . import gui
    except Exception:  # an Anki too old/new for even our imports: stay inert
        import os
        import time
        import traceback

        folder = os.path.join(os.path.dirname(__file__), "user_files")

        def _log(msg: str) -> None:
            try:
                if not os.path.isdir(os.path.dirname(__file__)):
                    return  # the add-on was deleted while running: never recreate its folder
                os.makedirs(folder, exist_ok=True)
                with open(os.path.join(folder, "batch_reviews.log"), "a", encoding="utf8") as f:
                    f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
            except Exception:
                pass

        _log("not available: import failed\n" + traceback.format_exc())
        try:  # recover.py needs nothing but the standard library
            from . import recover

            recover.install_fallback(os.path.dirname(__file__), _log, "import failed")
        except Exception:
            _log("recovery not installed:\n" + traceback.format_exc())
        try:  # one short notice, once the main window is up
            from aqt.qt import QTimer
            from aqt.utils import tooltip

            QTimer.singleShot(4000, lambda: tooltip(
                "Batch Reviews could not start on this Anki version and stays off "
                "(details in its user_files log).", period=8000))
        except Exception:
            pass
    else:
        gui.setup()
        # this copy is the active one (also when it found this Anki unsupported:
        # it then handles recovery); set only now, so a copy whose import failed
        # never makes another copy stand down
        setattr(mw, _MARKER, __name__)
