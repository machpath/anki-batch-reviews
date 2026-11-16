# Changelog

## 1.1.2

First public release.

- Shows each deck's due reviews for the day in fixed-size batches, one batch at a time (`batch_size`, default 25).
- Toolbar link **Batch: ON / OFF** to switch it; off by default.
- Grid of batches on the deck list (gray: not started, light blue: started, dark blue: done; a ring on the current batch), collapsible by deck, and the studied deck's row in the reviewer's bottom bar.
- Separate batches for every deck and subdeck. Click a box to pick a batch; click a finished or current box to see its cards in the Browser.
- Optional pause screen between batches (`pause_between_batches`, default on). Ctrl+Z still undoes the answer that finished a batch.
- Later batches are hidden with Anki's buried state without marking cards as modified. Everything is put back before every sync and before exports that include scheduling, when Anki closes, when batching is turned off, and when the add-on is updated, disabled or removed (it then pauses itself until Anki is restarted: **Batch: restart Anki**).
- Cards in filtered decks are left alone.
- Startup self-check: on an Anki it cannot work with, the add-on shows **Batch: unavailable** and stays off. Only one copy is active if the add-on is installed twice.
- Requires Anki 26.05 or newer with the v3 scheduler.
