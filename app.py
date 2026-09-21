import os
from datetime import datetime

from flask import Flask, flash, redirect, render_template, request, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "classfind-dev-key")

database_url = os.getenv("DATABASE_URL", "sqlite:///classfind.db")
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)

app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db = SQLAlchemy(app)

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
        return {"Lost": "status-lost", "Found": "status-found", "Resolved": "status-resolved"}.get(self.status, "")

with app.app_context():
    db.create_all()

@app.route("/")
def index():
    query = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    status = request.args.get("status", "").strip()

    items_query = Item.query
    if query:
        pattern = f"%{query}%"
        items_query = items_query.filter(or_(
            Item.title.ilike(pattern),
            Item.description.ilike(pattern),
            Item.location.ilike(pattern),
            Item.category.ilike(pattern),
        ))
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
    categories = [row[0] for row in db.session.query(Item.category).distinct().order_by(Item.category).all()]

    return render_template("index.html", items=items, stats=stats, categories=categories,
                           query=query, selected_category=category, selected_status=status)

@app.route("/report", methods=["GET", "POST"])
def report():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        category = request.form.get("category", "").strip()
        location = request.form.get("location", "").strip()
        status = request.form.get("status", "").strip()
        reporter_name = request.form.get("reporter_name", "").strip()
        contact = request.form.get("contact", "").strip()
        image_url = request.form.get("image_url", "").strip()

        if not all([title, description, category, location, status, reporter_name, contact]):
            flash("Please fill in every required field.", "error")
            return render_template("report.html")
        if status not in {"Lost", "Found"}:
            flash("Choose either Lost or Found.", "error")
            return render_template("report.html")

        item = Item(title=title, description=description, category=category, location=location,
                    status=status, reporter_name=reporter_name, contact=contact,
                    image_url=image_url or None)
        db.session.add(item)
        db.session.commit()
        flash("Your report has been added to ClassFind.", "success")
        return redirect(url_for("item_detail", item_id=item.id))
    return render_template("report.html")

@app.route("/item/<int:item_id>")
def item_detail(item_id):
    item = db.get_or_404(Item, item_id)
    return render_template("item.html", item=item)

@app.post("/item/<int:item_id>/resolve")
def resolve_item(item_id):
    item = db.get_or_404(Item, item_id)
    item.status = "Resolved"
    db.session.commit()
    flash("This report has been marked as resolved.", "success")
    return redirect(url_for("item_detail", item_id=item.id))

@app.post("/item/<int:item_id>/delete")
def delete_item(item_id):
    item = db.get_or_404(Item, item_id)
    db.session.delete(item)
    db.session.commit()
    flash("Report deleted.", "success")
    return redirect(url_for("index"))

@app.errorhandler(404)
def not_found(_error):
    return render_template("404.html"), 404

if __name__ == "__main__":
    app.run(debug=True)
