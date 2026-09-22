# ClassFind

**Online Lost and Found System**

ClassFind is a cloud-ready campus lost-and-found application where students can report missing belongings, post found items, search community reports, and surface potential lost/found matches.

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
- **Deployment:** AWS Elastic Beanstalk + Gunicorn
- **Image storage:** Amazon S3

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

## AWS deployment

ClassFind is designed to run on AWS using:

- **Elastic Beanstalk** for the Flask web application
- **RDS for PostgreSQL** for users, reports and claims
- **S3** for persistent item images

Elastic Beanstalk supports Python web applications and can run Flask behind WSGI/Gunicorn. The repository includes a Procfile with the Gunicorn start command.

### 1. Create an S3 bucket

Create a private S3 bucket for ClassFind item images. Keep Block Public Access enabled. The application generates time-limited presigned GET URLs for displaying private images. AWS documents presigned URLs as the way to grant temporary access to private S3 objects.

### 2. Configure AWS permissions

Give the Elastic Beanstalk EC2 instance role permission to work with the ClassFind bucket. The application uses the AWS SDK for Python (Boto3) and its S3 upload APIs.

Minimum object permissions:

~~~text
s3:GetObject
s3:PutObject
s3:DeleteObject
~~~

Scope them to the ClassFind bucket and the items/ prefix.

### 3. Create PostgreSQL on RDS

Create a PostgreSQL database in Amazon RDS and make it reachable from the Elastic Beanstalk environment. AWS documents RDS integration with Elastic Beanstalk for PostgreSQL applications.

Set the application environment variable:

~~~text
DATABASE_URL=postgresql://<user>:<password>@<host>:5432/<database>
~~~

### 4. Create the Elastic Beanstalk environment

Use the AWS Elastic Beanstalk Python platform and deploy this repository/source bundle. Elastic Beanstalk can deploy Flask applications and uses the Procfile in the source bundle to configure the WSGI server.

Set these environment variables in the environment:

~~~text
SECRET_KEY=<long-random-secret>
ADMIN_EMAIL=<admin-account-email>
S3_BUCKET=<your-s3-bucket-name>
AWS_REGION=ap-south-1
DATABASE_URL=<your-rds-connection-string>
~~~

The application does not require AWS access keys in source code. On Elastic Beanstalk, use the environment's IAM role for S3 permissions.

### Local vs AWS storage

Without S3 configuration, local development keeps uploaded files under:

~~~text
static/uploads/
~~~

When S3_BUCKET is configured, uploads go to:

~~~text
s3://<bucket>/items/<random-file-name>
~~~

The database stores the S3 object reference, and the application generates a temporary URL when the image needs to be displayed.

### AWS architecture

~~~text
Browser
   |
   v
AWS Elastic Beanstalk
   |
   +---- Flask + Python
   |
   +---- Amazon RDS PostgreSQL
   |       - Users
   |       - Reports
   |       - Claims
   |
   +---- Amazon S3
           - Item images
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
