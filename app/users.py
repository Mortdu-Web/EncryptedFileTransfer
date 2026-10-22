"""Utilisateur courant = ligne de la table `users` du hub, retrouvee par son identifiant (username).

L'authentification (Keycloak via auth_client, ou identite simulee en dev) ne change pas : elle fournit un
nom d'utilisateur, que l'on relie ici au compte de la table `users`. Un nom absent de la table n'a pas acces.
Ce service ne cree ni ne modifie aucun utilisateur.
"""
from flask import abort, current_app, g

from . import db
from .config import Config


def current_user() -> dict:
    """{id, username, label} de l'utilisateur connecte (id = users.id). 403 si son compte est inconnu de la table.

    Sans auth (dev local) : identite simulee par DEV_USERNAME, qui doit exister dans `users`.
    """
    cached = getattr(g, "ft_user", None)
    if cached:
        return cached

    if Config.AUTH_ENABLED:
        claims = getattr(g, "user", None) or {}
        username = claims.get("username") or claims.get("preferred_username")
        if not username:
            raise RuntimeError("Utilisateur non authentifié (g.user absent) : route sans @require_auth() ?")
    else:
        username = Config.DEV_USERNAME

    row = db.get_user_by_username(username)
    if row is None:
        current_app.logger.warning("Connexion refusee : « %s » n'existe pas dans la table users", username)
        abort(403)

    g.ft_user = {"id": row["id"], "username": row["username"], "label": row["username"]}
    return g.ft_user


def list_other_users() -> list[dict]:
    """Utilisateurs avec qui on peut partager (tous sauf soi-meme), pour le menu « Partager avec »."""
    me = current_user()
    return [u for u in db.list_users() if u["id"] != me["id"]]
