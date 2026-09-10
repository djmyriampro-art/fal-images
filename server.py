"""Serveur MCP fal.ai — génération et édition d'images (version connecteur distant).

Expose deux outils à Claude :
  - generate_image : texte -> image
  - edit_image     : image(s) source + instruction -> image éditée

Conçu pour être hébergé (FastMCP Cloud / Prefect Horizon, Render, etc.) puis
ajouté dans claude.ai comme "connecteur personnalisé". L'accès est protégé par
une clé secrète envoyée en en-tête HTTP (x-api-key).

Variables d'environnement :
  FAL_KEY            (obligatoire)  clé API fal.ai
  FAL_MCP_SECRET     (recommandé)   clé partagée exigée en en-tête x-api-key
  FAL_OUTPUT_DIR     (optionnel)    dossier de sauvegarde (défaut : ~/fal-images)
  FAL_MCP_TRANSPORT  (optionnel)    "http" (défaut) ou "stdio"
  PORT               (optionnel)    port en mode http (défaut : 8000)
"""

import os
import datetime as dt
import mimetypes
from pathlib import Path
from urllib.request import urlopen, Request

import fal_client
from fastmcp import FastMCP
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.dependencies import get_http_headers
from fastmcp.exceptions import ToolError

try:
    from fastmcp.utilities.types import Image
except ImportError:  # compat versions plus anciennes
    from fastmcp import Image


OUTPUT_DIR = Path(os.environ.get("FAL_OUTPUT_DIR", Path.home() / "fal-images"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

SECRET = os.environ.get("FAL_MCP_SECRET")   # si défini, exigé en en-tête x-api-key
SECRET_HEADER = "x-api-key"


class SecretGate(Middleware):
    """Bloque les appels d'outils si la clé partagée est absente ou fausse."""

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        if SECRET:
            headers = get_http_headers() or {}
            if headers.get(SECRET_HEADER) != SECRET:
                raise ToolError("Accès refusé : clé manquante ou invalide.")
        return await call_next(context)


mcp = FastMCP("fal.ai images")   # doit rester au niveau module pour l'hébergeur
mcp.add_middleware(SecretGate())


def _download(url: str) -> tuple[bytes, str]:
    req = Request(url, headers={"User-Agent": "fal-mcp/1.0"})
    with urlopen(req, timeout=180) as resp:
        data = resp.read()
        ctype = resp.headers.get("Content-Type", "").split(";")[0].strip()
    ext = mimetypes.guess_extension(ctype) or ".png"
    if ext == ".jpe":
        ext = ".jpg"
    return data, ext


def _save(data: bytes, ext: str, tag: str) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = OUTPUT_DIR / f"{tag}-{stamp}{ext}"
    path.write_bytes(data)
    return path


def _handle_result(result: dict, tag: str, embed: bool) -> list:
    images = result.get("images") or []
    if not images:
        return [f"Aucune image renvoyée. Réponse brute : {result}"]

    previews, lines = [], []
    for i, img in enumerate(images, start=1):
        url = img.get("url")
        if not url:
            continue
        data, ext = _download(url)
        path = _save(data, ext, tag)
        lines.append(f"Image {i} : {url}\n  -> enregistrée : {path}")
        if embed:
            fmt = ext.lstrip(".").replace("jpg", "jpeg")
            try:
                previews.append(Image(data=data, format=fmt))
            except Exception:
                pass
    return ["\n".join(lines), *previews]


@mcp.tool
def generate_image(
    prompt: str,
    model: str = "fal-ai/flux/dev",
    image_size: str = "landscape_4_3",
    num_images: int = 1,
    seed: int | None = None,
    embed_preview: bool = True,
) -> list:
    """Génère une image à partir d'une description textuelle via fal.ai.

    Args:
        prompt: Description de l'image à générer.
        model: Endpoint fal. Ex. 'fal-ai/flux/dev' (qualité),
            'fal-ai/flux/schnell' (rapide), 'fal-ai/nano-banana-2'
            (suivi de prompt + texte lisible),
            'fal-ai/flux-pro/v1.1-ultra' (jusqu'à 2K).
        image_size: 'square_hd', 'square', 'portrait_4_3', 'portrait_16_9',
            'landscape_4_3', 'landscape_16_9' (selon le modèle).
        num_images: Nombre d'images (1 à 4 selon le modèle).
        seed: Graine aléatoire pour reproductibilité (optionnel).
        embed_preview: Affiche l'aperçu dans la conversation si True.
    """
    args: dict = {"prompt": prompt, "num_images": num_images}
    if image_size:
        args["image_size"] = image_size
    if seed is not None:
        args["seed"] = seed
    result = fal_client.subscribe(model, arguments=args, with_logs=False)
    return _handle_result(result, tag="gen", embed=embed_preview)


@mcp.tool
def edit_image(
    prompt: str,
    image_urls: list[str],
    model: str = "fal-ai/nano-banana-2/edit",
    num_images: int = 1,
    seed: int | None = None,
    embed_preview: bool = True,
) -> list:
    """Édite / recompose une ou plusieurs images existantes via fal.ai.

    Idéal pour retoucher un packshot produit, changer un fond, faire du
    compositing ou du transfert de style.

    Args:
        prompt: Instruction d'édition en langage naturel.
        image_urls: URLs des images source (jusqu'à ~14 selon le modèle).
        model: Endpoint d'édition. Ex. 'fal-ai/nano-banana-2/edit' (rapide),
            'fal-ai/nano-banana-pro/edit' (raisonnement max),
            'fal-ai/flux-pro/kontext' (édition FLUX).
        num_images: Nombre d'images à générer (1 à 4).
        seed: Graine aléatoire (optionnel).
        embed_preview: Affiche l'aperçu dans la conversation si True.
    """
    args: dict = {"prompt": prompt, "image_urls": image_urls, "num_images": num_images}
    if seed is not None:
        args["seed"] = seed
    result = fal_client.subscribe(model, arguments=args, with_logs=False)
    return _handle_result(result, tag="edit", embed=embed_preview)


if __name__ == "__main__":
    transport = os.environ.get("FAL_MCP_TRANSPORT", "http")
    if transport == "stdio":
        mcp.run()
    else:
        mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
