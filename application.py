# Elastic Beanstalk compatibility entry point.
# Exposes the Flask app under the conventional WSGI name too.

from app import app

application = app
