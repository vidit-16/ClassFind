import os
import shutil
import tempfile
import unittest
from io import BytesIO

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret"

from app import Claim, Item, User, app, db


class ClassFindTestCase(unittest.TestCase):
    def setUp(self):
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        self.upload_dir = tempfile.mkdtemp()
        app.config["UPLOAD_FOLDER"] = self.upload_dir
        self.client = app.test_client()
        with app.app_context():
            db.drop_all()
            db.create_all()

    def tearDown(self):
        with app.app_context():
            db.session.remove()
            db.drop_all()
        shutil.rmtree(self.upload_dir, ignore_errors=True)

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

    def report(self, title, description, category, location, status, image_url=""):
        return self.client.post(
            "/report",
            data={
                "title": title,
                "description": description,
                "category": category,
                "location": location,
                "status": status,
                "image_url": image_url,
            },
            follow_redirects=True,
        )

    def test_public_home_and_auth_pages(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/login").status_code, 200)
        self.assertEqual(self.client.get("/register").status_code, 200)
        self.assertEqual(self.client.get("/matches").status_code, 200)
        self.assertEqual(self.client.get("/claims").status_code, 302)

    def test_registration_report_search_match_and_resolution(self):
        response = self.register()
        self.assertEqual(response.status_code, 200)

        with app.app_context():
            user = User.query.filter_by(email="alice@example.com").first()
            self.assertIsNotNone(user)
            self.assertTrue(user.is_admin)

        response = self.report(
            "Black wallet",
            "Black leather wallet with student ID",
            "Wallet & ID",
            "BIT Library",
            "Lost",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Black wallet", response.data)

        self.client.post("/logout", follow_redirects=True)

        self.register("Bob", "bob@example.com", "secret123")
        self.report(
            "Black leather wallet",
            "Wallet found near the library with a student ID",
            "Wallet & ID",
            "BIT Library",
            "Found",
        )

        response = self.client.get("/?q=wallet&status=Found&sort=oldest")
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

    def test_edit_and_image_upload(self):
        self.register()
        self.report(
            "Blue bottle",
            "Metal bottle with sticker",
            "Accessories",
            "Lab 1",
            "Lost",
        )
        with app.app_context():
            item = Item.query.filter_by(title="Blue bottle").first()
            item_id = item.id

        response = self.client.get(f"/item/{item_id}/edit")
        self.assertEqual(response.status_code, 200)

        response = self.client.post(
            f"/item/{item_id}/edit",
            data={
                "title": "Blue steel bottle",
                "description": "Steel bottle with a mountain sticker",
                "category": "Accessories",
                "location": "Lab 3",
                "status": "Lost",
                "image_file": (BytesIO(b"fake-image-data"), "bottle.png"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Blue steel bottle", response.data)

        with app.app_context():
            item = db.session.get(Item, item_id)
            self.assertEqual(item.location, "Lab 3")
            self.assertTrue(item.image_url.startswith("/static/uploads/"))
            stored_name = os.path.basename(item.image_url)

        self.assertTrue(os.path.exists(os.path.join(self.upload_dir, stored_name)))

    def test_claim_workflow(self):
        self.register("Finder", "finder@example.com")
        self.report(
            "Black AirPods",
            "Black earbuds in a charging case",
            "Electronics",
            "Library",
            "Found",
        )
        with app.app_context():
            found = Item.query.filter_by(title="Black AirPods").first()
            found_id = found.id

        self.client.post("/logout", follow_redirects=True)
        self.register("Owner", "owner@example.com")

        response = self.client.post(
            f"/item/{found_id}/claim",
            data={"message": "I lost these after studying in the library."},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Claim submitted", response.data)

        with app.app_context():
            claim = Claim.query.first()
            claim_id = claim.id
            self.assertEqual(claim.status, "Pending")

        self.client.post("/logout", follow_redirects=True)
        self.login("finder@example.com")

        response = self.client.post(
            f"/claims/{claim_id}/accept",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Claim accepted", response.data)

        with app.app_context():
            claim = db.session.get(Claim, claim_id)
            self.assertEqual(claim.status, "Accepted")
            self.assertEqual(db.session.get(Item, found_id).status, "Resolved")

    def test_admin_dashboard_and_delete(self):
        self.register()
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"ClassFind control room", response.data)

        self.report(
            "Keys",
            "Silver keys",
            "Keys",
            "Lab 2",
            "Found",
        )

        with app.app_context():
            item = Item.query.filter_by(title="Keys").first()
            item_id = item.id

        filtered = self.client.get("/admin?q=Keys&status=Found")
        self.assertEqual(filtered.status_code, 200)
        self.assertIn(b"Keys", filtered.data)

        response = self.client.post(f"/item/{item_id}/delete", follow_redirects=True)
        self.assertEqual(response.status_code, 200)

        with app.app_context():
            self.assertIsNone(db.session.get(Item, item_id))

    def test_withdraw_pending_claim(self):
        self.register("Finder", "finder2@example.com")
        self.report(
            "Green bottle",
            "Green bottle found outside the lab",
            "Accessories",
            "Lab 5",
            "Found",
        )
        with app.app_context():
            found = Item.query.filter_by(title="Green bottle").first()
            found_id = found.id

        self.client.post("/logout", follow_redirects=True)
        self.register("Owner", "owner2@example.com")
        response = self.client.post(
            f"/item/{found_id}/claim",
            data={"message": "I lost this green bottle near the lab yesterday."},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)

        with app.app_context():
            claim = Claim.query.filter_by(item_id=found_id).first()
            self.assertIsNotNone(claim)
            claim_id = claim.id

        response = self.client.post(
            f"/claims/{claim_id}/withdraw",
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"claim was withdrawn", response.data)

        with app.app_context():
            self.assertIsNone(db.session.get(Claim, claim_id))


    def test_delete_found_item_cascades_claims(self):
        self.register("Finder", "finder@example.com")
        self.report(
            "Black bag",
            "Black backpack found in the lab",
            "Accessories",
            "Lab 4",
            "Found",
        )
        with app.app_context():
            found = Item.query.filter_by(title="Black bag").first()
            found_id = found.id

        self.client.post("/logout", follow_redirects=True)
        self.register("Owner", "owner@example.com")
        self.client.post(
            f"/item/{found_id}/claim",
            data={"message": "I lost this backpack in the lab yesterday."},
            follow_redirects=True,
        )

        with app.app_context():
            claim = Claim.query.first()
            self.assertIsNotNone(claim)
            claim_id = claim.id

        self.client.post("/logout", follow_redirects=True)
        self.login("finder@example.com")
        response = self.client.post(f"/item/{found_id}/delete", follow_redirects=True)
        self.assertEqual(response.status_code, 200)

        with app.app_context():
            self.assertIsNone(db.session.get(Item, found_id))
            self.assertIsNone(db.session.get(Claim, claim_id))

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
