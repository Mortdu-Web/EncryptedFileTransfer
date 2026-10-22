-- Tables du service de transfert de fichiers (préfixe ft_ : pas de conflit avec les autres services
-- si la base est partagée). Exécuté au démarrage si DB_AUTO_CREATE=true, ou à la main :
--   psql -h <hote> -U <user> -d <base> -f app/schema.sql
SELECT pg_advisory_xact_lock(727002);  -- evite les courses entre workers gunicorn au demarrage

-- Utilisateurs connus du service (copie de l'annuaire Keycloak, mise à jour à chaque visite).
CREATE TABLE IF NOT EXISTS ft_users (
    keycloak_id  TEXT PRIMARY KEY,
    username     TEXT NOT NULL,
    display_name TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS ft_files (
    id              SERIAL PRIMARY KEY,
    owner_id        TEXT NOT NULL REFERENCES ft_users(keycloak_id) ON DELETE CASCADE,
    file_name       TEXT NOT NULL,                 -- nom d'origine, affiché et utilisé au téléchargement
    stored_filename TEXT NOT NULL UNIQUE,          -- nom aléatoire sur disque (<uuid>.enc)
    size_bytes      BIGINT NOT NULL DEFAULT 0,
    uploaded_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ft_files_owner_idx ON ft_files (owner_id);

CREATE TABLE IF NOT EXISTS ft_file_shares (
    file_id        INTEGER NOT NULL REFERENCES ft_files(id) ON DELETE CASCADE,
    shared_with_id TEXT NOT NULL REFERENCES ft_users(keycloak_id) ON DELETE CASCADE,
    shared_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (file_id, shared_with_id)
);
CREATE INDEX IF NOT EXISTS ft_file_shares_user_idx ON ft_file_shares (shared_with_id);
