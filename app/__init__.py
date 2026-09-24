import os

from flask import Flask

from .config import Config


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    from . import scheduler, views

    app.register_blueprint(views.bp)
    app.jinja_env.filters["human_size"] = views.human_size
    app.jinja_env.filters["iso_date"] = views.iso_date

    os.makedirs(app.config["DATA_DIR"], exist_ok=True)
    if app.config["ENABLE_MIRROR"]:
        os.makedirs(app.config["CACHE_DIR"], exist_ok=True)

    scheduler.start(app)
    return app