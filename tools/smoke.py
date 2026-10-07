"""Check a running ClassFind from the outside, after a deploy.

    python tools/smoke.py https://your-environment.elasticbeanstalk.com

Only reads public pages and APIs: it never signs in, reports or changes
anything, so it is safe to run against the live site. Exits 1 if any check
fails.
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

DEFAULT_URL = "https://classfind-prod.eba-ttyqcasp.ap-south-1.elasticbeanstalk.com"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch(url, follow=True):
    """(status, headers, body, seconds) for a GET, without raising on HTTP errors."""
    opener = urllib.request.build_opener() if follow else urllib.request.build_opener(NoRedirect)
    request = urllib.request.Request(url, headers={"User-Agent": "ClassFind-smoke/1.0"})
    started = time.monotonic()
    try:
        with opener.open(request, timeout=20) as response:
            return response.status, response.headers, response.read(), time.monotonic() - started
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read(), time.monotonic() - started


def main(base):
    base = base.rstrip("/")
    results = []

    def check(name, ok, detail=""):
        results.append(ok)
        print(f"{'PASS' if ok else 'FAIL'}  {name}{f'  ({detail})' if detail else ''}")

    status, _, body, took = fetch(base + "/health")
    health = json.loads(body or b"{}") if status == 200 else {}
    check("health answers ok", status == 200 and health.get("status") == "ok", f"{status}, {took:.2f}s")
    check("database is PostgreSQL", health.get("engine") == "postgresql", health.get("engine", "?"))

    status, headers, body, took = fetch(base + "/")
    page = body.decode("utf-8", "replace")
    check("home page loads", status == 200, f"{status}, {took:.2f}s")
    check("home page draws the campus map", 'data-place="main-block"' in page and "map.js" in page)
    check("security policy blocks inline scripts", "script-src 'self'" in (headers.get("Content-Security-Policy") or ""))
    check("microphone allowed for this site only", "microphone=(self)" in (headers.get("Permissions-Policy") or ""))
    if base.startswith("https://"):
        check("HSTS is sent over HTTPS", bool(headers.get("Strict-Transport-Security")))
        status, headers, _, _ = fetch("http://" + base[len("https://"):] + "/", follow=False)
        check("plain HTTP is sent to HTTPS", status in (301, 308) and (headers.get("Location") or "").startswith("https://"),
              f"{status}")

    for asset in ("static/campus.json", "static/map.js", "static/picker.js", "static/voice.js", "static/style.css"):
        status, _, body, _ = fetch(f"{base}/{asset}")
        check(f"{asset} is served", status == 200 and len(body) > 100, f"{status}, {len(body)} bytes")
    status, _, body, _ = fetch(base + "/static/campus.json")
    campus = json.loads(body) if status == 200 else {}
    check("campus map has every place", len(campus.get("places", [])) >= 29, f"{len(campus.get('places', []))} places")

    status, _, body, took = fetch(base + "/api/map")
    feed = json.loads(body) if status == 200 else {}
    check("map feed answers", status == 200 and {"places", "desk", "items"} <= set(feed), f"{took:.2f}s")

    def place(text):
        status, _, body, _ = fetch(base + "/api/place?" + urllib.parse.urlencode({"q": text}))
        return json.loads(body) if status == 200 else {}

    check("\"mechanical parking\" is Mech Parking", place("mechanical parking").get("place") == "mech-parking")
    check("\"CS lab\" is the Main Block", place("CS lab, 3rd floor").get("place") == "main-block")
    check("\"canteen\" asks which of three", place("near the canteen").get("candidates") == ["canteen", "puff-shop", "nandini"])
    check("\"Canara ATM\" is the ATM", place("Canara ATM").get("place") == "atm")

    since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    query = urllib.parse.urlencode({"stops": "canteen,main-block", "passed": "road-main", "since": since})
    status, _, body, took = fetch(f"{base}/api/retrace?{query}")
    check("Retrace answers", status == 200 and "items" in json.loads(body or b"{}"), f"{took:.2f}s")

    for path in ("/login", "/register", "/matches"):
        status, _, _, _ = fetch(base + path)
        check(f"{path} loads", status == 200, f"{status}")
    status, headers, _, _ = fetch(base + "/report", follow=False)
    check("reporting needs a sign-in", status in (301, 302, 303) and "login" in (headers.get("Location") or ""), f"{status}")
    status, _, _, _ = fetch(base + "/admin", follow=False)
    check("admin is not public", status in (301, 302, 303, 403), f"{status}")

    passed = sum(results)
    print(f"\n{passed} of {len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL))
