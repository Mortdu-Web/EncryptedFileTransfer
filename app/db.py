"""Acces PostgreSQL du service de transfert de fichiers, sur les tables du hub : users, files, file_shares.

Connexion configuree via le .env : DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD (+ DB_POOL_MIN / DB_POOL_MAX).
Les routes n'appellent que les fonctions ci-dessous : c'est le seul fichier a adapter pour une autre base.
Les identifiants d'utilisateurs sont ceux de la table `users` (entiers) ; ce service ne cree ni ne modifie
aucun utilisateur.

Gestion des connexions (meme principe que l'ancien core/db : un pool, une connexion empruntee le temps
d'une operation, puis rendue) :
  - un pool par worker gunicorn, cree dans create_app (donc ne pas lancer gunicorn avec --preload) ;
  - chaque fonction emprunte une connexion avec `_cursor()` : commit si tout va bien, rollback sinon,
    puis retour au pool dans tous les cas ;
  - une connexion coupee cote reseau est detectee et remplacee avant usage ;
  - le demarrage echoue immediatement, avec un message clair, si la base est injoignable ou si la
    migration schema.sql n'a pas ete appliquee.
"""
import atexit
from contextlib import contextmanager

import psycopg
from flask import current_app
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_EXT_KEY = "ft_db_pool"


# ── Connexion / pool ─────────────────────────────────────────────────────────

def init_app(app) -> None:
    """Ouvre le pool et verifie la base. Leve RuntimeError (message lisible) au moindre probleme."""
    cfg = app.config
    missing = [k for k in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD") if not cfg.get(k)]
    if missing:
        raise RuntimeError(f"Variables manquantes dans le .env : {', '.join(missing)}")

    conninfo = make_conninfo(
        host=cfg["DB_HOST"],
        port=cfg["DB_PORT"],
        dbname=cfg["DB_NAME"],
        user=cfg["DB_USER"],
        password=cfg["DB_PASSWORD"],
        connect_timeout=10,
        client_encoding="UTF8",
    )

    # Test direct avant le pool : le pool, lui, cache la vraie cause (mauvais mot de passe, hote
    # injoignable...) derriere un simple « timeout ».
    try:
        psycopg.connect(conninfo).close()
    except psycopg.Error as exc:
        raise RuntimeError(
            f"[DB] Impossible de se connecter à PostgreSQL {cfg['DB_HOST']}:{cfg['DB_PORT']} "
            f"(base « {cfg['DB_NAME']} », utilisateur « {cfg['DB_USER']} ») : {exc}"
        ) from exc

    pool = ConnectionPool(
        conninfo,
        min_size=cfg["DB_POOL_MIN"],
        max_size=cfg["DB_POOL_MAX"],
        kwargs={"row_factory": dict_row},
        check=ConnectionPool.check_connection,  # remplace une connexion morte au lieu de la renvoyer
        timeout=10,  # attente maximale d'une connexion libre (sinon PoolTimeout -> 503)
        name="file-transfer",
        open=True,
    )
    try:
        _check_schema(pool)
    except Exception:
        pool.close()
        raise
    app.extensions[_EXT_KEY] = pool
    atexit.register(pool.close)


def _check_schema(pool: ConnectionPool) -> None:
    """Verifie que les tables du hub existent et que la migration schema.sql a ete appliquee."""
    problems = []
    with pool.connection() as conn:
        for table in ("users", "files", "file_shares"):
            if conn.execute("SELECT to_regclass(%s) AS found", (table,)).fetchone()["found"] is None:
                problems.append(f"table « {table} » introuvable")
        if not problems:
            if not conn.execute(
                """SELECT 1 FROM information_schema.columns
                   WHERE table_schema = current_schema() AND table_name = 'files' AND column_name = 'size_bytes'"""
            ).fetchone():
                problems.append("colonne files.size_bytes absente")
            if not conn.execute(
                "SELECT 1 FROM pg_constraint WHERE conname = 'file_shares_file_user_key'"
            ).fetchone():
                problems.append("contrainte file_shares_file_user_key absente")
    if problems:
        raise RuntimeError(
            f"[DB] Base non prête : {' ; '.join(problems)}. Appliquer la migration une fois, avec le propriétaire "
            "des tables : `sudo -u postgres psql -d <base> -f schema.sql`."
        )


@contextmanager
def _cursor():
    """Emprunte une connexion du pool : commit en sortie normale, rollback si exception, retour au pool."""
    with current_app.extensions[_EXT_KEY].connection() as conn:
        with conn.cursor() as cur:
            yield cur


# ── Utilisateurs (table `users`, en lecture seule) ───────────────────────────

def get_user_by_username(username: str) -> dict | None:
    """{id, username} du compte portant cet identifiant, ou None. Le mot de passe n'est jamais lu."""
    with _cursor() as cur:
        cur.execute("SELECT id, username FROM users WHERE username = %s ORDER BY id LIMIT 1", (username,))
        return cur.fetchone()


def list_users() -> list[dict]:
    """[{id, username, label}] : `label` = ce qu'affiche le menu « Partager avec »."""
    with _cursor() as cur:
        cur.execute("SELECT id, username, username AS label FROM users ORDER BY LOWER(username), id")
        return cur.fetchall()


# ── Fichiers ─────────────────────────────────────────────────────────────────

def list_files_by_owner(owner_id: int) -> list[dict]:
    """Fichiers de l'utilisateur, avec `shared_with_ids` (liste d'identifiants)."""
    with _cursor() as cur:
        cur.execute(
            """SELECT f.id, f.file_name, f.size_bytes, f.uploaded_at,
                      COALESCE(array_agg(s.shared_with_user_id) FILTER (WHERE s.shared_with_user_id IS NOT NULL), '{}') AS shared_with_ids
               FROM files f
               LEFT JOIN file_shares s ON s.file_id = f.id
               WHERE f.user_id = %s
               GROUP BY f.id
               ORDER BY f.uploaded_at DESC, f.id DESC""",
            (owner_id,),
        )
        return cur.fetchall()


def list_files_shared_with(user_id: int) -> list[dict]:
    """Fichiers partages avec l'utilisateur, avec `shared_by` (nom de l'expediteur) et `shared_at`."""
    with _cursor() as cur:
        cur.execute(
            """SELECT f.id, f.file_name, f.size_bytes, s.shared_at, u.username AS shared_by
               FROM file_shares s
               JOIN files f ON f.id = s.file_id
               JOIN users u ON u.id = s.shared_by_user_id
               WHERE s.shared_with_user_id = %s
               ORDER BY s.shared_at DESC, f.id DESC""",
            (user_id,),
        )
        return cur.fetchall()


def get_file(file_id: int) -> dict | None:
    """Le proprietaire est expose sous la cle `owner_id`."""
    with _cursor() as cur:
        cur.execute(
            "SELECT id, user_id AS owner_id, file_name, stored_filename, size_bytes FROM files WHERE id = %s",
            (file_id,),
        )
        return cur.fetchone()


def is_shared_with(file_id: int, user_id: int) -> bool:
    with _cursor() as cur:
        cur.execute("SELECT 1 FROM file_shares WHERE file_id = %s AND shared_with_user_id = %s", (file_id, user_id))
        return cur.fetchone() is not None


def add_file(owner_id: int, file_name: str, stored_filename: str, size_bytes: int) -> int:
    with _cursor() as cur:
        cur.execute(
            """INSERT INTO files (user_id, file_name, stored_filename, size_bytes)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (owner_id, file_name, stored_filename, size_bytes),
        )
        return cur.fetchone()["id"]


def delete_file(file_id: int, owner_id: int) -> str | None:
    """Supprime le fichier s'il appartient a `owner_id` (verification et suppression en une seule requete).

    Retourne le nom stocke sur disque, ou None si le fichier n'existe pas / n'est pas a cet utilisateur.
    Les partages partent en cascade.
    """
    with _cursor() as cur:
        cur.execute("DELETE FROM files WHERE id = %s AND user_id = %s RETURNING stored_filename", (file_id, owner_id))
        row = cur.fetchone()
        return row["stored_filename"] if row else None


# ── Partages ─────────────────────────────────────────────────────────────────

def add_share(file_id: int, shared_by_id: int, shared_with_id: int) -> None:
    try:
        with _cursor() as cur:
            cur.execute(
                """INSERT INTO file_shares (file_id, shared_by_user_id, shared_with_user_id) VALUES (%s, %s, %s)
                   ON CONFLICT (file_id, shared_with_user_id) DO NOTHING""",
                (file_id, shared_by_id, shared_with_id),
            )
    except psycopg.errors.ForeignKeyViolation as exc:
        # La contrainte violee dit laquelle des references manque.
        if exc.diag.constraint_name == "file_shares_file_id_fkey":
            raise ValueError("Fichier introuvable.") from exc
        raise ValueError("Utilisateur introuvable.") from exc


def remove_share(file_id: int, shared_with_id: int) -> None:
    with _cursor() as cur:
        cur.execute("DELETE FROM file_shares WHERE file_id = %s AND shared_with_user_id = %s", (file_id, shared_with_id))
