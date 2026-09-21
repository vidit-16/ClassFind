# ClassFind

**Online Lost and Found System — Cloud Computing Lab (Batch 9)**

ClassFind is a cloud-ready campus lost-and-found application where students can report missing belongings, post found items, search community reports, and surface potential lost/found matches.

## Project requirement

> **Online Lost and Found System** — Students can report and search for lost items.

## Features

- Student registration, login and logout
- Report lost or found items
- Automatic reporter identity from the signed-in account
- Search reports by keyword
- Filter by category and status
- Individual report pages
- Manage your own reports
- Mark an item as resolved
- Delete your own reports
- Dashboard counters for total, lost, found and resolved reports
- Deterministic potential-match detection between active Lost and Found reports
- Admin dashboard for report and user oversight
- Optional item image URL
- Responsive interface for desktop and mobile
- SQLite for local development and PostgreSQL for cloud deployment

## Technology stack

- **Frontend:** HTML, CSS, vanilla JavaScript
- **Backend:** Python + Flask
- **Database:** SQLAlchemy ORM
- **Authentication:** Flask sessions + Werkzeug password hashing
- **Local database:** SQLite
- **Cloud database:** PostgreSQL
- **Deployment:** Render + Gunicorn

## Run locally

Clone the repository:

~~~bash
git clone https://github.com/vidit-16/ClassFind.git
cd ClassFind
python -m venv .venv
~~~

On Windows PowerShell, when execution-policy settings prevent activating the environment, run the venv Python directly:

~~~powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe app.py
~~~

Otherwise, after activating the environment:

~~~bash
pip install -r requirements.txt
python app.py
~~~

Open http://127.0.0.1:5000.

The SQLite database is created automatically. Updating from the original version will also create the new User table without requiring a separate database server.

## First-use demo

1. Create the first student account.
2. The first account is treated as the initial admin account for an easy lab demonstration.
3. Create one Lost report and one Found report.
4. Search and filter reports.
5. Open the Matches page to see potential Lost/Found pairs.
6. Open My Reports to manage your own reports.
7. Open Admin to view users and recent reports.
8. Mark a report as Resolved.

For a deployed environment, set the ADMIN_EMAIL environment variable to the account that should have admin access instead of relying on the first-account behaviour.

## Cloud deployment with Render

Connect the repository to Render and create a new Blueprint. Render reads the render.yaml deployment configuration and creates:

- a Python web service
- a PostgreSQL database

The service reads the PostgreSQL connection string from DATABASE_URL.

Recommended environment variables:

~~~text
SECRET_KEY=<long-random-secret>
ADMIN_EMAIL=<admin-account-email>
~~~

Manual settings:

~~~text
Build command: pip install -r requirements.txt
Start command: gunicorn app:app
~~~

## Matching logic

ClassFind's matching feature is deliberately explainable for a lab project. It compares each active Lost report with active Found reports using:

- description/title keyword overlap
- matching categories
- overlapping location terms

The result is shown as a percentage with the matching reasons, rather than relying on a hidden model.

## Cloud computing concepts demonstrated

- **Cloud-hosted application:** Flask can run on a public cloud platform.
- **Cloud database:** PostgreSQL persists application data separately from the web process.
- **Environment configuration:** secrets and database connection details are supplied through environment variables.
- **Stateless web layer:** user session state is kept in signed cookies while application records remain in the database.
- **CRUD operations:** create, read, update status, and delete reports.
- **Role-based access:** student and admin views expose different management actions.
- **Scalable architecture:** multiple web processes can share the managed PostgreSQL database.

## Project structure

~~~text
ClassFind/
├── app.py
├── requirements.txt
├── Procfile
├── render.yaml
├── README.md
├── .gitignore
├── templates/
│   ├── base.html
│   ├── login.html
│   ├── register.html
│   ├── profile.html
│   ├── report.html
│   ├── item.html
│   ├── matches.html
│   ├── admin.html
│   └── 404.html
└── static/
    ├── style.css
    └── app.js
~~~

## Future enhancements

- Real image uploads using cloud object storage
- Email notifications for potential matches
- Campus-specific departments and locations
- Moderation actions and audit logs
- Stronger fuzzy matching or ML-based similarity
