**Batch Reviews** splits each deck's reviews for today into small batches and shows
you one batch at a time. When a batch is done, the next one follows (after a short
pause screen, if you like). Your scheduling is not changed: the cards of the other
batches are only kept out of sight for the moment.

**Turning it on and off:** click **Batch: ON / OFF** in the top toolbar. While it is
ON, the deck list shows a row of boxes for each deck with reviews today (gray: not
started, light blue: started, dark blue: done). Click a deck's name in that list to
show its subdecks; click a box to make that batch the current one; click the current
box to see its cards in the Browser. Opening a deck to study gives that deck's own
batch.

**Settings** (Tools > Add-ons > Batch Reviews > Config):

- **batch_size** (default `25`): how many review cards go into each batch. Batches
  are made once a day, so a change takes effect the next day, or after you turn
  batching OFF and ON again.
- **pause_between_batches** (default `true`): after finishing a batch, show "Batch is
  done. Press Space or Enter to move to the next batch." before the next one (Ctrl+Z
  still undoes your last answer). `false`: the next batch starts right away. Takes
  effect immediately.

**Good habits:** turn batching **OFF** before you update, disable or remove the
add-on. (It puts every card back by itself in those cases too, and whenever Anki
closes or syncs; turning it OFF first just makes sure.) Install only one copy of the
add-on: if you installed an earlier copy from a file, remove it before installing
from AnkiWeb.

**Undo:** Ctrl+Z undoes your answers, including the one that finished a batch. But
Anki's earlier undo history (for example a filtered deck's Empty, or an edit) is
cleared whenever the add-on changes which cards are hidden: a new batch, clicking a
box, opening another deck in the same tree, or a sync.

Cards in filtered decks are left alone. A filtered deck inside a batched deck shows
its own cards as usual, so that deck's Due count can be larger than its batch.

Needs Anki 26.05 or newer with the v3 scheduler.

**Log:** if something goes wrong, the add-on switches itself off, puts every card
back, and writes details to `batch_reviews.log` (Tools > Add-ons > Batch Reviews >
View Files > user_files).
