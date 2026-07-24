# Working on this

## Layout

| File | Does |
|---|---|
| `bot.py` | Slash commands and Discord wiring |
| `db.py` | All SQL. No ORM — one function per query |
| `tree_render.py` | Draws the tech tree PNG with Pillow |
| `wizard.py` | The `/start` pop-up forms |
| `seed.py` | Bulk load from YAML or CSV |
| `planner.html` | Offline browser planner, exports CSV |

## Rules that keep it working

**Schema changes go through `MIGRATIONS` in `db.py`.** Add the column to the
`SCHEMA` string *and* to the `MIGRATIONS` list. New installs get it from the
schema; existing databases get an `ALTER TABLE` on next start. Never rely on
`CREATE TABLE IF NOT EXISTS` to add a column — it silently does nothing.

**Node state is derived, never stored.** `tree_state()` computes locked /
available / active / pending / complete from task data every time it's called.
Don't add a status column to `milestones`.

**XP mints once, at completion.** `settled` guards it. Test any change to
`settle_milestone` against a double call.

## Local testing without a token

Everything except the Discord layer runs headless:

```bash
python -c "
import db; db.connect(':memory:')
g = 1
t = db.create_tree(g, 'demo', 'Demo')
m = db.create_milestone(g, 'a', 'First thing')
db.add_to_tree(t, m)
print([(n['key'], n['state']) for n in db.tree_view(g, 'demo')])
"
```

`python tree_render.py` writes `demo_tree.png` from sample data — the quickest
way to check a rendering change.
