"""Stockage (PostgreSQL, sur une machine distante) des utilisateurs, fichiers et partages.

Connexion configurée via le .env : DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD.
Les routes n'appellent que les fonctions ci-dessous : c'est le seul fichier à adapter pour une autre base.
Les identifiants d'utilisateurs sont ceux de Keycloak (`sub`), stockés en texte.
"""
from pathlib import Path

import psycopg
from flask import current_app, g
from psycopg.rows import dict_row

SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")


def _connect() -> psycopg.Connection:
    cfg = current_app.config
    return psycopg.connect(
        host=cfg["DB_HOST"],
        port=cfg["DB_PORT"],
        dbname=cfg["DB_NAME"],
        user=cfg["DB_USER"],
        password=cfg["DB_PASSWORD"],
        row_factory=dict_row,
        connect_timeout=10,
        client_encoding="UTF8",
    )


def get_db() -> psycopg.Connection:
    if "db" not in g:
        g.db = _connect()
    return g.db


def _close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_app(app) -> None:
    missing = [k for k in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD") if not app.config.get(k)]
    if missing:
        raise RuntimeError(f"Variables manquantes dans le .env : {', '.join(missing)}")

    app.teardown_appcontext(_close_db)

    # Les tables sont creees seulement si DB_AUTO_CREATE=true. Cette operation exige les droits CREATE
    # sur le schema public ; sinon, lancer app/schema.sql une fois avec un compte qui les a.
    if app.config.get("DB_AUTO_CREATE"):
        with app.app_context():
            db = get_db()
            db.execute(SCHEMA)
            db.commit()


# ── Utilisateurs ─────────────────────────────────────────────────────────────

_LABEL = "COALESCE(NULLIF(display_name, ''), username)"


def upsert_users(users: list[dict]) -> None:
    """users = [{"keycloak_id", "username", "display_name"}] : cree ou met a jour."""
    users = [u for u in users if u.get("keycloak_id")]
    if not users:
        return
    db = get_db()
    try:
        with db.cursor() as cur:
            cur.executemany(
                """INSERT INTO ft_users (keycloak_id, username, display_name) VALUES (%s, %s, %s)
                   ON CONFLICT (keycloak_id) DO UPDATE
                   SET username = EXCLUDED.username, display_name = EXCLUDED.display_name""",
                [(u["keycloak_id"], u.get("username") or u["keycloak_id"], u.get("display_name") or "") for u in users],
            )
        db.commit()
    except psycopg.Error:
        db.rollback()
        raise


def list_users() -> list[dict]:
    """[{id, username, label}] : `label` = nom complet, a defaut l'identifiant."""
    return get_db().execute(
        f"SELECT keycloak_id AS id, username, {_LABEL} AS label FROM ft_users ORDER BY LOWER({_LABEL})"
    ).fetchall()


# ── Fichiers ─────────────────────────────────────────────────────────────────

def list_files_by_owner(owner_id: str) -> list[dict]:
    """Fichiers de l'utilisateur, avec `shared_with_ids` (liste d'identifiants)."""
    return get_db().execute(
        """SELECT f.id, f.file_name, f.size_bytes, f.uploaded_at,
                  COALESCE(array_agg(s.shared_with_id) FILTER (WHERE s.shared_with_id IS NOT NULL), '{}') AS shared_with_ids
           FROM ft_files f
           LEFT JOIN ft_file_shares s ON s.file_id = f.id
           WHERE f.owner_id = %s
           GROUP BY f.id
           ORDER BY f.uploaded_at DESC, f.id DESC""",
        (owner_id,),
    ).fetchall()


def list_files_shared_with(user_id: str) -> list[dict]:
    """Fichiers partages avec l'utilisateur, avec `shared_by` (nom du proprietaire) et `shared_at`."""
    return get_db().execute(
        f"""SELECT f.id, f.file_name, f.size_bytes, s.shared_at,
                   COALESCE(NULLIF(u.display_name, ''), u.username) AS shared_by
            FROM ft_file_shares s
            JOIN ft_files f ON f.id = s.file_id
            JOIN ft_users u ON u.keycloak_id = f.owner_id
            WHERE s.shared_with_id = %s
            ORDER BY s.shared_at DESC, f.id DESC""",
        (user_id,),
    ).fetchall()


def get_file(file_id: int) -> dict | None:
    return get_db().execute(
        "SELECT id, owner_id, file_name, stored_filename, size_bytes FROM ft_files WHERE id = %s", (file_id,)
    ).fetchone()


def is_shared_with(file_id: int, user_id: str) -> bool:
    return (
        get_db().execute(
            "SELECT 1 FROM ft_file_shares WHERE file_id = %s AND shared_with_id = %s", (file_id, user_id)
        ).fetchone()
        is not None
    )


def add_file(owner_id: str, file_name: str, stored_filename: str, size_bytes: int) -> int:
    db = get_db()
    try:
        row = db.execute(
            """INSERT INTO ft_files (owner_id, file_name, stored_filename, size_bytes)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (owner_id, file_name, stored_filename, size_bytes),
        ).fetchone()
        db.commit()
    except psycopg.Error:
        db.rollback()
        raise
    return row["id"]


def delete_file(file_id: int) -> None:
    db = get_db()
    db.execute("DELETE FROM ft_files WHERE id = %s", (file_id,))  # les partages partent en cascade
    db.commit()


# ── Partages ─────────────────────────────────────────────────────────────────

def add_share(file_id: int, shared_with_id: str) -> None:
    db = get_db()
    try:
        db.execute(
            "INSERT INTO ft_file_shares (file_id, shared_with_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
            (file_id, shared_with_id),
        )
        db.commit()
    except psycopg.errors.ForeignKeyViolation as exc:
        db.rollback()
        raise ValueError("Utilisateur introuvable.") from exc


def remove_share(file_id: int, shared_with_id: str) -> None:
    db = get_db()
    db.execute(
        "DELETE FROM ft_file_shares WHERE file_id = %s AND shared_with_id = %s", (file_id, shared_with_id)
    )
    db.commit()
