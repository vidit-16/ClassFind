# ClassFind

**Online Lost and Found System — Cloud Computing Lab (Batch 9)**

ClassFind is a lightweight cloud-ready web application where students can report lost or found belongings and search community reports.

## Project requirement

> **Online Lost and Found System** — Students can report and search for lost items.

## Features

- Report a lost item
- Report a found item
- Search reports by keyword
- Filter by category and status
- View complete report details and contact information
- Mark a report as resolved
- Delete a report
- Dashboard counters for total, lost, found and resolved reports
- Optional item image URL
- Responsive interface for desktop and mobile
- SQLite for local development and PostgreSQL for cloud deployment

## Technology stack

- **Frontend:** HTML, CSS, vanilla JavaScript
- **Backend:** Python + Flask
- **Database:** SQLAlchemy ORM
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

Activate the environment, then:

~~~bash
pip install -r requirements.txt
python app.py
~~~

Open http://127.0.0.1:5000. The local SQLite database is created automatically.

## Cloud deployment with Render

1. Push or connect this repository to Render.
2. Create a new Blueprint and select the repository.
3. Render reads render.yaml and creates the Python web service plus PostgreSQL database.
4. Deploy and open the generated public URL.

The application reads the PostgreSQL connection string from the DATABASE_URL environment variable.

Manual settings:

~~~text
Build command: pip install -r requirements.txt
Start command: gunicorn app:app
~~~

## Suggested demo flow

1. Open the dashboard and show the report counters.
2. Create a Lost report for a sample item.
3. Create a Found report for another sample item.
4. Search by item name or location.
5. Filter the results by Lost or Found.
6. Open a report and show the stored details.
7. Mark the report as Resolved.
8. Show the updated dashboard counter.

## Cloud computing concepts demonstrated

- **Cloud-hosted application:** the Flask service can run on a public cloud platform.
- **Cloud database:** PostgreSQL can persist application data separately from the web server.
- **Environment configuration:** database connection details are supplied through environment variables.
- **Scalability path:** the stateless Flask application can be scaled horizontally while using the managed database as the shared data layer.
- **CRUD operations:** create, read, update status, and delete lost/found reports.

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
│   ├── index.html
│   ├── report.html
│   ├── item.html
│   └── 404.html
└── static/
    ├── style.css
    └── app.js
~~~

## Future enhancements

- Student login and role-based access
- Real image uploads using cloud object storage
- Email notifications for potential matches
- Automatic similarity matching between lost and found descriptions
- Admin moderation dashboard
- Campus-specific locations and departments
