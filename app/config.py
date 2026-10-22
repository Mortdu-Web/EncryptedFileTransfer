import os

try:  # chargement optionnel du .env en local (en Docker, c'est compose qui injecte les variables)
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in ("1", "true", "yes", "on")


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")

    # Route racine derrière le reverse proxy (ex. /file-transfer). Vide = à la racine.
    URL_PREFIX = os.getenv("URL_PREFIX", "").rstrip("/")
    PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:5000").rstrip("/")
    BEHIND_PROXY = _bool("BEHIND_PROXY", False)

    # Base PostgreSQL hébergée sur une autre machine (voir .env).
    DB_HOST = os.getenv("DB_HOST", "")
    DB_PORT = int(os.getenv("DB_PORT") or 5432)
    DB_NAME = os.getenv("DB_NAME", "")
    DB_USER = os.getenv("DB_USER", "")
    DB_PASSWORD = os.getenv("DB_PASSWORD", "")
    # Pool de connexions (un pool par worker gunicorn) : connexions gardees ouvertes / maximum simultane.
    DB_POOL_MIN = int(os.getenv("DB_POOL_MIN") or 1)
    DB_POOL_MAX = int(os.getenv("DB_POOL_MAX") or 5)
    # true = cree les tables au demarrage (exige le droit CREATE sur le schema public)
    DB_AUTO_CREATE = _bool("DB_AUTO_CREATE", False)

    # Stockage et chiffrement des fichiers.
    UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", "instance/uploads")
    ENCRYPTION_KEY = os.getenv("ENCRYPTION_KEY", "")
    MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB") or 100)
    MAX_CONTENT_LENGTH = MAX_UPLOAD_MB * 1024 * 1024 + 1024 * 1024  # +1 Mo de marge pour l'enveloppe multipart

    # Authentification via le paquet auth_client (service d'auth séparé).
    # Désactivée par défaut pour pouvoir développer en local sans le service.
    AUTH_ENABLED = _bool("AUTH_ENABLED", False)
    AUTH_INTERNAL_URL = os.getenv("AUTH_INTERNAL_URL", "")
    AUTH_PUBLIC_URL = os.getenv("AUTH_PUBLIC_URL", "")
    # Identité utilisée quand l'auth est désactivée (dev local).
    DEV_USER_ID = os.getenv("DEV_USER_ID", "dev-user")
    DEV_USERNAME = os.getenv("DEV_USERNAME", "dev")

    SESSION_COOKIE_NAME = "site_session"
    SESSION_COOKIE_DOMAIN = os.getenv("SESSION_COOKIE_DOMAIN") or None
    SESSION_COOKIE_SECURE = _bool("SESSION_COOKIE_SECURE", False)
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = int(os.getenv("SESSION_LIFETIME_MINUTES", "480")) * 60
