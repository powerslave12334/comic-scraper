# -*- coding: utf-8 -*-
"""
Cliente de la API de Pawchive (https://pawchive.pw/api/v1).

Archivo público estilo kemono con posts de Patreon / Fanbox / Discord.
JSON sin Cloudflare; se usa el mismo motor HTTP que el resto del proyecto
(curl_cffi con huella TLS de Chrome, vía brave_cf).

Claves técnicas (verificadas en vivo):
  - El listado (/{service}/user/{id}?o=...) pagina de 50 en 50 y solo da
    `path` por archivo (sin servidor).
  - La URL completa de un archivo SOLO está en el detalle del post:
    {server}/data{path} — cada archivo vive en un nodo concreto
    (n1..n4.pawchive.pw) y ese nodo no sirve contenido de otros.
  - `preview_only` = solo hay miniatura (típico en posts de vídeo).
  - Vídeos y adjuntos "diferidos" quedan fuera: solo se guardan imágenes.

Creadores configurados en pawchive_creators.json (la GUI 🐾 lo edita):
    [{"service": "patreon", "id": "12345"}, ...]

Este módulo NO importa scraper.py (evita importaciones circulares):
la parte con BD vive en scraper.py (scrape_pawchive_*).
"""
import csv
import hashlib
import json
import re
import time
from pathlib import Path

import brave_cf
from config import PCH_PAUSE, PCH_MAX_RETRIES, PCH_IDX_MAX, SCRIPT_DIR
from exceptions import PawchiveApiError

API = "https://pawchive.pw/api/v1"
WEB = "https://pawchive.pw"
IMG = "https://img.pawchive.pw"   # nodo de miniaturas y archivos
CREATORS_FILE = SCRIPT_DIR / "pawchive_creators.json"

PAUSA = PCH_PAUSE           # pausa entre peticiones (educada; sin límite documentado)
RE_INTENTOS = PCH_MAX_RETRIES
PCH_IDX_MAX_ROWS = PCH_IDX_MAX
IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".gif")

RE_CREATOR = re.compile(
    r"(?:pawchive\.pw/)?(patreon|fanbox|discord)/user/(\d+)", re.I)
RE_POST = re.compile(
    r"pawchive\.pw/(patreon|fanbox|discord)/user/(\d+)/post/(\d+)", re.I)


# -------------- CREADORES (pawchive_creators.json) --------------
def load_creators() -> list[dict]:
    """Lista de creadores; vacía si el archivo no existe o es inválido."""
    try:
        datos = json.loads(CREATORS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except Exception:
        return []
    out = []
    for c in datos if isinstance(datos, list) else []:
        if not isinstance(c, dict):
            continue
        svc = str(c.get("service", "")).lower().strip()
        cid = str(c.get("id", "")).strip()
        if svc and cid:
            out.append({"service": svc, "id": cid})
    return out


def save_creators(creators) -> None:
    """Guarda la lista (sin duplicados service+id)."""
    vistos: set = set()
    limpio = []
    for c in creators:
        if not isinstance(c, dict):
            continue
        svc = str(c.get("service", "")).lower().strip()
        cid = str(c.get("id", "")).strip()
        if not svc or not cid or (svc, cid) in vistos:
            continue
        vistos.add((svc, cid))
        limpio.append({"service": svc, "id": cid})
    CREATORS_FILE.write_text(
        json.dumps(limpio, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_creator_url(url: str) -> dict | None:
    """Acepta 'https://pawchive.pw/patreon/user/12345' (y variantes)."""
    m = RE_CREATOR.search(url or "")
    if not m:
        return None
    return {"service": m.group(1).lower(), "id": m.group(2)}


def creator_url(service: str, cid: str) -> str:
    return f"{WEB}/{service}/user/{cid}"


def post_url(service: str, cid: str, post_id: str) -> str:
    return f"{WEB}/{service}/user/{cid}/post/{post_id}"


# -------------- HTTP (JSON con pausa + reintentos) --------------
def fetch_json(rel: str, intentos: int = RE_INTENTOS):
    """GET contra la API. `rel` puede ser relativa (a API) o una URL.

    Pausa PAUSA tras cada éxito y reintenta con espera creciente ante
    errores de red/HTTP; los errores de la API ({"error": ...}) NO se
    reintentan. Lanza PawchiveApiError si sigue fallando.
    """
    url = rel if rel.startswith("http") else API + rel
    ultimo = None
    for intento in range(intentos):
        try:
            texto = brave_cf.fetch(url, timeout=40)
        except Exception as e:            # red / bloqueo / HTTP 4xx-5xx
            ultimo = e
            if intento < intentos - 1:
                time.sleep(1.5 * (intento + 1))
            continue
        try:
            datos = json.loads(texto)     # página de error HTML => reintentar
        except Exception as e:
            ultimo = e
            if intento < intentos - 1:
                time.sleep(1.5 * (intento + 1))
            continue
        time.sleep(PAUSA)
        if isinstance(datos, dict) and datos.get("error"):
            raise PawchiveApiError(datos['error'], endpoint=url)
        return datos
    raise PawchiveApiError(f"Fallo al consultar {url}: {ultimo}", endpoint=url)


def fetch_profile(service: str, cid: str) -> dict:
    d = fetch_json(f"/{service}/user/{cid}/profile")
    return d if isinstance(d, dict) else {}


def fetch_post(service: str, cid: str, post_id: str) -> dict:
    d = fetch_json(f"/{service}/user/{cid}/post/{post_id}")
    return d if isinstance(d, dict) else {}


def iter_posts(service: str, cid: str, max_posts: int | None = None):
    """Posts de un creador. `o` debe ser múltiplo de 50 (lo exige la API)."""
    o = 0
    vistos = 0
    while True:
        lote = fetch_json(f"/{service}/user/{cid}?o={o}")
        if not isinstance(lote, list) or not lote:
            return
        for post in lote:
            yield post
            vistos += 1
            if max_posts and vistos >= max_posts:
                return
        if len(lote) < 50:      # última página
            return
        o += 50


# -------------- IMÁGENES Y TAGS --------------
def is_image(name: str) -> bool:
    low = (name or "").split("?")[0].lower()
    return low.endswith(IMG_EXTS)


def count_images_list(post: dict) -> int:
    """Nº de imágenes que promete un post según el LISTADO (por nombres).

    El archivo principal solo cuenta si NO es preview_only (si lo es, es la
    miniatura de un vídeo)."""
    n = 0
    f = post.get("file") or {}
    if (not f.get("preview_only")
            and is_image(f.get("name") or f.get("path") or "")):
        n += 1
    for a in post.get("attachments") or []:
        if isinstance(a, dict) and is_image(a.get("name") or a.get("path") or ""):
            n += 1
    return n


def count_all_files_list(post: dict) -> int:
    """Nº de archivos (imagen, video o attachment) que promete un post
    según el LISTADO (por nombres). El archivo principal solo cuenta si
    NO es preview_only."""
    n = 0
    f = post.get("file") or {}
    if (not f.get("preview_only")
            and (f.get("name") or f.get("path") or "")):
        n += 1
    for a in post.get("attachments") or []:
        if isinstance(a, dict) and (a.get("name") or a.get("path") or ""):
            n += 1
    return n


def count_by_type_list(post: dict) -> dict[str, int]:
    """Cuenta archivos por tipo (imagen, video, archivo) según el LISTADO.

    El archivo principal solo cuenta si NO es preview_only.
    Devuelve: {'imagen': n, 'video': n, 'archivo': n}
    """
    counts = {"imagen": 0, "video": 0, "archivo": 0}
    f = post.get("file") or {}
    if (not f.get("preview_only")
            and (f.get("name") or f.get("path") or "")):
        counts[_classify_file(f.get("name") or f.get("path") or "")] += 1
    for a in post.get("attachments") or []:
        if isinstance(a, dict) and (a.get("name") or a.get("path") or ""):
            counts[_classify_file(a.get("name") or a.get("path") or "")] += 1
    return counts


def _classify_file(name: str = "", path: str = "") -> str:
    """Clasifica un archivo por su extensión: 'imagen', 'video' o 'archivo'."""
    VIDEO_EXTS = (".mp4", ".webm", ".mov", ".avi", ".mkv", ".flv", ".wmv")
    # Acepta name y path como argumentos separados o combinados
    source = name if name else path
    low = (source or "").split("?")[0].lower()
    if low.endswith(IMG_EXTS):
        return "imagen"
    if low.endswith(VIDEO_EXTS):
        return "video"
    return "archivo"


def image_urls(detalle: dict) -> list[str]:
    """URLs completas de las imágenes según el DETALLE del post.

    Formato: {server}/data{path} (nodo n*.pawchive.pw asignado al archivo).
    Descarta preview_only y todo lo que no sea imagen."""
    urls: list[str] = []

    def add(f):
        if not isinstance(f, dict) or f.get("preview_only"):
            return
        server, path = f.get("server"), f.get("path")
        if not server or not path:
            return
        if not is_image(f.get("name") or path):
            return
        urls.append(server.rstrip("/") + "/data" + path)

    add(detalle.get("file"))
    for a in detalle.get("attachments") or []:
        add(a)
    return urls


def all_files(detalle: dict) -> list[dict]:
    """Todos los archivos descargables del post según el DETALLE.

    Devuelve lista de dicts: {url, tipo, nombre, server, path, preview_only}
      tipo: 'imagen' | 'video' | 'archivo'
    Descarta preview_only y archivos sin server/path.
    """
    def add(f, out):
        if not isinstance(f, dict):
            return
        server, path = f.get("server"), f.get("path")
        if not server or not path:
            return
        nombre = f.get("name") or path
        out.append({
            "url": server.rstrip("/") + "/data" + path,
            "tipo": _classify_file(nombre, path),
            "nombre": nombre,
            "server": server,
            "path": path,
            "preview_only": bool(f.get("preview_only")),
        })

    out: list[dict] = []
    add(detalle.get("file"), out)
    for a in detalle.get("attachments") or []:
        add(a, out)
    return out


def post_tags(detalle: dict) -> list[tuple[str, str | None]]:
    """Tags del post -> [(nombre, slug)] como espera guardar_metadata.

    En el DETALLE el campo `tags` es un STRING estilo SET de MySQL:
        '{breanna,bridal,mari,wedding}'
        '{"League of Legends",NSFW,Qiyana,"Tier 2"}'
        None  (sin tags)
    Antes se iteraba ese string y se guardaba CADA CARÁCTER como tag;
    ahora se trocea respetando las comillas."""
    raw = detalle.get("tags")
    nombres: list = []

    def _trocear(s: str) -> list:
        """'{a,b,"c d"}' -> ['a','b','c d']; 'solo' -> ['solo']."""
        s = s.strip()
        if s.startswith("{") and s.endswith("}") and len(s) >= 2:
            s = s[1:-1].strip()
            if not s:
                return []
            try:
                return [x.strip() for x in
                        next(csv.reader([s], skipinitialspace=True))]
            except Exception:
                return [x.strip() for x in s.split(",")]
        return [s] if s else []

    if isinstance(raw, str):
        nombres = _trocear(raw)
    elif isinstance(raw, (list, tuple)):
        for t in raw:
            if isinstance(t, str):
                nombres.extend(_trocear(t))
            elif isinstance(t, dict):
                nombres.append(
                    str(t.get("tag") or t.get("name") or "").strip())

    out: list[tuple[str, str | None]] = []
    for nombre in nombres:
        if nombre:
            out.append((nombre, None))
    return out


def norm_title(titulo: str) -> str:
    """Normalización compartida con insertar_comic (scraper.py)."""
    return " ".join((titulo or "").split()).lower()


# -------------- MINIATURAS --------------

THUMB_DIR = SCRIPT_DIR / "pawchive_thumbs"


def thumb_creator_url(service: str, cid: str) -> str:
    """Avatar del creador: https://pawchive.pw/icons/{service}/{id}."""
    return f"{WEB}/icons/{service}/{cid}"


def thumb_post_url(post: dict) -> str | None:
    """URL de miniatura de un post según el LISTADO.

    Devuelve None si el post no tiene imagen (preview_only, vídeo, sin file).
    """
    f = post.get("file") or {}
    path = f.get("path") or f.get("name") or ""
    if (f.get("preview_only") or not path or not is_image(path)):
        for a in post.get("attachments") or []:
            if not isinstance(a, dict):
                continue
            p = a.get("path") or a.get("name") or ""
            if p and is_image(p) and not a.get("preview_only"):
                path = p
                break
        else:
            path = ""
    if not path:
        return None
    url = f"{IMG}/thumbnail/data{path}"
    if not url.lower().endswith(IMG_EXTS):
        url += ".png"
    return url


def fetch_thumb(url: str, timeout: int = 45) -> bytes:
    """Descarga una miniatura (sin leer la BD de Brave)."""
    return brave_cf.fetch_image(url, timeout=timeout)


def thumb_path(clave: str) -> Path:
    """Ruta del archivo caché de una miniatura (nombre seguro).

    Las claves de posts llevan versión ("p2") porque su formato cambió:
    antes se guardaban sobre un fondo gris (con bandas) y ahora se guarda
    el recorte cuadrado de la portada, que luego se recorta al tamaño de
    la tarjeta al pintarla."""
    pre = "p2:" if clave.startswith("post:") else ""
    seguro = hashlib.sha1((pre + clave).encode("utf-8")).hexdigest()
    return THUMB_DIR / f"{seguro}.png"