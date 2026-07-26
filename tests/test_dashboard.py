import json
import os
import tempfile
import unittest
from pathlib import Path

import dashboard
import db


class DashboardStateTest(unittest.TestCase):
    def setUp(self):
        self.old_db = dashboard.DB_PATH
        self.old_guild = os.environ.get("TRACKER_GUILD_ID")
        self.old_admin_token = os.environ.get("TRACKER_ADMIN_TOKEN")
        self.temp = tempfile.TemporaryDirectory()
        dashboard.DB_PATH = Path(self.temp.name) / "tracker.db"
        os.environ["TRACKER_GUILD_ID"] = "42"
        db.connect(dashboard.DB_PATH)

    def tearDown(self):
        if db._conn is not None:
            db._conn.close()
            db._conn = None
        dashboard.DB_PATH = self.old_db
        if self.old_guild is None:
            os.environ.pop("TRACKER_GUILD_ID", None)
        else:
            os.environ["TRACKER_GUILD_ID"] = self.old_guild
        if self.old_admin_token is None:
            os.environ.pop("TRACKER_ADMIN_TOKEN", None)
        else:
            os.environ["TRACKER_ADMIN_TOKEN"] = self.old_admin_token
        self.temp.cleanup()

    def test_private_milestones_do_not_leave_the_database(self):
        project_id = db.create_project(42, "Public project", "", 1)
        public_id = db.create_milestone(42, "public", "Public milestone")
        private_id = db.create_milestone(42, "private", "Private milestone", private=True)
        tree_id = db.create_tree(42, "roadmap", "Roadmap")
        db.set_milestone_tags(public_id, grp="Demo", region="Global", team="Leadership")
        db.link_project(public_id, project_id)
        db.add_to_tree(tree_id, public_id)
        db.add_to_tree(tree_id, private_id)

        state = dashboard.public_state()

        self.assertEqual([node["key"] for node in state["milestones"]], ["public"])
        self.assertEqual(state["trees"][0]["members"], ["public"])
        self.assertEqual(state["milestones"][0]["group"], "Demo")

    def test_project_linked_to_private_work_is_not_public(self):
        project_id = db.create_project(42, "Private project", "Do not publish", 1)
        db.add_task(project_id, "Private task", assignee_id=123)
        private_id = db.create_milestone(42, "private", "Private milestone", private=True)
        db.link_project(private_id, project_id)

        state = dashboard.public_state()

        self.assertEqual(state["projects"], [])
        self.assertEqual(state["tasks"], [])
        self.assertNotIn("Private project", json.dumps(state))
        self.assertNotIn("Private task", dashboard.task_report(state).decode())

    def test_reports_include_work_without_discord_user_ids(self):
        project_id = db.create_project(42, "Website", "Ship the dashboard", 1, difficulty=6)
        db.set_project_tags(project_id, grp="Product", region="North")
        db.add_task(project_id, "Publish it", assignee_id=987654321, due_date="2026-08-01", weight=2)

        state = dashboard.public_state()
        task_csv = dashboard.task_report(state).decode()

        self.assertEqual(state["tasks"][0]["assigned"], True)
        self.assertEqual(state["projects"][0]["difficulty"], 6.0)
        self.assertEqual(state["projects"][0]["group"], "Product")
        self.assertIn("Product", state["filters"]["groups"])
        self.assertNotIn("assignee_id", json.dumps(state))
        self.assertIn("Website,Publish it,todo,2026-08-01,2,yes", task_csv)

    def test_dashboard_links_to_both_offline_helpers(self):
        self.assertIn('/tools/planner.html', dashboard.PAGE)
        self.assertIn('/tools/config_panel.html', dashboard.PAGE)
        self.assertTrue((dashboard.ROOT / "planner.html").is_file())
        self.assertTrue((dashboard.ROOT / "config_panel.html").is_file())

    def test_dashboard_includes_the_bev_idle_animation(self):
        self.assertIn('/assets/prophet-bev-bubble-monkey-run.webp', dashboard.PAGE)
        self.assertIn('/assets/prophet-bev-bubble-monkey-wave.webp', dashboard.PAGE)
        self.assertIn('.bev:hover .bev-wave', dashboard.PAGE)
        self.assertTrue((dashboard.ROOT / "assets" / "prophet-bev-bubble-monkey-run.webp").is_file())
        self.assertTrue((dashboard.ROOT / "assets" / "prophet-bev-bubble-monkey-wave.webp").is_file())

    def test_dashboard_includes_expandable_work_details(self):
        self.assertIn('function showMilestone', dashboard.PAGE)
        self.assertIn('function projectCard', dashboard.PAGE)
        self.assertIn('function taskCard', dashboard.PAGE)
        self.assertIn('tree-detail', dashboard.PAGE)

    def test_admin_state_and_live_updates_are_scoped_to_the_tracker_guild(self):
        project_id = db.create_project(42, "Website", "Old copy", 1)
        task_id = db.add_task(project_id, "Draft", due_date="2026-08-01", weight=1)
        milestone_id = db.create_milestone(42, "launch", "Launch", description="Old milestone")
        db.link_project(milestone_id, project_id)
        other_project = db.create_project(99, "Other guild", "", 1)

        state = dashboard.admin_state()
        self.assertEqual(state["projects"][0]["tasks"][0]["id"], task_id)
        self.assertEqual(state["milestones"][0]["projects"], ["Website"])

        dashboard.apply_admin_update({
            "kind": "project", "id": project_id, "name": "Website refresh",
            "description": "New copy", "status": "active", "difficulty": 7,
            "group": "Product", "region": "Global", "team": "Web",
        })
        dashboard.apply_admin_update({
            "kind": "task", "id": task_id, "title": "Publish", "status": "doing",
            "assignee_id": "123", "due_date": "2026-08-03", "weight": 4,
        })
        dashboard.apply_admin_update({
            "kind": "milestone", "id": milestone_id, "name": "Launch day",
            "description": "New milestone", "unlocks": "Public release", "xp": 250,
            "difficulty": 8, "private": True, "auto_close": False,
            "group": "Product", "region": "Global", "team": "Web",
        })

        project = db.get_project(42, "Website refresh")
        task = db.get_task(42, task_id)
        milestone = db.get_milestone(42, "launch")
        self.assertEqual((project["description"], project["difficulty"], project["grp"]),
                         ("New copy", 7, "Product"))
        self.assertEqual((task["title"], task["status"], task["assignee_id"], task["weight"]),
                         ("Publish", "doing", 123, 4))
        self.assertEqual((milestone["name"], milestone["xp"], milestone["private"], milestone["auto_close"]),
                         ("Launch day", 250, 1, 0))
        self.assertIsNotNone(db.get_project(99, "Other guild"))
        self.assertEqual(other_project, db.get_project(99, "Other guild")["id"])

    def test_admin_updates_reject_invalid_task_status(self):
        project_id = db.create_project(42, "Website", "", 1)
        task_id = db.add_task(project_id, "Draft")
        with self.assertRaisesRegex(ValueError, "Task status"):
            dashboard.apply_admin_update({
                "kind": "task", "id": task_id, "title": "Draft", "status": "magic",
                "assignee_id": "", "due_date": "", "weight": 1,
            })

    def test_admin_token_must_match_exactly(self):
        os.environ["TRACKER_ADMIN_TOKEN"] = "right-token-with-enough-length"
        self.assertTrue(dashboard.admin_authorized({"Authorization": "Bearer right-token-with-enough-length"}))
        self.assertFalse(dashboard.admin_authorized({"Authorization": "Bearer wrong-token-with-enough-length"}))
        self.assertFalse(dashboard.admin_authorized({}))

    def test_admin_editor_has_shared_search_and_tag_filters(self):
        page = (dashboard.ROOT / "admin.html").read_text(encoding="utf-8")
        self.assertIn('id="admin-search"', page)
        self.assertIn('id="admin-group"', page)
        self.assertIn('id="admin-region"', page)
        self.assertIn("function render()", page)

    def test_admin_can_wire_milestones_and_reject_a_cycle_without_partial_edit(self):
        project_id = db.create_project(42, "Website", "", 1)
        first_id = db.create_milestone(42, "first", "First")
        second_id = db.create_milestone(42, "second", "Second")
        tree_id = db.create_tree(42, "roadmap", "Roadmap")
        base = {"description": "", "unlocks": "", "xp": 100, "difficulty": 1,
                "private": False, "auto_close": True, "group": "Universal",
                "region": "Universal", "team": "Universal"}
        dashboard.apply_admin_update({**base, "kind": "milestone", "id": second_id,
                                      "name": "Second", "project_ids": [project_id],
                                      "prerequisite_ids": [first_id], "tree_ids": [tree_id]})
        state = dashboard.admin_state()
        second = next(item for item in state["milestones"] if item["id"] == second_id)
        self.assertEqual((second["project_ids"], second["prerequisite_ids"], second["tree_ids"]),
                         ([project_id], [first_id], [tree_id]))
        with self.assertRaisesRegex(ValueError, "cycle"):
            dashboard.apply_admin_update({**base, "kind": "milestone", "id": first_id,
                                          "name": "Should not save", "project_ids": [],
                                          "prerequisite_ids": [second_id], "tree_ids": []})
        self.assertEqual(db.get_milestone(42, "first")["name"], "First")

    def test_admin_can_create_and_delete_live_work(self):
        dashboard.apply_admin_update({"kind": "create_project", "name": "New project",
                                      "description": "", "owner_id": 123, "difficulty": 2})
        project = db.get_project(42, "New project")
        dashboard.apply_admin_update({"kind": "create_task", "project_id": project["id"],
                                      "title": "New task", "status": "todo", "assignee_id": "",
                                      "due_date": "", "weight": 1})
        task = db.list_tasks(project["id"])[0]
        dashboard.apply_admin_update({"kind": "delete", "target": "task", "id": task["id"],
                                      "confirm": "DELETE"})
        self.assertEqual(db.list_tasks(project["id"]), [])

    def test_admin_can_save_core_server_settings(self):
        db.create_tree(42, "roadmap", "Roadmap")
        dashboard.apply_admin_update({
            "kind": "settings", "signoff_role": "123", "universal_role": "456", "layout": "tb",
            "digest_channel": "111", "digest_weekday": 2, "digest_hour": 15,
            "board_channel": "222", "board_weekday": 3, "board_hour": 16, "board_tree": "roadmap",
            "stale_channel": "333", "stale_days": 5, "stale_roles": [789],
        })
        settings = db.get_settings(42)
        self.assertEqual((settings["layout"], settings["signoff_role"], settings["board_tree"], settings["stale_days"]),
                         ("tb", 123, "roadmap", 5))
        self.assertEqual(db.stale_alert_settings(42)["roles"], [789])


if __name__ == "__main__":
    unittest.main()
