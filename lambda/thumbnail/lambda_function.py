"""Make a small JPEG thumbnail whenever ClassFind uploads a report photo to S3.

Triggered by s3:ObjectCreated on the items/ prefix. Each photo items/<name>.<ext>
gets a thumbnail at thumbs/items/<name>.jpg, at most THUMB_SIZE pixels on the
long side (480 by default). The home page shows thumbnails instead of the
full photos, so it loads faster, and falls back to the full photo when a
thumbnail is not there yet. Writing to thumbs/ never retriggers the function,
because the trigger only watches items/.
"""
import io
import os
import urllib.parse

import boto3
from PIL import Image, ImageOps

s3 = boto3.client("s3")
MAX_SIDE = int(os.environ.get("THUMB_SIZE", "480"))


def thumbnail_key(key):
    return "thumbs/" + key.rsplit(".", 1)[0] + ".jpg"


def make_thumbnail(data, max_side=MAX_SIDE):
    image = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    image = image.convert("RGB")
    image.thumbnail((max_side, max_side))
    out = io.BytesIO()
    image.save(out, "JPEG", quality=80, optimize=True)
    return out.getvalue()


def lambda_handler(event, context):
    made = []
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        if not key.startswith("items/"):
            continue
        data = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        s3.put_object(
            Bucket=bucket,
            Key=thumbnail_key(key),
            Body=make_thumbnail(data),
            ContentType="image/jpeg",
            CacheControl="max-age=86400",
        )
        made.append(thumbnail_key(key))
    return {"thumbnails": made}
