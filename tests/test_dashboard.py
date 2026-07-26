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


if __name__ == "__main__":
    unittest.main()
