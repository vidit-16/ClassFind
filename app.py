import base64
import json
import os
import re
import urllib.error
import urllib.request
import threading
import time
from datetime import datetime, timedelta, timezone
from hmac import compare_digest
from secrets import randbelow, token_urlsafe
from difflib import SequenceMatcher
from functools import wraps
from pathlib import Path
from uuid import uuid4

import boto3
import qrcode
import qrcode.image.svg
from botocore.exceptions import BotoCoreError, ClientError

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import and_, or_
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

import campus

app = Flask(__name__)
# nginx terminates HTTPS and forwards to Gunicorn over plain HTTP, setting
# X-Forwarded-Proto and X-Forwarded-For. Trusting one hop of each is what lets
# request.is_secure see HTTPS and request.remote_addr see the student rather
# than nginx, which the FORCE_HTTPS redirect, HSTS and rate limits rely on.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)


def is_production(env=None):
    """True when CLASSFIND_ENV names a deployed environment."""
    env = os.environ if env is None else env
    return env.get("CLASSFIND_ENV", "development").strip().lower() in {"production", "prod"}


def resolve_secret_key(env=None):
    """The session signing key.

    A key committed to a public repository signs nothing: anyone who reads it
    can mint a session cookie for any account, including an admin one. So there
    is no default. In production a missing key stops the app. Elsewhere it gets
    a random key, which costs a sign-in whenever the process restarts and is the
    cheap reminder to set SECRET_KEY.
    """
    env = os.environ if env is None else env
    key = env.get("SECRET_KEY", "").strip()
    if key:
        return key
    if is_production(env):
        raise RuntimeError(
            "SECRET_KEY is not set. Set it on the environment before deploying: "
            "any session cookie signed with a shared key can be forged."
        )
    app.logger.warning(
        "SECRET_KEY is not set; using a random key for this process. "
        "Sessions will not survive a restart. Set SECRET_KEY to keep them."
    )
    return token_urlsafe(32)


def admin_email(env=None):
    """The one account allowed to hold admin rights, from ADMIN_EMAIL."""
    env = os.environ if env is None else env
    return env.get("ADMIN_EMAIL", "").strip().lower()


def staff_emails(env=None):
    """Accounts that work the security desk, from STAFF_EMAILS (comma separated)."""
    env = os.environ if env is None else env
    return {e.strip().lower() for e in env.get("STAFF_EMAILS", "").split(",") if e.strip()}


def env_flag(name, env=None):
    """True when the named variable is set to 1, true or yes."""
    env = os.environ if env is None else env
    return env.get(name, "").strip().lower() in {"1", "true", "yes"}


app.config["SECRET_KEY"] = resolve_secret_key()
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# FORCE_HTTPS is off by default because a deployment whose certificate could not
# be issued still has to answer on HTTP. Turn it on once HTTPS is known to work.
app.config["FORCE_HTTPS"] = env_flag("FORCE_HTTPS")
app.config["SESSION_COOKIE_SECURE"] = app.config["FORCE_HTTPS"] or env_flag("COOKIE_SECURE")
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
app.config["UPLOAD_FOLDER"] = os.getenv(
    "UPLOAD_FOLDER", str(Path(app.static_folder) / "uploads")
)
app.config["ALLOWED_EXTENSIONS"] = {"png", "jpg", "jpeg", "gif", "webp"}
app.config["S3_BUCKET"] = os.getenv("S3_BUCKET", "").strip()
app.config["AWS_REGION"] = os.getenv("AWS_REGION", "ap-south-1")

database_url = os.getenv("DATABASE_URL", "sqlite:///classfind.db")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
if is_production() and database_url.startswith("sqlite"):
    # Not fatal, because an environment already running this way would stop
    # serving on its next deploy. /health reports the engine so it can be seen.
    app.logger.warning(
        "DATABASE_URL is not set, so reports are being written to a SQLite file "
        "on this instance. It is lost whenever the instance is replaced or the "
        "app is redeployed. Point DATABASE_URL at PostgreSQL."
    )

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 1800}

CSRF_SESSION_KEY = "_csrf_token"


def csrf_token():
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


app.jinja_env.globals["csrf_token"] = csrf_token
app.jinja_env.globals["campus_map"] = campus.CAMPUS


@app.before_request
def redirect_to_https():
    """Send plain-HTTP requests to HTTPS when FORCE_HTTPS is on.

    Without this a sign-in typed at the http:// address sends the password in
    the clear. It is done here rather than in nginx because Let's Encrypt
    renews the certificate by fetching a file over port 80, which nginx answers
    before the request reaches the app. /health is left alone so a local check
    over HTTP still sees the app's own answer.
    """
    if not app.config["FORCE_HTTPS"] or request.is_secure or request.path == "/health":
        return
    target = "https://" + request.url.split("://", 1)[1]
    # 308 keeps the method and body, so a form posted over HTTP is not replayed as a GET.
    return redirect(target, code=301 if request.method in {"GET", "HEAD"} else 308)


@app.before_request
def protect_csrf():
    expected = session.get(CSRF_SESSION_KEY)
    if not expected:
        expected = csrf_token()
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not expected or not supplied or not compare_digest(expected, supplied):
        abort(400, description="Invalid or missing CSRF token.")


# POSTs allowed per client address in a window of seconds, by endpoint. Kept in
# memory, which is enough because Gunicorn runs a single process; a restart
# simply starts the counts again.
RATE_LIMITS = {
    "login": (10, 60),
    "register": (5, 60 * 60),
    "report": (20, 60 * 60),
    "submit_claim": (20, 60 * 60),
    # Collection codes are six digits, so the desk screen caps guesses.
    "desk_handover": (10, 60),
    # Each parse may call the model, which costs a request.
    "parse_report": (30, 60 * 60),
    "parse_retrace": (30, 60 * 60),
    # Each clip is sent to Gemini.
    "voice": (40, 60 * 60),
}
app.config["RATE_LIMITS_ENABLED"] = True
_rate_hits = {}
_rate_lock = threading.Lock()


@app.before_request
def rate_limit():
    """Answer 429 when one address posts to a limited form too often.

    Guards password guessing on sign-in and bulk account or report creation.
    """
    if request.method != "POST" or not app.config["RATE_LIMITS_ENABLED"]:
        return
    limit = RATE_LIMITS.get(request.endpoint)
    if not limit:
        return
    count, window = limit
    key = (request.endpoint, request.remote_addr)
    now = time.monotonic()
    with _rate_lock:
        recent = [t for t in _rate_hits.get(key, ()) if now - t < window]
        if len(recent) >= count:
            _rate_hits[key] = recent
            retry_after = int(window - (now - recent[0])) + 1
            abort(429, description=f"Too many attempts. Try again in {retry_after} seconds.")
        recent.append(now)
        _rate_hits[key] = recent
        # Drop addresses with nothing left in their window so the table stays small.
        if len(_rate_hits) > 10000:
            for stale in [k for k, hits in _rate_hits.items() if now - hits[-1] >= RATE_LIMITS[k[0]][1]]:
                del _rate_hits[stale]


@app.after_request
def apply_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(self), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "style-src 'self' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; "
        "script-src 'self'; "
        "img-src 'self' data: https:; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    )
    if request.is_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


db = SQLAlchemy(app)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(160), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    # Security desk staff log handed-in items, verify claims and release items.
    is_staff = db.Column(db.Boolean, default=False, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    @property
    def works_desk(self):
        return self.is_staff or self.is_admin


class Item(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(60), nullable=False)
    location = db.Column(db.String(120), nullable=False)
    status = db.Column(db.String(20), nullable=False)
    reporter_name = db.Column(db.String(100), nullable=False)
    contact = db.Column(db.String(160), nullable=False)
    image_url = db.Column(db.String(500))
    # Who may edit this report. Ownership used to be inferred by comparing the
    # contact field to the signed-in email, which made an account detail into an
    # access rule. Rows created before this column exists carry NULL and fall
    # back to that comparison; see backfill_item_owners().
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), index=True)
    # The place on the campus map (static/campus.json): a building or a road.
    # `location` keeps the reporter's own words, e.g. "CS lab, 3rd floor".
    place = db.Column(db.String(40), index=True)
    # "outside" for things found next to a building rather than in it.
    place_side = db.Column(db.String(10))
    # What Rekognition saw in the photo, comma separated, e.g. "Bottle,Shaker".
    image_labels = db.Column(db.String(300))
    # Where a found item physically is: awaiting drop-off at the security desk,
    # held there, or released to its owner. Empty for lost reports.
    custody = db.Column(db.String(20))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    claims = db.relationship("Claim", back_populates="item", cascade="all, delete-orphan")
    events = db.relationship(
        "CustodyEvent", back_populates="item", cascade="all, delete-orphan",
        order_by="CustodyEvent.created_at",
    )

    @property
    def is_valuable(self):
        return self.category in VALUABLE_CATEGORIES

    @property
    def custody_label(self):
        return CUSTODY_LABELS.get(self.custody, "")

    @property
    def place_name(self):
        return campus.place_name(self.place) if self.place else ""

    @property
    def label_list(self):
        return [label for label in (self.image_labels or "").split(",") if label]

    @property
    def image_src(self):
        if not self.image_url:
            return None
        if self.image_url.startswith("s3://"):
            bucket, key = self.image_url[5:].split("/", 1)
            return s3_client.generate_presigned_url(
                "get_object",
                Params={"Bucket": bucket, "Key": key},
                ExpiresIn=3600,
            )
        return self.image_url

    @property
    def thumb_src(self):
        """The Lambda-made thumbnail of an S3 photo, when THUMBNAILS is on.

        The page falls back to the full photo if the thumbnail is not there yet.
        """
        if not env_flag("THUMBNAILS") or not (self.image_url or "").startswith("s3://"):
            return None
        bucket, key = self.image_url[5:].split("/", 1)
        return s3_client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": "thumbs/" + key.rsplit(".", 1)[0] + ".jpg"},
            ExpiresIn=3600,
        )

    @property
    def status_class(self):
        return {
            "Lost": "status-lost",
            "Found": "status-found",
            "Resolved": "status-resolved",
        }.get(self.status, "")


class Claim(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id", ondelete="CASCADE"), nullable=False, index=True)
    claimant_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    message = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="Pending")
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    decided_at = db.Column(db.DateTime)
    # Issued when the desk approves the claim, shown only to the claimant, and
    # entered by staff at handover. Single use, and it expires.
    handover_code = db.Column(db.String(6))
    code_expires_at = db.Column(db.DateTime)
    collected_at = db.Column(db.DateTime)
    released_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    item = db.relationship("Item", back_populates="claims")
    claimant = db.relationship("User", foreign_keys=[claimant_id])
    released_by = db.relationship("User", foreign_keys=[released_by_id])

    @property
    def code_is_live(self):
        return bool(
            self.status == "Approved" and self.handover_code
            and self.code_expires_at and self.code_expires_at > datetime.utcnow()
        )


class Tag(db.Model):
    """A printable QR sticker for something a student owns.

    The code links to a public page that names the item but not its owner, and
    lets whoever finds it file a found report with one click.
    """
    id = db.Column(db.Integer, primary_key=True)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    label = db.Column(db.String(60), nullable=False)
    token = db.Column(db.String(32), unique=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    owner = db.relationship("User")


class StaffInvite(db.Model):
    """An admin's invitation for someone to work the security desk.

    Desk access is only granted when the invited person accepts it from their
    own account, so a mistyped address cannot hand it to a stranger who never
    asked, and nobody becomes staff without knowing.
    """
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(160), nullable=False, index=True)
    token = db.Column(db.String(32), unique=True, nullable=False, index=True)
    invited_by_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    responded_at = db.Column(db.DateTime)
    accepted = db.Column(db.Boolean)
    invited_by = db.relationship("User")

    @property
    def is_open(self):
        return self.responded_at is None and self.created_at > datetime.utcnow() - timedelta(days=INVITE_DAYS)


INVITE_DAYS = 7


class CustodyEvent(db.Model):
    """One line of a found item's chain of custody."""
    id = db.Column(db.Integer, primary_key=True)
    item_id = db.Column(db.Integer, db.ForeignKey("item.id", ondelete="CASCADE"), nullable=False, index=True)
    action = db.Column(db.String(300), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id"))
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    item = db.relationship("Item", back_populates="events")
    actor = db.relationship("User")


# Claims on these need a longer proof that names an identifier.
VALUABLE_CATEGORIES = {"Electronics", "Wallet & ID", "Keys"}
CUSTODY_LABELS = {
    "awaiting": "Awaiting drop-off at the security desk",
    "held": "Held at the security desk",
    "office": "Moved to the admin office for safekeeping",
    "released": "Returned to its owner",
}
# Where an item can be collected from. The admin office keeps valuables the
# desk has held for too long, and releases them the same way.
IN_STORAGE = {"held", "office"}
HANDOVER_CODE_HOURS = 48
ESCALATE_AFTER_HOURS = int(os.getenv("ESCALATE_AFTER_HOURS", "72"))


def escalate_unclaimed_valuables():
    """Move valuables held at the desk past the limit to the admin office.

    The desk is a counter; the admin office has a safe. Run whenever the desk
    or admin pages load, so no scheduler is needed. Returns how many moved.
    """
    cutoff = datetime.utcnow() - timedelta(hours=ESCALATE_AFTER_HOURS)
    moved = 0
    for item in Item.query.filter_by(status="Found", custody="held").all():
        if not item.is_valuable:
            continue
        received = next(
            (e.created_at for e in reversed(item.events)
             if e.action.startswith(("Received at", "Logged at"))),
            item.created_at,
        )
        if received > cutoff:
            continue
        if any(c.status == "Approved" for c in item.claims):
            continue
        item.custody = "office"
        log_custody(item, f"Moved to the admin office, unclaimed after {ESCALATE_AFTER_HOURS} hours")
        moved += 1
    if moved:
        db.session.commit()
    return moved


def log_custody(item, action, actor=None):
    db.session.add(CustodyEvent(item=item, action=action, actor=actor))


def backfill_item_owners():
    """Add item.owner_id where the schema predates it, and fill it in.

    create_all() makes missing tables but never alters an existing one, so a
    database created before this column stays without it and every query
    referring to owner_id fails. This runs the one statement needed and matches
    each report to the account whose email it was filed under.
    """
    inspector = db.inspect(db.engine)
    if "item" not in inspector.get_table_names():
        return
    if any(column["name"] == "owner_id" for column in inspector.get_columns("item")):
        return

    db.session.execute(db.text("ALTER TABLE item ADD COLUMN owner_id INTEGER"))
    db.session.execute(db.text(
        "UPDATE item SET owner_id = ("
        "  SELECT id FROM \"user\" WHERE lower(\"user\".email) = lower(item.contact)"
        ")"
    ))
    db.session.commit()
    app.logger.info("Added item.owner_id and matched existing reports to their accounts.")


def add_desk_columns():
    """Add the security desk columns to tables that predate them.

    Found reports from before the desk existed are treated as already held, so
    they can still be claimed and released through the desk.
    """
    inspector = db.inspect(db.engine)
    wanted = {
        "user": {"is_staff": "BOOLEAN NOT NULL DEFAULT FALSE"},
        "item": {"custody": "VARCHAR(20)", "image_labels": "VARCHAR(300)"},
        "claim": {
            "handover_code": "VARCHAR(6)",
            "code_expires_at": "TIMESTAMP",
            "collected_at": "TIMESTAMP",
            "released_by_id": "INTEGER",
        },
    }
    tables = inspector.get_table_names()
    added = False
    for table, columns in wanted.items():
        if table not in tables:
            continue
        existing = {column["name"] for column in inspector.get_columns(table)}
        for name, ddl in columns.items():
            if name not in existing:
                db.session.execute(db.text(f'ALTER TABLE "{table}" ADD COLUMN {name} {ddl}'))
                added = True
    if added:
        db.session.execute(db.text(
            "UPDATE item SET custody = 'held' WHERE status = 'Found' AND custody IS NULL"
        ))
        db.session.commit()
        app.logger.info("Added the security desk columns.")


def set_place(item, place="", side=""):
    """Put a report on the map.

    A place picked on the map wins. Otherwise the location text is read, and the
    place is only set when the text names exactly one, e.g. not plain "xerox".
    """
    place = (place or "").strip()
    if campus.is_place(place):
        item.place = place
        item.place_side = "outside" if side == "outside" or place in campus.ROADS else "inside"
        return
    found = campus.resolve_place(item.location)
    item.place = found["place"]
    item.place_side = found["side"] if found["place"] else None


def add_place_columns():
    """Add item.place to tables that predate the map, and place old reports from their text."""
    inspector = db.inspect(db.engine)
    if "item" not in inspector.get_table_names():
        return
    existing = {column["name"] for column in inspector.get_columns("item")}
    if "place" in existing:
        return
    db.session.execute(db.text("ALTER TABLE item ADD COLUMN place VARCHAR(40)"))
    db.session.execute(db.text("ALTER TABLE item ADD COLUMN place_side VARCHAR(10)"))
    db.session.execute(db.text("CREATE INDEX IF NOT EXISTS ix_item_place ON item (place)"))
    db.session.commit()
    placed = 0
    for item in Item.query.all():
        set_place(item)
        placed += bool(item.place)
    db.session.commit()
    app.logger.info("Added item.place and placed %s existing reports on the map.", placed)


with app.app_context():
    db.create_all()
    backfill_item_owners()
    add_desk_columns()
    add_place_columns()

if not app.config["S3_BUCKET"]:
    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)

s3_client = boto3.client("s3", region_name=app.config["AWS_REGION"]) if app.config["S3_BUCKET"] else None
_aws_clients = {}


def aws_client(service):
    """One boto3 client per service, made on first use so tests and local runs never need AWS."""
    if service not in _aws_clients:
        _aws_clients[service] = boto3.client(service, region_name=app.config["AWS_REGION"])
    return _aws_clients[service]


def send_email(to, subject, body):
    """Send a plain-text email through Amazon SES from SES_SENDER.

    Email is a courtesy: everything it says is also on the site, so a missing
    sender, an unverified address in the SES sandbox, or an AWS error is logged
    and the request carries on. Returns whether SES accepted the message.
    """
    sender = os.getenv("SES_SENDER", "").strip()
    if not sender or not to:
        return False
    try:
        aws_client("ses").send_email(
            Source=f"ClassFind <{sender}>",
            Destination={"ToAddresses": [to]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": body + "\n\n- ClassFind, BIT campus lost and found", "Charset": "UTF-8"}},
            },
        )
        return True
    except (ClientError, BotoCoreError):
        app.logger.warning("SES could not send %r to %s", subject, to)
        return False


# Labels too generic to tell two items apart.
GENERIC_LABELS = {"Text", "Person", "Human", "Indoors", "Room", "Floor", "Table", "Furniture",
                  "Wood", "Pattern", "Background", "Photography", "Adult", "Male", "Female", "Man", "Woman"}


def photo_labels(file_storage):
    """Ask Amazon Rekognition what is in an uploaded photo. Returns up to six labels.

    Runs only with PHOTO_LABELS on. The stream is rewound afterwards so the
    upload that follows still has the whole file.
    """
    if not env_flag("PHOTO_LABELS") or not file_storage or not file_storage.filename:
        return []
    data = file_storage.stream.read()
    file_storage.stream.seek(0)
    if not data or len(data) > 5 * 1024 * 1024:
        return []
    try:
        response = aws_client("rekognition").detect_labels(
            Image={"Bytes": data}, MaxLabels=15, MinConfidence=75)
    except (ClientError, BotoCoreError):
        app.logger.warning("Rekognition could not label the photo")
        return []
    labels = [label["Name"] for label in response.get("Labels", []) if label["Name"] not in GENERIC_LABELS]
    return labels[:6]


def get_current_user():
    user_id = session.get("user_id")
    return db.session.get(User, user_id) if user_id else None


def filed_by(user):
    """Query filter for the reports this account filed.

    The same rule as owns_item(): owner_id decides, and the contact field is
    consulted only for reports from before the column existed.
    """
    return or_(
        Item.owner_id == user.id,
        db.and_(Item.owner_id.is_(None), Item.contact.ilike(user.email)),
    )


def pending_claim_counts(user):
    """Claims waiting on this account: reviews for desk staff, and the account's own open claims."""
    if not user:
        return 0, 0
    incoming = Claim.query.filter_by(status="Pending").count() if user.works_desk else 0
    outgoing = Claim.query.filter(
        Claim.claimant_id == user.id, Claim.status.in_(("Pending", "Approved"))
    ).count()
    return incoming, outgoing


@app.context_processor
def inject_current_user():
    user = get_current_user()
    incoming, outgoing = pending_claim_counts(user)
    invite = None
    if user and not user.works_desk:
        invite = next((i for i in StaffInvite.query.filter_by(email=user.email.lower(), responded_at=None)
                       .order_by(StaffInvite.created_at.desc()).limit(3) if i.is_open), None)
    return {
        "current_user": user,
        "incoming_claim_count": incoming,
        "outgoing_claim_count": outgoing,
        "staff_invite": invite,
    }


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not get_current_user():
            flash("Please sign in to continue.", "error")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = get_current_user()
        if not user:
            flash("Please sign in to continue.", "error")
            return redirect(url_for("login", next=request.path))
        if not user.is_admin:
            flash("Admin access is required.", "error")
            return redirect(url_for("index"))
        return view(*args, **kwargs)

    return wrapped


def staff_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        user = get_current_user()
        if not user:
            flash("Please sign in to continue.", "error")
            return redirect(url_for("login", next=request.path))
        if not user.works_desk:
            flash("Security desk access is required.", "error")
            return redirect(url_for("index"))
        return view(*args, **kwargs)

    return wrapped


def apply_role_emails(user):
    """Grant the rights named in ADMIN_EMAIL and STAFF_EMAILS. True if anything changed."""
    changed = False
    if admin_email() and user.email == admin_email() and not user.is_admin:
        user.is_admin = True
        changed = True
    if user.email in staff_emails() and not user.is_staff:
        user.is_staff = True
        changed = True
    return changed


def owns_item(user, item):
    """Whether this account filed the report.

    owner_id decides it. The email comparison is only for reports filed before
    the column existed, where owner_id is NULL.
    """
    if not user:
        return False
    if item.owner_id is not None:
        return item.owner_id == user.id
    return item.contact.lower() == user.email.lower()


def can_manage_item(item):
    """Lost reports belong to whoever filed them. A found item is the finder's
    only until it is handed in; from then on the desk manages it."""
    user = get_current_user()
    if not user:
        return False
    if user.is_admin:
        return True
    if item.custody:
        if user.is_staff:
            return True
        return owns_item(user, item) and item.custody == "awaiting"
    return owns_item(user, item)


def allowed_file(filename):
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in app.config["ALLOWED_EXTENSIONS"]
    )


def save_uploaded_image(file_storage):
    if not file_storage or not file_storage.filename:
        return None

    if not allowed_file(file_storage.filename):
        raise ValueError("Use a PNG, JPG, JPEG, GIF or WEBP image.")

    safe_name = secure_filename(file_storage.filename)
    extension = safe_name.rsplit(".", 1)[1].lower()
    filename = f"{uuid4().hex}.{extension}"

    if s3_client:
        key = f"items/{filename}"
        try:
            s3_client.upload_fileobj(
                file_storage.stream,
                app.config["S3_BUCKET"],
                key,
                ExtraArgs={
                    "ContentType": file_storage.mimetype or "application/octet-stream"
                },
            )
        except ClientError as exc:
            app.logger.exception("S3 upload failed")
            raise ValueError("Image upload failed. Please try again.") from exc
        return f"s3://{app.config['S3_BUCKET']}/{key}"

    destination = Path(app.config["UPLOAD_FOLDER"]) / filename
    file_storage.save(destination)
    return url_for("static", filename=f"uploads/{filename}")


def delete_image(image_url):
    if not image_url:
        return

    if image_url.startswith("s3://") and s3_client:
        bucket, key = image_url[5:].split("/", 1)
        try:
            s3_client.delete_object(Bucket=bucket, Key=key)
        except ClientError:
            app.logger.exception("S3 delete failed")
        if env_flag("THUMBNAILS"):
            try:
                s3_client.delete_object(Bucket=bucket, Key="thumbs/" + key.rsplit(".", 1)[0] + ".jpg")
            except ClientError:
                app.logger.warning("Thumbnail delete failed")
        return

    prefix = "/static/uploads/"
    if image_url.startswith(prefix):
        filename = image_url[len(prefix):]
        path = Path(app.config["UPLOAD_FOLDER"]) / filename
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


@app.get("/health")
def health():
    try:
        db.session.execute(db.text("SELECT 1"))
        # The engine is named because SQLite and PostgreSQL both answer "ok",
        # and only one of them survives a redeploy.
        return {"status": "ok", "database": "ok", "engine": db.engine.dialect.name}, 200
    except Exception:
        db.session.rollback()
        return {"status": "error", "database": "unavailable"}, 503


PAGE_SIZE = 24
# The map shows open reports: lost ones not yet resolved, and found ones not yet
# returned. Older ones stay searchable but leave the map.
MAP_ITEM_LIMIT = 400
MAP_DAYS = 90
# Below this many reports in the last 30 days, a heat map would only show noise.
HOTSPOT_MIN_REPORTS = 8


def map_summary():
    """What the campus map draws: counts per place, the desk, and open reports."""
    since = datetime.utcnow() - timedelta(days=MAP_DAYS)
    open_items = (
        Item.query.filter(
            Item.place.isnot(None),
            Item.status.in_(("Lost", "Found")),
            or_(Item.custody.is_(None), Item.custody != "released"),
            Item.created_at >= since,
        )
        .order_by(Item.created_at.desc())
        .limit(MAP_ITEM_LIMIT)
        .all()
    )
    places = {}
    for item in open_items:
        counts = places.setdefault(item.place, {"found": 0, "lost": 0, "recent": 0})
        counts["found" if item.status == "Found" else "lost"] += 1
    month = datetime.utcnow() - timedelta(days=30)
    recent_total = 0
    for place, count in (
        db.session.query(Item.place, db.func.count(Item.id))
        .filter(Item.place.isnot(None), Item.created_at >= month)
        .group_by(Item.place)
    ):
        places.setdefault(place, {"found": 0, "lost": 0, "recent": 0})["recent"] = count
        recent_total += count
    desk = Item.query.filter(Item.status == "Found", Item.custody.in_(IN_STORAGE)).count()
    return {
        "places": places,
        "desk": desk,
        "hotspots": recent_total >= HOTSPOT_MIN_REPORTS,
        "items": open_items,
    }


@app.get("/api/place")
def place_lookup():
    """Which places some location text could mean, for the map picker to highlight."""
    found = campus.resolve_place(request.args.get("q", "")[:120])
    return {**found, "names": {i: campus.place_short(i) for i in found["candidates"]}}


RETRACE_LIMIT = 30


def retrace_matches(stops, passed, since, words=""):
    """Found items someone could have dropped on the route they walked.

    `stops` are the places they went into: anything reported there counts.
    `passed` are places and roads they only walked past or along: only things
    found outside count, plus anything on the roads themselves. Items found
    before they were there are left out by `since`. Matching words in the
    description rank an item higher but never hide one.
    """
    stops, passed = set(stops), set(passed) - set(stops)
    if not stops and not passed:
        return []
    candidates = (
        Item.query.filter(
            Item.status == "Found",
            or_(Item.custody.is_(None), Item.custody != "released"),
            Item.created_at >= since,
            Item.place.in_(stops | passed),
        )
        .order_by(Item.created_at.desc())
        .limit(MATCH_SCAN_LIMIT)
        .all()
    )
    wanted = tokens(words)
    ranked = []
    for item in candidates:
        on_road = item.place in campus.ROADS
        if item.place in passed and not on_road and item.place_side != "outside":
            continue
        score = 0.5 if item.place in stops else 0.35
        if wanted:
            have = tokens(f"{item.title} {item.description} {item.category} {item.image_labels or ''}")
            score += 0.5 * len(wanted & have) / len(wanted)
        if item.place in stops:
            why = f"at {campus.place_short(item.place)}, where you went"
        elif on_road:
            why = "on a path you walked"
        else:
            why = f"outside {campus.place_short(item.place)}, which you passed"
        ranked.append((round(score, 3), item, why))
    ranked.sort(key=lambda entry: (-entry[0], -entry[1].created_at.timestamp()))
    return ranked[:RETRACE_LIMIT]


@app.get("/api/retrace")
def retrace():
    """Retrace for the home page: ?stops=a,b&passed=c,d&since=ISO-time&q=words."""
    def ids(name):
        return [i for i in request.args.get(name, "").split(",") if campus.is_place(i)][:60]

    try:
        since = datetime.fromisoformat(request.args.get("since", "").replace("Z", "+00:00"))
        if since.tzinfo:
            since = since.astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        since = datetime.utcnow() - timedelta(days=1)
    results = retrace_matches(ids("stops"), ids("passed"), since, request.args.get("q", "")[:200])
    return {"items": [
        {
            "id": item.id, "title": item.title, "place": item.place,
            "side": item.place_side or "inside", "where": item.location, "why": why,
            "custody": item.custody_label, "at": item.created_at.isoformat() + "Z",
            "url": url_for("item_detail", item_id=item.id), "image": item.thumb_src or item.image_src,
            "score": score,
        }
        for score, item, why in results
    ]}


@app.get("/api/map")
def map_feed():
    """The map's data as JSON. The home page polls this to stay live."""
    summary = map_summary()
    items = [
        {
            "id": item.id,
            "title": item.title,
            "status": item.status,
            "category": item.category,
            "place": item.place,
            "side": item.place_side or "inside",
            "where": item.location,
            "custody": item.custody_label if item.status == "Found" else "",
            "at": item.created_at.isoformat() + "Z",
            "url": url_for("item_detail", item_id=item.id),
            "image": item.thumb_src or item.image_src,
        }
        for item in summary["items"]
    ]
    body = {"places": summary["places"], "desk": summary["desk"], "hotspots": summary["hotspots"],
            "items": items}
    response = app.response_class(json.dumps(body), mimetype="application/json")
    response.headers["Cache-Control"] = "no-store"
    return response


@app.route("/")
def index():
    query = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    status = request.args.get("status", "").strip()
    sort_order = request.args.get("sort", "newest").strip()

    place = request.args.get("place", "").strip()

    items_query = Item.query
    if query:
        items_query = items_query.filter(search_filter(query))
    if place:
        items_query = items_query.filter(Item.place.in_(campus.GROUPS.get(place, [place])))
    if category:
        items_query = items_query.filter_by(category=category)
    if status in {"Lost", "Found", "Resolved"}:
        items_query = items_query.filter_by(status=status)

    if sort_order == "oldest":
        ordered = items_query.order_by(Item.created_at.asc())
    else:
        sort_order = "newest"
        ordered = items_query.order_by(Item.created_at.desc())

    # Every report used to be loaded on every visit. One page at a time keeps
    # the query bounded however many reports the campus files.
    page = request.args.get("page", 1, type=int)
    pages = db.paginate(ordered, page=max(page, 1), per_page=PAGE_SIZE, error_out=False)
    items = pages.items

    stats = {
        "total": Item.query.count(),
        "lost": Item.query.filter_by(status="Lost").count(),
        "found": Item.query.filter_by(status="Found").count(),
        "resolved": Item.query.filter_by(status="Resolved").count(),
    }
    categories = [
        row[0]
        for row in db.session.query(Item.category).distinct().order_by(Item.category).all()
    ]
    latest_item = Item.query.order_by(Item.created_at.desc()).first()

    return render_template(
        "index.html",
        items=items,
        pages=pages,
        stats=stats,
        categories=categories,
        latest_item=latest_item,
        query=query,
        selected_category=category,
        selected_status=status,
        selected_place=place,
        selected_place_name=campus.place_short(place) or place.title(),
        map_data=map_summary(),
        sort_order=sort_order,
    )


def text_filter(words):
    pattern = f"%{words}%"
    return or_(
        Item.title.ilike(pattern),
        Item.description.ilike(pattern),
        Item.location.ilike(pattern),
        Item.category.ilike(pattern),
    )


def search_filter(query):
    """Reports matching the words, plus reports at any place the search names.

    "mechanical parking" finds what was reported as "mech parking", and "bottle
    canteen" finds bottles at the canteen, the puff shop or Nandini.
    """
    places, rest = campus.split_search(query)
    if not places:
        return text_filter(query)
    at_place = Item.place.in_(places)
    return or_(text_filter(query), and_(at_place, text_filter(rest)) if rest else at_place)


@app.route("/register", methods=["GET", "POST"])
def register():
    if get_current_user():
        return redirect(url_for("index"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not name or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            flash("Enter a valid name and email address.", "error")
            return render_template("register.html")
        if len(password) < 6:
            flash("Password must be at least 6 characters.", "error")
            return render_template("register.html")
        if User.query.filter_by(email=email).first():
            flash("An account with that email already exists.", "error")
            return render_template("register.html")

        # Admin comes from ADMIN_EMAIL only. It used to be granted to whoever
        # registered first, which on a public deployment is a stranger.
        is_admin = bool(admin_email()) and email == admin_email()

        user = User(
            name=name,
            email=email,
            password_hash=generate_password_hash(password),
            is_admin=bool(is_admin),
            is_staff=email in staff_emails(),
        )
        db.session.add(user)
        db.session.commit()
        session["user_id"] = user.id
        flash("Account created. Welcome to ClassFind!", "success")
        return redirect(url_for("index"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if get_current_user():
        return redirect(url_for("index"))

    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(email=email).first()

        if not user or not check_password_hash(user.password_hash, password):
            flash("Incorrect email or password.", "error")
            return render_template("login.html")

        # ADMIN_EMAIL and STAFF_EMAILS may be set after the account was made.
        if apply_role_emails(user):
            db.session.commit()

        session["user_id"] = user.id
        next_url = request.args.get("next") or url_for("index")
        if not next_url.startswith("/") or next_url.startswith("//"):
            next_url = url_for("index")
        return redirect(next_url)

    return render_template("login.html")


@app.post("/logout")
def logout():
    session.clear()
    flash("You have been signed out.", "success")
    return redirect(url_for("index"))


@app.route("/me")
@login_required
def profile():
    user = get_current_user()
    items = (
        Item.query.filter(filed_by(user))
        .order_by(Item.created_at.desc())
        .all()
    )
    incoming_claims = (
        Claim.query.join(Item, Claim.item_id == Item.id)
        .filter(filed_by(user))
        .order_by(Claim.created_at.desc())
        .all()
    )
    outgoing_claims = (
        Claim.query.filter_by(claimant_id=user.id)
        .order_by(Claim.created_at.desc())
        .all()
    )
    return render_template(
        "profile.html",
        user=user,
        items=items,
        incoming_claims=incoming_claims,
        outgoing_claims=outgoing_claims,
    )


@app.route("/report", methods=["GET", "POST"])
@login_required
def report():
    user = get_current_user()

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        category = request.form.get("category", "").strip()
        location = request.form.get("location", "").strip()
        status = request.form.get("status", "").strip()

        if not all([title, description, category, location, status]):
            flash("Please fill in every required field.", "error")
            return render_template("report.html")
        if status not in {"Lost", "Found"}:
            flash("Choose either Lost or Found.", "error")
            return render_template("report.html")

        uploaded_url = None
        labels = []
        if request.files.get("image_file"):
            labels = photo_labels(request.files["image_file"])
            try:
                uploaded_url = save_uploaded_image(request.files["image_file"])
            except ValueError as exc:
                flash(str(exc), "error")
                return render_template("report.html")

        item = Item(
            title=title,
            description=description,
            category=category,
            location=location,
            status=status,
            reporter_name=user.name,
            contact=user.email,
            owner_id=user.id,
            image_url=uploaded_url,
            image_labels=",".join(labels) or None,
        )
        set_place(item, request.form.get("place"), request.form.get("place_side"))
        db.session.add(item)
        if status == "Found":
            # Every found item goes to the security desk. Staff logging one are at the desk already.
            if user.works_desk:
                item.custody = "held"
                log_custody(item, "Logged at the security desk", user)
            else:
                item.custody = "awaiting"
                log_custody(item, "Reported found; awaiting drop-off at the security desk", user)
        db.session.commit()
        if status == "Found":
            alert_possible_owners(item)
        if item.custody == "awaiting":
            flash("Report added. Please hand the item in at the security desk.", "success")
        else:
            flash("Your report has been added to ClassFind.", "success")
        return redirect(url_for("item_detail", item_id=item.id))

    return render_template("report.html")


@app.route("/item/<int:item_id>")
def item_detail(item_id):
    item = db.get_or_404(Item, item_id)
    user = get_current_user()
    existing_claim = None
    if user:
        existing_claim = Claim.query.filter_by(
            item_id=item.id, claimant_id=user.id
        ).order_by(Claim.created_at.desc()).first()

    return render_template(
        "item.html",
        item=item,
        can_manage=can_manage_item(item),
        existing_claim=existing_claim,
        show_custody=bool(user and (user.works_desk or owns_item(user, item))),
    )


@app.route("/item/<int:item_id>/edit", methods=["GET", "POST"])
@login_required
def edit_item(item_id):
    item = db.get_or_404(Item, item_id)
    if not can_manage_item(item):
        flash("You can only edit your own reports.", "error")
        return redirect(url_for("item_detail", item_id=item.id))

    if request.method == "POST":
        item.title = request.form.get("title", "").strip()
        item.description = request.form.get("description", "").strip()
        item.category = request.form.get("category", "").strip()
        # A place picked on the map earlier stays unless the location text changes.
        kept_place = item.place if request.form.get("location", "").strip() == item.location else ""
        item.location = request.form.get("location", "").strip()
        status = request.form.get("status", "").strip()

        if not all([item.title, item.description, item.category, item.location, status]):
            flash("Please fill in every required field.", "error")
            return render_template("edit.html", item=item)
        if status not in {"Lost", "Found", "Resolved"}:
            flash("Choose Lost, Found or Resolved.", "error")
            return render_template("edit.html", item=item)

        item.status = status
        set_place(item, request.form.get("place") or kept_place,
                  request.form.get("place_side") or item.place_side or "")

        if request.form.get("remove_image") == "1":
            delete_image(item.image_url)
            item.image_url = None
            item.image_labels = None

        if request.files.get("image_file"):
            labels = photo_labels(request.files["image_file"])
            try:
                new_url = save_uploaded_image(request.files["image_file"])
            except ValueError as exc:
                flash(str(exc), "error")
                return render_template("edit.html", item=item)
            delete_image(item.image_url)
            item.image_url = new_url
            item.image_labels = ",".join(labels) or None

        db.session.commit()
        # An edit changes neither the counts nor the newest date the cache key
        # is built from, so the matches would keep showing the old wording.
        _match_cache["key"] = None
        flash("Report updated successfully.", "success")
        return redirect(url_for("item_detail", item_id=item.id))

    return render_template("edit.html", item=item)


@app.post("/item/<int:item_id>/resolve")
@login_required
def resolve_item(item_id):
    item = db.get_or_404(Item, item_id)
    if not can_manage_item(item):
        flash("You can only manage your own reports.", "error")
        return redirect(url_for("item_detail", item_id=item.id))
    if item.custody in IN_STORAGE and not get_current_user().is_admin:
        flash("An item held at the desk is closed by releasing it to its owner.", "error")
        return redirect(url_for("item_detail", item_id=item.id))

    item.status = "Resolved"
    db.session.commit()
    flash("This report has been marked as resolved.", "success")
    return redirect(url_for("item_detail", item_id=item.id))


@app.post("/item/<int:item_id>/delete")
@login_required
def delete_item(item_id):
    item = db.get_or_404(Item, item_id)
    if not can_manage_item(item):
        flash("You can only manage your own reports.", "error")
        return redirect(url_for("item_detail", item_id=item.id))

    delete_image(item.image_url)
    db.session.delete(item)
    db.session.commit()
    flash("Report deleted.", "success")
    return redirect(url_for("index"))


@app.post("/item/<int:item_id>/claim")
@login_required
def submit_claim(item_id):
    item = db.get_or_404(Item, item_id)
    user = get_current_user()

    if item.status != "Found":
        flash("Claims can only be submitted for active found items.", "error")
        return redirect(url_for("item_detail", item_id=item.id))
    if owns_item(user, item):
        flash("You cannot claim your own found report.", "error")
        return redirect(url_for("item_detail", item_id=item.id))

    message = request.form.get("message", "").strip()
    minimum = 30 if item.is_valuable else 10
    if len(message) < minimum:
        if item.is_valuable:
            flash("For a valuable item, give a detail only the owner would know, such as a "
                  "serial number, IMEI, lock screen or a mark on it.", "error")
        else:
            flash("Tell the security desk why you believe this item is yours.", "error")
        return redirect(url_for("item_detail", item_id=item.id))

    existing = Claim.query.filter(
        Claim.item_id == item.id, Claim.claimant_id == user.id,
        Claim.status.in_(("Pending", "Approved")),
    ).first()
    if existing:
        flash("You already have an open claim for this item.", "error")
        return redirect(url_for("item_detail", item_id=item.id))

    claim = Claim(
        item_id=item.id,
        claimant_id=user.id,
        message=message,
        status="Pending",
    )
    db.session.add(claim)
    db.session.commit()
    flash("Claim submitted. The security desk will review it.", "success")
    return redirect(url_for("claims"))


@app.route("/claims")
@login_required
def claims():
    user = get_current_user()
    outgoing = (
        Claim.query.filter_by(claimant_id=user.id)
        .order_by(Claim.created_at.desc())
        .all()
    )
    return render_template("claims.html", outgoing=outgoing)


def can_manage_claim(claim):
    """Claims are decided at the security desk, not by whoever found the item."""
    user = get_current_user()
    return bool(user and user.works_desk)


@app.post("/claims/<int:claim_id>/accept")
@login_required
def accept_claim(claim_id):
    claim = db.get_or_404(Claim, claim_id)
    if not can_manage_claim(claim):
        flash("You cannot manage this claim.", "error")
        return redirect(url_for("claims"))
    if claim.status != "Pending":
        flash("This claim has already been decided.", "error")
        return redirect(url_for("desk"))
    if Claim.query.filter_by(item_id=claim.item_id, status="Approved").first():
        flash("Another claim for this item is already approved and awaiting collection.", "error")
        return redirect(url_for("desk"))

    issue_handover_code(claim)
    claim.status = "Approved"
    claim.decided_at = datetime.utcnow()
    log_custody(claim.item, f"Claim by {claim.claimant.name} approved; collection code issued",
                get_current_user())
    db.session.commit()
    email_collection_code(claim)
    flash("Claim approved. The owner now has a collection code.", "success")
    return redirect(url_for("desk"))


def email_collection_code(claim):
    send_email(
        claim.claimant.email,
        f"Your claim for {claim.item.title} is approved",
        f"Hi {claim.claimant.name},\n\nThe security desk approved your claim for "
        f"\"{claim.item.title}\".\n\nYour collection code is {claim.handover_code[:3]} {claim.handover_code[3:]}"
        f" and it is valid until {claim.code_expires_at.strftime('%d %b, %I:%M %p')} UTC.\n\n"
        "Bring this code and your college ID to the security desk to collect it. "
        "Don't share the code with anyone.",
    )


def issue_handover_code(claim):
    claim.handover_code = f"{randbelow(10**6):06d}"
    claim.code_expires_at = datetime.utcnow() + timedelta(hours=HANDOVER_CODE_HOURS)


@app.post("/claims/<int:claim_id>/withdraw")
@login_required
def withdraw_claim(claim_id):
    claim = db.get_or_404(Claim, claim_id)
    user = get_current_user()

    if claim.claimant_id != user.id:
        flash("You can only withdraw your own claims.", "error")
        return redirect(url_for("claims"))
    if claim.status != "Pending":
        flash("Only pending claims can be withdrawn.", "error")
        return redirect(url_for("claims"))

    db.session.delete(claim)
    db.session.commit()
    flash("Your claim was withdrawn.", "success")
    return redirect(url_for("claims"))


@app.post("/claims/<int:claim_id>/reject")
@login_required
def reject_claim(claim_id):
    claim = db.get_or_404(Claim, claim_id)
    if not can_manage_claim(claim):
        flash("You cannot manage this claim.", "error")
        return redirect(url_for("claims"))
    if claim.status not in {"Pending", "Approved"}:
        flash("This claim has already been decided.", "error")
        return redirect(url_for("desk"))

    claim.status = "Rejected"
    claim.decided_at = datetime.utcnow()
    claim.handover_code = None
    log_custody(claim.item, f"Claim by {claim.claimant.name} rejected", get_current_user())
    db.session.commit()
    send_email(
        claim.claimant.email,
        f"Update on your claim for {claim.item.title}",
        f"Hi {claim.claimant.name},\n\nThe security desk could not approve your claim for "
        f"\"{claim.item.title}\". If it is yours, visit the desk with more proof, such as a "
        "receipt, a serial number or a photo of you with it.",
    )
    flash("Claim rejected.", "success")
    return redirect(url_for("desk"))


# Words that mark a Hinglish or Kanglish sentence and carry no item details.
MIXED_WORDS = {
    "mujhe", "mera", "meri", "mere", "maine", "humne", "ek", "mein", "me", "hai", "tha", "thi", "ho",
    "mila", "mili", "mile", "gaya", "gayi", "kho", "gum", "ko", "ka", "ki", "ke", "se", "par", "pe", "aaj",
    "kal", "yeh", "ye", "woh", "wo", "nanna", "nange", "nanu", "ondu", "alli", "sikkitu", "sikkide",
    "hoyitu", "kaledu", "kalkonde", "ivattu", "ninne", "hatra", "hathra", "andar", "bahar", "paas",
}


def parse_mixed_rules(text, status):
    """A Hinglish or Kanglish sentence without a model: drop the joining words and the places."""
    words = [w for w in re.findall(r"[\w']+", text) if w.lower() not in MIXED_WORDS]
    _, rest = campus.split_search(" ".join(words))
    title = " ".join(w for w in rest.split() if w not in STOP_WORDS)[:80].strip()
    first = campus.find_places(text)
    category = next((name for name, keys in CATEGORY_WORDS.items()
                     if any(re.search(rf"\b{re.escape(k)}\b", title.lower()) for k in keys)), "Other")
    return {
        "title": title[:1].upper() + title[1:] if title else "",
        "description": text.strip()[:1000],
        "category": category,
        "location": first[0]["words"].title() if first else "",
        "status": status,
    }


REPORT_CATEGORIES = [
    "Electronics", "Books & Notes", "Wallet & ID", "Keys",
    "Clothing", "Stationery", "Accessories", "Other",
]
CATEGORY_WORDS = {
    "Electronics": ("phone", "iphone", "laptop", "charger", "earbuds", "airpods", "headphone",
                    "calculator", "tablet", "ipad", "watch", "power bank", "mouse", "pendrive"),
    "Books & Notes": ("book", "notebook", "notes", "textbook", "record", "diary", "file"),
    "Wallet & ID": ("wallet", "purse", "id card", "identity", "aadhaar", "card", "license"),
    "Keys": ("key", "keys", "keychain"),
    "Clothing": ("jacket", "hoodie", "sweater", "shirt", "cap", "scarf", "shoe", "coat"),
    "Stationery": ("pen", "pencil", "geometry", "stapler", "scale", "eraser"),
    "Accessories": ("bottle", "bag", "backpack", "umbrella", "spectacles", "glasses", "sunglasses", "lunch box"),
}


LANGUAGES_NOTE = (
    "The student may write or speak in English, Hindi, Kannada, Hinglish, Kanglish or a mix. "
    "Always reply in English. Found means they found or picked up something (found, mila, mili, "
    "mil gaya, sikkitu, sikkide); Lost means they lost it (lost, kho gaya, gum gaya, kaledu hoyitu). "
)
REPORT_PROMPT = (
    "Extract a campus lost-and-found report. " + LANGUAGES_NOTE +
    "Reply with JSON only: "
    '{"title": the item in one to four English words, e.g. "Key" or "Black water bottle", '
    '"description": one or two English sentences describing the item for whoever might own it, using '
    "only details the student gave (colour, brand, size, marks, what was with it); leave out the place "
    'and phrases like "I found", '
    f'"category": one of {REPORT_CATEGORIES}, "location": the place on campus in English or "", '
    '"status": "Lost" or "Found"}. Do not invent details.'
)


def parse_report_rules(text):
    """Fill the report form from one sentence without a model.

    Good enough for "lost my black bottle near the library": the verb decides
    Lost or Found, keywords pick the category, and the words after a place
    preposition become the location.
    """
    lowered = text.lower()
    found_words = r"\b(found|picked up|someone left|left behind|mila|mili|mile|mil gaya|mil gayi|milgaya|sikkitu|sikkide|sikkid[ae])\b"
    status = "Found" if re.search(found_words, lowered) else "Lost"
    if set(lowered.split()) & MIXED_WORDS and len(set(lowered.split()) & MIXED_WORDS) >= 2:
        return parse_mixed_rules(text, status)
    category = "Other"
    for name, words in CATEGORY_WORDS.items():
        if any(re.search(rf"\b{re.escape(word)}\b", lowered) for word in words):
            category = name
            break
    location = ""
    match = re.search(r"\b(?:near|at|in|inside|outside|beside|behind)\s+(?:the\s+)?([a-z0-9 ,'-]{3,60})", lowered)
    if match:
        location = re.split(r"\b(?:yesterday|today|this|last|on|around|while|and|but|when)\b|[.;]",
                            match.group(1))[0].strip(" ,'-")
        location = " ".join(word[:1].upper() + word[1:] for word in location.split())
    title = re.sub(
        r"^(?:i\s+)?(?:have\s+)?(?:lost|found|picked up|misplaced|someone left)\s+(?:my|an|a|the|someone'?s)?\s*",
        "", text.strip(), flags=re.IGNORECASE,
    )
    title = re.split(r"\s+(?:near|at|in|inside|outside|beside|behind|yesterday|today)\b", title, maxsplit=1)[0]
    title = title.strip(" .,")[:80]
    # Capitalise "black bottle" but leave names like "iPhone" alone.
    if title[:1].islower() and not title[1:2].isupper():
        title = title[:1].upper() + title[1:]
    return {
        "title": title,
        "description": text.strip()[:1000],
        "category": category,
        "location": location[:120],
        "status": status,
    }


# OpenAI-compatible chat endpoints that can fill the report form. Whichever key
# is set decides the provider; LLM_MODEL overrides the default model.
LLM_PROVIDERS = (
    ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
     "gemini-2.5-flash"),
    ("CEREBRAS_API_KEY", "https://api.cerebras.ai/v1/chat/completions", "llama-3.3-70b"),
    ("GROQ_API_KEY", "https://api.groq.com/openai/v1/chat/completions", "llama-3.3-70b-versatile"),
)


def llm_provider():
    """(url, key, model) for the first provider with a key set, or None."""
    for key_name, url, model in LLM_PROVIDERS:
        key = os.getenv(key_name, "").strip()
        if key:
            return url, key, os.getenv("LLM_MODEL", "").strip() or model
    return None


def parse_report_with_model(text, prompt=None):
    """Ask an LLM on Gemini, Cerebras or Groq to fill the form. Returns None without a key or on any failure."""
    provider = llm_provider()
    if not provider:
        return None
    url, key, model = provider
    prompt = prompt or REPORT_PROMPT
    body = json.dumps({
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": prompt}, {"role": "user", "content": text}],
    }).encode()
    request_ = urllib.request.Request(
        url, data=body, method="POST",
        # Some providers refuse urllib's default user agent.
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "User-Agent": "ClassFind/2.0"},
    )
    try:
        with urllib.request.urlopen(request_, timeout=8) as response:
            reply = json.loads(response.read())
        content = reply["choices"][0]["message"]["content"].strip()
        # Tolerate a reply wrapped in a ```json fence.
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
        return json.loads(content)
    except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError):
        app.logger.warning("Report parsing by the model failed; using the rules instead.")
        return None


def clean_parsed_report(fields, text):
    """Keep only valid values, so the model can never put junk in the form."""
    fallback = parse_report_rules(text)
    if not isinstance(fields, dict):
        return fallback
    result = {}
    for name, limit in (("title", 120), ("description", 1000), ("location", 120)):
        value = fields.get(name)
        result[name] = value.strip()[:limit] if isinstance(value, str) and value.strip() else fallback[name]
    result["category"] = fields.get("category") if fields.get("category") in REPORT_CATEGORIES else fallback["category"]
    result["status"] = fields.get("status") if fields.get("status") in {"Lost", "Found"} else fallback["status"]
    return result


@app.post("/report/parse")
@login_required
def parse_report():
    """Turn one sentence into report fields for the form to review before publishing."""
    text = (request.get_json(silent=True) or {}).get("text", "").strip()
    if len(text) < 5:
        return {"error": "Describe the item in a sentence."}, 400
    text = text[:600]
    parsed = parse_report_with_model(text)
    return {"fields": clean_parsed_report(parsed, text), "source": "model" if parsed else "rules"}


RETRACE_PROMPT = (
    "A student describes what they lost and where they went on campus. " + LANGUAGES_NOTE +
    "Reply with JSON only: "
    '{"item": the lost item in a few English words or "", '
    '"places": the campus places they mention, in the order they went, each in English, '
    '"when": "today", "yesterday" or "week"}. '
    "Use the place names as said, translated to English; do not add places that are not mentioned."
)
WHEN_WORDS = (("week", r"\b(this|last|past) week\b|\bdays? ago\b"), ("yesterday", r"\byesterday\b|ನಿನ್ನೆ|कल"))


def parse_retrace_rules(text):
    """What was lost, where, and when, from one sentence without a model."""
    lowered = text.lower()
    when = next((name for name, pattern in WHEN_WORDS if re.search(pattern, lowered)), "today")
    item = parse_report_rules(text)["title"]
    item = re.split(r",|(?:i|and|then|was|when|while|went)", item, flags=re.IGNORECASE)[0].strip(" .")
    # The title rule stops at the first place word; anything left that names a place is not the item.
    if campus.find_places(item):
        item = ""
    return {"item": item, "places": [text], "when": when}


def plan_retrace(text, parsed=None):
    """Turn a sentence (or what the model already made of it) into Retrace's stops, words and time."""
    if parsed is None:
        parsed = parse_report_with_model(text, RETRACE_PROMPT)
    source = "model" if isinstance(parsed, dict) else "rules"
    if source == "rules":
        parsed = parse_retrace_rules(text)
    phrases = parsed.get("places") if isinstance(parsed.get("places"), list) else [text]
    stops = []
    for phrase in phrases[:12]:
        if not isinstance(phrase, str):
            continue
        for mention in campus.find_places(phrase):
            ids = mention["ids"]
            if ids and (not stops or stops[-1]["ids"] != ids):
                stops.append({"ids": ids, "names": [campus.place_short(i) for i in ids]})
    if not stops and source == "model":
        stops = [{"ids": m["ids"], "names": [campus.place_short(i) for i in m["ids"]]}
                 for m in campus.find_places(text)]
    item = parsed.get("item") if isinstance(parsed.get("item"), str) else ""
    when = parsed.get("when") if parsed.get("when") in {"today", "yesterday", "week"} else "today"
    return {"stops": stops, "item": item.strip()[:80], "when": when, "source": source}


@app.post("/api/retrace/parse")
def parse_retrace():
    """Read one spoken or typed sentence into a Retrace search."""
    text = (request.get_json(silent=True) or {}).get("text", "").strip()[:600]
    if len(text) < 3:
        return {"error": "Say where you went and what you lost."}, 400
    return plan_retrace(text)


VOICE_PROMPT = (
    "Listen to a student talking to a campus lost-and-found app. " + LANGUAGES_NOTE +
    "Reply with JSON only: "
    '{"transcript": what they said, in the script they spoke, '
    '"english": the same in plain English, '
    '"status": "Lost" or "Found", '
    '"title": the item in one to four English words or "", '
    '"description": one or two English sentences describing the item for whoever might own it, using only '
    "details they gave (colour, brand, size, marks); no place and no \"I found\", "
    f'"category": one of {REPORT_CATEGORIES}, '
    '"location": where the item was, in English, or "", '
    '"places": every campus place they mention, in English, in the order they went there, '
    '"when": "today", "yesterday" or "week"}. Do not invent anything they did not say.'
)
VOICE_MAX_BYTES = 2 * 1024 * 1024
GEMINI_AUDIO_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


def hear_with_gemini(audio):
    """Gemini listens to the clip and fills VOICE_PROMPT. None without a key or on any failure.

    Sending the audio, rather than text from the browser, is what lets one
    request understand Kannada, Hindi, English and any mix of them.
    """
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        return None
    model = os.getenv("GEMINI_AUDIO_MODEL", "gemini-2.5-flash")
    body = json.dumps({
        "contents": [{"parts": [
            {"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(audio).decode()}},
            {"text": VOICE_PROMPT},
        ]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }).encode()
    request_ = urllib.request.Request(
        GEMINI_AUDIO_URL.format(model=model), data=body, method="POST",
        headers={"x-goog-api-key": key, "Content-Type": "application/json", "User-Agent": "ClassFind/3.0"},
    )
    try:
        with urllib.request.urlopen(request_, timeout=25) as response:
            reply = json.loads(response.read())
        content = reply["candidates"][0]["content"]["parts"][0]["text"].strip()
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content)
        heard = json.loads(content)
        return heard if isinstance(heard, dict) else None
    except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError):
        app.logger.warning("Gemini could not read the voice clip; using the browser's text instead.")
        return None


def search_from(heard_text, item, places_said):
    """A spoken search: the words to look for and, if one place or one group was named, that place."""
    place = ""
    for phrase in places_said:
        mentions = campus.find_places(phrase) if isinstance(phrase, str) else []
        if mentions:
            ids = mentions[0]["ids"]
            group = next((name for name, members in campus.GROUPS.items() if members == ids), "")
            place = ids[0] if len(ids) == 1 else group
            break
    if not item:
        _, item = campus.split_search(heard_text)
    return {"q": item.strip()[:80], "place": place}


@app.post("/api/voice")
def voice():
    """One spoken clip, read for Retrace, search or a report.

    The page sends the recording (16 kHz WAV) and, when the browser could
    caption it, its own text. Gemini hears the recording; if that fails the
    browser's text goes through the same readers a typed sentence would.
    """
    mode = request.form.get("mode", "")
    if mode not in {"retrace", "search", "report"}:
        return {"error": "Unknown voice mode."}, 400
    audio = request.files.get("audio")
    data = audio.read(VOICE_MAX_BYTES + 1) if audio else b""
    if len(data) > VOICE_MAX_BYTES:
        return {"error": "That was too long. Keep it under half a minute."}, 413
    captions = request.form.get("transcript", "").strip()[:600]
    heard = hear_with_gemini(data) if data[:4] == b"RIFF" else None
    if heard is None and len(captions) < 3:
        return {"error": "Couldn't make that out. Try again, or type it."}, 422

    def text_of(name):
        value = heard.get(name) if heard else ""
        return value.strip() if isinstance(value, str) else ""

    said = text_of("transcript") or captions
    english = text_of("english") or said
    places_said = [p for p in (heard.get("places") if heard and isinstance(heard.get("places"), list) else [])
                   if isinstance(p, str)]
    result = {"heard": said, "source": "gemini" if heard else "browser"}
    if mode == "retrace":
        parsed = None
        if heard:
            parsed = {"item": text_of("title"), "places": places_said or [english],
                      "when": heard.get("when")}
        result.update(plan_retrace(english, parsed))
        result["source"] = "gemini" if heard else result["source"]
    elif mode == "search":
        result.update(search_from(english, text_of("title"), places_said or [english]))
    else:
        if heard:
            fields = clean_parsed_report(heard, english)
        else:
            fields = clean_parsed_report(parse_report_with_model(said), said)
        result["fields"] = fields
    return result


ALERT_SCORE = 50


def alert_possible_owners(found):
    """Email the people whose lost reports look like this newly found item."""
    for lost, matched, score, reasons in build_matches():
        if matched.id != found.id or score < ALERT_SCORE:
            continue
        owner = db.session.get(User, lost.owner_id) if lost.owner_id else None
        if not owner:
            continue
        send_email(
            owner.email,
            f"A found item may be your {lost.title}",
            f"Hi {owner.name},\n\nSomeone reported a found item that matches your lost report "
            f"\"{lost.title}\" ({score}% match: {', '.join(reasons)}).\n\n"
            f"Found item: {found.title}, near {found.location}.\n"
            f"{url_for('item_detail', item_id=found.id, _external=True)}\n\n"
            "If it's yours, open the link and submit a claim.",
        )


MAX_TAGS = 10


def qr_svg(url):
    """The QR code for a URL as an SVG string, drawn without any image library."""
    image = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage, border=2)
    return image.to_string(encoding="unicode")


@app.route("/tags", methods=["GET", "POST"])
@login_required
def tags():
    user = get_current_user()
    if request.method == "POST":
        label = request.form.get("label", "").strip()[:60]
        if not label:
            flash("Name the item the tag goes on, e.g. Blue backpack.", "error")
        elif Tag.query.filter_by(owner_id=user.id).count() >= MAX_TAGS:
            flash(f"You can have up to {MAX_TAGS} tags. Delete one you no longer use.", "error")
        else:
            db.session.add(Tag(owner_id=user.id, label=label, token=token_urlsafe(9)))
            db.session.commit()
            flash("Tag created. Print it and stick it on the item.", "success")
        return redirect(url_for("tags"))
    own = Tag.query.filter_by(owner_id=user.id).order_by(Tag.created_at.desc()).all()
    codes = {tag.id: qr_svg(url_for("scan_tag", token=tag.token, _external=True)) for tag in own}
    return render_template("tags.html", tags=own, codes=codes, max_tags=MAX_TAGS)


@app.post("/tags/<int:tag_id>/delete")
@login_required
def delete_tag(tag_id):
    tag = db.get_or_404(Tag, tag_id)
    if tag.owner_id != get_current_user().id:
        abort(404)
    db.session.delete(tag)
    db.session.commit()
    flash("Tag deleted. Its QR code no longer works.", "success")
    return redirect(url_for("tags"))


@app.route("/t/<token>")
def scan_tag(token):
    tag = Tag.query.filter_by(token=token).first_or_404()
    return render_template("scan.html", tag=tag)


@app.post("/t/<token>/found")
@login_required
def report_tag_found(token):
    """File a found report for a tagged item and tell its owner straight away."""
    tag = Tag.query.filter_by(token=token).first_or_404()
    finder = get_current_user()
    if tag.owner_id == finder.id:
        flash("That's your own tag.", "error")
        return redirect(url_for("scan_tag", token=token))
    location = request.form.get("location", "").strip()[:120]
    if not location:
        flash("Say where you found it.", "error")
        return redirect(url_for("scan_tag", token=token))
    item = Item(
        title=tag.label,
        description=f"Found with a ClassFind QR tag. {request.form.get('note', '').strip()[:500]}".strip(),
        category="Other",
        location=location,
        status="Found",
        reporter_name=finder.name,
        contact=finder.email,
        owner_id=finder.id,
    )
    set_place(item, request.form.get("place"), request.form.get("place_side"))
    db.session.add(item)
    if finder.works_desk:
        item.custody = "held"
        log_custody(item, "Logged at the security desk from a QR tag scan", finder)
    else:
        item.custody = "awaiting"
        log_custody(item, "Found by QR tag scan; awaiting drop-off at the security desk", finder)
    db.session.commit()
    send_email(
        tag.owner.email,
        f"Your {tag.label} was found",
        f"Hi {tag.owner.name},\n\nSomeone scanned the ClassFind tag on your {tag.label} and reported it "
        f"found near {location}. It is going to the security desk.\n\n"
        f"Claim it here: {url_for('item_detail', item_id=item.id, _external=True)}",
    )
    flash("Thanks! The owner has been told. Please hand it in at the security desk.", "success")
    return redirect(url_for("item_detail", item_id=item.id))


@app.route("/desk")
@staff_required
def desk():
    moved = escalate_unclaimed_valuables()
    if moved:
        flash(f"{moved} unclaimed valuable item{'s' if moved != 1 else ''} moved to the admin office.", "success")
    awaiting = Item.query.filter_by(status="Found", custody="awaiting").order_by(Item.created_at).all()
    held = (Item.query.filter(Item.status == "Found", Item.custody.in_(IN_STORAGE))
            .order_by(Item.created_at).all())
    pending = Claim.query.filter_by(status="Pending").order_by(Claim.created_at).all()
    approved = Claim.query.filter_by(status="Approved").order_by(Claim.decided_at).all()
    return render_template("desk.html", awaiting=awaiting, held=held, pending=pending, approved=approved)


@app.post("/desk/item/<int:item_id>/received")
@staff_required
def desk_receive(item_id):
    item = db.get_or_404(Item, item_id)
    if item.status != "Found" or item.custody != "awaiting":
        flash("That item is not awaiting drop-off.", "error")
        return redirect(url_for("desk"))
    item.custody = "held"
    log_custody(item, "Received at the security desk", get_current_user())
    db.session.commit()
    finder = db.session.get(User, item.owner_id) if item.owner_id else None
    if finder:
        send_email(
            finder.email,
            f"Thanks for handing in {item.title}",
            f"Hi {finder.name},\n\nThe security desk has received \"{item.title}\". "
            "We'll take it from here. Thank you for returning it.",
        )
    flash(f"{item.title} is now held at the desk.", "success")
    return redirect(url_for("desk"))


@app.post("/claims/<int:claim_id>/reissue")
@staff_required
def reissue_code(claim_id):
    claim = db.get_or_404(Claim, claim_id)
    if claim.status != "Approved":
        flash("Only approved claims have a collection code.", "error")
        return redirect(url_for("desk"))
    issue_handover_code(claim)
    log_custody(claim.item, f"New collection code issued to {claim.claimant.name}", get_current_user())
    db.session.commit()
    email_collection_code(claim)
    flash("A new collection code was issued.", "success")
    return redirect(url_for("desk"))


@app.post("/desk/handover")
@staff_required
def desk_handover():
    """Release an item to the claimant whose collection code this is.

    The code must belong to an approved, unexpired claim on an item the desk
    holds. The claim becomes Collected, the item Resolved, any other open claim
    on it Rejected, and the release is logged under the staff member.
    """
    code = re.sub(r"[^0-9]", "", request.form.get("code", ""))
    claim = None
    if len(code) == 6:
        for candidate in Claim.query.filter_by(status="Approved").all():
            if candidate.handover_code and compare_digest(candidate.handover_code, code):
                claim = candidate
                break
    if not claim:
        flash("That code does not match an approved claim.", "error")
        return redirect(url_for("desk"))
    if not claim.code_is_live:
        flash("That code has expired. Issue a new one from the approved claims list.", "error")
        return redirect(url_for("desk"))
    if claim.item.custody not in IN_STORAGE:
        flash("The item has not been received at the desk yet.", "error")
        return redirect(url_for("desk"))

    staff = get_current_user()
    now = datetime.utcnow()
    claim.status = "Collected"
    claim.collected_at = now
    claim.released_by = staff
    claim.handover_code = None
    claim.item.status = "Resolved"
    claim.item.custody = "released"
    for other in Claim.query.filter(
        Claim.item_id == claim.item_id, Claim.id != claim.id,
        Claim.status.in_(("Pending", "Approved")),
    ).all():
        other.status = "Rejected"
        other.decided_at = now
        other.handover_code = None
    log_custody(claim.item, f"Released to {claim.claimant.name} ({claim.claimant.email})", staff)
    db.session.commit()
    send_email(
        claim.claimant.email,
        f"You collected {claim.item.title}",
        f"Hi {claim.claimant.name},\n\n\"{claim.item.title}\" was released to you at the security desk "
        f"by {staff.name} on {claim.collected_at.strftime('%d %b %Y, %I:%M %p')} UTC. "
        "If you did not collect it, tell the security desk straight away.",
    )
    flash(f"Released {claim.item.title} to {claim.claimant.name}.", "success")
    return redirect(url_for("desk"))


@app.post("/admin/users/<int:user_id>/staff")
@admin_required
def toggle_staff(user_id):
    """Take desk access away. Giving it is done by invitation, see invite_staff()."""
    user = db.get_or_404(User, user_id)
    if not user.is_staff:
        flash("To add desk staff, invite them by email.", "error")
        return redirect(url_for("admin_dashboard"))
    user.is_staff = False
    db.session.commit()
    flash(f"{user.name} can no longer work the security desk.", "success")
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/staff/invite")
@admin_required
def invite_staff():
    email = request.form.get("email", "").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        flash("Enter the email address of the person to invite.", "error")
        return redirect(url_for("admin_dashboard"))
    existing = User.query.filter(db.func.lower(User.email) == email).first()
    if existing and existing.works_desk:
        flash(f"{existing.name} already works the desk.", "error")
        return redirect(url_for("admin_dashboard"))
    for old in StaffInvite.query.filter_by(email=email, responded_at=None):
        db.session.delete(old)
    invite = StaffInvite(email=email, token=token_urlsafe(16), invited_by=get_current_user())
    db.session.add(invite)
    db.session.commit()
    send_email(
        email,
        "You've been invited to work the ClassFind security desk",
        "You've been invited to help at the BIT security desk on ClassFind: receiving handed-in "
        "items, checking claims and releasing items with a collection code.\n\n"
        f"Sign in with this email address and accept here within {INVITE_DAYS} days:\n"
        f"{url_for('staff_invite', token=invite.token, _external=True)}",
    )
    flash(f"Invitation sent to {email}. They get desk access once they accept.", "success")
    return redirect(url_for("admin_dashboard"))


@app.post("/admin/staff/invite/<int:invite_id>/cancel")
@admin_required
def cancel_invite(invite_id):
    invite = db.get_or_404(StaffInvite, invite_id)
    if invite.responded_at is None:
        db.session.delete(invite)
        db.session.commit()
        flash(f"Invitation to {invite.email} cancelled.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/staff/invite/<token>", methods=["GET", "POST"])
@login_required
def staff_invite(token):
    """The invited person accepts or declines, signed in as the invited address."""
    invite = StaffInvite.query.filter_by(token=token).first_or_404()
    user = get_current_user()
    if invite.email != user.email.lower():
        flash(f"This invitation is for {invite.email}. Sign in with that account to answer it.", "error")
        return redirect(url_for("index"))
    if not invite.is_open:
        flash("This invitation has already been answered or has expired.", "error")
        return redirect(url_for("index"))
    if request.method == "POST":
        accepted = request.form.get("answer") == "accept"
        invite.accepted = accepted
        invite.responded_at = datetime.utcnow()
        if accepted:
            user.is_staff = True
        db.session.commit()
        flash("You can now work the security desk." if accepted else "Invitation declined.", "success")
        return redirect(url_for("desk" if accepted else "index"))
    return render_template("staff_invite.html", invite=invite)


# A pair below this is not worth showing. The two reports the matches page
# compares are capped so one popular week cannot turn this into a slow page.
MATCH_THRESHOLD = 0.25
MATCH_SCAN_LIMIT = 500
TITLE_WEIGHT = 0.25

STOP_WORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "at", "for",
    "with", "is", "my", "this", "that", "item", "lost", "found", "near",
}


def tokens(value):
    words = re.findall(r"[a-z0-9]+", value.lower())
    return {word for word in words if len(word) > 2 and word not in STOP_WORDS}


def overlap(first, second):
    if not first or not second:
        return 0.0
    return len(first & second) / len(first | second)


def build_matches():
    """Rank Lost reports against Found reports by how much they have in common.

    Two things keep this affordable. Each report is tokenised once instead of
    once per comparison, and the title similarity, which is the expensive part,
    is skipped for any pair that could not reach the cut-off even with a perfect
    title. SequenceMatcher offers two cheap upper bounds on its own result,
    real_quick_ratio and quick_ratio, so a pair is dropped as soon as one of
    them puts it out of reach. Both are upper bounds, so nothing that would have
    been shown is lost.

    Equal scores are ordered by report id, so the page does not reshuffle
    between requests.
    """
    lost_items = (
        Item.query.filter_by(status="Lost")
        .order_by(Item.created_at.desc())
        .limit(MATCH_SCAN_LIMIT)
        .all()
    )
    found_items = (
        Item.query.filter_by(status="Found")
        .order_by(Item.created_at.desc())
        .limit(MATCH_SCAN_LIMIT)
        .all()
    )

    def prepared(items):
        return [
            (
                item,
                tokens(f"{item.title} {item.description}"),
                tokens(item.location),
                item.place,
                item.title.lower(),
                item.category.lower(),
                {label.lower() for label in item.label_list},
            )
            for item in items
        ]

    lost_prepared = prepared(lost_items)
    pairs = []

    for found, found_text, found_location, found_place, found_title, found_category, found_labels in prepared(found_items):
        # One matcher per found title: its index of that string is built once
        # and reused against every lost title.
        matcher = SequenceMatcher(None, "", found_title)

        for lost, lost_text, lost_location, lost_place, lost_title, lost_category, lost_labels in lost_prepared:
            text_score = overlap(lost_text, found_text)
            if lost_place and lost_place == found_place:
                location_score = 1.0
            else:
                location_score = overlap(lost_location, found_location)
            days_apart = abs((lost.created_at - found.created_at).total_seconds()) / 86400
            recency_score = max(0.0, 1.0 - min(days_apart / 14.0, 1.0))
            same_category = lost_category == found_category

            score = (
                text_score * 0.40
                + location_score * 0.15
                + recency_score * 0.10
                + (0.10 if same_category else 0.0)
            )
            # Two photos Rekognition describes the same way add up to 0.10 more.
            shared_labels = lost_labels & found_labels
            if shared_labels:
                score += 0.10 * overlap(lost_labels, found_labels)

            matcher.set_seq1(lost_title)
            if score + TITLE_WEIGHT * matcher.real_quick_ratio() < MATCH_THRESHOLD:
                continue
            if score + TITLE_WEIGHT * matcher.quick_ratio() < MATCH_THRESHOLD:
                continue

            score = min(score + TITLE_WEIGHT * matcher.ratio(), 0.99)
            if score < MATCH_THRESHOLD:
                continue

            reasons = []
            if same_category:
                reasons.append("same category")
            if location_score > 0:
                reasons.append("similar location")
            if shared_labels:
                reasons.append(f"photos show: {', '.join(sorted(shared_labels)[:3])}")
            if recency_score >= 0.75:
                reasons.append("reported close together")
            shared = sorted(lost_text & found_text)
            if shared:
                reasons.append(f"shared terms: {', '.join(shared[:3])}")

            pairs.append((lost, found, round(score * 100), reasons or ["related details"]))

    pairs.sort(key=lambda pair: (-pair[2], pair[0].id, pair[1].id))
    return pairs[:30]


_match_cache = {"key": None, "pairs": []}


def matches_cache_key():
    """Changes whenever a report that matching reads is added, removed or changes status.

    An edit to the wording changes none of these, so edit_item() clears the
    cache itself.
    """
    counts = dict(
        db.session.query(Item.status, db.func.count(Item.id))
        .filter(Item.status.in_(("Lost", "Found")))
        .group_by(Item.status)
        .all()
    )
    newest = db.session.query(db.func.max(Item.created_at)).scalar()
    return (counts.get("Lost", 0), counts.get("Found", 0), newest, Item.query.count())


def cached_matches():
    """build_matches() is quadratic, so repeat visits reuse the last result."""
    key = matches_cache_key()
    if _match_cache["key"] != key:
        _match_cache["pairs"] = build_matches()
        _match_cache["key"] = key
    return _match_cache["pairs"]


@app.route("/matches")
def matches():
    return render_template("matches.html", pairs=cached_matches())


def dashboard_insights(weeks=8, now=None):
    """Numbers for the admin charts, computed from reports and the custody log.

    Kept to plain counts so every figure can be checked against the database:
    reports per week split into lost and found, the busiest categories and
    locations, how many found items reached their owner, where found items are
    now, and the median time from reaching the desk to being collected.
    """
    now = now or datetime.utcnow()
    start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    start -= timedelta(weeks=weeks - 1)
    items = Item.query.filter(Item.created_at >= start).all()

    weekly = []
    for index in range(weeks):
        week_start = start + timedelta(weeks=index)
        week_end = week_start + timedelta(weeks=1)
        in_week = [i for i in items if week_start <= i.created_at < week_end]
        # A resolved report started as lost or found; its custody tells which.
        found = sum(1 for i in in_week if i.status == "Found" or i.custody)
        weekly.append({"label": week_start.strftime("%d %b"), "lost": len(in_week) - found, "found": found})

    def top(column, limit):
        rows = (db.session.query(column, db.func.count(Item.id))
                .group_by(column).order_by(db.func.count(Item.id).desc(), column).limit(limit).all())
        return [{"label": label, "count": count} for label, count in rows]

    custody_counts = dict(
        db.session.query(Item.custody, db.func.count(Item.id))
        .filter(Item.custody.isnot(None)).group_by(Item.custody).all()
    )
    found_total = sum(custody_counts.values())
    returned = custody_counts.get("released", 0)

    hours = []
    for claim in Claim.query.filter_by(status="Collected").all():
        received = next((e.created_at for e in claim.item.events
                         if e.action.startswith(("Received at", "Logged at"))), None)
        if received and claim.collected_at:
            hours.append((claim.collected_at - received).total_seconds() / 3600)
    hours.sort()
    median_hours = None
    if hours:
        middle = len(hours) // 2
        median_hours = hours[middle] if len(hours) % 2 else (hours[middle - 1] + hours[middle]) / 2

    return {
        "weekly": weekly,
        "weekly_max": max([w["lost"] + w["found"] for w in weekly] + [1]),
        "categories": top(Item.category, 6),
        "locations": top(Item.location, 6),
        "custody": [
            {"key": key, "label": label, "count": custody_counts.get(key, 0)}
            for key, label in (("awaiting", "Awaiting drop-off"), ("held", "At the desk"),
                               ("office", "Admin office"), ("released", "Returned"))
        ],
        "found_total": found_total,
        "returned": returned,
        "return_rate": round(100 * returned / found_total) if found_total else 0,
        "median_hours": median_hours,
    }


@app.route("/admin")
@admin_required
def admin_dashboard():
    escalate_unclaimed_valuables()
    query = request.args.get("q", "").strip()
    status = request.args.get("status", "").strip()

    items_query = Item.query
    if query:
        pattern = f"%{query}%"
        items_query = items_query.filter(
            or_(
                Item.title.ilike(pattern),
                Item.description.ilike(pattern),
                Item.location.ilike(pattern),
                Item.category.ilike(pattern),
                Item.reporter_name.ilike(pattern),
                Item.contact.ilike(pattern),
            )
        )
    if status in {"Lost", "Found", "Resolved"}:
        items_query = items_query.filter_by(status=status)
    else:
        status = ""

    items = items_query.order_by(Item.created_at.desc()).limit(50).all()
    stats = {
        "total": Item.query.count(),
        "lost": Item.query.filter_by(status="Lost").count(),
        "found": Item.query.filter_by(status="Found").count(),
        "resolved": Item.query.filter_by(status="Resolved").count(),
    }
    return render_template(
        "admin.html",
        stats=stats,
        items=items,
        user_count=User.query.count(),
        users=User.query.order_by(User.created_at.desc()).limit(50).all(),
        invites=[i for i in StaffInvite.query.filter_by(responded_at=None).order_by(StaffInvite.created_at.desc())
                 if i.is_open],
        insights=dashboard_insights(),
        pending_claims=Claim.query.filter_by(status="Pending").count(),
        query=query,
        selected_status=status,
    )


@app.errorhandler(404)
def not_found(_error):
    return render_template("404.html"), 404


@app.errorhandler(400)
def bad_request(error):
    return render_template("400.html", message=getattr(error, "description", "Bad request.")), 400


@app.errorhandler(429)
def too_many_requests(error):
    return render_template("400.html", message=getattr(error, "description", "Too many attempts.")), 429


@app.errorhandler(413)
def too_large(_error):
    flash("Image is too large. Maximum upload size is 5 MB.", "error")
    return redirect(url_for("report"))


@app.errorhandler(500)
def server_error(_error):
    db.session.rollback()
    app.logger.exception("Unhandled application error")
    return render_template("500.html"), 500


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG") == "1")
