import os

from flask import Flask, jsonify
from werkzeug.middleware.proxy_fix import ProxyFix

from . import crypto, db
from .config import Config
from .helpers import human_size
from .routes import bp as files_bp


def create_app() -> Flask:
    # Les fichiers statiques sont servis sous URL_PREFIX (ex. /file-transfer/static/...).
    app = Flask(__name__, static_url_path=f"{Config.URL_PREFIX}/static")
    app.config.from_object(Config)

    if Config.BEHIND_PROXY:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    crypto.init_app(app)  # echoue des le demarrage si ENCRYPTION_KEY est absente ou invalide
    db.init_app(app)
    os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)

    app.jinja_env.filters["filesize"] = human_size
    app.register_blueprint(files_bp, url_prefix=Config.URL_PREFIX or None)

    @app.errorhandler(413)
    def too_large(_exc):
        return jsonify({"error": f"Fichier trop volumineux (maximum {Config.MAX_UPLOAD_MB} Mo)."}), 413

    return app
