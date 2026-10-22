"""Stockage (PostgreSQL, sur une machine distante) des utilisateurs, fichiers et partages.

Connexion configurée via le .env : DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD (+ DB_POOL_MIN / DB_POOL_MAX).
Les routes n'appellent que les fonctions ci-dessous : c'est le seul fichier à adapter pour une autre base.
Les identifiants d'utilisateurs sont ceux de Keycloak (`sub`), stockés en texte.

Gestion des connexions (même principe que l'ancien core/db : un pool, une connexion empruntée le temps
d'une opération, puis rendue) :
  - un pool par worker gunicorn, créé dans create_app (donc ne pas lancer gunicorn avec --preload) ;
  - chaque fonction emprunte une connexion avec `_cursor()` : commit si tout va bien, rollback sinon,
    puis retour au pool dans tous les cas ;
  - une connexion coupée côté réseau (base distante, pare-feu) est détectée et remplacée avant usage ;
  - le démarrage échoue immédiatement, avec un message clair, si la base est injoignable ou si les
    tables ft_* sont absentes.
"""
import atexit
from contextlib import contextmanager
from pathlib import Path

import psycopg
from flask import current_app
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
TABLES = ("ft_users", "ft_files", "ft_file_shares")
_EXT_KEY = "ft_db_pool"


# ── Connexion / pool ─────────────────────────────────────────────────────────

def init_app(app) -> None:
    """Ouvre le pool et vérifie la base. Lève RuntimeError (message lisible) au moindre problème."""
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
        _ensure_schema(cfg, pool)
    except Exception:
        pool.close()
        raise
    app.extensions[_EXT_KEY] = pool
    atexit.register(pool.close)


def _ensure_schema(cfg, pool: ConnectionPool) -> None:
    """Cree les tables (DB_AUTO_CREATE=true) ou verifie qu'elles existent."""
    try:
        with pool.connection() as conn:
            if cfg.get("DB_AUTO_CREATE"):
                conn.execute(SCHEMA)
                return
            absent = [
                t for t in TABLES if conn.execute("SELECT to_regclass(%s) AS found", (t,)).fetchone()["found"] is None
            ]
    except psycopg.errors.InsufficientPrivilege as exc:
        raise RuntimeError(
            "[DB] DB_AUTO_CREATE=true, mais l'utilisateur n'a pas le droit CREATE sur le schéma public "
            "(PostgreSQL 15+ le retire par défaut). Lancer app/schema.sql une fois avec un compte "
            "administrateur, puis mettre DB_AUTO_CREATE=false."
        ) from exc
    if absent:
        raise RuntimeError(
            f"[DB] Tables absentes : {', '.join(absent)}. Lancer une fois "
            "`psql -h <hote> -U <compte_admin> -d <base> -f app/schema.sql` "
            "(ou DB_AUTO_CREATE=true si l'utilisateur a le droit CREATE sur le schéma public)."
        )


@contextmanager
def _cursor():
    """Emprunte une connexion du pool : commit en sortie normale, rollback si exception, retour au pool."""
    with current_app.extensions[_EXT_KEY].connection() as conn:
        with conn.cursor() as cur:
            yield cur


# ── Utilisateurs ─────────────────────────────────────────────────────────────

_LABEL = "COALESCE(NULLIF(display_name, ''), username)"


def upsert_users(users: list[dict]) -> None:
    """users = [{"keycloak_id", "username", "display_name"}] : cree ou met a jour (sans ecriture si rien n'a change)."""
    rows = sorted(
        (u["keycloak_id"], u.get("username") or u["keycloak_id"], u.get("display_name") or "")
        for u in users
        if u.get("keycloak_id")
    )  # tri : les lignes sont verrouillees toujours dans le meme ordre, donc pas d'interblocage entre requetes
    if not rows:
        return
    with _cursor() as cur:
        cur.executemany(
            """INSERT INTO ft_users (keycloak_id, username, display_name) VALUES (%s, %s, %s)
               ON CONFLICT (keycloak_id) DO UPDATE
               SET username = EXCLUDED.username, display_name = EXCLUDED.display_name
               WHERE (ft_users.username, ft_users.display_name) IS DISTINCT FROM (EXCLUDED.username, EXCLUDED.display_name)""",
            rows,
        )


def list_users() -> list[dict]:
    """[{id, username, label}] : `label` = nom complet, a defaut l'identifiant."""
    with _cursor() as cur:
        cur.execute(f"SELECT keycloak_id AS id, username, {_LABEL} AS label FROM ft_users ORDER BY LOWER({_LABEL})")
        return cur.fetchall()


# ── Fichiers ─────────────────────────────────────────────────────────────────

def list_files_by_owner(owner_id: str) -> list[dict]:
    """Fichiers de l'utilisateur, avec `shared_with_ids` (liste d'identifiants)."""
    with _cursor() as cur:
        cur.execute(
            """SELECT f.id, f.file_name, f.size_bytes, f.uploaded_at,
                      COALESCE(array_agg(s.shared_with_id) FILTER (WHERE s.shared_with_id IS NOT NULL), '{}') AS shared_with_ids
               FROM ft_files f
               LEFT JOIN ft_file_shares s ON s.file_id = f.id
               WHERE f.owner_id = %s
               GROUP BY f.id
               ORDER BY f.uploaded_at DESC, f.id DESC""",
            (owner_id,),
        )
        return cur.fetchall()


def list_files_shared_with(user_id: str) -> list[dict]:
    """Fichiers partages avec l'utilisateur, avec `shared_by` (nom du proprietaire) et `shared_at`."""
    with _cursor() as cur:
        cur.execute(
            """SELECT f.id, f.file_name, f.size_bytes, s.shared_at,
                      COALESCE(NULLIF(u.display_name, ''), u.username) AS shared_by
               FROM ft_file_shares s
               JOIN ft_files f ON f.id = s.file_id
               JOIN ft_users u ON u.keycloak_id = f.owner_id
               WHERE s.shared_with_id = %s
               ORDER BY s.shared_at DESC, f.id DESC""",
            (user_id,),
        )
        return cur.fetchall()


def get_file(file_id: int) -> dict | None:
    with _cursor() as cur:
        cur.execute(
            "SELECT id, owner_id, file_name, stored_filename, size_bytes FROM ft_files WHERE id = %s", (file_id,)
        )
        return cur.fetchone()


def is_shared_with(file_id: int, user_id: str) -> bool:
    with _cursor() as cur:
        cur.execute("SELECT 1 FROM ft_file_shares WHERE file_id = %s AND shared_with_id = %s", (file_id, user_id))
        return cur.fetchone() is not None


def add_file(owner_id: str, file_name: str, stored_filename: str, size_bytes: int) -> int:
    with _cursor() as cur:
        cur.execute(
            """INSERT INTO ft_files (owner_id, file_name, stored_filename, size_bytes)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (owner_id, file_name, stored_filename, size_bytes),
        )
        return cur.fetchone()["id"]


def delete_file(file_id: int, owner_id: str) -> str | None:
    """Supprime le fichier s'il appartient a `owner_id` (verification et suppression en une seule requete).

    Retourne le nom stocke sur disque, ou None si le fichier n'existe pas / n'est pas a cet utilisateur.
    Les partages partent en cascade.
    """
    with _cursor() as cur:
        cur.execute(
            "DELETE FROM ft_files WHERE id = %s AND owner_id = %s RETURNING stored_filename", (file_id, owner_id)
        )
        row = cur.fetchone()
        return row["stored_filename"] if row else None


# ── Partages ─────────────────────────────────────────────────────────────────

def add_share(file_id: int, shared_with_id: str) -> None:
    try:
        with _cursor() as cur:
            cur.execute(
                "INSERT INTO ft_file_shares (file_id, shared_with_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                (file_id, shared_with_id),
            )
    except psycopg.errors.ForeignKeyViolation as exc:
        # La contrainte violee dit laquelle des deux references manque.
        if "file_id" in (exc.diag.constraint_name or ""):
            raise ValueError("Fichier introuvable.") from exc
        raise ValueError("Utilisateur introuvable.") from exc


def remove_share(file_id: int, shared_with_id: str) -> None:
    with _cursor() as cur:
        cur.execute(
            "DELETE FROM ft_file_shares WHERE file_id = %s AND shared_with_id = %s", (file_id, shared_with_id)
        )