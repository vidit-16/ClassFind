import os
import re
from datetime import datetime
from functools import wraps

from flask import Flask, flash, redirect, render_template, request, session, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "classfind-dev-key")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

database_url = os.getenv("DATABASE_URL", "sqlite:///classfind.db")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

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
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    @property
    def status_class(self):
        return {
            "Lost": "status-lost",
            "Found": "status-found",
            "Resolved": "status-resolved",
        }.get(self.status, "")


with app.app_context():
    db.create_all()


def get_current_user():
    user_id = session.get("user_id")
    return db.session.get(User, user_id) if user_id else None


@app.context_processor
def inject_current_user():
    return {"current_user": get_current_user()}


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


def can_manage_item(item):
    user = get_current_user()
    return bool(user and (user.is_admin or item.contact.lower() == user.email.lower()))


@app.route("/")
def index():
    query = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
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
            )
        )
    if category:
        items_query = items_query.filter_by(category=category)
    if status in {"Lost", "Found", "Resolved"}:
        items_query = items_query.filter_by(status=status)

    items = items_query.order_by(Item.created_at.desc()).all()
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

    return render_template(
        "index.html",
        items=items,
        stats=stats,
        categories=categories,
        query=query,
        selected_category=category,
        selected_status=status,
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    if get_current_user():
        return redirect(url_for("index"))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not name or not email or len(password) < 6:
            flash("Enter your name, a valid email, and a password of at least 6 characters.", "error")
            return render_template("register.html")

        if User.query.filter_by(email=email).first():
            flash("An account with that email already exists.", "error")
            return render_template("register.html")

        is_first_user = User.query.count() == 0
        admin_email = os.getenv("ADMIN_EMAIL", "").strip().lower()
        is_admin = is_first_user or (admin_email and email == admin_email)

        user = User(
            name=name,
            email=email,
            password_hash=generate_password_hash(password),
            is_admin=is_admin,
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

        session["user_id"] = user.id
        next_url = request.args.get("next") or url_for("index")
        if not next_url.startswith("/"):
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
    return render_template("profile.html", user=user, items=items)


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

        if status not in {"Lost", "Found"}:
            flash("Choose either Lost or Found.", "error")
            return render_template("report.html")

        item = Item(
            title=title,
            description=description,
            category=category,
            location=location,
            status=status,
            reporter_name=user.name,
            contact=user.email,
            image_url=image_url or None,
        )
        db.session.add(item)
        db.session.commit()
        flash("Your report has been added to ClassFind.", "success")
        return redirect(url_for("item_detail", item_id=item.id))

    return render_template("report.html")


@app.route("/item/<int:item_id>")
def item_detail(item_id):
    item = db.get_or_404(Item, item_id)
    return render_template("item.html", item=item, can_manage=can_manage_item(item))


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

    db.session.delete(item)
    db.session.commit()
    flash("Report deleted.", "success")
    return redirect(url_for("index"))


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
    lost_items = Item.query.filter_by(status="Lost").order_by(Item.created_at.desc()).all()
    found_items = Item.query.filter_by(status="Found").order_by(Item.created_at.desc()).all()
    pairs = []

    for lost in lost_items:
        for found in found_items:
            lost_text = tokens(f"{lost.title} {lost.description}")
            found_text = tokens(f"{found.title} {found.description}")
            text_score = overlap(lost_text, found_text)

            lost_location = tokens(lost.location)
            found_location = tokens(found.location)
            location_score = overlap(lost_location, found_location)

            score = text_score * 0.55
            reasons = []

            if lost.category.lower() == found.category.lower():
                score += 0.20
                reasons.append("same category")

            if location_score > 0:
                score += location_score * 0.25
                reasons.append("similar location")

            shared = lost_text & found_text
            if shared:
                shown = ", ".join(sorted(shared)[:3])
                reasons.append(f"shared terms: {shown}")

            score = min(score, 0.99)

            if score >= 0.20:
                pairs.append((lost, found, round(score * 100), reasons or ["related details"]))

    pairs.sort(key=lambda pair: pair[2], reverse=True)
    return pairs[:30]


@app.route("/matches")
def matches():
    return render_template("matches.html", pairs=build_matches())


@app.route("/admin")
@admin_required
def admin_dashboard():
    stats = {
        "total": Item.query.count(),
        "lost": Item.query.filter_by(status="Lost").count(),
        "found": Item.query.filter_by(status="Found").count(),
        "resolved": Item.query.filter_by(status="Resolved").count(),
    }
    items = Item.query.order_by(Item.created_at.desc()).limit(50).all()
    return render_template(
        "admin.html",
        stats=stats,
        items=items,
        user_count=User.query.count(),
    )


@app.errorhandler(404)
def not_found(_error):
    return render_template("404.html"), 404


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG") == "1")
