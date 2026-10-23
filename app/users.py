"""Utilisateur courant = ligne de la table `users` du hub, retrouvee par son identifiant (username).

L'authentification (Keycloak via auth_client, ou identite simulee en dev) ne change pas : elle fournit un
nom d'utilisateur, que l'on relie ici au compte de la table `users`. Un compte Keycloak sans ligne liée dans core.accounts n'a pas acces.
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
        # Même identification que les autres services du hub : par l'identifiant Keycloak (sub), pas par le nom
        # (le nom peut différer entre le token et core.accounts, et deux comptes peuvent être homonymes).
        sub = claims.get("keycloak_id") or claims.get("sub")
        if not sub and not claims:
            raise RuntimeError("Utilisateur non authentifié (g.user absent) : route sans @require_auth() ?")
        if not sub:
            current_app.logger.warning("Connexion refusee : identifiant Keycloak absent du token (clés : %s)",
                                       sorted(claims.keys()))
            abort(403)
        row = db.get_user_by_keycloak_id(str(sub))
        if row is None:
            current_app.logger.warning("Connexion refusee : aucun compte actif lié au compte Keycloak « %s » "
                                       "(se connecter une fois sur tasks / jeux le crée)",
                                       claims.get("username") or claims.get("preferred_username") or sub)
            abort(403)
    else:
        row = db.get_user_by_username(Config.DEV_USERNAME)  # dev local : identité simulée
        if row is None:
            current_app.logger.warning("DEV_USERNAME « %s » absent de core.accounts", Config.DEV_USERNAME)
            abort(403)

    g.ft_user = {"id": row["id"], "username": row["username"], "label": row["username"]}
    return g.ft_user


def list_other_users() -> list[dict]:
    """Utilisateurs avec qui on peut partager (tous sauf soi-meme), pour le menu « Partager avec »."""
    me = current_user()
    return [u for u in db.list_users() if u["id"] != me["id"]]