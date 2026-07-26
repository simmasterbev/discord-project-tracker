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
        self.temp.cleanup()

    def test_private_milestones_do_not_leave_the_database(self):
        project_id = db.create_project(42, "Public project", "", 1)
        public_id = db.create_milestone(42, "public", "Public milestone")
        private_id = db.create_milestone(42, "private", "Private milestone", private=True)
        tree_id = db.create_tree(42, "roadmap", "Roadmap")
        db.link_project(public_id, project_id)
        db.add_to_tree(tree_id, public_id)
        db.add_to_tree(tree_id, private_id)

        state = dashboard.public_state()

        self.assertEqual([node["key"] for node in state["milestones"]], ["public"])
        self.assertEqual(state["trees"][0]["members"], ["public"])


if __name__ == "__main__":
    unittest.main()
