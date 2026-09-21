import os
import tempfile
import unittest

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret"

from app import Item, User, app, db


class ClassFindTestCase(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.client = app.test_client()
        with app.app_context():
            db.drop_all()
            db.create_all()

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.drop_all()

    def register(self, name="Alice", email="alice@example.com", password="secret123"):
        return self.client.post(
            "/register",
            data={"name": name, "email": email, "password": password},
            follow_redirects=True,
        )

    def login(self, email="alice@example.com", password="secret123"):
        return self.client.post(
            "/login",
            data={"email": email, "password": password},
            follow_redirects=True,
        )

    def test_public_home_and_auth_pages(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/login").status_code, 200)
        self.assertEqual(self.client.get("/register").status_code, 200)
        self.assertEqual(self.client.get("/matches").status_code, 200)

    def test_registration_report_search_match_and_resolution(self):
        response = self.register()
        self.assertEqual(response.status_code, 200)

        with app.app_context():
            user = User.query.filter_by(email="alice@example.com").first()
            self.assertIsNotNone(user)
            self.assertTrue(user.is_admin)

        response = self.client.post(
            "/report",
            data={
                "title": "Black wallet",
                "description": "Black leather wallet with student ID",
                "category": "Wallet & ID",
                "location": "BIT Library",
                "status": "Lost",
                "image_url": "",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Black wallet", response.data)

        self.client.post(
            "/logout",
            follow_redirects=True,
        )

        self.register("Bob", "bob@example.com", "secret123")
        self.client.post(
            "/report",
            data={
                "title": "Black leather wallet",
                "description": "Wallet found near the library with a student ID",
                "category": "Wallet & ID",
                "location": "BIT Library",
                "status": "Found",
                "image_url": "",
            },
            follow_redirects=True,
        )

        response = self.client.get("/?q=wallet&status=Found")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Black leather wallet", response.data)

        response = self.client.get("/matches")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Potential item matches", response.data)

        with app.app_context():
            lost = Item.query.filter_by(status="Lost").first()
            found = Item.query.filter_by(status="Found").first()
            self.assertIsNotNone(lost)
            self.assertIsNotNone(found)

        self.login("bob@example.com")
        response = self.client.post(f"/item/{lost.id}/resolve", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"only manage your own reports", response.data)

        self.client.post("/logout", follow_redirects=True)
        self.login("alice@example.com")
        response = self.client.post(f"/item/{lost.id}/resolve", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        with app.app_context():
            self.assertEqual(db.session.get(Item, lost.id).status, "Resolved")

    def test_admin_dashboard_and_delete(self):
        self.register()
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"ClassFind control room", response.data)

        self.client.post(
            "/report",
            data={
                "title": "Keys",
                "description": "Silver keys",
                "category": "Keys",
                "location": "Lab 2",
                "status": "Found",
                "image_url": "",
            },
            follow_redirects=True,
        )

        with app.app_context():
            item = Item.query.filter_by(title="Keys").first()
            item_id = item.id

        response = self.client.post(f"/item/{item_id}/delete", follow_redirects=True)
        self.assertEqual(response.status_code, 200)

        with app.app_context():
            self.assertIsNone(db.session.get(Item, item_id))

    def test_login_rejects_bad_password(self):
        self.register()
        self.client.post("/logout", follow_redirects=True)
        response = self.client.post(
            "/login",
            data={"email": "alice@example.com", "password": "wrongpass"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Incorrect email or password", response.data)


if __name__ == "__main__":
    unittest.main()
