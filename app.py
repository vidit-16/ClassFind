import os
import re
import threading
import time
from datetime import datetime, timedelta
from hmac import compare_digest
from secrets import randbelow, token_urlsafe
from difflib import SequenceMatcher
from functools import wraps
from pathlib import Path
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

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
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
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
    "released": "Returned to its owner",
}
HANDOVER_CODE_HOURS = 48


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
        "item": {"custody": "VARCHAR(20)"},
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


with app.app_context():
    db.create_all()
    backfill_item_owners()
    add_desk_columns()

if not app.config["S3_BUCKET"]:
    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)

s3_client = boto3.client("s3", region_name=app.config["AWS_REGION"]) if app.config["S3_BUCKET"] else None


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
    return {
        "current_user": user,
        "incoming_claim_count": incoming,
        "outgoing_claim_count": outgoing,
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


@app.route("/")
def index():
    query = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    status = request.args.get("status", "").strip()
    sort_order = request.args.get("sort", "newest").strip()

    items_query = Item.query
    if query:
        pattern = f"%{query}%"
        items_query = items_query.filter(
            or_(
                Item.title.ilike(pattern),
                Item.description.ilike(pattern),
                Item.location.ilike(pattern),
                Item.category.ilike(pattern),
            )
        )
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
        sort_order=sort_order,
    )


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
        if request.files.get("image_file"):
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
        )
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
        item.location = request.form.get("location", "").strip()
        status = request.form.get("status", "").strip()

        if not all([item.title, item.description, item.category, item.location, status]):
            flash("Please fill in every required field.", "error")
            return render_template("edit.html", item=item)
        if status not in {"Lost", "Found", "Resolved"}:
            flash("Choose Lost, Found or Resolved.", "error")
            return render_template("edit.html", item=item)

        item.status = status

        if request.form.get("remove_image") == "1":
            delete_image(item.image_url)
            item.image_url = None

        if request.files.get("image_file"):
            try:
                new_url = save_uploaded_image(request.files["image_file"])
            except ValueError as exc:
                flash(str(exc), "error")
                return render_template("edit.html", item=item)
            delete_image(item.image_url)
            item.image_url = new_url

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
    if item.custody == "held" and not get_current_user().is_admin:
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
    flash("Claim approved. The owner now has a collection code.", "success")
    return redirect(url_for("desk"))


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
    flash("Claim rejected.", "success")
    return redirect(url_for("desk"))


@app.route("/desk")
@staff_required
def desk():
    awaiting = Item.query.filter_by(status="Found", custody="awaiting").order_by(Item.created_at).all()
    held = Item.query.filter_by(status="Found", custody="held").order_by(Item.created_at).all()
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
    if claim.item.custody != "held":
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
    flash(f"Released {claim.item.title} to {claim.claimant.name}.", "success")
    return redirect(url_for("desk"))


@app.post("/admin/users/<int:user_id>/staff")
@admin_required
def toggle_staff(user_id):
    user = db.get_or_404(User, user_id)
    user.is_staff = not user.is_staff
    db.session.commit()
    flash(f"{user.name} {'can now' if user.is_staff else 'can no longer'} work the security desk.", "success")
    return redirect(url_for("admin_dashboard"))


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
                item.title.lower(),
                item.category.lower(),
            )
            for item in items
        ]

    lost_prepared = prepared(lost_items)
    pairs = []

    for found, found_text, found_location, found_title, found_category in prepared(found_items):
        # One matcher per found title: its index of that string is built once
        # and reused against every lost title.
        matcher = SequenceMatcher(None, "", found_title)

        for lost, lost_text, lost_location, lost_title, lost_category in lost_prepared:
            text_score = overlap(lost_text, found_text)
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


@app.route("/admin")
@admin_required
def admin_dashboard():
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
