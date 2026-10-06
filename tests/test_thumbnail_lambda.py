import io
import sys
import unittest
from pathlib import Path
from unittest import mock

try:
    from PIL import Image
except ImportError:  # Pillow is only needed for the Lambda, not the web app.
    Image = None

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lambda" / "thumbnail"))


@unittest.skipUnless(Image, "Pillow is not installed")
class ThumbnailLambdaTestCase(unittest.TestCase):
    def setUp(self):
        with mock.patch("boto3.client"):
            import lambda_function
        self.fn = lambda_function

    def photo(self, size=(4000, 3000)):
        data = io.BytesIO()
        Image.new("RGB", size, (20, 110, 70)).save(data, "PNG")
        return data.getvalue()

    def test_thumbnail_is_a_small_jpeg(self):
        thumb = Image.open(io.BytesIO(self.fn.make_thumbnail(self.photo())))
        self.assertEqual((thumb.format, thumb.size), ("JPEG", (480, 360)))

    def test_handler_writes_thumbs_and_ignores_other_prefixes(self):
        s3 = mock.Mock()
        s3.get_object.return_value = {"Body": io.BytesIO(self.photo((800, 600)))}
        event = {"Records": [
            {"s3": {"bucket": {"name": "b"}, "object": {"key": "items/abc.png"}}},
            {"s3": {"bucket": {"name": "b"}, "object": {"key": "thumbs/items/abc.jpg"}}},
        ]}
        with mock.patch.object(self.fn, "s3", s3):
            result = self.fn.lambda_handler(event, None)
        self.assertEqual(result, {"thumbnails": ["thumbs/items/abc.jpg"]})
        self.assertEqual(s3.put_object.call_args.kwargs["Key"], "thumbs/items/abc.jpg")


if __name__ == "__main__":
    unittest.main()
