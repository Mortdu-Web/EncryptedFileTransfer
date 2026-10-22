from .config import Config

# Usage dans les routes : @require_auth()  ou  @require_auth("role")  ou  @require_auth("a", "b", mode="all")
if Config.AUTH_ENABLED:
    from auth_client import require_auth
else:

    def require_auth(*roles: str, mode: str = "any"):
        """Auth desactivee (dev local) : ne controle rien."""

        def decorator(view):
            return view

        return decorator


def human_size(n: int | None) -> str:
    """Taille lisible en francais : 1536 -> « 1,5 Ko »."""
    size = float(n or 0)
    for unit in ("o", "Ko", "Mo", "Go"):
        if size < 1024 or unit == "Go":
            text = f"{size:.0f}" if unit == "o" else f"{size:.1f}".replace(".", ",").removesuffix(",0")
            return f"{text} {unit}"
        size /= 1024
