"""Logique métier des fichiers : chiffrement + disque + base. Les routes ne font qu'appeler ces méthodes."""
import io
import os
import uuid

from . import db
from .config import Config
from .crypto import decrypt_file, encrypt_file


def _path(stored_filename: str) -> str:
    # basename : le nom vient de la base, mais on ne sort jamais du dossier de stockage.
    return os.path.join(Config.UPLOAD_FOLDER, os.path.basename(stored_filename))


def _clean_name(raw: str | None) -> str:
    name = os.path.basename((raw or "").replace("\\", "/")).strip()
    return name[:255]


class FileService:
    @staticmethod
    def upload(file, owner_id: str) -> int:
        """Chiffre, stocke sur disque, enregistre en base. Lève ValueError si le fichier est inacceptable."""
        name = _clean_name(file.filename)
        if not name:
            raise ValueError("Nom de fichier manquant.")
        data = file.read()
        if not data:
            raise ValueError("Le fichier est vide.")
        if len(data) > Config.MAX_UPLOAD_MB * 1024 * 1024:
            raise ValueError(f"Fichier trop volumineux (maximum {Config.MAX_UPLOAD_MB} Mo).")

        # Nom aléatoire sans l'extension d'origine : le disque ne révèle rien sur le contenu.
        stored_filename = f"{uuid.uuid4().hex}.enc"
        path = _path(stored_filename)
        encrypt_file(data, path)
        try:
            return db.add_file(owner_id, name, stored_filename, len(data))
        except Exception:
            if os.path.isfile(path):  # pas de fichier orphelin si l'insertion en base échoue
                os.remove(path)
            raise

    @staticmethod
    def download(file_id: int, user_id: str) -> tuple[io.BytesIO, dict]:
        """Vérifie les droits, déchiffre. Lève FileNotFoundError, PermissionError ou DecryptionError."""
        file = db.get_file(file_id)
        if not file:
            raise FileNotFoundError()
        if file["owner_id"] != user_id and not db.is_shared_with(file_id, user_id):
            raise PermissionError()
        return decrypt_file(_path(file["stored_filename"])), file

    @staticmethod
    def delete(file_id: int, user_id: str) -> None:
        """Vérifie que l'utilisateur est propriétaire, supprime disque + base."""
        file = db.get_file(file_id)
        if not file or file["owner_id"] != user_id:
            raise PermissionError()

        path = _path(file["stored_filename"])
        if os.path.isfile(path):
            os.remove(path)
        db.delete_file(file_id)
