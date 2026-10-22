"""Chiffrement des fichiers au repos : AES-256-GCM.

Format du fichier sur disque : b"EFT1" | nonce (12 octets) | texte chiffré + tag d'authentification.
Le nom du fichier stocké est lié au contenu (données associées) : un fichier chiffré renommé/échangé
avec un autre ne peut pas être déchiffré.

Seul fichier à adapter pour changer d'algorithme (les routes n'appellent que encrypt_file / decrypt_file).
"""
import base64
import binascii
import io
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .config import Config

MAGIC = b"EFT1"
NONCE_SIZE = 12

_KEY_HELP = (
    "ENCRYPTION_KEY invalide : il faut 32 octets encodés en base64. Pour en générer une :\n"
    '  python -c "import secrets,base64;print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"'
)


class DecryptionError(Exception):
    """Fichier corrompu, clé incorrecte ou fichier qui n'a pas été chiffré par ce service."""


def _load_key() -> bytes:
    raw = Config.ENCRYPTION_KEY.strip()
    if not raw:
        raise RuntimeError("Variable manquante dans le .env : ENCRYPTION_KEY.\n" + _KEY_HELP)
    try:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError(_KEY_HELP) from exc
    if len(key) != 32:
        raise RuntimeError(_KEY_HELP)
    return key


def init_app(app) -> None:
    """Échoue au démarrage (et non au premier upload) si la clé est absente ou mal formée."""
    _load_key()


def encrypt_file(data: bytes, path: str) -> None:
    """Chiffre `data` et l'écrit dans `path` (écriture atomique)."""
    nonce = os.urandom(NONCE_SIZE)
    aad = os.path.basename(path).encode()
    payload = MAGIC + nonce + AESGCM(_load_key()).encrypt(nonce, data, aad)

    tmp = f"{path}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(payload)
    os.replace(tmp, path)


def decrypt_file(path: str) -> io.BytesIO:
    """Lit et déchiffre `path`. Lève FileNotFoundError ou DecryptionError."""
    with open(path, "rb") as fh:
        payload = fh.read()
    if not payload.startswith(MAGIC) or len(payload) < len(MAGIC) + NONCE_SIZE:
        raise DecryptionError("Format de fichier chiffré inconnu.")

    nonce = payload[len(MAGIC) : len(MAGIC) + NONCE_SIZE]
    ciphertext = payload[len(MAGIC) + NONCE_SIZE :]
    try:
        clear = AESGCM(_load_key()).decrypt(nonce, ciphertext, os.path.basename(path).encode())
    except InvalidTag as exc:
        raise DecryptionError("Déchiffrement impossible (clé incorrecte ou fichier altéré).") from exc
    return io.BytesIO(clear)
