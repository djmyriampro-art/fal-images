"""Serveur MCP fal.ai — accès à TOUS les modèles fal (connecteur distant).

Ce connecteur ne se limite à aucun modèle : il peut exécuter n'importe lequel
des 500+ modèles hébergés sur fal.ai — génération d'images, retouche, vidéo,
image->vidéo, avatars qui parlent (UGC), lip-sync, audio/voix, etc. — en
passant simplement l'identifiant du modèle (l'« endpoint ») et ses paramètres.

Outils exposés à Claude :
  - run_model        : exécute N'IMPORTE QUEL modèle fal.
  - get_model_schema : renvoie les paramètres attendus par un modèle donné,
                       pour savoir quoi passer à run_model.

Conçu pour être hébergé puis ajouté dans claude.ai comme "connecteur
personnalisé". L'accès est protégé par une clé secrète en en-tête x-api-key.

Variables d'environnement :
  FAL_KEY            (obligatoire)  clé API fal.ai
  FAL_MCP_SECRET     (recommandé)   clé partagée exigée en en-tête x-api-key
  FAL_MCP_TRANSPORT  (optionnel)    "http" (défaut) ou "stdio"
  PORT               (optionnel)    port en mode http (défaut : 8000)
"""

import os
import json
from typing import Any
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


SECRET = os.environ.get("FAL_MCP_SECRET")   # si défini, exigé en en-tête x-api-key
SECRET_HEADER = "x-api-key"

IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "gif", "avif", "bmp"}


class SecretGate(Middleware):
    """Bloque les appels d'outils si la clé partagée est absente ou fausse."""

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        if SECRET:
            headers = get_http_headers() or {}
            if headers.get(SECRET_HEADER) != SECRET:
                raise ToolError("Accès refusé : clé manquante ou invalide.")
        return await call_next(context)


mcp = FastMCP("fal.ai")   # doit rester au niveau module pour l'hébergeur
mcp.add_middleware(SecretGate())


def _ext(url: str) -> str:
    tail = url.split("?")[0].rsplit("/", 1)[-1]
    return tail.rsplit(".", 1)[-1].lower() if "." in tail else ""


def _download(url: str) -> bytes:
    req = Request(url, headers={"User-Agent": "fal-mcp/1.0"})
    with urlopen(req, timeout=180) as resp:
        return resp.read()


def _collect_urls(obj: Any, out: list) -> None:
    """Trouve récursivement toutes les URLs média d'une réponse fal."""
    if isinstance(obj, dict):
        u = obj.get("url")
        if isinstance(u, str):
            out.append(u)
        for v in obj.values():
            _collect_urls(v, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect_urls(v, out)


def _format_result(result: dict, embed_images: bool) -> list:
    urls: list[str] = []
    _collect_urls(result, urls)

    if not urls:
        blob = json.dumps(result, ensure_ascii=False)[:4000]
        return [f"Terminé. Réponse du modèle : {blob}"]

    lines, previews = [], []
    for u in urls:
        lines.append(u)
        if embed_images and _ext(u) in IMAGE_EXTS:
            try:
                data = _download(u)
                fmt = _ext(u).replace("jpg", "jpeg")
                previews.append(Image(data=data, format=fmt))
            except Exception:
                pass  # on garde au moins l'URL

    header = "Résultat(s) — les fichiers sont hébergés par fal (clique pour ouvrir) :\n" + "\n".join(lines)
    return [header, *previews]


@mcp.tool
def run_model(
    endpoint: str,
    arguments: dict[str, Any],
    embed_images: bool = True,
) -> list:
    """Exécute N'IMPORTE QUEL modèle de fal.ai et renvoie le résultat.

    Fonctionne pour tous les types : image, retouche d'image, vidéo,
    image->vidéo, avatar qui parle (UGC), lip-sync, audio/voix, etc.
    Les images produites sont affichées dans la conversation ; les vidéos et
    audios sont renvoyés sous forme de lien à ouvrir.

    Args:
        endpoint: L'identifiant du modèle fal (son « endpoint »). Exemples :
            'fal-ai/flux/dev', 'fal-ai/nano-banana-2', 'fal-ai/nano-banana-2/edit',
            'fal-ai/veo3', 'fal-ai/kling-video/v2/master/image-to-video',
            'fal-ai/ai-avatar/single-text', 'fal-ai/sync-lipsync'.
            Catalogue complet : https://fal.ai/models
        arguments: Le dictionnaire des paramètres attendus par CE modèle, ex.
            {"prompt": "..."} pour une image, ou
            {"image_url": "...", "prompt": "..."} pour une vidéo à partir d'image.
            En cas de doute, appelle d'abord get_model_schema(endpoint).
        embed_images: Affiche les images produites dans la conversation (True).
    """
    result = fal_client.subscribe(endpoint, arguments=arguments, with_logs=False)
    return _format_result(result, embed_images)


@mcp.tool
def get_model_schema(endpoint: str) -> dict:
    """Renvoie les paramètres d'entrée attendus par un modèle fal.ai précis.

    À utiliser avant run_model quand on ne connaît pas les paramètres d'un modèle.

    Args:
        endpoint: L'identifiant du modèle, ex. 'fal-ai/veo3'.
    """
    url = f"https://fal.ai/api/openapi/queue/openapi.json?endpoint_id={endpoint}"
    try:
        req = Request(url, headers={"User-Agent": "fal-mcp/1.0"})
        with urlopen(req, timeout=60) as resp:
            spec = json.load(resp)
    except Exception as exc:  # noqa: BLE001
        return {"endpoint": endpoint, "erreur": str(exc),
                "astuce": "Vérifie l'identifiant sur https://fal.ai/models"}

    schemas = spec.get("components", {}).get("schemas", {})
    for key, val in schemas.items():
        if key.lower().endswith("input"):
            return {"endpoint": endpoint, "parametres_entree": val,
                    "schemas_disponibles": list(schemas.keys())}
    return {"endpoint": endpoint, "schemas": schemas}


if __name__ == "__main__":
    transport = os.environ.get("FAL_MCP_TRANSPORT", "http")
    if transport == "stdio":
        mcp.run()
    else:
        mcp.run(transport="http", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
