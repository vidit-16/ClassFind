# Setting up the v2 AWS services

Everything here is done in the AWS console, in the **Asia Pacific (Mumbai)
ap-south-1** region. It takes about fifteen minutes. The app runs without any of
it: each feature stays off until its setting is in place.

| Feature | AWS service | Setting that turns it on |
| --- | --- | --- |
| Email alerts | Amazon SES | `SES_SENDER` |
| Photo labels in matching | Amazon Rekognition | `PHOTO_LABELS=true` |
| Thumbnails on the home page | AWS Lambda + S3 trigger | `THUMBNAILS=true` |
| One-sentence reports | Groq (not AWS) | `GROQ_API_KEY` |
| Site-down email | CloudWatch alarm + SNS | none, it watches from outside |

## 1. Let the server use Rekognition, SES and the thumbnails

1. Open **IAM → Roles → ClassFind-EC2-Role**.
2. **Add permissions → Create inline policy → JSON**, and paste:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       { "Effect": "Allow", "Action": "rekognition:DetectLabels", "Resource": "*" },
       { "Effect": "Allow", "Action": ["ses:SendEmail", "ses:SendRawEmail"], "Resource": "*" },
       { "Effect": "Allow", "Action": ["s3:GetObject", "s3:DeleteObject"], "Resource": "arn:aws:s3:::classfind-vidit-2026/thumbs/*" }
     ]
   }
   ```

3. **Next**, name it `ClassFind-v2-services`, **Create policy**.

## 2. SES

1. **Amazon SES → Configuration → Identities → Create identity → Email address.**
   Enter the sender address and click the link AWS emails to it.
2. Do the same for every address that should *receive* email while the account
   is in the SES sandbox. In the sandbox SES only delivers to verified
   addresses. Production access needs a verified domain of your own.

## 3. Thumbnail Lambda

1. **Lambda → Create function → Author from scratch.**
   - Name: `classfind-thumbnail`
   - Runtime: **Python 3.12**, architecture **x86_64**
   - Execution role: **Create a new role with basic Lambda permissions**
2. **Code → Upload from → .zip file**, and choose `classfind-thumbnail-lambda.zip`.
   The handler is `lambda_function.lambda_handler`, which is the default.
3. **Configuration → General configuration → Edit**: memory **512 MB**, timeout **30 seconds**.
4. **Configuration → Permissions →** click the role name, then
   **Add permissions → Create inline policy → JSON**:

   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       { "Effect": "Allow", "Action": "s3:GetObject", "Resource": "arn:aws:s3:::classfind-vidit-2026/items/*" },
       { "Effect": "Allow", "Action": "s3:PutObject", "Resource": "arn:aws:s3:::classfind-vidit-2026/thumbs/*" }
     ]
   }
   ```

   Name it `classfind-thumbnail-s3`.
5. **Add trigger → S3**: bucket `classfind-vidit-2026`, event **All object create
   events**, prefix `items/`. Tick the acknowledgement and **Add**. The prefix
   matters: thumbnails are written to `thumbs/`, so the function never
   triggers itself.
6. Check it: report an item with a photo on the site, then look for
   `thumbs/items/...jpg` in the bucket, or at **Monitor → View CloudWatch logs**.

## 4. Site-down alarm

1. **CloudWatch → Alarms → Create alarm → Select metric → EC2 → Per-Instance
   Metrics**, pick `StatusCheckFailed` for the ClassFind instance.
2. Statistic **Maximum**, period **1 minute**, condition **Greater/Equal 1**.
3. **Notification → Create new topic** `classfind-alerts` with your email, then
   confirm the subscription email SNS sends you.
4. Name it `classfind-instance-down` and create it.

Elastic Beanstalk replaces the instance on some deploys; if that happens, point
the alarm at the new instance ID.

## 5. Elastic Beanstalk settings

**Elastic Beanstalk → your environment → Configuration → Updates, monitoring
and logging → Environment properties**, add:

| Name | Value |
| --- | --- |
| `SES_SENDER` | the verified sender address |
| `PHOTO_LABELS` | `true` |
| `THUMBNAILS` | `true` |
| `GROQ_API_KEY` | your key from console.groq.com |
| `STAFF_EMAILS` | desk staff addresses, comma separated |

Apply, wait for the environment to turn green, and open `/health`.
