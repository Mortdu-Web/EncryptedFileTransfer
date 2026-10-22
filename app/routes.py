"""Blueprint « files » : page principale, upload, téléchargement, partage et suppression."""
from flask import Blueprint, abort, current_app, jsonify, render_template, request, send_file

from . import db, users
from .config import Config
from .crypto import DecryptionError
from .fileservice import FileService
from .helpers import require_auth

bp = Blueprint("files", __name__)


# ─── Page principale ─────────────────────────────────────────────────────────
@bp.route("/")
@require_auth()
def index():
    me = users.current_user()
    return render_template(
        "files.html",
        current_user=me,
        my_files=db.list_files_by_owner(me["id"]),
        shared_files=db.list_files_shared_with(me["id"]),
        all_users=users.list_other_users(),
        max_upload_mb=Config.MAX_UPLOAD_MB,
    )


# ─── Upload / téléchargement ─────────────────────────────────────────────────
@bp.route("/upload", methods=["POST"])
@require_auth()
def upload():
    if "file" not in request.files:
        return jsonify({"error": "Aucun fichier reçu"}), 400
    try:
        FileService.upload(request.files["file"], users.current_user()["id"])
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception:
        current_app.logger.exception("Echec de l'upload")
        return jsonify({"error": "Erreur interne lors de l'enregistrement du fichier."}), 500
    return jsonify({"message": "Fichier uploadé avec succès"}), 200


@bp.route("/download/<int:file_id>")
@require_auth()
def download(file_id):
    try:
        decrypted, file = FileService.download(file_id, users.current_user()["id"])
    except PermissionError:
        abort(403)
    except FileNotFoundError:
        abort(404)  # fichier inconnu en base, ou absent du disque
    except DecryptionError:
        current_app.logger.exception("Déchiffrement impossible pour le fichier %s", file_id)
        abort(500)

    response = send_file(decrypted, as_attachment=True, download_name=file["file_name"])
    response.headers["Cache-Control"] = "private, no-store"
    return response


# ─── Partage / retrait ───────────────────────────────────────────────────────
@bp.route("/share", methods=["POST"])
@require_auth()
def share():
    data = request.get_json(silent=True) or {}
    try:
        file_id = int(data.get("file_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Fichier invalide"}), 400
    try:
        target_id = int(data.get("user_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "Utilisateur invalide"}), 400
    add = bool(data.get("add", True))

    me = users.current_user()
    file = db.get_file(file_id)
    if not file or file["owner_id"] != me["id"]:
        return jsonify({"error": "Non autorisé"}), 403
    if target_id == me["id"]:
        return jsonify({"error": "Ce fichier vous appartient déjà"}), 400

    if add:
        try:
            db.add_share(file_id, me["id"], target_id)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"message": "Partagé ✓"})

    db.remove_share(file_id, target_id)
    return jsonify({"message": "Accès retiré"})


# ─── Suppression ─────────────────────────────────────────────────────────────
@bp.route("/<int:file_id>", methods=["DELETE"])
@require_auth()
def delete(file_id):
    try:
        FileService.delete(file_id, users.current_user()["id"])
    except PermissionError:
        return jsonify({"error": "Non autorisé"}), 403
    return jsonify({"message": "Fichier supprimé"})
