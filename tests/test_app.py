import json
import os
import urllib.error
import shutil
import tempfile
import unittest
from unittest import mock

from botocore.exceptions import ClientError
from datetime import datetime, timedelta
from io import BytesIO

os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["SECRET_KEY"] = "test-secret"

from app import (
    parse_report_with_model,
    Tag,
    send_email,
    dashboard_insights,
    clean_parsed_report,
    escalate_unclaimed_valuables,
    parse_report_rules,
    CustodyEvent,
    add_desk_columns,
    add_place_columns,
    StaffInvite,
    _rate_hits,
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
        app.config.update(TESTING=True, WTF_CSRF_ENABLED=False, RATE_LIMITS_ENABLED=False)
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

    def report(self, title, description, category, location, status):
        return self.post(
            "/report",
            data={
                "title": title,
                "description": description,
                "category": category,
                "location": location,
                "status": status,
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
        self.assertEqual(response.get_json()["engine"], "sqlite")
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])

    def test_force_https_redirects_plain_http(self):
        app.config["FORCE_HTTPS"] = True
        self.addCleanup(app.config.update, FORCE_HTTPS=False)

        response = self.client.get("/login?next=/report")
        self.assertEqual(response.status_code, 301)
        self.assertEqual(response.headers["Location"], "https://localhost/login?next=/report")

        response = self.client.post("/login", data={"email": "a@b.co", "password": "x"})
        self.assertEqual(response.status_code, 308)

        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/login", base_url="https://localhost").status_code, 200)

    def test_https_behind_the_proxy_is_not_redirected(self):
        # nginx forwards HTTPS to the app over HTTP with this header. Without
        # ProxyFix every such request was redirected to itself, forever.
        app.config["FORCE_HTTPS"] = True
        self.addCleanup(app.config.update, FORCE_HTTPS=False)

        response = self.client.get("/login", headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Strict-Transport-Security", response.headers)

    def enable_rate_limits(self):
        app.config["RATE_LIMITS_ENABLED"] = True
        _rate_hits.clear()
        self.addCleanup(app.config.update, RATE_LIMITS_ENABLED=False)
        self.addCleanup(_rate_hits.clear)

    def test_repeated_failed_logins_are_limited(self):
        self.enable_rate_limits()
        bad = {"email": "nobody@example.com", "password": "wrong"}
        for _ in range(10):
            self.assertEqual(self.post("/login", data=bad).status_code, 200)
        response = self.post("/login", data=bad)
        self.assertEqual(response.status_code, 429)
        self.assertIn(b"Too many attempts", response.data)
        # Viewing the page is not a POST, so it is never limited.
        self.assertEqual(self.client.get("/login").status_code, 200)

    def test_rate_limits_are_counted_per_client_address(self):
        self.enable_rate_limits()
        bad = {"email": "nobody@example.com", "password": "wrong"}
        for _ in range(10):
            self.post("/login", data=bad, headers={"X-Forwarded-For": "10.0.0.1"})
        limited = self.post("/login", data=bad, headers={"X-Forwarded-For": "10.0.0.1"})
        other = self.post("/login", data=bad, headers={"X-Forwarded-For": "10.0.0.2"})
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(other.status_code, 200)

    def test_http_is_served_when_force_https_is_off(self):
        self.assertFalse(app.config["FORCE_HTTPS"])
        self.assertEqual(self.client.get("/login").status_code, 200)

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

    def test_contact_email_is_hidden_from_signed_out_visitors(self):
        self.register()
        self.report("Blue bottle", "Steel bottle", "Other", "Canteen", "Lost")
        with app.app_context():
            item_id = Item.query.filter_by(title="Blue bottle").first().id

        self.assertIn(b"alice@example.com", self.client.get(f"/item/{item_id}").data)

        self.post("/logout", follow_redirects=True)
        response = self.client.get(f"/item/{item_id}")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"alice@example.com", response.data)
        self.assertIn(b"Sign in to see contact", response.data)

    def test_a_posted_image_url_is_ignored(self):
        """The form no longer has the field, and a hand-made request cannot bring it back."""
        self.register()
        fields = {
            "title": "Linked image",
            "description": "Testing that links are not stored",
            "category": "Other",
            "location": "Lab 1",
            "status": "Lost",
            "image_url": "https://example.com/tracker.png",
        }
        response = self.post("/report", data=fields, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"example.com/tracker.png", response.data)

        with app.app_context():
            item = Item.query.filter_by(title="Linked image").first()
            self.assertIsNone(item.image_url)
            item_id = item.id

        self.post(f"/item/{item_id}/edit", data=fields, follow_redirects=True)
        with app.app_context():
            self.assertIsNone(db.session.get(Item, item_id).image_url)

        self.assertNotIn(b'name="image_url"', self.client.get("/report").data)
        self.assertNotIn(b'name="image_url"', self.client.get(f"/item/{item_id}/edit").data)

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

    def register_staff(self, email="desk@example.com"):
        """Register an account STAFF_EMAILS names, so it works the security desk."""
        os.environ["STAFF_EMAILS"] = email
        self.addCleanup(os.environ.pop, "STAFF_EMAILS", None)
        return self.register("Desk", email)

    def logout(self):
        self.post("/logout", follow_redirects=True)

    def found_item(self, title="Black AirPods", category="Accessories"):
        """A finder reports a found item and signs out. Returns the item id."""
        self.register("Finder", "finder@example.com")
        self.report(title, "Black earbuds in a charging case", category, "Library", "Found")
        self.logout()
        with app.app_context():
            return Item.query.filter_by(title=title).one().id

    def claim(self, item_id, message="These are mine, the case has a blue sticker inside."):
        return self.post(f"/item/{item_id}/claim", data={"message": message}, follow_redirects=True)

    def code_for(self, claim_id):
        with app.app_context():
            return db.session.get(Claim, claim_id).handover_code

    def test_found_items_go_through_the_security_desk(self):
        item_id = self.found_item()
        with app.app_context():
            item = db.session.get(Item, item_id)
            self.assertEqual(item.custody, "awaiting")
            self.assertEqual(len(item.events), 1)

        self.register("Owner", "owner@example.com")
        response = self.claim(item_id)
        self.assertIn(b"The security desk will review it", response.data)
        with app.app_context():
            claim_id = Claim.query.one().id

        # Neither the claimant nor the finder decides the claim.
        self.post(f"/claims/{claim_id}/accept", follow_redirects=True)
        self.logout()
        self.login("finder@example.com")
        self.post(f"/claims/{claim_id}/accept", follow_redirects=True)
        with app.app_context():
            self.assertEqual(db.session.get(Claim, claim_id).status, "Pending")
        self.assertNotIn(b"blue sticker", self.client.get("/claims").data)
        self.logout()

        self.register_staff()
        desk = self.client.get("/desk")
        self.assertEqual(desk.status_code, 200)
        self.assertIn(b"blue sticker", desk.data)
        self.post(f"/desk/item/{item_id}/received", follow_redirects=True)
        response = self.post(f"/claims/{claim_id}/accept", follow_redirects=True)
        self.assertIn(b"collection code", response.data)
        code = self.code_for(claim_id)
        self.assertRegex(code, r"^[0-9]{6}$")
        self.logout()

        # Only the claimant sees the code.
        self.login("owner@example.com")
        claims_page = self.client.get("/claims").data
        self.assertIn(f"{code[:3]} {code[3:]}".encode(), claims_page)
        self.logout()
        self.login("finder@example.com")
        self.assertNotIn(f"{code[:3]} {code[3:]}".encode(), self.client.get("/claims").data)
        self.logout()

        self.login("desk@example.com")
        wrong = "000000" if code != "000000" else "111111"
        response = self.post("/desk/handover", data={"code": wrong}, follow_redirects=True)
        self.assertIn(b"does not match", response.data)
        response = self.post("/desk/handover", data={"code": f"{code[:3]} {code[3:]}"}, follow_redirects=True)
        self.assertIn(b"Released Black AirPods to Owner", response.data)

        with app.app_context():
            claim = db.session.get(Claim, claim_id)
            item = db.session.get(Item, item_id)
            self.assertEqual(claim.status, "Collected")
            self.assertIsNone(claim.handover_code)
            self.assertEqual(claim.released_by.email, "desk@example.com")
            self.assertEqual(item.status, "Resolved")
            self.assertEqual(item.custody, "released")
            actions = [event.action for event in item.events]
        self.assertEqual(len(actions), 4)
        self.assertTrue(actions[-1].startswith("Released to Owner"))

        # The code works once.
        response = self.post("/desk/handover", data={"code": code}, follow_redirects=True)
        self.assertIn(b"does not match", response.data)

    def test_handover_waits_for_the_item_to_reach_the_desk(self):
        item_id = self.found_item()
        self.register("Owner", "owner@example.com")
        self.claim(item_id)
        self.logout()
        self.register_staff()
        with app.app_context():
            claim_id = Claim.query.one().id
        self.post(f"/claims/{claim_id}/accept", follow_redirects=True)
        response = self.post("/desk/handover", data={"code": self.code_for(claim_id)}, follow_redirects=True)
        self.assertIn(b"not been received", response.data)
        with app.app_context():
            self.assertEqual(db.session.get(Claim, claim_id).status, "Approved")

    def test_an_expired_code_is_refused_until_a_new_one_is_issued(self):
        item_id = self.found_item()
        self.register("Owner", "owner@example.com")
        self.claim(item_id)
        self.logout()
        self.register_staff()
        with app.app_context():
            claim_id = Claim.query.one().id
        self.post(f"/desk/item/{item_id}/received", follow_redirects=True)
        self.post(f"/claims/{claim_id}/accept", follow_redirects=True)
        old_code = self.code_for(claim_id)
        with app.app_context():
            claim = db.session.get(Claim, claim_id)
            claim.code_expires_at = datetime.utcnow() - timedelta(minutes=1)
            db.session.commit()
        response = self.post("/desk/handover", data={"code": old_code}, follow_redirects=True)
        self.assertIn(b"expired", response.data)

        self.post(f"/claims/{claim_id}/reissue", follow_redirects=True)
        new_code = self.code_for(claim_id)
        response = self.post("/desk/handover", data={"code": new_code}, follow_redirects=True)
        self.assertIn(b"Released", response.data)

    def test_valuable_items_need_a_stronger_claim(self):
        item_id = self.found_item("Grey phone", category="Electronics")
        self.register("Owner", "owner@example.com")
        response = self.claim(item_id, "It is my phone.")
        self.assertIn(b"serial number, IMEI", response.data)
        with app.app_context():
            self.assertEqual(Claim.query.count(), 0)
        self.claim(item_id, "IMEI ends in 4471 and the lock screen is a beach photo.")
        with app.app_context():
            self.assertEqual(Claim.query.count(), 1)

    def test_the_finder_lets_go_of_a_found_item_once_it_is_handed_in(self):
        item_id = self.found_item()
        self.login("finder@example.com")
        self.assertEqual(self.client.get(f"/item/{item_id}/edit").status_code, 200)
        self.logout()

        self.register_staff()
        self.post(f"/desk/item/{item_id}/received", follow_redirects=True)
        self.logout()

        self.login("finder@example.com")
        response = self.client.get(f"/item/{item_id}/edit", follow_redirects=True)
        self.assertIn(b"only edit your own reports", response.data)
        self.post(f"/item/{item_id}/delete", follow_redirects=True)
        with app.app_context():
            self.assertIsNotNone(db.session.get(Item, item_id))

    def test_staff_logging_an_item_puts_it_straight_into_custody(self):
        self.register_staff()
        self.report("Blue umbrella", "Folding umbrella", "Other", "Main gate", "Found")
        with app.app_context():
            item = Item.query.filter_by(title="Blue umbrella").one()
            self.assertEqual(item.custody, "held")
            self.assertEqual(item.events[0].action, "Logged at the security desk")

    def test_the_desk_is_for_staff_only(self):
        self.register("Student", "student@example.com")
        response = self.client.get("/desk", follow_redirects=True)
        self.assertIn(b"Security desk access is required", response.data)

    def test_desk_access_is_given_by_invitation_and_accepted(self):
        self.register("Student", "student@example.com")
        self.logout()
        self.register_admin("admin@example.com")
        with app.app_context():
            student_id = User.query.filter_by(email="student@example.com").one().id
        # The old one-click grant no longer gives access.
        self.post(f"/admin/users/{student_id}/staff", follow_redirects=True)
        with app.app_context():
            self.assertFalse(db.session.get(User, student_id).is_staff)
        with mock.patch("app.send_email") as sent:
            self.post("/admin/staff/invite", data={"email": "Student@Example.com"}, follow_redirects=True)
        self.assertEqual(sent.call_args.args[0], "student@example.com")
        with app.app_context():
            token = StaffInvite.query.one().token
            self.assertFalse(db.session.get(User, student_id).is_staff)
        self.assertIn(b"waiting for them to accept", self.client.get("/admin").data)
        self.logout()
        # Someone else signed in cannot answer it.
        self.register("Other", "other@example.com")
        self.post(f"/staff/invite/{token}", data={"answer": "accept"})
        self.logout()
        self.login("student@example.com")
        self.assertIn(b"invited you to work the security desk", self.client.get("/").data)
        self.post(f"/staff/invite/{token}", data={"answer": "accept"}, follow_redirects=True)
        with app.app_context():
            self.assertTrue(db.session.get(User, student_id).is_staff)
            self.assertFalse(User.query.filter_by(email="other@example.com").one().is_staff)
        self.assertNotIn(b"invited you to work the security desk", self.client.get("/").data)
        self.logout()
        self.login("admin@example.com")
        self.post(f"/admin/users/{student_id}/staff", follow_redirects=True)
        with app.app_context():
            self.assertFalse(db.session.get(User, student_id).is_staff)

    def test_an_invitation_expires_and_can_be_declined(self):
        self.register_admin("admin@example.com")
        with mock.patch("app.send_email"):
            self.post("/admin/staff/invite", data={"email": "late@example.com"})
            self.post("/admin/staff/invite", data={"email": "no@example.com"})
        with app.app_context():
            late = StaffInvite.query.filter_by(email="late@example.com").one()
            late.created_at = datetime.utcnow() - timedelta(days=8)
            db.session.commit()
            late_token = late.token
            no_token = StaffInvite.query.filter_by(email="no@example.com").one().token
        self.logout()
        self.register("Late", "late@example.com")
        self.post(f"/staff/invite/{late_token}", data={"answer": "accept"})
        self.logout()
        self.register("No", "no@example.com")
        self.post(f"/staff/invite/{no_token}", data={"answer": "decline"})
        with app.app_context():
            self.assertFalse(any(user.is_staff for user in User.query.all()))

    def test_a_report_is_put_on_the_map_from_its_location(self):
        self.register()
        self.report("Blue bottle", "Steel", "Accessories", "Mechanical parking, near the bikes", "Lost")
        self.report("Pen drive", "16 GB", "Electronics", "Xerox", "Found")
        with app.app_context():
            bottle = Item.query.filter_by(title="Blue bottle").one()
            self.assertEqual((bottle.place, bottle.place_side), ("mech-parking", "inside"))
            # "Xerox" could be either shop, so it is left for the map picker.
            self.assertIsNone(Item.query.filter_by(title="Pen drive").one().place)

    def test_a_place_picked_on_the_map_wins_over_the_text(self):
        self.register()
        self.post("/report", data={
            "title": "Umbrella", "description": "Black", "category": "Accessories",
            "location": "by the xerox", "status": "Found", "place": "xerox-mech", "place_side": "outside",
        })
        self.post("/report", data={
            "title": "Cap", "description": "Red", "category": "Clothing",
            "location": "Canteen", "status": "Found", "place": "not-a-place",
        })
        with app.app_context():
            umbrella = Item.query.filter_by(title="Umbrella").one()
            self.assertEqual((umbrella.place, umbrella.place_side), ("xerox-mech", "outside"))
            self.assertIsNone(Item.query.filter_by(title="Cap").one().place)

    def test_search_by_any_name_for_a_place(self):
        self.register()
        self.report("Blue bottle", "Steel", "Accessories", "Mech parking", "Lost")
        self.report("Red umbrella", "Folding", "Accessories", "Library", "Lost")
        def results(url):
            return self.client.get(url).data.split(b'id="results"', 1)[1]

        page = results("/?q=mechanical+parking")
        self.assertIn(b"Blue bottle", page)
        self.assertNotIn(b"Red umbrella", page)
        self.assertIn(b"Red umbrella", results("/?q=umbrella+cse+department"))
        page = results("/?place=mech-parking")
        self.assertIn(b"Blue bottle", page)
        self.assertNotIn(b"Red umbrella", page)

    def test_reports_at_the_same_place_match_on_location(self):
        self.register()
        self.report("Steel bottle", "Blue steel bottle with dents", "Accessories", "mech parking", "Lost")
        self.report("Steel bottle", "Blue steel bottle with dents", "Accessories", "garage", "Found")
        with app.app_context():
            (_, _, _, reasons), = build_matches()
            self.assertIn("similar location", reasons)

    def test_older_reports_are_placed_when_the_column_is_added(self):
        self.register()
        self.report("Old wallet", "Brown", "Wallet & ID", "near the canteen xerox", "Found")
        with app.app_context():
            db.session.execute(db.text("DROP INDEX ix_item_place"))
            db.session.execute(db.text("ALTER TABLE item DROP COLUMN place"))
            db.session.execute(db.text("ALTER TABLE item DROP COLUMN place_side"))
            db.session.commit()
            add_place_columns()
            row = db.session.execute(db.text("SELECT place, place_side FROM item")).one()
            self.assertEqual(tuple(row), ("xerox-canteen", "outside"))

    def test_the_map_feed_counts_open_reports_per_place(self):
        self.register()
        self.report("Blue bottle", "Steel", "Accessories", "Mech parking", "Found")
        self.report("Red cap", "Cotton", "Clothing", "outside the mech parking", "Lost")
        self.report("Old pen", "Blue", "Stationery", "Hostel", "Lost")
        with app.app_context():
            Item.query.filter_by(title="Blue bottle").one().custody = "held"
            db.session.commit()
        data = self.client.get("/api/map").get_json()
        self.assertEqual(data["places"]["mech-parking"]["found"], 1)
        self.assertEqual(data["places"]["mech-parking"]["lost"], 1)
        self.assertEqual(data["desk"], 1)
        self.assertFalse(data["hotspots"])
        sides = {item["title"]: item["side"] for item in data["items"]}
        # A report with no place on the map stays in the list but off the map.
        self.assertEqual(sides, {"Blue bottle": "inside", "Red cap": "outside"})

    def test_returned_items_leave_the_map(self):
        self.register()
        self.report("Blue bottle", "Steel", "Accessories", "Mech parking", "Found")
        with app.app_context():
            Item.query.one().custody = "released"
            db.session.commit()
        self.assertEqual(self.client.get("/api/map").get_json()["items"], [])

    def test_home_page_draws_the_campus_map(self):
        self.register()
        self.report("Blue bottle", "Steel", "Accessories", "Mech parking", "Found")
        page = self.client.get("/").data
        self.assertIn(b'data-place="main-block"', page)
        self.assertIn(b'data-badge="mech-parking"', page)
        self.assertIn(b"map.js", page)

    def test_the_place_lookup_names_the_candidates(self):
        found = self.client.get("/api/place?q=near+the+canteen").get_json()
        self.assertIsNone(found["place"])
        self.assertEqual(found["side"], "outside")
        self.assertEqual(found["names"], {"canteen": "Canteen", "puff-shop": "Puff shop", "nandini": "Nandini"})
        self.assertEqual(self.client.get("/api/place?q=mesh+parking").get_json()["place"], "mech-parking")

    def test_report_and_edit_forms_show_the_map_picker(self):
        self.register()
        page = self.client.get("/report?status=Found").data
        self.assertIn(b"place-picker", page)
        self.assertIn(b'value="Found" selected', page)
        self.post("/report", data={
            "title": "Umbrella", "description": "Black", "category": "Accessories",
            "location": "by the bikes", "status": "Found", "place": "p2", "place_side": "outside",
        })
        with app.app_context():
            item_id = Item.query.one().id
        page = self.client.get(f"/item/{item_id}/edit").data
        self.assertIn(b'name="place" value="p2"', page)
        self.assertIn(b'value="outside" checked', page)

    def found_at(self, title, location, place, side="inside", hours_ago=1):
        self.post("/report", data={
            "title": title, "description": f"{title} with a sticker", "category": "Other",
            "location": location, "status": "Found", "place": place, "place_side": side,
        })
        with app.app_context():
            item = Item.query.filter_by(title=title).one()
            item.created_at = datetime.utcnow() - timedelta(hours=hours_ago)
            db.session.commit()

    def retrace(self, **params):
        return [item["title"] for item in self.client.get("/api/retrace", query_string=params).get_json()["items"]]

    def test_retrace_finds_items_along_the_route(self):
        self.register()
        self.found_at("Wallet", "canteen counter", "canteen")
        self.found_at("Umbrella", "outside the main block", "main-block", "outside")
        self.found_at("Laptop", "CS lab", "main-block", "inside")
        self.found_at("Keys", "main road", "road-main")
        self.found_at("Cap", "dental college", "dental")
        since = (datetime.utcnow() - timedelta(hours=6)).isoformat() + "Z"
        found = self.retrace(stops="canteen", passed="main-block,road-main", since=since)
        # Inside the canteen counts; the Main Block was only passed, so only what was outside it.
        self.assertEqual(sorted(found), ["Keys", "Umbrella", "Wallet"])
        found = self.retrace(stops="canteen,main-block", passed="", since=since, q="umbrella")
        self.assertEqual(found[0], "Umbrella")
        self.assertIn("Laptop", found)

    def test_retrace_leaves_out_what_was_found_before_you_were_there(self):
        self.register()
        self.found_at("Old wallet", "canteen", "canteen", hours_ago=30)
        self.found_at("New wallet", "canteen", "canteen", hours_ago=2)
        since = (datetime.utcnow() - timedelta(hours=12)).isoformat() + "Z"
        self.assertEqual(self.retrace(stops="canteen", since=since), ["New wallet"])
        self.assertEqual(self.retrace(stops="not-a-place", since=since), [])

    def test_save_as_lost_report_fills_the_form(self):
        self.register()
        page = self.client.get("/report?status=Lost&title=Black+bottle&location=P1+%E2%86%92+Canteen").data
        self.assertIn(b'value="Black bottle"', page)
        self.assertIn("value=\"P1 → Canteen\"".encode(), page)

    def test_a_spoken_sentence_becomes_a_retrace_search(self):
        token = self.csrf_token()
        response = self.client.post(
            "/api/retrace/parse",
            json={"text": "I lost my blue bottle, I was in P1 then the canteen and then mesh parking yesterday"},
            headers={"X-CSRF-Token": token},
        )
        plan = response.get_json()
        self.assertEqual(plan["source"], "rules")
        self.assertEqual(plan["item"], "Blue bottle")
        self.assertEqual(plan["when"], "yesterday")
        self.assertEqual([stop["ids"] for stop in plan["stops"]],
                         [["p1"], ["canteen", "puff-shop", "nandini"], ["mech-parking"]])

    def test_a_retrace_plan_from_the_model_is_resolved_to_places(self):
        reply = {"item": "water bottle", "places": ["Parking one", "the library", "Library"], "when": "week"}
        with mock.patch("app.parse_report_with_model", return_value=reply):
            token = self.csrf_token()
            plan = self.client.post("/api/retrace/parse", json={"text": "ನಾನು ನೀರಿನ ಬಾಟಲ್ ಕಳೆದುಕೊಂಡೆ"},
                                    headers={"X-CSRF-Token": token}).get_json()
        self.assertEqual(plan["source"], "model")
        # The same place said twice in a row is one stop.
        self.assertEqual([stop["ids"] for stop in plan["stops"]], [["p1"], ["main-block"]])
        self.assertEqual((plan["item"], plan["when"]), ("water bottle", "week"))

    def test_the_page_may_use_the_microphone(self):
        policy = self.client.get("/").headers["Permissions-Policy"]
        self.assertIn("microphone=(self)", policy)
        self.assertIn("camera=()", policy)

    def voice(self, mode, heard=None, transcript="", audio=b"RIFF....WAVEfmt "):
        token = self.csrf_token()
        data = {"mode": mode, "transcript": transcript}
        if audio:
            data["audio"] = (BytesIO(audio), "clip.wav")
        with mock.patch("app.hear_with_gemini", return_value=heard) as heard_mock:
            response = self.client.post("/api/voice", data=data, headers={"X-CSRF-Token": token},
                                        content_type="multipart/form-data")
        return response, heard_mock

    def test_voice_report_takes_gemini_fields_in_english(self):
        self.register()
        heard = {"transcript": "Mujhe computer lab mein ek key mili hai", "english": "I found a key in the computer lab",
                 "status": "Found", "title": "Key", "description": "A single key, found in the computer lab.",
                 "category": "Keys", "location": "computer lab", "places": ["computer lab"], "when": "today"}
        response, heard_mock = self.voice("report", heard)
        data = response.get_json()
        heard_mock.assert_called_once()
        self.assertEqual(data["source"], "gemini")
        self.assertEqual(data["heard"], "Mujhe computer lab mein ek key mili hai")
        self.assertEqual((data["fields"]["title"], data["fields"]["status"], data["fields"]["category"]),
                         ("Key", "Found", "Keys"))
        self.assertNotIn("Mujhe", data["fields"]["description"])

    def test_voice_retrace_and_search_from_gemini(self):
        heard = {"transcript": "ನಾನು P1 ಇಂದ canteen ಗೆ ಹೋದೆ", "english": "I went from P1 to the canteen",
                 "title": "water bottle", "places": ["P1", "canteen", "mesh parking"], "when": "yesterday"}
        data = self.voice("retrace", heard)[0].get_json()
        self.assertEqual([stop["ids"] for stop in data["stops"]],
                         [["p1"], ["canteen", "puff-shop", "nandini"], ["mech-parking"]])
        self.assertEqual((data["item"], data["when"]), ("water bottle", "yesterday"))
        search = self.voice("search", {"english": "black wallet near the canteen", "title": "black wallet",
                                       "places": ["canteen"]})[0].get_json()
        self.assertEqual((search["q"], search["place"]), ("black wallet", "canteen"))
        search = self.voice("search", {"english": "umbrella in mech parking", "title": "umbrella",
                                       "places": ["mechanical parking"]})[0].get_json()
        self.assertEqual(search["place"], "mech-parking")

    def test_voice_falls_back_to_the_browser_captions(self):
        data = self.voice("search", None, transcript="red umbrella mech parking", audio=b"")[0].get_json()
        self.assertEqual((data["source"], data["q"], data["place"]), ("browser", "red umbrella", "mech-parking"))
        response = self.voice("retrace", None, transcript="", audio=b"")[0]
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.voice("sing", None, transcript="hello")[0].status_code, 400)

    def test_rules_write_a_description_instead_of_echoing(self):
        fields = parse_report_rules("found a black Notebook in the mechanical parking")
        self.assertEqual(fields["title"], "Black Notebook")
        self.assertNotIn("found a black", fields["description"])
        self.assertTrue(fields["description"].startswith("Black Notebook found"))

    def test_the_ai_check_reports_the_providers_error(self):
        self.register_admin()
        os.environ["GEMINI_API_KEY"] = "test-key"
        self.addCleanup(os.environ.pop, "GEMINI_API_KEY", None)
        def refuse(request_, timeout):
            raise urllib.error.HTTPError("https://x", 403, "Forbidden", {}, BytesIO(b'{"error": "API key not valid"}'))

        with mock.patch("urllib.request.urlopen", side_effect=refuse):
            page = self.post("/admin/ai-check", follow_redirects=True).data
        self.assertIn(b"HTTP 403", page)
        self.assertIn(b"API key not valid", page)
        self.assertNotIn(b"test-key", page)

    def test_the_model_is_asked_again_without_settings_it_rejects(self):
        os.environ["GEMINI_API_KEY"] = "test-key"
        self.addCleanup(os.environ.pop, "GEMINI_API_KEY", None)
        sent = []

        class Reply:
            def __init__(self, body):
                self.body = body
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def read(self):
                return self.body

        def fake(request_, timeout):
            body = json.loads(request_.data)
            sent.append(body)
            if "reasoning_effort" in body:
                raise urllib.error.HTTPError(request_.full_url, 400, "Bad", {}, BytesIO(b"unknown field"))
            answer = {"title": "Key", "description": "A single key.", "category": "Keys",
                      "location": "computer lab", "status": "Found"}
            return Reply(json.dumps({"choices": [{"message": {"content": json.dumps(answer)}}]}).encode())

        with mock.patch("urllib.request.urlopen", side_effect=fake):
            fields = parse_report_with_model("Mujhe computer lab mein ek key mili")
        self.assertEqual(fields["title"], "Key")
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0]["model"], "gemini-3.8-flash")
        self.assertNotIn("reasoning_effort", sent[1])

    def model_reply(self, answer):
        body = json.dumps({"choices": [{"message": {"content": json.dumps(answer)}}]}).encode()
        reply = mock.MagicMock()
        reply.__enter__.return_value.read.return_value = body
        return reply

    def test_a_busy_model_is_tried_again(self):
        os.environ["GEMINI_API_KEY"] = "test-key"
        self.addCleanup(os.environ.pop, "GEMINI_API_KEY", None)
        busy = urllib.error.HTTPError("https://x", 503, "Busy", {}, BytesIO(b"high demand"))
        answer = {"title": "Key", "description": "A key.", "category": "Keys", "location": "", "status": "Found"}
        with mock.patch("app.BUSY_RETRY_WAITS", (0, 0)),                 mock.patch("urllib.request.urlopen", side_effect=[busy, busy, self.model_reply(answer)]) as calls:
            self.assertEqual(parse_report_with_model("found a key")["title"], "Key")
        self.assertEqual(calls.call_count, 3)

    def test_the_next_provider_answers_when_gemini_is_down(self):
        os.environ["GEMINI_API_KEY"] = "test-key"
        os.environ["GROQ_API_KEY"] = "other-key"
        self.addCleanup(os.environ.pop, "GEMINI_API_KEY", None)
        self.addCleanup(os.environ.pop, "GROQ_API_KEY", None)
        down = urllib.error.HTTPError("https://x", 503, "Busy", {}, BytesIO(b"high demand"))
        answer = {"title": "Cap", "description": "A red cap.", "category": "Clothing", "location": "", "status": "Lost"}
        urls = []

        def fake(request_, timeout):
            urls.append(request_.full_url)
            if "generativelanguage" in request_.full_url:
                raise down
            return self.model_reply(answer)

        with mock.patch("app.BUSY_RETRY_WAITS", (0, 0)), mock.patch("urllib.request.urlopen", side_effect=fake):
            self.assertEqual(parse_report_with_model("lost my red cap")["title"], "Cap")
        self.assertIn("groq", urls[-1])

    def test_hinglish_and_kanglish_without_a_model(self):
        found = parse_report_rules("Mujhe computer lab Mein Ek key Mili Hai")
        self.assertEqual((found["title"], found["status"], found["category"]), ("Key", "Found", "Keys"))
        lost = parse_report_rules("mera black wallet canteen mein kho gaya")
        self.assertEqual((lost["title"], lost["status"]), ("Black wallet", "Lost"))
        self.assertEqual(parse_report_rules("nanna bottle library alli kaledu hoyitu")["title"], "Bottle")
        science = parse_report_rules("Mujhe computer science lab Mein Ek notebook Mili Hai")
        self.assertEqual(science["location"], "Computer Science Lab")
        self.assertEqual(science["description"], "Notebook found at the computer science lab.")
        hindi = parse_report_rules("मुझे कंप्यूटर साइंस लैब में एक नोटबुक मिल गया")
        self.assertEqual((hindi["title"], hindi["status"]), ("नोटबुक", "Found"))
        self.assertEqual(parse_report_rules("ನನಗೆ ಲೈಬ್ರರಿ ಅಲ್ಲಿ ಒಂದು ಬಾಟಲ್ ಸಿಕ್ಕಿತು")["status"], "Found")
        self.assertEqual(parse_report_rules("मेरा wallet canteen में खो गया")["status"], "Lost")
        gaya = parse_report_rules("Mujhe computer lab Mein Ek notebook Mil Gaya")
        self.assertEqual((gaya["title"], gaya["status"]), ("Notebook", "Found"))

    def test_desk_columns_are_added_to_an_older_database(self):
        """A database from before the desk gets its columns, and found items count as held."""
        with app.app_context():
            db.drop_all()
            for statement in (
                'CREATE TABLE "user" (id INTEGER PRIMARY KEY, name VARCHAR(100) NOT NULL, '
                "email VARCHAR(160) NOT NULL, password_hash VARCHAR(255) NOT NULL, "
                "is_admin BOOLEAN NOT NULL, created_at DATETIME NOT NULL)",
                "CREATE TABLE item (id INTEGER PRIMARY KEY, title VARCHAR(120) NOT NULL, "
                "description TEXT NOT NULL, category VARCHAR(60) NOT NULL, location VARCHAR(120) NOT NULL, "
                "status VARCHAR(20) NOT NULL, reporter_name VARCHAR(100) NOT NULL, contact VARCHAR(160) NOT NULL, "
                "image_url VARCHAR(500), owner_id INTEGER, created_at DATETIME NOT NULL)",
                "CREATE TABLE claim (id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL, claimant_id INTEGER NOT NULL, "
                "message TEXT NOT NULL, status VARCHAR(20) NOT NULL, created_at DATETIME NOT NULL, decided_at DATETIME)",
                "INSERT INTO item (title, description, category, location, status, reporter_name, contact, created_at) "
                "VALUES ('Old bottle', 'Steel bottle', 'Other', 'Gym', 'Found', 'X', 'x@example.com', '2026-09-01')",
            ):
                db.session.execute(db.text(statement))
            db.session.commit()

            add_desk_columns()

            columns = {c["name"] for c in db.inspect(db.engine).get_columns("claim")}
            self.assertTrue({"handover_code", "code_expires_at", "collected_at", "released_by_id"} <= columns)
            self.assertIn("is_staff", {c["name"] for c in db.inspect(db.engine).get_columns("user")})
            custody = db.session.execute(db.text("SELECT custody FROM item")).scalar()
            self.assertEqual(custody, "held")
            db.create_all()

    def test_one_sentence_fills_the_report_form(self):
        self.register()
        token = self.csrf_token()
        response = self.client.post(
            "/report/parse",
            json={"text": "Lost my black Milton bottle near the library yesterday"},
            headers={"X-CSRF-Token": token},
        )
        self.assertEqual(response.status_code, 200)
        fields = response.get_json()["fields"]
        self.assertEqual(fields["status"], "Lost")
        self.assertEqual(fields["category"], "Accessories")
        self.assertEqual(fields["location"], "Library")
        self.assertEqual(fields["title"], "Black Milton bottle")
        self.assertEqual(response.get_json()["source"], "rules")

        found = parse_report_rules("Found an iPhone 13 with a clear case at the canteen")
        self.assertEqual((found["status"], found["category"], found["title"]),
                         ("Found", "Electronics", "iPhone 13 with a clear case"))

    def test_parsing_needs_a_session_and_a_csrf_token(self):
        self.assertEqual(self.client.post("/report/parse", json={"text": "lost keys"}).status_code, 400)
        self.register()
        response = self.client.post("/report/parse", json={"text": "lost keys at gym"})
        self.assertEqual(response.status_code, 400)

    def test_model_output_is_checked_before_it_reaches_the_form(self):
        text = "lost my keys at the gym"
        cleaned = clean_parsed_report(
            {"title": "Keys", "category": "Weapons", "status": "Stolen", "location": 5}, text)
        self.assertEqual(cleaned["category"], "Keys")
        self.assertEqual(cleaned["status"], "Lost")
        self.assertEqual(cleaned["location"], "Gym")
        self.assertEqual(clean_parsed_report("not a dict", text)["title"], "Keys")

    def test_unclaimed_valuables_move_to_the_admin_office(self):
        item_id = self.found_item("Grey phone", category="Electronics")
        self.register_staff()
        self.post(f"/desk/item/{item_id}/received", follow_redirects=True)
        with app.app_context():
            for event in db.session.get(Item, item_id).events:
                event.created_at = datetime.utcnow() - timedelta(hours=100)
            db.session.commit()
            self.assertEqual(escalate_unclaimed_valuables(), 1)
            item = db.session.get(Item, item_id)
            self.assertEqual(item.custody, "office")
            self.assertIn("admin office", item.events[-1].action)
            self.assertEqual(escalate_unclaimed_valuables(), 0)

        # It can still be claimed and released from the office.
        self.logout()
        self.register("Owner", "owner@example.com")
        self.claim(item_id, "IMEI ends in 4471 and the lock screen is a beach photo.")
        self.logout()
        self.login("desk@example.com")
        with app.app_context():
            claim_id = Claim.query.one().id
        self.post(f"/claims/{claim_id}/accept", follow_redirects=True)
        response = self.post("/desk/handover", data={"code": self.code_for(claim_id)}, follow_redirects=True)
        self.assertIn(b"Released Grey phone", response.data)

    def test_ordinary_items_and_recent_valuables_stay_at_the_desk(self):
        self.register_staff()
        self.report("Blue bottle", "Steel bottle", "Accessories", "Gym", "Found")
        self.report("Black laptop", "Dell laptop", "Electronics", "Lab 1", "Found")
        with app.app_context():
            bottle = Item.query.filter_by(title="Blue bottle").one()
            for event in bottle.events:
                event.created_at = datetime.utcnow() - timedelta(hours=500)
            db.session.commit()
            self.assertEqual(escalate_unclaimed_valuables(), 0)
            self.assertEqual(Item.query.filter_by(custody="office").count(), 0)

    def test_dashboard_insights_count_what_happened(self):
        item_id = self.found_item("Grey phone", category="Electronics")
        self.register("Owner", "owner@example.com")
        self.report("Black wallet", "Leather wallet", "Wallet & ID", "Library", "Lost")
        self.claim(item_id, "IMEI ends in 4471 and the lock screen is a beach photo.")
        self.logout()
        self.register_staff()
        self.post(f"/desk/item/{item_id}/received", follow_redirects=True)
        with app.app_context():
            claim_id = Claim.query.one().id
        self.post(f"/claims/{claim_id}/accept", follow_redirects=True)
        self.post("/desk/handover", data={"code": self.code_for(claim_id)}, follow_redirects=True)

        with app.app_context():
            insights = dashboard_insights()
        this_week = insights["weekly"][-1]
        self.assertEqual((this_week["lost"], this_week["found"]), (1, 1))
        self.assertEqual(insights["found_total"], 1)
        self.assertEqual(insights["returned"], 1)
        self.assertEqual(insights["return_rate"], 100)
        self.assertIsNotNone(insights["median_hours"])
        self.assertEqual({row["label"] for row in insights["locations"]}, {"Library"})
        self.assertEqual(dict((r["key"], r["count"]) for r in insights["custody"])["released"], 1)

        self.logout()
        self.register_admin("admin@example.com")
        page = self.client.get("/admin").data
        self.assertIn(b"Reports per week", page)
        self.assertIn(b"100%", page)
        self.assertIn(b"Hotspots on campus", page)
        title = page.split(b"<title>", 1)[1].split(b"</title>", 1)[0]
        self.assertNotIn(b"ACCOUNTS", title)
        self.assertIn(b"Security desk staff", page.split(b"</title>", 1)[1])

    def test_approval_rejection_and_collection_are_emailed(self):
        item_id = self.found_item()
        self.register("Owner", "owner@example.com")
        self.claim(item_id)
        self.logout()
        self.register_staff()
        with app.app_context():
            claim_id = Claim.query.one().id
        with mock.patch("app.send_email") as sent:
            self.post(f"/desk/item/{item_id}/received", follow_redirects=True)
            self.post(f"/claims/{claim_id}/accept", follow_redirects=True)
            code = self.code_for(claim_id)
            self.post("/desk/handover", data={"code": code}, follow_redirects=True)
        recipients = [call.args[0] for call in sent.call_args_list]
        self.assertEqual(recipients, ["finder@example.com", "owner@example.com", "owner@example.com"])
        approval = sent.call_args_list[1].args
        self.assertIn(f"{code[:3]} {code[3:]}", approval[2])
        self.assertIn("approved", approval[1])

    def test_email_is_skipped_without_a_sender_and_survives_aws_errors(self):
        os.environ.pop("SES_SENDER", None)
        self.assertFalse(send_email("a@example.com", "Hi", "Body"))

        os.environ["SES_SENDER"] = "desk@example.com"
        self.addCleanup(os.environ.pop, "SES_SENDER", None)
        failing = mock.Mock()
        failing.send_email.side_effect = ClientError({"Error": {"Code": "MessageRejected"}}, "SendEmail")
        with mock.patch("app.aws_client", return_value=failing):
            self.assertFalse(send_email("a@example.com", "Hi", "Body"))
        working = mock.Mock()
        with mock.patch("app.aws_client", return_value=working):
            self.assertTrue(send_email("a@example.com", "Hi", "Body"))
        self.assertEqual(working.send_email.call_args.kwargs["Source"], "ClassFind <desk@example.com>")

    def test_a_new_found_item_alerts_the_owner_of_a_matching_lost_report(self):
        self.register("Owner", "owner@example.com")
        self.report("Black leather wallet", "Black leather wallet with my student ID",
                    "Wallet & ID", "Library", "Lost")
        self.logout()
        self.register("Finder", "finder@example.com")
        with mock.patch("app.send_email") as sent:
            self.report("Black leather wallet", "Black leather wallet with a student ID inside",
                        "Wallet & ID", "Library", "Found")
        self.assertEqual(sent.call_count, 1)
        self.assertEqual(sent.call_args.args[0], "owner@example.com")
        self.assertIn("may be your Black leather wallet", sent.call_args.args[1])

    def test_photo_labels_are_saved_shown_and_used_in_matching(self):
        os.environ["PHOTO_LABELS"] = "true"
        self.addCleanup(os.environ.pop, "PHOTO_LABELS", None)
        rekognition = mock.Mock()
        rekognition.detect_labels.return_value = {"Labels": [
            {"Name": "Bottle"}, {"Name": "Indoors"}, {"Name": "Shaker"}]}
        self.register()
        with mock.patch("app.aws_client", return_value=rekognition):
            for title, status in (("Steel thing", "Lost"), ("Metal item", "Found")):
                self.post("/report", data={
                    "title": title, "description": "Grey and heavy", "category": "Other",
                    "location": "Gym", "status": status,
                    "image_file": (BytesIO(b"fake-image-data"), "photo.png"),
                }, content_type="multipart/form-data", follow_redirects=True)
        with app.app_context():
            lost = Item.query.filter_by(title="Steel thing").one()
            self.assertEqual(lost.image_labels, "Bottle,Shaker")
            reasons = [r for l, f, s, r in build_matches() if l.title == "Steel thing"][0]
        self.assertIn("photos show: bottle, shaker", reasons)
        page = self.client.get(f"/item/{lost.id}").data
        self.assertIn(b"In the photo", page)
        self.assertIn(b"Shaker", page)

    def test_photo_labels_stay_off_without_the_setting(self):
        os.environ.pop("PHOTO_LABELS", None)
        rekognition = mock.Mock()
        self.register()
        with mock.patch("app.aws_client", return_value=rekognition):
            self.post("/report", data={
                "title": "Bottle", "description": "Blue bottle", "category": "Other",
                "location": "Gym", "status": "Lost",
                "image_file": (BytesIO(b"fake-image-data"), "photo.png"),
            }, content_type="multipart/form-data", follow_redirects=True)
        rekognition.detect_labels.assert_not_called()

    def test_a_scanned_tag_reports_the_item_and_tells_its_owner(self):
        self.register("Owner", "owner@example.com")
        self.post("/tags", data={"label": "Blue Wildcraft backpack"}, follow_redirects=True)
        page = self.client.get("/tags").data
        self.assertIn(b"<svg", page)
        with app.app_context():
            token = Tag.query.one().token
        self.logout()

        # Anyone can see the scan page; it names the item but not the owner.
        scan = self.client.get(f"/t/{token}").data
        self.assertIn(b"Blue Wildcraft backpack", scan)
        self.assertNotIn(b"owner@example.com", scan)
        self.assertNotIn(b"Owner", scan.split(b"<main", 1)[1])

        self.register("Finder", "finder@example.com")
        with mock.patch("app.send_email") as sent:
            response = self.post(f"/t/{token}/found", data={"location": "Library"}, follow_redirects=True)
        self.assertIn(b"The owner has been told", response.data)
        self.assertEqual(sent.call_args.args[0], "owner@example.com")
        with app.app_context():
            item = Item.query.filter_by(title="Blue Wildcraft backpack").one()
            self.assertEqual((item.status, item.custody, item.location), ("Found", "awaiting", "Library"))

    def test_tags_belong_to_their_owner(self):
        self.register("Owner", "owner@example.com")
        self.post("/tags", data={"label": "Calculator"}, follow_redirects=True)
        with app.app_context():
            tag = Tag.query.one()
            tag_id, token = tag.id, tag.token
        response = self.post(f"/t/{token}/found", data={"location": "Lab"}, follow_redirects=True)
        self.assertIn(b"your own tag", response.data)
        self.logout()
        self.register("Other", "other@example.com")
        self.assertEqual(self.post(f"/tags/{tag_id}/delete").status_code, 404)
        self.assertEqual(self.client.get("/t/not-a-real-token").status_code, 404)

    def test_home_page_wraps_results_for_live_search(self):
        self.register()
        self.report("Red umbrella", "Folding umbrella", "Other", "Hostel", "Lost")
        page = self.client.get("/?q=umbrella").data
        results = page.split(b'id="results"', 1)[1]
        self.assertIn(b"Red umbrella", results)
        self.assertIn(b"live-filters", page)

    def test_claim_page_shows_progress(self):
        item_id = self.found_item()
        self.register("Owner", "owner@example.com")
        self.claim(item_id)
        page = self.client.get("/claims").data
        self.assertIn(b"claim-steps", page)
        self.assertIn(b'class="done">Submitted', page)
        self.assertNotIn(b'class="done">Ready to collect', page)

    def test_the_report_model_uses_whichever_provider_has_a_key(self):
        reply = mock.MagicMock()
        reply.__enter__.return_value.read.return_value = json.dumps({"choices": [{"message": {
            "content": '```json\n{"title": "Keys", "status": "Lost"}\n```'}}]}).encode()
        names = ("GEMINI_API_KEY", "CEREBRAS_API_KEY", "GROQ_API_KEY")
        for key_name, host in (("GEMINI_API_KEY", "generativelanguage.googleapis.com"),
                               ("CEREBRAS_API_KEY", "api.cerebras.ai"), ("GROQ_API_KEY", "api.groq.com")):
            for name in names:
                os.environ.pop(name, None)
            os.environ[key_name] = "test-key"
            self.addCleanup(os.environ.pop, key_name, None)
            with mock.patch("app.urllib.request.urlopen", return_value=reply) as opened:
                self.assertEqual(parse_report_with_model("lost keys"), {"title": "Keys", "status": "Lost"})
            request_ = opened.call_args.args[0]
            self.assertIn(host, request_.full_url)
            self.assertEqual(request_.get_header("User-agent"), "ClassFind/2.0")
        for name in names:
            os.environ.pop(name, None)
        self.assertIsNone(parse_report_with_model("lost keys"))

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
        self.assertEqual(first.count(b"class=\"result-row\""), PAGE_SIZE)
        self.assertIn(b"Page 1 of 2", first)

        second = self.client.get("/?page=2").data
        self.assertEqual(second.count(b"class=\"result-row\""), 4)
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

    def test_editing_a_report_refreshes_the_matches(self):
        self.register()
        self.report("Black wallet", "Leather wallet", "Wallet & ID", "Library", "Lost")
        self.report("Wallet", "Leather wallet found", "Wallet & ID", "Library", "Found")

        with app.app_context():
            before = cached_matches()
            self.assertEqual(before[0][0].title, "Black wallet")
            lost_id = before[0][0].id

        self.post(
            f"/item/{lost_id}/edit",
            data={
                "title": "Brown wallet",
                "description": "Leather wallet",
                "category": "Wallet & ID",
                "location": "Library",
                "status": "Lost",
            },
            follow_redirects=True,
        )
        with app.app_context():
            after = cached_matches()
            self.assertIsNot(after, before)
            self.assertEqual(after[0][0].title, "Brown wallet")

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
