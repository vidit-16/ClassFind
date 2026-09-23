# AWS Deployment Guide

This guide deploys ClassFind as a small AWS architecture:
- Amazon Elastic Beanstalk: Flask web application
- Amazon RDS for PostgreSQL: application database
- Amazon S3: persistent item images

## 1. Create the S3 bucket

Create a dedicated private S3 bucket for ClassFind images. Keep Block Public Access enabled. Use the same AWS Region as the application, for example ap-south-1.

ClassFind stores objects under `items/<random-file-name>` and stores the S3 reference in PostgreSQL. The application generates time-limited presigned GET URLs when images are displayed.

## 2. Give Elastic Beanstalk access to S3

Grant the Elastic Beanstalk EC2 instance role these object permissions:

`s3:GetObject`
`s3:PutObject`
`s3:DeleteObject`

Restrict the resource to `arn:aws:s3:::YOUR_BUCKET/items/*`.

Use the policy template in `aws/s3-item-images-policy.json`. Replace the placeholder bucket name before attaching it.

Do not put AWS access keys in app.py or commit them to Git. The application should use the Elastic Beanstalk instance role.

## 3. Create PostgreSQL on RDS

Create a PostgreSQL DB instance in the same Region. Configure network access so the Elastic Beanstalk application can connect on port 5432.

Set the connection string as `DATABASE_URL` in the Elastic Beanstalk environment:

`postgresql://USERNAME:PASSWORD@RDS_ENDPOINT:5432/DATABASE_NAME`

## 4. Create the Elastic Beanstalk environment

Create an Elastic Beanstalk application using the Python platform and deploy this repository.

The included Procfile starts Gunicorn on port 8000:

`gunicorn --bind 0.0.0.0:8000 application:application`

Set these environment variables:

`CLASSFIND_ENV=production`
`SECRET_KEY=<long-random-secret>`
`ADMIN_EMAIL=<admin-email>`
`DATABASE_URL=<RDS connection string>`
`S3_BUCKET=<bucket name>`
`AWS_REGION=<bucket region>`

Set CLASSFIND_ENV and SECRET_KEY before the first deploy. With CLASSFIND_ENV=production
and no SECRET_KEY the app stops on startup rather than signing sessions with a key
published in this repository. ADMIN_EMAIL decides which account sees the admin pages.

## 5. Verify

Open the Elastic Beanstalk URL and then `/health`.

Expected response:

`{"status":"ok","database":"ok"}`

## 6. Demo flow

Use two student accounts: create a Lost report with Account A, a Found report with Account B, open Matches, submit a claim as Account A, then review the claim as Account B.

Also demonstrate My Reports, Edit, image upload, Search/Filters, Resolve, and Admin.

## AWS architecture

`Browser -> Elastic Beanstalk (Flask/Gunicorn) -> RDS PostgreSQL`
`                                      |-> S3 item images`

## Cleanup

When testing is finished, review the Elastic Beanstalk environment, RDS instance and S3 bucket so unused resources do not remain active.