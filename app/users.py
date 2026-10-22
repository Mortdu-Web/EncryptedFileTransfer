"""Utilisateurs = comptes Keycloak (via le service d'auth), copies dans la table `ft_users`."""
import requests
from flask import current_app, g, request

from . import db
from .config import Config


def _fetch_keycloak_users() -> list[dict]:
    """Interroge GET <AUTH_INTERNAL_URL>/api/users en transmettant la session de l'utilisateur."""
    headers = {h: request.headers[h] for h in ("Cookie", "Authorization") if request.headers.get(h)}
    resp = requests.get(f"{Config.AUTH_INTERNAL_URL}/api/users", headers=headers, timeout=5)
    resp.raise_for_status()
    return resp.json()["users"]


def _full_name(first: str | None, last: str | None) -> str:
    return " ".join(p.strip() for p in (first, last) if p and p.strip())


def current_user() -> dict:
    """{id, username, label} de l'utilisateur connecté (g.user, posé par require_auth), enregistré si besoin.

    Sans auth (dev local) : identité simulée par DEV_USER_ID / DEV_USERNAME.
    """
    cached = getattr(g, "ft_user", None)
    if cached:
        return cached

    if Config.AUTH_ENABLED:
        user = getattr(g, "user", None) or {}
        sub = user.get("sub")
        if not sub:
            raise RuntimeError("Utilisateur non authentifié (g.user absent) : route sans @require_auth() ?")
        username = user.get("username") or user.get("preferred_username") or sub
        name = _full_name(user.get("given_name"), user.get("family_name")) or user.get("name") or ""
    else:
        sub, username, name = Config.DEV_USER_ID, Config.DEV_USERNAME, ""

    db.upsert_users([{"keycloak_id": sub, "username": username, "display_name": name}])
    g.ft_user = {"id": sub, "username": username, "label": name or username}
    return g.ft_user


def list_other_users() -> list[dict]:
    """Utilisateurs avec qui on peut partager (tous sauf soi-même), pour le menu « Partager avec ».

    Avec l'auth : annuaire Keycloak, recopié en base. Si Keycloak est injoignable (ou sans auth) :
    utilisateurs déjà connus en base.
    """
    me = current_user()
    if Config.AUTH_ENABLED:
        try:
            db.upsert_users(
                [
                    {
                        "keycloak_id": u.get("id"),
                        "username": u.get("username") or "",
                        "display_name": _full_name(u.get("firstName"), u.get("lastName")),
                    }
                    for u in _fetch_keycloak_users()
                ]
            )
        except (requests.RequestException, ValueError, KeyError):
            current_app.logger.exception("Liste Keycloak indisponible : repli sur les utilisateurs deja connus en base")
    return [u for u in db.list_users() if u["id"] != me["id"]]
