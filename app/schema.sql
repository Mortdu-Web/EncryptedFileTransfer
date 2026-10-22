-- Migration des tables EXISTANTES (users, files, file_shares) pour le service de transfert de fichiers.
-- Aucune table n'est creee : on complete celles du hub. A lancer UNE fois, avec le proprietaire des tables
-- (postgres) -- l'utilisateur de l'application n'a pas besoin de ce droit :
--   sudo -u postgres psql -d <base> -f schema.sql
-- Le script peut etre relance sans risque.

BEGIN;

-- Taille du fichier (en clair), affichee dans l'interface.
ALTER TABLE files ADD COLUMN IF NOT EXISTS size_bytes BIGINT NOT NULL DEFAULT 0;

-- Un fichier ne peut etre partage qu'une fois avec un meme utilisateur (sans cette contrainte,
-- "INSERT ... ON CONFLICT DO NOTHING" ne protege de rien et les doublons s'accumulent).
DELETE FROM file_shares a
      USING file_shares b
      WHERE a.id > b.id AND a.file_id = b.file_id AND a.shared_with_user_id = b.shared_with_user_id;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'file_shares_file_user_key') THEN
        ALTER TABLE file_shares
            ADD CONSTRAINT file_shares_file_user_key UNIQUE (file_id, shared_with_user_id);
    END IF;
END $$;

-- Index sur les colonnes de recherche (les cles etrangeres PostgreSQL ne sont pas indexees automatiquement).
CREATE INDEX IF NOT EXISTS files_user_id_idx            ON files (user_id);
CREATE INDEX IF NOT EXISTS file_shares_shared_with_idx  ON file_shares (shared_with_user_id);

COMMIT;
