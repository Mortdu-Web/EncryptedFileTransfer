"""Accès PostgreSQL du service de transfert de fichiers : schéma `transfer` + comptes `core.accounts`.

REMPLACANT DIRECT de l'ancien fichier : mêmes fonctions, mêmes paramètres, mêmes retours (clés `file_name`,
`stored_filename`, `owner_id`...), même init_app(app). Fichier autonome (psycopg, psycopg_pool, Flask).
Les identifiants d'utilisateurs et de fichiers sont les anciens entiers : la migration les a conservés.

La base ne stocke que les métadonnées ; le contenu chiffré reste sur disque sous `stored_filename`, et les clés de
chiffrement ne doivent JAMAIS être en base. `stored_filename` doit être généré par le serveur (voir
new_stored_filename), jamais dérivé du nom envoyé par l'utilisateur.

Nouveautés côté .env (toutes optionnelles) :
  DB_USER     : rôle PROPRE à ce service (svc_transfer, voir sql/003_login_roles.sql)
  DB_SSLMODE  : « require » par défaut ; « verify-full » + DB_SSLROOTCERT en production
Plus de DB_AUTO_CREATE ni de vérification de droits GRANT à la main : voir sql/001_schema.sql.

Sécurité renforcée sans changer vos routes
  - la base refuse de partager un fichier dont on n'est pas propriétaire, ou avec soi-même ;
  - suppression et partage sont inscrits au journal d'audit (core.audit_log) ;
  - les partages peuvent expirer (add_share(..., expires_at=...) ; is_shared_with et les listes les ignorent ensuite).
Pour que get_file passe aussi par la RLS (et ne renvoie que les fichiers accessibles), appelez
set_current_account(user_id) dans un before_request une fois l'utilisateur identifié ; sinon get_file renvoie
les métadonnées par id comme avant, et vos routes continuent de vérifier propriétaire / partage.
"""
import atexit
import uuid
from contextlib import contextmanager
from datetime import datetime

import psycopg
from flask import current_app, g
from psycopg.conninfo import make_conninfo
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

_EXT_KEY = "hub_db_pool"
_REQUIRED = ("transfer.files", "transfer.file_shares", "core.accounts")


# ── Connexion / pool ─────────────────────────────────────────────────────────

def init_app(app) -> None:
    """Ouvre le pool et vérifie la base. Lève RuntimeError (message lisible) au moindre problème.

    Un pool par worker gunicorn, créé ici : ne pas lancer gunicorn avec --preload.
    """
    cfg = app.config
    missing = [k for k in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD") if not cfg.get(k)]
    if missing:
        raise RuntimeError(f"Variables manquantes dans le .env : {', '.join(missing)}")

    conninfo = make_conninfo(
        host=cfg["DB_HOST"],
        port=cfg.get("DB_PORT") or 5432,
        dbname=cfg["DB_NAME"],
        user=cfg["DB_USER"],
        password=cfg["DB_PASSWORD"],
        connect_timeout=10,
        client_encoding="UTF8",
        sslmode=cfg.get("DB_SSLMODE") or "require",
        sslrootcert=cfg.get("DB_SSLROOTCERT") or None,
        options="-c statement_timeout=10000 -c lock_timeout=5000 -c idle_in_transaction_session_timeout=15000",
    )
    # Test direct avant le pool : le pool masque la vraie cause (mauvais mot de passe, hôte injoignable...).
    try:
        psycopg.connect(conninfo).close()
    except psycopg.Error as exc:
        raise RuntimeError(
            f"[DB] Impossible de se connecter à PostgreSQL {cfg['DB_HOST']}:{cfg.get('DB_PORT') or 5432} "
            f"(base « {cfg['DB_NAME']} », utilisateur « {cfg['DB_USER']} ») : {exc}"
        ) from exc

    pool = ConnectionPool(
        conninfo,
        min_size=cfg.get("DB_POOL_MIN") or 1,
        max_size=cfg.get("DB_POOL_MAX") or 5,
        kwargs={"row_factory": dict_row},
        check=ConnectionPool.check_connection,  # remplace une connexion morte au lieu de la renvoyer
        timeout=10,                              # attente maximale d'une connexion libre (sinon PoolTimeout -> 503)
        name="hub-transfer",
        open=True,
    )
    try:
        problems = []
        with pool.connection() as conn:
            for t in _REQUIRED:
                row = conn.execute(
                    "SELECT to_regclass(%s) IS NOT NULL AS ok, "
                    "COALESCE(has_table_privilege(to_regclass(%s), 'SELECT'), false) AS can_read", (t, t)
                ).fetchone()
                if not row["ok"]:
                    problems.append(f"table {t} introuvable (sql/001_schema.sql appliqué ?)")
                elif not row["can_read"]:
                    problems.append(f"pas de droit SELECT sur {t} (DB_USER rattaché à app_transfer ?)")
        if problems:
            raise RuntimeError("[DB] Base non prête : " + " ; ".join(problems))
    except Exception:
        pool.close()
        raise
    app.extensions[_EXT_KEY] = pool
    atexit.register(pool.close)


@contextmanager
def _session(account_id: int | None = None, *, readonly: bool = False):
    """Curseur dans une transaction (commit / rollback / retour au pool automatiques).

    `account_id` est posé pour la durée de la transaction : c'est lui que la RLS utilise pour filtrer.
    """
    with current_app.extensions[_EXT_KEY].connection() as conn:
        with conn.cursor() as cur:
            if readonly:
                cur.execute("SET TRANSACTION READ ONLY")  # doit être la première instruction
            cur.execute("SELECT set_config('app.account_id', %s, true)",
                        ("" if account_id is None else str(int(account_id)),))
            yield cur


def _audit(cur, action: str, file_id: int, details: dict | None = None) -> None:
    cur.execute("INSERT INTO core.audit_log (service, action, entity, entity_id, details) "
                "VALUES ('transfer', %s, 'file', %s, %s)",
                (action, str(file_id), Jsonb(details) if details else None))


def set_current_account(user_id: int | None) -> None:
    """Optionnel : à appeler dans un before_request une fois l'utilisateur identifié (voir l'en-tête)."""
    g.account_id = user_id


def new_stored_filename() -> str:
    """Nom aléatoire pour le fichier sur disque (à passer à add_file), sans lien avec le nom d'origine."""
    return uuid.uuid4().hex


# ── Utilisateurs (comptes, en lecture seule) ─────────────────────────────────

def get_user_by_username(username: str) -> dict | None:
    """{id, username} du compte portant cet identifiant, ou None."""
    with _session(readonly=True) as cur:
        cur.execute("SELECT id, username FROM core.accounts WHERE lower(username) = lower(%s) AND is_active "
                    "ORDER BY id LIMIT 1", (username,))
        return cur.fetchone()


def list_users() -> list[dict]:
    """[{id, username, label}] : `label` = ce qu'affiche le menu « Partager avec »."""
    with _session(readonly=True) as cur:
        cur.execute("SELECT id, username, username AS label FROM core.accounts "
                    "WHERE is_active AND kind = 'personal' ORDER BY lower(username), id")
        return cur.fetchall()


# ── Fichiers ─────────────────────────────────────────────────────────────────

def list_files_by_owner(owner_id: int) -> list[dict]:
    """Fichiers de l'utilisateur, avec `shared_with_ids` (liste d'identifiants, partages expirés exclus)."""
    with _session(owner_id, readonly=True) as cur:
        cur.execute(
            """SELECT f.id, f.display_name AS file_name, f.size_bytes, f.uploaded_at,
                      COALESCE(array_agg(s.shared_with) FILTER (WHERE s.shared_with IS NOT NULL), '{}') AS shared_with_ids
               FROM transfer.files f
               LEFT JOIN transfer.file_shares s
                      ON s.file_id = f.id AND (s.expires_at IS NULL OR s.expires_at > now())
               WHERE f.owner_id = %s
               GROUP BY f.id
               ORDER BY f.uploaded_at DESC, f.id DESC""",
            (owner_id,),
        )
        return cur.fetchall()


def list_files_shared_with(user_id: int) -> list[dict]:
    """Fichiers partagés avec l'utilisateur (non expirés), avec `shared_by` (nom de l'expéditeur) et `shared_at`."""
    with _session(user_id, readonly=True) as cur:
        cur.execute(
            """SELECT f.id, f.display_name AS file_name, f.size_bytes, s.shared_at, u.username AS shared_by
               FROM transfer.file_shares s
               JOIN transfer.files f ON f.id = s.file_id
               JOIN core.accounts u ON u.id = s.shared_by
               WHERE s.shared_with = %s AND (s.expires_at IS NULL OR s.expires_at > now())
               ORDER BY s.shared_at DESC, f.id DESC""",
            (user_id,),
        )
        return cur.fetchall()


def get_file(file_id: int) -> dict | None:
    """Le propriétaire est exposé sous la clé `owner_id`.

    Avec set_current_account(), ne renvoie que les fichiers accessibles à l'utilisateur courant (propriétaire ou
    partage valide) : un fichier inexistant et un fichier interdit sont alors indiscernables (même 404).
    """
    account = g.get("account_id")
    with _session(account, readonly=True) as cur:
        if account is not None:
            cur.execute("SELECT id, owner_id, display_name AS file_name, storage_key AS stored_filename, size_bytes "
                        "FROM transfer.files WHERE id = %s", (file_id,))
        else:
            cur.execute("SELECT id, owner_id, display_name AS file_name, storage_key AS stored_filename, size_bytes "
                        "FROM transfer.get_file_meta(%s)", (file_id,))
        return cur.fetchone()


def is_shared_with(file_id: int, user_id: int) -> bool:
    """True si le fichier est partagé avec l'utilisateur et que le partage n'a pas expiré."""
    with _session(user_id, readonly=True) as cur:
        cur.execute("SELECT 1 FROM transfer.file_shares WHERE file_id = %s AND shared_with = %s "
                    "AND (expires_at IS NULL OR expires_at > now())", (file_id, user_id))
        return cur.fetchone() is not None


def add_file(owner_id: int, file_name: str, stored_filename: str, size_bytes: int,
             sha256: bytes | None = None, content_type: str | None = None, key_version: int = 1) -> int:
    """Enregistre un fichier. `sha256` (32 octets, empreinte du contenu chiffré), `content_type` et `key_version`
    (rotation des clés) sont optionnels. ValueError si le nom ou la clé de stockage sont invalides."""
    try:
        with _session(owner_id) as cur:
            cur.execute(
                """INSERT INTO transfer.files (owner_id, display_name, storage_key, size_bytes, sha256,
                                               content_type, key_version)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                (owner_id, file_name, stored_filename, size_bytes, sha256, content_type, key_version),
            )
            file_id = cur.fetchone()["id"]
            _audit(cur, "upload", file_id, {"size": size_bytes})
            return file_id
    except psycopg.errors.CheckViolation as exc:  # nom (« / », « \\ », caractères de contrôle...) ou clé invalide
        raise ValueError("Nom de fichier ou métadonnées invalides.") from exc


def delete_file(file_id: int, owner_id: int) -> str | None:
    """Supprime le fichier s'il appartient à `owner_id` (vérification et suppression en une seule requête).

    Retourne le nom stocké sur disque, ou None si le fichier n'existe pas / n'est pas à cet utilisateur.
    Les partages partent en cascade. Effacer le fichier du disque APRÈS cet appel.
    """
    with _session(owner_id) as cur:
        cur.execute("DELETE FROM transfer.files WHERE id = %s AND owner_id = %s RETURNING storage_key",
                    (file_id, owner_id))
        row = cur.fetchone()
        if row:
            _audit(cur, "delete", file_id)
        return row["storage_key"] if row else None


# ── Partages ─────────────────────────────────────────────────────────────────

def add_share(file_id: int, shared_by_id: int, shared_with_id: int, expires_at: datetime | None = None) -> None:
    """Partage un fichier dont `shared_by_id` est propriétaire. ValueError (message affichable) si impossible.

    Un nouveau partage sur un fichier déjà partagé met à jour la date d'expiration.
    """
    try:
        with _session(shared_by_id) as cur:
            cur.execute(
                """INSERT INTO transfer.file_shares (file_id, shared_by, shared_with, expires_at) VALUES (%s, %s, %s, %s)
                   ON CONFLICT (file_id, shared_with) DO UPDATE SET expires_at = EXCLUDED.expires_at""",
                (file_id, shared_by_id, shared_with_id, expires_at),
            )
            _audit(cur, "share", file_id, {"with": shared_with_id,
                                           "expires_at": expires_at.isoformat() if expires_at else None})
    except psycopg.errors.InsufficientPrivilege as exc:  # pas propriétaire, ou fichier inexistant
        raise ValueError("Fichier introuvable.") from exc
    except psycopg.errors.ForeignKeyViolation as exc:
        # La contrainte violée dit laquelle des références manque.
        if exc.diag.constraint_name == "file_shares_file_id_fkey":
            raise ValueError("Fichier introuvable.") from exc
        raise ValueError("Utilisateur introuvable.") from exc
    except psycopg.errors.CheckViolation as exc:  # partage avec soi-même
        raise ValueError("Partage impossible avec ce compte.") from exc


def remove_share(file_id: int, shared_with_id: int) -> None:
    """Retire le partage d'un fichier pour un utilisateur.

    Agit au nom de l'utilisateur courant (set_current_account), à défaut du propriétaire du fichier.
    """
    actor = g.get("account_id")
    if actor is None:
        meta = get_file(file_id)
        if meta is None:
            return
        actor = meta["owner_id"]
    with _session(actor) as cur:
        cur.execute("DELETE FROM transfer.file_shares WHERE file_id = %s AND shared_with = %s",
                    (file_id, shared_with_id))
        if cur.rowcount:
            _audit(cur, "unshare", file_id, {"with": shared_with_id})
