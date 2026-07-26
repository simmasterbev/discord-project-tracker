"""Regression tests. Stdlib only — run with:

    python -m unittest discover tests -v

Covers the parts where a wrong answer is silent: state derivation, XP settling,
cycle refusal, and the bulk queries agreeing with the per-milestone ones.
"""

import json
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
            db.conn().close()
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

    def test_taxonomy_columns_apply(self):
        csv = ("tree,milestone,group,region,team\n"
               "T,Tagged,Forum,Delaware,Ops\n")
        seed.apply_doc(seed.parse(csv, "t.csv"), G, 0)
        m = db.get_milestone(G, "tagged")
        self.assertEqual((m["grp"], m["region"], m["team"]), ("Forum", "Delaware", "Ops"))

    def test_difficulty_column_applies(self):
        seed.apply_doc(seed.parse("tree,milestone,difficulty\nT,Hard,7.5\n", "d.csv"), G, 0)
        self.assertEqual(db.get_milestone(G, "hard")["difficulty"], 7.5)

    def test_private_column_applies(self):
        seed.apply_doc(seed.parse("tree,milestone,private\nT,Secret,true\n", "p.csv"), G, 0)
        self.assertEqual(db.get_milestone(G, "secret")["private"], 1)
        seed.apply_doc(seed.parse("tree,milestone,private\nT,Open,false\n", "o.csv"), G, 0)
        self.assertEqual(db.get_milestone(G, "open")["private"], 0)

    def test_yaml_friendly_group_key_maps_to_grp(self):
        y = ("milestones:\n"
             "  - key: m\n    name: M\n    group: Aviation\n"
             "    region: Broome\n    difficulty: 4\n    private: true\n")
        seed.apply_doc(seed.parse(y, "plan.yaml"), G, 0)
        m = db.get_milestone(G, "m")
        self.assertEqual(m["grp"], "Aviation")       # `group` -> `grp`
        self.assertEqual(m["region"], "Broome")
        self.assertEqual(m["difficulty"], 4.0)
        self.assertEqual(m["private"], 1)

    def test_plain_csv_defaults_to_universal(self):
        seed.apply_doc(seed.parse("tree,milestone\nT,Plain\n", "p.csv"), G, 0)
        m = db.get_milestone(G, "plain")
        self.assertEqual(m["grp"], "Universal")
        self.assertEqual(m["difficulty"], 1.0)


class TestPlannerServerExport(Base):
    def test_export_shape(self):
        project = db.create_project(G, "Forum", "", 1)
        db.set_project_tags(project, grp="Events")
        tree = db.create_tree(G, "ft", "Forum tree")
        db.set_tree_tags(tree, grp="Events")
        milestone = db.create_milestone(G, "venue", "Venue booked")
        db.add_to_tree(tree, milestone)

        doc = db.export_for_planner(G)

        self.assertEqual(doc["_kind"], "planner_server_export")
        self.assertEqual(doc["trees"][0]["key"], "ft")
        self.assertEqual(doc["trees"][0]["group"], "Events")
        self.assertEqual(
            doc["trees"][0]["milestones"],
            [{"key": "venue", "name": "Venue booked"}],
        )
        self.assertEqual(doc["projects"][0]["name"], "Forum")
        self.assertEqual(doc["projects"][0]["group"], "Events")

    def test_export_reflects_only_real_data(self):
        before = len(db.export_for_planner(G)["trees"])
        db.create_tree(G, "extra", "Extra tree")
        after = db.export_for_planner(G)["trees"]
        self.assertEqual(len(after), before + 1)
        self.assertTrue(any(tree["key"] == "extra" for tree in after))


class TestPlannerExtendExisting(Base):
    def test_json_plan_can_extend_existing_tree(self):
        tree = db.create_tree(G, "forum", "Forum")
        venue = db.create_milestone(G, "venue", "Venue", xp=150)
        db.add_to_tree(tree, venue)
        before = len(db.list_trees(G))

        plan = {
            "trees": [{
                "key": "forum",
                "name": "Forum",
                "milestones": [{
                    "key": "promo",
                    "name": "Promo",
                    "requires": ["Venue"],
                }],
            }],
        }
        seed.apply_doc(seed.parse(json.dumps(plan), "p.json"), G, 1)

        self.assertEqual(len(db.list_trees(G)), before)
        keys = db.tree_members(tree)
        self.assertEqual(keys, {"venue", "promo"})

    def test_json_plan_can_link_existing_project(self):
        db.create_project(G, "Forum", "", 1)
        tree = db.create_tree(G, "forum", "Forum")
        plan = {
            "trees": [{
                "key": "forum",
                "name": "Forum",
                "milestones": [{
                    "key": "venue",
                    "name": "Venue",
                    "projects": ["Forum"],
                }],
            }],
        }
        seed.apply_doc(seed.parse(json.dumps(plan), "p.json"), G, 1)

        milestone = db.get_milestone(G, "venue")
        self.assertEqual(
            [row["name"] for row in db.milestone_projects(milestone["id"])],
            ["Forum"],
        )

    def test_json_plan_creates_project_tasks_tags_and_milestone_link(self):
        text = '''{
          "projects": [{
            "name": "Venue work", "description": "Book a hall", "grp": "Events",
            "region": "North", "team": "Ops",
            "tasks": [{"title": "Get quotes", "weight": 3, "due": "2026-08-01", "assignee": 42}]
          }],
          "trees": [{
            "key": "launch", "name": "Launch", "grp": "Events", "region": "North", "team": "Ops",
            "milestones": [{"key": "venue", "name": "Venue booked", "projects": ["Venue work"]}]
          }]
        }'''
        doc = seed.parse(text, "launch.json")
        pv = seed.preview(doc, G)
        self.assertEqual(pv["new_projects"], ["Venue work"])
        self.assertEqual(pv["new_tasks"], ["Get quotes"])
        seed.apply_doc(doc, G, 0)
        project = db.get_project(G, "Venue work")
        task = db.list_tasks(project["id"])[0]
        milestone = db.get_milestone(G, "venue")
        self.assertEqual((project["grp"], project["region"], project["team"]),
                         ("Events", "North", "Ops"))
        self.assertEqual((task["title"], task["weight"], task["assignee_id"]),
                         ("Get quotes", 3, 42))
        self.assertEqual([row["id"] for row in db.milestone_projects(milestone["id"])],
                         [project["id"]])


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
            origins = {}
            if tb:
                for layer, keys in order.items():
                    x = tree_render.PAD
                    y = tree_render.TITLE_H + tree_render.PAD // 2 + layer * (
                        tree_render.NODE_H + tree_render.V_GAP
                    )
                    for k in keys:
                        w = tree_render.NODE_W if k in real else tree_render.DUMMY_W
                        origins[k] = (x, y)
                        x += w + tree_render.H_GAP
            else:
                lane_cols = min(3, max(1, (max(len(keys) for keys in order.values())
                                             + tree_render.MAX_LAYER_ROWS - 1)
                                            // tree_render.MAX_LAYER_ROWS))
                for layer, keys in order.items():
                    rows = (len(keys) + lane_cols - 1) // lane_cols
                    for i, k in enumerate(keys):
                        lane, row = divmod(i, rows)
                        origins[k] = (
                            tree_render.PAD + (layer * lane_cols + lane) * (
                                tree_render.NODE_W + tree_render.H_GAP
                            ),
                            tree_render.TITLE_H + tree_render.PAD // 2 + row * (
                                tree_render.NODE_H + tree_render.V_GAP
                            ),
                        )
            stray = 0
            for layer, keys in order.items():
                for k in keys:
                    w, h = ((tree_render.NODE_W, tree_render.NODE_H) if k in real
                            else ((tree_render.DUMMY_W, tree_render.NODE_H) if tb
                                  else (tree_render.NODE_W, tree_render.DUMMY_H)))
                    x0, y0 = origins[k]
                    if k in real:
                        inner = im.crop((x0 + 14, y0 + 14, x0 + w - 14, y0 + h - 14))
                        stray += sum(n for n, col in inner.getcolors(maxcolors=300000)
                                     if col == EDGE_GREY)
            self.assertLess(stray, 50, f"{mode}: {stray} edge pixels inside nodes")

    def test_oversized_tree_is_capped(self):
        from PIL import Image
        many = [{"key": f"n{i}", "name": f"Node {i}", "description": "x" * 40,
                 "state": "locked", "pct": 0, "xp": 100, "unlocks": "",
                 "people": []} for i in range(60)]
        edges = [(f"n{i}", f"n{i + 7}") for i in range(53)]
        im = Image.open(tree_render.render_tree(many, edges, "big"))
        self.assertLessEqual(max(im.size), tree_render.MAX_EDGE)

    def test_dense_layer_uses_multiple_lanes(self):
        from PIL import Image
        nodes = [{"key": f"n{i}", "name": f"Node {i}", "description": "",
                  "state": "available", "pct": 0, "xp": 100, "unlocks": "",
                  "people": []} for i in range(12)]
        im = Image.open(tree_render.render_tree(nodes, [], "dense"))
        self.assertLess(im.height, 1000)
        self.assertGreater(im.width, tree_render.NODE_W * 2)

    def test_cycle_in_data_does_not_hang_the_renderer(self):
        nodes = self.nodes()[:2]
        buf = tree_render.render_tree(nodes, [("a", "b"), ("b", "a")], "cycle")
        self.assertGreater(buf.getbuffer().nbytes, 1000)


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ==========================================================================
# Stage additions: taxonomy, difficulty, privacy, permissions, audit
# ==========================================================================

class TestDifficulty(Base):
    def test_clamps_to_range(self):
        self.assertEqual(db.clamp_difficulty(0), 1.0)
        self.assertEqual(db.clamp_difficulty(99), 10.0)
        self.assertEqual(db.clamp_difficulty(-4), 1.0)

    def test_snaps_to_half_steps(self):
        self.assertEqual(db.clamp_difficulty(4.3), 4.5)
        self.assertEqual(db.clamp_difficulty(4.7), 4.5)
        self.assertEqual(db.clamp_difficulty(4.24), 4.0)

    def test_garbage_defaults_to_one(self):
        self.assertEqual(db.clamp_difficulty("nonsense"), 1.0)
        self.assertEqual(db.clamp_difficulty(None), 1.0)

    def test_set_at_creation_and_edited(self):
        mid = db.create_milestone(G, "a", "A", difficulty=6.5)
        self.assertEqual(db.get_milestone(G, "a")["difficulty"], 6.5)
        db.set_difficulty(mid, 2)
        self.assertEqual(db.get_milestone(G, "a")["difficulty"], 2.0)


class TestTaxonomy(Base):
    def test_universal_always_listed_first(self):
        db.add_taxonomy(G, "grp", "Aviation")
        vals = db.list_taxonomy(G, "grp")
        self.assertEqual(vals[0], "Universal")
        self.assertIn("Aviation", vals)

    def test_scope_filter_hides_other_groups(self):
        db.create_milestone(G, "f", "Forum thing", grp="Forum")
        db.create_milestone(G, "a", "Aviation thing", grp="Aviation")
        db.create_milestone(G, "u", "Universal thing")
        seen = {m["name"] for m in db.milestones_in_scope(G, grp="Forum")}
        self.assertIn("Forum thing", seen)
        self.assertIn("Universal thing", seen)      # universal always visible
        self.assertNotIn("Aviation thing", seen)    # other group hidden

    def test_exclude_id_drops_self(self):
        a = db.create_milestone(G, "a", "A", grp="Forum")
        b = db.create_milestone(G, "b", "B", grp="Forum")
        ids = {m["id"] for m in db.milestones_in_scope(G, grp="Forum", exclude_id=a)}
        self.assertNotIn(a, ids)
        self.assertIn(b, ids)

    def test_milestone_inherits_then_can_be_retagged(self):
        mid = db.create_milestone(G, "a", "A", grp="Aviation", region="Broome")
        self.assertEqual(db.get_milestone(G, "a")["grp"], "Aviation")
        db.set_milestone_tags(mid, grp="Forum")
        m = db.get_milestone(G, "a")
        self.assertEqual(m["grp"], "Forum")
        self.assertEqual(m["region"], "Broome")     # untouched dimension survives


class TestPrivacy(Base):
    def _private_milestone_with_assignee(self, assignee):
        mid = db.create_milestone(G, "p", "Private", private=True)
        pid = db.create_project(G, "w", "", 1)
        db.link_project(mid, pid)
        db.add_task(pid, "x", assignee)
        return mid

    def test_public_description_readable_by_anyone(self):
        mid = db.create_milestone(G, "a", "A", private=False)
        self.assertTrue(db.can_read_description(G, mid, 999, set(), False))

    def test_private_hidden_from_strangers(self):
        mid = self._private_milestone_with_assignee(7)
        self.assertFalse(db.can_read_description(G, mid, 50, set(), False))

    def test_private_visible_to_assignee(self):
        mid = self._private_milestone_with_assignee(7)
        self.assertTrue(db.can_read_description(G, mid, 7, set(), False))

    def test_private_visible_to_manager(self):
        mid = self._private_milestone_with_assignee(7)
        self.assertTrue(db.can_read_description(G, mid, 50, set(), True))

    def test_private_visible_to_permitted_role(self):
        mid = self._private_milestone_with_assignee(7)
        db.set_cmd_perm(G, "milestone_private", 321)
        self.assertTrue(db.can_read_description(G, mid, 50, {321}, False))


class TestCommandPerms(Base):
    def test_unset_command_is_open(self):
        self.assertIsNone(db.get_cmd_perm(G, "tree_import"))

    def test_set_and_clear(self):
        db.set_cmd_perm(G, "tree_import", 555)
        self.assertEqual(db.get_cmd_perm(G, "tree_import"), 555)
        db.set_cmd_perm(G, "tree_import", None)
        self.assertIsNone(db.get_cmd_perm(G, "tree_import"))

    def test_universal_role_round_trip(self):
        db.set_universal_role(G, 42)
        self.assertEqual(db.get_universal_role(G), 42)
        db.set_universal_role(G, None)
        self.assertIsNone(db.get_universal_role(G))


class TestAudit(Base):
    def test_note_appends_to_description_and_logs(self):
        mid = db.create_milestone(G, "a", "A", description="original")
        db.append_milestone_note(mid, 7, "did a thing")
        desc = db.get_milestone(G, "a")["description"]
        self.assertIn("original", desc)
        self.assertIn("did a thing", desc)
        self.assertIn("UTC", desc)
        self.assertEqual(len(db.milestone_audit(mid)), 1)

    def test_history_survives_when_description_would_be_trimmed(self):
        mid = db.create_milestone(G, "a", "A")
        for i in range(30):
            db.append_milestone_note(mid, 7, f"entry {i}")
        # every entry is in the audit table regardless of description length
        self.assertEqual(len(db.milestone_audit(mid, limit=100)), 30)


class TestRenderingStage3(Base):
    def _node(self, **kw):
        base = {"key": "a", "name": "A", "description": "desc", "state": "active",
                "pct": 50, "xp": 100, "unlocks": "u", "people": [], "difficulty": 3,
                "private": False, "grp": "Universal", "region": "Universal",
                "team": "Universal"}
        base.update(kw)
        return [base]

    def _text_calls(self, nodes, edges=()):
        import PIL.ImageDraw as ID
        calls = []
        orig = ID.ImageDraw.text
        ID.ImageDraw.text = lambda self, xy, text, *a, **k: (
            calls.append(str(text)), orig(self, xy, text, *a, **k))[1]
        try:
            tree_render.render_tree(nodes, list(edges), "t", "lr")
        finally:
            ID.ImageDraw.text = orig
        return calls

    def test_private_description_never_reaches_the_canvas(self):
        calls = self._text_calls(self._node(private=True, description="SECRET DETAIL"))
        self.assertFalse(any("SECRET DETAIL" in c for c in calls))
        self.assertTrue(any("restricted" in c for c in calls))

    def test_public_description_is_drawn(self):
        calls = self._text_calls(self._node(private=False, description="OPEN DETAIL"))
        self.assertTrue(any("OPEN DETAIL" in c for c in calls))

    def test_non_universal_tags_are_drawn(self):
        calls = self._text_calls(self._node(grp="Aviation", region="Delaware"))
        self.assertTrue(any("Aviation" in c and "Delaware" in c for c in calls))

    def test_universal_tags_are_not_drawn(self):
        calls = self._text_calls(self._node())      # all Universal
        self.assertFalse(any("Universal" in c for c in calls))

    def test_difficulty_label_is_compact(self):
        self.assertEqual(tree_render.difficulty_label(1), "")
        self.assertEqual(tree_render.difficulty_label(3.5), "DIFFICULTY 3.5/10")
        self.assertEqual(tree_render.difficulty_label(99), "DIFFICULTY 10/10")


class TestConfigRoundTrip(Base):
    def _seed(self):
        db.add_taxonomy(G, "grp", "Aviation")
        db.add_taxonomy(G, "grp", "Forum")
        db.set_cmd_perm(G, "tree_import", 111)
        db.set_cmd_perm(G, "start", 222)
        db.set_universal_role(G, 333)
        db.set_signoff_role(G, 444)

    def test_export_uses_string_role_ids(self):
        self._seed()
        exp = db.export_config(G)
        self.assertIsInstance(exp["permissions"]["tree_import"], str)
        self.assertEqual(exp["universal_role"], "333")

    def test_export_then_import_is_a_noop(self):
        self._seed()
        exp = db.export_config(G)
        valid = {111, 222, 333, 444}
        rep = db.diff_config(G, exp, valid)
        changed = sum(len(v) for v in rep.values() if isinstance(v, list))
        self.assertEqual(changed, 0)
        self.assertIsNone(rep["universal"])
        self.assertIsNone(rep["signoff"])

    def test_replace_removes_absent_permissions(self):
        self._seed()
        doc = {"permissions": {"tree_import": "111"}, "taxonomy": {},
               "levels": [], "universal_role": "333", "signoff_role": "444"}
        rep = db.diff_config(G, doc, {111, 333, 444})
        self.assertIn(("start", 222), rep["perm_remove"])   # removal is reported
        db.apply_config(G, doc, {111, 333, 444})
        perms = {r["command"]: r["role_id"] for r in db.list_cmd_perms(G)}
        self.assertIn("tree_import", perms)
        self.assertNotIn("start", perms)          # dropped by replace

    def test_broken_role_gate_is_skipped_not_applied(self):
        doc = {"permissions": {"tree_import": "999"}, "taxonomy": {}, "levels": []}
        rep = db.diff_config(G, doc, {111})       # 999 invalid
        self.assertIn(("permission:tree_import", "999"), rep["skipped"])
        self.assertEqual(rep["perm_set"], [])
        db.apply_config(G, doc, {111})
        self.assertIsNone(db.get_cmd_perm(G, "tree_import"))

    def test_lockout_flag_when_config_import_gated_to_dead_role(self):
        doc = {"permissions": {"config_import": "999"}, "taxonomy": {}, "levels": []}
        rep = db.diff_config(G, doc, {111})
        self.assertTrue(rep["lockout"])

    def test_taxonomy_replace_adds_and_removes(self):
        db.add_taxonomy(G, "grp", "Old")
        doc = {"permissions": {}, "taxonomy": {"grp": ["New"]}, "levels": []}
        rep = db.diff_config(G, doc, set())
        self.assertIn(("grp", "New"), rep["tax_add"])
        self.assertIn(("grp", "Old"), rep["tax_remove"])
        db.apply_config(G, doc, set())
        self.assertEqual(db.list_taxonomy(G, "grp"), ["Universal", "New"])

    def test_levels_fully_replace_without_reseeding_defaults(self):
        db.set_level(G, 5000, "Legacy")           # a level the file omits
        doc = {"permissions": {}, "taxonomy": {},
               "levels": [{"xp": 0, "name": "Start"}, {"xp": 999, "name": "End"}]}
        db.apply_config(G, doc, set())
        got = [(r["threshold"], r["name"]) for r in db.list_levels(G)]
        self.assertEqual(got, [(0, "Start"), (999, "End")])   # Legacy + defaults gone


class TestModeratorTools(Base):
    def test_notification_targets_inherit_from_tree_project_and_milestone(self):
        milestone = self.milestone("gate")
        project = db.create_project(G, "Work", "", 1)
        db.link_project(milestone, project)
        db.add_notify(G, "tree", self.tree, "role", 11)
        db.add_notify(G, "project", project, "user", 22)
        db.add_notify(G, "milestone", milestone, "user", 33)
        self.assertEqual(db.effective_notify(G, milestone),
                         {"role": [11], "user": [22, 33]})

    def test_stuck_report_finds_old_idle_and_blocked_work(self):
        idle = self.milestone("idle")
        db._exec("UPDATE milestones SET created_at=datetime('now', '-10 days') WHERE id=?", (idle,))
        project = db.create_project(G, "Blocked", "", 1)
        db.add_task(project, "Waiting on permit", assignee_id=7)
        db._exec("UPDATE tasks SET status='blocked' WHERE project_id=?", (project,))
        report = db.stuck_report(G, stale_days=7)
        self.assertEqual([item["name"] for item in report["idle"]], ["Idle"])
        self.assertEqual([item["title"] for item in report["blocked"]], ["Waiting on permit"])

    def test_board_settings_and_announce_flag_round_trip(self):
        milestone = self.milestone("launch")
        db.set_board(G, 555, 3, 14, "t")
        db.set_announce_on_close(milestone, True)
        self.assertEqual(db.all_board_guilds()[0]["board_channel"], 555)
        self.assertIsNone(db.announce_fired(G))
        db.complete_milestone(milestone, user_id=7)
        self.assertIsNotNone(db.announce_fired(G))
        db.mark_board_sent(G)
        self.assertIsNone(db.announce_fired(G))
