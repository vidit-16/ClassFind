import os
import unittest
from pathlib import Path

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret")

from app import app


class ClassFindQualityTestCase(unittest.TestCase):
    EXPECTED_ENDPOINTS = {
        "index", "health", "register", "login", "logout", "profile", "report",
        "item_detail", "edit_item", "resolve_item", "delete_item",
        "submit_claim", "claims", "accept_claim", "reject_claim",
        "withdraw_claim", "matches", "admin_dashboard",
    }

    def test_route_parity(self):
        endpoints = {
            rule.endpoint
            for rule in app.url_map.iter_rules()
            if rule.endpoint and not rule.endpoint.startswith("static")
        }
        self.assertTrue(self.EXPECTED_ENDPOINTS.issubset(endpoints))
        self.assertEqual(len(endpoints), len(set(endpoints)))

    def test_post_templates_have_csrf(self):
        template_dir = Path(app.template_folder)
        protected_templates = [
            "base.html", "login.html", "register.html", "report.html",
            "edit.html", "item.html", "claims.html", "admin.html",
        ]
        for name in protected_templates:
            content = (template_dir / name).read_text(encoding="utf-8")
            self.assertIn('name="csrf_token"', content, name)

    def test_required_runtime_assets_exist(self):
        root = Path(__file__).resolve().parents[1]
        for relative in [
            "app.py", "requirements.txt", "Procfile",
            "templates/base.html", "templates/index.html",
            "static/style.css", "static/app.js",
        ]:
            self.assertTrue((root / relative).exists(), relative)


if __name__ == "__main__":
    unittest.main()
