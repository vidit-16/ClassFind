import os
import re
from datetime import datetime
from hmac import compare_digest
from secrets import token_urlsafe
from urllib.parse import urlparse
from difflib import SequenceMatcher
from functools import wraps
from pathlib import Path
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)


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


app.config["SECRET_KEY"] = resolve_secret_key()
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("COOKIE_SECURE", "").lower() in {"1", "true", "yes"}
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
def protect_csrf():
    expected = session.get(CSRF_SESSION_KEY)
    if not expected:
        expected = csrf_token()
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return
    supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
    if not expected or not supplied or not compare_digest(expected, supplied):
        abort(400, description="Invalid or missing CSRF token.")


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
        "img-src 'self' data: https: http:; "
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
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


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
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    claims = db.relationship("Claim", back_populates="item", cascade="all, delete-orphan")

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
    item = db.relationship("Item", back_populates="claims")
    claimant = db.relationship("User")


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


with app.app_context():
    db.create_all()
    backfill_item_owners()

if not app.config["S3_BUCKET"]:
    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)

s3_client = boto3.client("s3", region_name=app.config["AWS_REGION"]) if app.config["S3_BUCKET"] else None


def get_current_user():
    user_id = session.get("user_id")
    return db.session.get(User, user_id) if user_id else None


def pending_claim_counts(user):
    if not user:
        return 0, 0
    incoming = (
        Claim.query.join(Item, Claim.item_id == Item.id)
        .filter(
            or_(
                Item.owner_id == user.id,
                db.and_(Item.owner_id.is_(None), Item.contact.ilike(user.email)),
            ),
            Claim.status == "Pending",
        )
        .count()
    )
    outgoing = Claim.query.filter_by(claimant_id=user.id, status="Pending").count()
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
    user = get_current_user()
    return bool(user and (user.is_admin or owns_item(user, item)))


def allowed_file(filename):
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in app.config["ALLOWED_EXTENSIONS"]
    )


def is_safe_image_url(value):
    if not value:
        return True
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


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
        return {"status": "ok", "database": "ok"}, 200
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

        # ADMIN_EMAIL may be set after the account was made, so apply it here too.
        if admin_email() and user.email == admin_email() and not user.is_admin:
            user.is_admin = True
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
        Item.query.filter(Item.contact.ilike(user.email))
        .order_by(Item.created_at.desc())
        .all()
    )
    incoming_claims = (
        Claim.query.join(Item, Claim.item_id == Item.id)
        .filter(Item.contact.ilike(user.email))
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
        image_url = request.form.get("image_url", "").strip()

        if not all([title, description, category, location, status]):
            flash("Please fill in every required field.", "error")
            return render_template("report.html")
        if image_url and not is_safe_image_url(image_url):
            flash("Image URL must start with http:// or https://.", "error")
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
            image_url=uploaded_url or image_url or None,
        )
        db.session.add(item)
        db.session.commit()
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
        image_url = request.form.get("image_url", "").strip()

        if not all([item.title, item.description, item.category, item.location, status]):
            flash("Please fill in every required field.", "error")
            return render_template("edit.html", item=item)
        if image_url and not is_safe_image_url(image_url):
            flash("Image URL must start with http:// or https://.", "error")
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
        elif image_url:
            if item.image_url and item.image_url != image_url:
                delete_image(item.image_url)
            item.image_url = image_url

        db.session.commit()
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
    if item.contact.lower() == user.email.lower():
        flash("You cannot claim your own found report.", "error")
        return redirect(url_for("item_detail", item_id=item.id))

    message = request.form.get("message", "").strip()
    if len(message) < 10:
        flash("Tell the reporter why you believe this item is yours.", "error")
        return redirect(url_for("item_detail", item_id=item.id))

    existing = Claim.query.filter_by(item_id=item.id, claimant_id=user.id).first()
    if existing and existing.status == "Pending":
        flash("You already have a pending claim for this item.", "error")
        return redirect(url_for("item_detail", item_id=item.id))

    claim = Claim(
        item_id=item.id,
        claimant_id=user.id,
        message=message,
        status="Pending",
    )
    db.session.add(claim)
    db.session.commit()
    flash("Claim submitted. The reporter can now review it.", "success")
    return redirect(url_for("claims"))


@app.route("/claims")
@login_required
def claims():
    user = get_current_user()
    incoming = (
        Claim.query.join(Item, Claim.item_id == Item.id)
        .filter(Item.contact.ilike(user.email))
        .order_by(Claim.created_at.desc())
        .all()
    )
    outgoing = (
        Claim.query.filter_by(claimant_id=user.id)
        .order_by(Claim.created_at.desc())
        .all()
    )
    return render_template("claims.html", incoming=incoming, outgoing=outgoing)


def can_manage_claim(claim):
    user = get_current_user()
    return bool(user and (
        user.is_admin or claim.item.contact.lower() == user.email.lower()
    ))


@app.post("/claims/<int:claim_id>/accept")
@login_required
def accept_claim(claim_id):
    claim = db.get_or_404(Claim, claim_id)
    if not can_manage_claim(claim):
        flash("You cannot manage this claim.", "error")
        return redirect(url_for("claims"))
    if claim.status != "Pending":
        flash("This claim has already been decided.", "error")
        return redirect(url_for("claims"))

    claim.status = "Accepted"
    claim.decided_at = datetime.utcnow()
    claim.item.status = "Resolved"

    other_claims = Claim.query.filter(
        Claim.item_id == claim.item_id,
        Claim.id != claim.id,
        Claim.status == "Pending",
    ).all()
    for other in other_claims:
        other.status = "Rejected"
        other.decided_at = datetime.utcnow()

    db.session.commit()
    flash("Claim accepted and item marked as resolved.", "success")
    return redirect(url_for("claims"))


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
    if claim.status != "Pending":
        flash("This claim has already been decided.", "error")
        return redirect(url_for("claims"))

    claim.status = "Rejected"
    claim.decided_at = datetime.utcnow()
    db.session.commit()
    flash("Claim rejected.", "success")
    return redirect(url_for("claims"))


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
    """Changes whenever a report that matching reads is added, edited or removed."""
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
