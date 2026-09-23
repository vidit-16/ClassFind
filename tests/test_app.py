import os
import shutil
import tempfile
import unittest
from io import BytesIO

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret"

from app import (
    MATCH_THRESHOLD,
    PAGE_SIZE,
    Claim,
    Item,
    User,
    app,
    build_matches,
    cached_matches,
    db,
    overlap,
    resolve_secret_key,
    tokens,
)


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

    def csrf_token(self):
        self.client.get("/")
        with self.client.session_transaction() as session:
            return session["_csrf_token"]

    def post(self, url, data=None, **kwargs):
        payload = {} if data is None else dict(data)
        payload.setdefault("csrf_token", self.csrf_token())
        return self.client.post(url, data=payload, **kwargs)

    def register(self, name="Alice", email="alice@example.com", password="secret123"):
        return self.post(
            "/register",
            data={"name": name, "email": email, "password": password},
            follow_redirects=True,
        )

    def register_admin(self, email="alice@example.com"):
        """Register the one account ADMIN_EMAIL names, so it holds admin rights."""
        os.environ["ADMIN_EMAIL"] = email
        self.addCleanup(os.environ.pop, "ADMIN_EMAIL", None)
        return self.register(email=email)

    def login(self, email="alice@example.com", password="secret123"):
        return self.post(
            "/login",
            data={"email": email, "password": password},
            follow_redirects=True,
        )

    def report(self, title, description, category, location, status, image_url=""):
        return self.post(
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

    def test_csrf_protection_and_security_headers(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        response = self.client.post("/logout", follow_redirects=True)
        self.assertEqual(response.status_code, 400)
        with self.client.session_transaction() as session:
            token = session.get("_csrf_token")
        self.assertIsNotNone(token)
        response = self.client.post(
            "/logout",
            data={"csrf_token": "invalid"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 400)

        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["database"], "ok")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

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
            self.assertFalse(user.is_admin)

        response = self.report(
            "Black wallet",
            "Black leather wallet with student ID",
            "Wallet & ID",
            "BIT Library",
            "Lost",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Black wallet", response.data)

        self.post("/logout", follow_redirects=True)

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
        self.assertIn(b"Potential connections", response.data)

        with app.app_context():
            lost = Item.query.filter_by(status="Lost").first()
            found = Item.query.filter_by(status="Found").first()
            self.assertIsNotNone(lost)
            self.assertIsNotNone(found)

        self.login("bob@example.com")
        response = self.post(f"/item/{lost.id}/resolve", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"only manage your own reports", response.data)

        self.post("/logout", follow_redirects=True)
        self.login("alice@example.com")
        response = self.post(f"/item/{lost.id}/resolve", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        with app.app_context():
            self.assertEqual(db.session.get(Item, lost.id).status, "Resolved")

    def test_bad_image_url_is_rejected(self):
        self.register()
        response = self.post(
            "/report",
            data={
                "title": "Unsafe image",
                "description": "Testing URL validation",
                "category": "Other",
                "location": "Lab 1",
                "status": "Lost",
                "image_url": "javascript:alert(1)",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Image URL must start with http:// or https://.", response.data)

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

        response = self.post(
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

        self.post("/logout", follow_redirects=True)
        self.register("Owner", "owner@example.com")

        response = self.post(
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

        self.post("/logout", follow_redirects=True)
        self.login("finder@example.com")

        response = self.post(
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
        self.register_admin()
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"ADMIN CONTROL ROOM", response.data)

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

        response = self.post(f"/item/{item_id}/delete", follow_redirects=True)
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

        self.post("/logout", follow_redirects=True)
        self.register("Owner", "owner2@example.com")
        response = self.post(
            f"/item/{found_id}/claim",
            data={"message": "I lost this green bottle near the lab yesterday."},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)

        with app.app_context():
            claim = Claim.query.filter_by(item_id=found_id).first()
            self.assertIsNotNone(claim)
            claim_id = claim.id

        response = self.post(
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

        self.post("/logout", follow_redirects=True)
        self.register("Owner", "owner@example.com")
        self.post(
            f"/item/{found_id}/claim",
            data={"message": "I lost this backpack in the lab yesterday."},
            follow_redirects=True,
        )

        with app.app_context():
            claim = Claim.query.first()
            self.assertIsNotNone(claim)
            claim_id = claim.id

        self.post("/logout", follow_redirects=True)
        self.login("finder@example.com")
        response = self.post(f"/item/{found_id}/delete", follow_redirects=True)
        self.assertEqual(response.status_code, 200)

        with app.app_context():
            self.assertIsNone(db.session.get(Item, found_id))
            self.assertIsNone(db.session.get(Claim, claim_id))

    def test_first_account_is_not_an_admin(self):
        self.register()
        self.assertEqual(self.client.get("/admin").status_code, 302)
        with app.app_context():
            self.assertFalse(User.query.filter_by(email="alice@example.com").first().is_admin)

    def test_only_the_named_account_becomes_admin(self):
        os.environ["ADMIN_EMAIL"] = "owner@example.com"
        self.addCleanup(os.environ.pop, "ADMIN_EMAIL", None)

        self.register(name="Alice", email="alice@example.com")
        self.post("/logout", follow_redirects=True)
        self.register(name="Owner", email="owner@example.com")

        with app.app_context():
            self.assertFalse(User.query.filter_by(email="alice@example.com").first().is_admin)
            self.assertTrue(User.query.filter_by(email="owner@example.com").first().is_admin)
        self.assertEqual(self.client.get("/admin").status_code, 200)

    def test_admin_email_set_after_the_account_exists_applies_on_sign_in(self):
        self.register(name="Owner", email="owner@example.com")
        self.post("/logout", follow_redirects=True)

        os.environ["ADMIN_EMAIL"] = "owner@example.com"
        self.addCleanup(os.environ.pop, "ADMIN_EMAIL", None)
        self.login(email="owner@example.com")

        self.assertEqual(self.client.get("/admin").status_code, 200)

    def test_home_page_shows_one_page_at_a_time(self):
        self.register()
        with app.app_context():
            owner = User.query.filter_by(email="alice@example.com").first()
            for number in range(PAGE_SIZE + 4):
                db.session.add(Item(
                    title=f"Bottle {number}",
                    description="Steel water bottle",
                    category="Other",
                    location="Canteen",
                    status="Lost",
                    reporter_name=owner.name,
                    contact=owner.email,
                    owner_id=owner.id,
                ))
            db.session.commit()

        first = self.client.get("/").data
        self.assertEqual(first.count(b"View report"), PAGE_SIZE)
        self.assertIn(b"Page 1 of 2", first)

        second = self.client.get("/?page=2").data
        self.assertEqual(second.count(b"View report"), 4)
        self.assertIn(b"Page 2 of 2", second)

    def test_a_second_account_cannot_edit_someone_elses_report(self):
        self.register(name="Alice", email="alice@example.com")
        self.report("Blue umbrella", "Blue folding umbrella", "Other", "Gate 2", "Lost")
        self.post("/logout", follow_redirects=True)

        self.register(name="Bob", email="bob@example.com")
        response = self.post(
            "/item/1/edit",
            data={
                "title": "Mine now",
                "description": "Taken over",
                "category": "Other",
                "location": "Gate 2",
                "status": "Lost",
            },
            follow_redirects=True,
        )
        self.assertIn(b"You can only edit your own reports.", response.data)
        with app.app_context():
            self.assertEqual(db.session.get(Item, 1).title, "Blue umbrella")

    def test_reports_filed_before_owner_id_existed_stay_editable(self):
        """Rows from the old schema carry NULL, and fall back to the contact field."""
        self.register(name="Alice", email="alice@example.com")
        with app.app_context():
            db.session.add(Item(
                title="Old report",
                description="Filed before the column existed",
                category="Other",
                location="Library",
                status="Lost",
                reporter_name="Alice",
                contact="alice@example.com",
                owner_id=None,
            ))
            db.session.commit()

        response = self.post(
            "/item/1/resolve", follow_redirects=True
        )
        self.assertIn(b"marked as resolved", response.data)

    def test_faster_matching_returns_what_the_plain_version_would(self):
        """The early exit only skips pairs that could not have cleared the cut-off."""
        self.register()
        seeds = [
            ("Black wallet", "Black leather wallet with student ID", "Wallet & ID", "Library", "Lost"),
            ("Wallet found", "Black leather wallet near the library steps", "Wallet & ID", "Library", "Found"),
            ("Blue bottle", "Steel blue water bottle", "Other", "Canteen", "Lost"),
            ("Bottle", "Blue steel bottle left on a table", "Other", "Canteen", "Found"),
            ("Calculator", "Casio scientific calculator", "Electronics", "Lab 2", "Lost"),
            ("Umbrella", "Red umbrella", "Other", "Hostel", "Found"),
        ]
        for title, description, category, location, status in seeds:
            self.report(title, description, category, location, status)

        with app.app_context():
            fast = [(lost.id, found.id, score) for lost, found, score, _ in build_matches()]
            self.assertEqual(fast, self.reference_matches())

    @staticmethod
    def reference_matches():
        """Score every pair the long way, with no early exit."""
        from difflib import SequenceMatcher

        pairs = []
        for lost in Item.query.filter_by(status="Lost").all():
            for found in Item.query.filter_by(status="Found").all():
                lost_text = tokens(f"{lost.title} {lost.description}")
                found_text = tokens(f"{found.title} {found.description}")
                days_apart = abs((lost.created_at - found.created_at).total_seconds()) / 86400
                score = (
                    overlap(lost_text, found_text) * 0.40
                    + SequenceMatcher(None, lost.title.lower(), found.title.lower()).ratio() * 0.25
                    + overlap(tokens(lost.location), tokens(found.location)) * 0.15
                    + max(0.0, 1.0 - min(days_apart / 14.0, 1.0)) * 0.10
                    + (0.10 if lost.category.lower() == found.category.lower() else 0.0)
                )
                score = min(score, 0.99)
                if score >= MATCH_THRESHOLD:
                    pairs.append((lost.id, found.id, round(score * 100)))
        pairs.sort(key=lambda pair: pair[2], reverse=True)
        return pairs[:30]

    def test_matches_are_reused_until_a_report_changes(self):
        self.register()
        self.report("Black wallet", "Leather wallet", "Wallet & ID", "Library", "Lost")
        self.report("Wallet", "Leather wallet found", "Wallet & ID", "Library", "Found")

        with app.app_context():
            first = cached_matches()
            self.assertTrue(first)
            self.assertIs(cached_matches(), first)

        self.report("Blue bottle", "Steel bottle", "Other", "Canteen", "Found")
        with app.app_context():
            self.assertIsNot(cached_matches(), first)

    def test_login_rejects_bad_password(self):
        self.register()
        self.post("/logout", follow_redirects=True)
        response = self.post(
            "/login",
            data={"email": "alice@example.com", "password": "wrongpass"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Incorrect email or password", response.data)


if __name__ == "__main__":
    unittest.main()


class SecretKeyTestCase(unittest.TestCase):
    """The key that signs sessions is never a value committed to the repository."""

    def test_configured_key_is_used(self):
        self.assertEqual(resolve_secret_key({"SECRET_KEY": "from-the-environment"}),
                         "from-the-environment")

    def test_production_without_a_key_refuses_to_start(self):
        with self.assertRaises(RuntimeError):
            resolve_secret_key({"CLASSFIND_ENV": "production"})
        with self.assertRaises(RuntimeError):
            resolve_secret_key({"CLASSFIND_ENV": "production", "SECRET_KEY": "   "})

    def test_development_without_a_key_gets_a_random_one(self):
        first = resolve_secret_key({})
        second = resolve_secret_key({})
        self.assertNotEqual(first, second)
        self.assertGreater(len(first), 20)
