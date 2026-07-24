"""Regression tests. Stdlib only — run with:

    python -m unittest discover tests -v

Covers the parts where a wrong answer is silent: state derivation, XP settling,
cycle refusal, and the bulk queries agreeing with the per-milestone ones.
"""

import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db          # noqa: E402
import seed        # noqa: E402
import tree_render  # noqa: E402

G = 1


class Base(unittest.TestCase):
    def setUp(self):
        db.connect(":memory:")
        self.tree = db.create_tree(G, "t", "Tree")

    def milestone(self, key, name=None, xp=100, auto=True, tasks=(), members=True):
        mid = db.create_milestone(G, key, name or key.title(), "", xp, "", auto)
        if members:
            db.add_to_tree(self.tree, mid)
        if tasks:
            pid = db.create_project(G, f"{key}-work", "", 1)
            db.link_project(mid, pid)
            for title, weight, assignee, status in tasks:
                tid = db.add_task(pid, title, assignee, None, weight)
                if status != "todo":
                    db.set_task_status(tid, status)
        return mid

    def node(self, key):
        return next(n for n in db.tree_state(G) if n["key"] == key)


class TestStates(Base):
    def test_no_tasks_is_available(self):
        self.milestone("a")
        self.assertEqual(self.node("a")["state"], "available")

    def test_locked_until_prerequisite_completes(self):
        a = self.milestone("a", tasks=[("x", 1, 5, "todo")])
        b = self.milestone("b")
        db.add_dep(b, a)
        self.assertEqual(self.node("b")["state"], "locked")
        db.set_task_status(db.list_tasks(db.milestone_projects(a)[0]["id"])[0]["id"], "done")
        self.assertEqual(self.node("a")["state"], "complete")
        self.assertEqual(self.node("b")["state"], "available")

    def test_partial_work_is_active(self):
        self.milestone("a", tasks=[("x", 1, 5, "done"), ("y", 1, 5, "todo")])
        n = self.node("a")
        self.assertEqual(n["state"], "active")
        self.assertEqual(n["pct"], 50)

    def test_weighted_progress_is_not_a_task_count(self):
        self.milestone("a", tasks=[("big", 3, 5, "done"), ("small", 1, 5, "todo")])
        self.assertEqual(self.node("a")["pct"], 75)

    def test_manual_close_waits_for_signoff(self):
        self.milestone("a", auto=False, tasks=[("x", 1, 5, "done")])
        self.assertEqual(self.node("a")["state"], "pending")

    def test_pending_still_gates_downstream(self):
        a = self.milestone("a", auto=False, tasks=[("x", 1, 5, "done")])
        b = self.milestone("b")
        db.add_dep(b, a)
        self.assertEqual(self.node("b")["state"], "locked")
        db.complete_milestone(a, user_id=9)
        self.assertEqual(self.node("b")["state"], "available")

    def test_out_of_order_completion_is_allowed_and_flagged(self):
        a = self.milestone("gate")
        b = self.milestone("late", tasks=[("x", 1, 5, "done")])
        db.add_dep(b, a)
        n = self.node("late")
        self.assertEqual(n["state"], "complete")
        self.assertTrue(n["out_of_order"])
        self.assertFalse(self.node("gate")["out_of_order"])


class TestXP(Base):
    def test_even_split_distributes_remainder_to_first_names(self):
        self.assertEqual(db.even_split(100, [1, 2, 3]), {1: 34, 2: 33, 3: 33})
        self.assertEqual(db.even_split(10, [1, 2, 3, 4]), {1: 3, 2: 3, 3: 2, 4: 2})
        self.assertEqual(db.even_split(50, []), {})

    def test_split_ignores_task_weight(self):
        mid = self.milestone("a", xp=90, tasks=[
            ("heavy", 9, 11, "done"), ("light", 1, 22, "done")])
        self.assertEqual(db.settle_milestone(G, mid, 90), {11: 45, 22: 45})

    def test_xp_mints_only_once(self):
        mid = self.milestone("a", xp=60, tasks=[("x", 1, 5, "done")])
        first = db.settle_milestone(G, mid, 60)
        self.assertEqual(first, {5: 60})
        self.assertEqual(db.settle_milestone(G, mid, 60), {})
        self.assertEqual(db.user_xp(G, 5), 60)

    def test_explicit_credits_beat_task_assignees(self):
        mid = self.milestone("a", xp=100, tasks=[("x", 1, 5, "done")])
        db.complete_milestone(mid, user_id=9, credit_ids=[7, 8])
        self.assertEqual(db.settle_milestone(G, mid, 100), {7: 50, 8: 50})

    def test_signer_credited_when_there_is_no_one_else(self):
        mid = self.milestone("a", xp=75)
        db.complete_milestone(mid, user_id=42)
        self.assertEqual(db.settle_milestone(G, mid, 75), {42: 75})


class TestCycles(Base):
    def test_self_dependency_refused(self):
        a = self.milestone("a")
        self.assertFalse(db.add_dep(a, a))

    def test_direct_cycle_refused(self):
        a, b = self.milestone("a"), self.milestone("b")
        self.assertTrue(db.add_dep(a, b))
        self.assertFalse(db.add_dep(b, a))
        self.assertEqual(db.deps(G), [("b", "a")])

    def test_indirect_cycle_refused(self):
        a, b, c = self.milestone("a"), self.milestone("b"), self.milestone("c")
        db.add_dep(a, b)
        db.add_dep(b, c)
        self.assertFalse(db.add_dep(c, a))

    def test_diamond_is_not_a_cycle(self):
        root, l, r, tip = (self.milestone(k) for k in ("root", "l", "r", "tip"))
        self.assertTrue(db.add_dep(l, root))
        self.assertTrue(db.add_dep(r, root))
        self.assertTrue(db.add_dep(tip, l))
        self.assertTrue(db.add_dep(tip, r))


class TestStubs(Base):
    def test_unknown_prerequisite_becomes_a_stub(self):
        mid, created = db.find_or_stub(G, "Something Later")
        self.assertTrue(created)
        self.assertTrue(db.get_milestone(G, "something-later")["is_stub"])

    def test_existing_milestone_is_matched_not_duplicated(self):
        a = self.milestone("venue", name="Venue booked")
        found, created = db.find_or_stub(G, "Venue booked")
        self.assertFalse(created)
        self.assertEqual(found, a)

    def test_describing_a_stub_clears_the_flag(self):
        mid, _ = db.find_or_stub(G, "Funding")
        db.update_milestone(mid, description="the money")
        db.clear_stub(mid)
        self.assertEqual(db.list_stubs(G), [])


class TestTreeViews(Base):
    def test_external_prerequisite_is_included_and_marked(self):
        other = db.create_tree(G, "other", "Other")
        gate = db.create_milestone(G, "gate", "Gate")
        db.add_to_tree(other, gate)
        member = self.milestone("member")
        db.add_dep(member, gate)
        view = {n["key"]: n for n in db.tree_view(G, "t")}
        self.assertIn("gate", view)
        self.assertEqual(view["gate"]["external_from"], "other")
        self.assertIsNone(view["member"]["external_from"])

    def test_shared_milestone_appears_in_both_trees(self):
        other = db.create_tree(G, "other", "Other")
        shared = self.milestone("shared")
        db.add_to_tree(other, shared)
        for key in ("t", "other"):
            self.assertIn("shared", [n["key"] for n in db.tree_view(G, key)])


class TestBulkQueriesMatchPerMilestone(Base):
    """The N+1 rewrite must not have changed any answer."""

    def test_progress_and_people_agree(self):
        for i in range(12):
            self.milestone(f"m{i}", tasks=[
                ("a", 2, 100 + i, "done"),
                ("b", 1, 200 + i, "todo" if i % 2 else "done"),
            ])
        bulk = {n["key"]: n for n in db.tree_state(G)}
        for i in range(12):
            m = db.get_milestone(G, f"m{i}")
            slow = db.milestone_progress(m["id"])
            fast = bulk[f"m{i}"]
            self.assertEqual(slow["pct"], fast["pct"], f"pct m{i}")
            self.assertEqual(slow["remaining"], fast["remaining"], f"remaining m{i}")
            complete = fast["state"] == "complete"
            self.assertEqual(sorted(db.people_on(m["id"], complete)),
                             sorted(fast["people"]), f"people m{i}")

    def test_query_count_does_not_grow_with_the_tree(self):
        for i in range(30):
            self.milestone(f"m{i}", tasks=[("x", 1, 5, "done")])
        calls = []
        original = db._q
        db._q = lambda *a, **k: (calls.append(1), original(*a, **k))[1]
        try:
            db.tree_state(G)
        finally:
            db._q = original
        self.assertLessEqual(len(calls), 6, f"{len(calls)} queries for 30 milestones")


class TestLevels(Base):
    def test_thresholds_are_inclusive(self):
        self.assertEqual(db.level_for(G, 249)["name"], "Newcomer")
        self.assertEqual(db.level_for(G, 250)["name"], "Regular")

    def test_top_of_ladder_has_no_next(self):
        top = db.level_for(G, 99999)
        self.assertIsNone(top["next_at"])
        self.assertEqual(top["pct"], 100)

    def test_level_up_fires_hook_once(self):
        seen = []
        db.LEVEL_HOOKS.append(lambda g, u, o, n: seen.append((u, n["name"])))
        try:
            mid = self.milestone("a", xp=300, tasks=[("x", 1, 5, "done")])
            awards = db.settle_milestone(G, mid, 300)
            ups = db.apply_level_ups(G, awards)
            self.assertEqual([u["to"]["name"] for u in ups], ["Regular"])
            self.assertEqual(seen, [(5, "Regular")])
        finally:
            db.LEVEL_HOOKS.clear()

    def test_a_broken_hook_cannot_lose_xp(self):
        def explode(*_):
            raise RuntimeError("reward system on fire")
        db.LEVEL_HOOKS.append(explode)
        try:
            mid = self.milestone("a", xp=300, tasks=[("x", 1, 5, "done")])
            awards = db.settle_milestone(G, mid, 300)
            db.apply_level_ups(G, awards)
            self.assertEqual(db.user_xp(G, 5), 300)
        finally:
            db.LEVEL_HOOKS.clear()


class TestMigrations(unittest.TestCase):
    def test_old_database_gains_columns_without_losing_rows(self):
        path = "test_migration.db"
        Path(path).unlink(missing_ok=True)
        c = sqlite3.connect(path)
        c.executescript("""
            CREATE TABLE milestones (id INTEGER PRIMARY KEY AUTOINCREMENT,
              guild_id INTEGER NOT NULL, key TEXT NOT NULL, name TEXT NOT NULL,
              unlocks TEXT NOT NULL DEFAULT '', xp INTEGER NOT NULL DEFAULT 100,
              completed_at TEXT, settled INTEGER NOT NULL DEFAULT 0,
              UNIQUE (guild_id, key));
            INSERT INTO milestones (guild_id, key, name) VALUES (5, 'old', 'Old milestone');
        """)
        c.commit()
        c.close()
        try:
            db.connect(path)
            row = db.get_milestone(5, "old")
            self.assertEqual(row["name"], "Old milestone")
            for column in ("description", "auto_close", "is_stub", "credit_ids"):
                self.assertIn(column, row.keys())
            self.assertEqual(row["auto_close"], 1)
        finally:
            Path(path).unlink(missing_ok=True)


class TestImport(Base):
    CSV = ("tree,milestone,description,unlocks,requires,xp,auto_close\n"
           "Forum,Venue booked,call halls,date is set,,150,true\n"
           "Forum,Panel confirmed,four agree,promo starts,Venue booked,250,false\n")

    def test_csv_round_trip(self):
        doc = seed.parse(self.CSV, "plan.csv")
        seed.apply_doc(doc, G, 0)
        view = {n["key"]: n for n in db.tree_view(G, "forum")}
        self.assertEqual(view["panel-confirmed"]["prereqs"], ["venue-booked"])
        self.assertFalse(view["panel-confirmed"]["auto_close"])
        self.assertEqual(view["venue-booked"]["xp"], 150)

    def test_reapplying_updates_rather_than_duplicating(self):
        doc = seed.parse(self.CSV, "plan.csv")
        seed.apply_doc(doc, G, 0)
        before = len(db.list_milestones(G))
        seed.apply_doc(doc, G, 0)
        self.assertEqual(len(db.list_milestones(G)), before)

    def test_preview_flags_undefined_prerequisites_as_stubs(self):
        doc = seed.parse(self.CSV + "Forum,Promo,,turnout,Money secured,100,true\n",
                         "plan.csv")
        self.assertIn("Money secured", seed.preview(doc, G)["stubs"])

    def test_missing_header_is_reported(self):
        self.assertTrue(seed.parse("foo,bar\n1,2\n", "x.csv")["problems"])


class TestRendering(Base):
    def nodes(self):
        return [{"key": k, "name": k.title(), "description": "d", "state": "locked",
                 "pct": 0, "xp": 100, "unlocks": "u", "people": []}
                for k in "abcdefgh"]

    EDGES = [("a", "b"), ("b", "c"), ("c", "d"), ("a", "d"),
             ("a", "h"), ("b", "h"), ("e", "f"), ("f", "g"), ("e", "d")]

    def test_both_orientations_render(self):
        for mode in ("lr", "tb"):
            buf = tree_render.render_tree(self.nodes(), self.EDGES, "t", mode)
            self.assertGreater(buf.getbuffer().nbytes, 1000)

    def test_long_edges_do_not_cross_node_bodies(self):
        from PIL import Image
        EDGE_GREY = (48, 54, 61)
        for mode in ("lr", "tb"):
            nodes = self.nodes()
            im = Image.open(tree_render.render_tree(nodes, self.EDGES, "t", mode)).convert("RGB")
            depth, order, _ = tree_render.plan_layout(nodes, self.EDGES)
            real = {n["key"] for n in nodes}
            tb = mode == "tb"
            ms, step = ((tree_render.TITLE_H + tree_render.PAD // 2,
                         tree_render.NODE_H + tree_render.V_GAP) if tb
                        else (tree_render.PAD, tree_render.NODE_W + tree_render.H_GAP))
            cs, gap = ((tree_render.PAD, tree_render.H_GAP) if tb
                       else (tree_render.TITLE_H + tree_render.PAD // 2, tree_render.V_GAP))
            stray = 0
            for layer, keys in order.items():
                c, main = cs, ms + layer * step
                for k in keys:
                    w, h = ((tree_render.NODE_W, tree_render.NODE_H) if k in real
                            else ((tree_render.DUMMY_W, tree_render.NODE_H) if tb
                                  else (tree_render.NODE_W, tree_render.DUMMY_H)))
                    x0, y0 = (c, main) if tb else (main, c)
                    if k in real:
                        inner = im.crop((x0 + 14, y0 + 14, x0 + w - 14, y0 + h - 14))
                        stray += sum(n for n, col in inner.getcolors(maxcolors=300000)
                                     if col == EDGE_GREY)
                    c += (w if tb else h) + gap
            self.assertLess(stray, 50, f"{mode}: {stray} edge pixels inside nodes")

    def test_oversized_tree_is_capped(self):
        from PIL import Image
        many = [{"key": f"n{i}", "name": f"Node {i}", "description": "x" * 40,
                 "state": "locked", "pct": 0, "xp": 100, "unlocks": "",
                 "people": []} for i in range(60)]
        edges = [(f"n{i}", f"n{i + 7}") for i in range(53)]
        im = Image.open(tree_render.render_tree(many, edges, "big"))
        self.assertLessEqual(max(im.size), tree_render.MAX_EDGE)

    def test_cycle_in_data_does_not_hang_the_renderer(self):
        nodes = self.nodes()[:2]
        buf = tree_render.render_tree(nodes, [("a", "b"), ("b", "a")], "cycle")
        self.assertGreater(buf.getbuffer().nbytes, 1000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
