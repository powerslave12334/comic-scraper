# -*- coding: utf-8 -*-
"""
Acceso HTTP directo a vercomicsporno.com y vermangasporno.com (y sus CDNs)
sin abrir navegador.

- Lee las cookies (cf_clearance) de Brave automáticamente (Brave debe estar
  cerrado cuando se ejecuta el scraper por primera vez).
- Las guarda en caché (cf_cache.json) para reutilizarlas mientras sigan vigentes.
- Usa curl_cffi para imitar el cifrado TLS/HTTP2 de Chrome (sin esto,
  Cloudflare rechaza la petición aunque la cookie sea válida).

Uso:
    from brave_cf import get_session, fetch, CfBlocked
    texto = fetch("https://vercomicsporno.com/sex-comics")
"""
import ctypes
import ctypes.wintypes
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import urlparse

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from curl_cffi import requests as creq

# -------------- CONFIGURACIÓN --------------
DOMAIN = "vercomicsporno.com"
BASE_URL = f"https://{DOMAIN}"
SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_FILE = SCRIPT_DIR / "cf_cache.json"


def origin_of(url: str) -> str:
    """'https://host/ruta' -> 'https://host' (origen para el cabecero Referer)."""
    p = urlparse(url)
    return f"{p.scheme}://{p.netloc}"


def domain_of(url: str) -> str:
    """Dominio del sitio de `url` sin 'www.' (para buscar cookies en Brave)."""
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host

BRAVE_USER_DATA = (Path(os.environ.get("LOCALAPPDATA", "")) /
                   "BraveSoftware" / "Brave-Browser" / "User Data")

# UA de respaldo; lo correcto es el de Brave (se deriva de su versión)
FALLBACK_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36")

# Edad máxima de la caché (la cookie real caducará antes si Cloudflare lo decide)
CACHE_MAX_AGE = 6 * 3600

# Marcas de que Cloudflare sigue bloqueando
CHALLENGE_MARKERS = (
    "un momento", "just a moment", "verificación de seguridad",
    "verify you are human", "cf-chl-", "_cf_chl_opt", "turnstile",
)


class CfBlocked(Exception):
    """Cloudflare sigue bloqueando: hay que renovar cookies o usar el navegador."""


# -------------- USER-AGENT DE BRAVE --------------
def brave_ua() -> str:
    """Construye el User-Agent exacto que usaría Brave (Chrome/<mayor>)."""
    try:
        ver = (BRAVE_USER_DATA / "Last Version").read_text(encoding="utf-8").strip()
        major = ver.split(".")[0]
        return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36")
    except Exception:
        return FALLBACK_UA


# -------------- COOKIES --------------
def _cookies_db_paths() -> list[Path]:
    """Busca los archivos 'Network/Cookies' de todos los perfiles de Brave."""
    out = []
    try:
        for profile in BRAVE_USER_DATA.iterdir():
            db = profile / "Network" / "Cookies"
            if db.is_file():
                out.append(db)
    except Exception:
        pass
    return out


def dpapi(data: bytes) -> bytes:
    """Descifra un blob con la API de Windows (DPAPI, no requiere admin)."""
    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(data, len(data))
    inb = DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    outb = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(inb), None, None, None, None, 0, ctypes.byref(outb))
    if not ok:
        raise OSError("CryptUnprotectData devolvió error")
    return ctypes.string_at(outb.pbData, outb.cbData)


def _master_key() -> bytes:
    """Clave de cifrado de Brave (Local State -> os_crypt.encrypted_key)."""
    import base64
    state = json.loads((BRAVE_USER_DATA / "Local State").read_text(encoding="utf-8"))
    blob = base64.b64decode(state["os_crypt"]["encrypted_key"])
    if blob[:5] == b"DPAPI":
        blob = blob[5:]
    return dpapi(blob)


def _decrypt_value(enc: bytes, master: bytes) -> str | None:
    """Descifra un valor de cookie (v10/v11/v20 o DPAPI puro)."""
    if not enc:
        return None
    pref = enc[:3]
    if pref in (b"v10", b"v11", b"v20") and len(enc) > 15:
        try:
            from Cryptodome.Cipher import AES
            nonce, ct = enc[3:15], enc[15:]
            plain = AES.new(master, AES.MODE_GCM, nonce=nonce).decrypt(ct)
            return plain[:-16].decode("utf-8", "replace")
        except Exception:
            pass
    try:                                  # versiones antiguas: DPAPI directo
        return dpapi(enc).decode("utf-8", "replace")
    except Exception:
        return None


def cookies_from_brave(domain: str = DOMAIN) -> dict | None:
    """Lee y descifra las cookies de Brave SIN permisos de admin.

    Requiere Brave CERRADO (si está abierto, Windows bloquea la BD).
    Si el método directo falla, prueba browser_cookie3 (que puede requerir
    permisos de administrador).

    domain: dominio cuyas cookies se buscan (por defecto el sitio principal)."""
    dbs = _cookies_db_paths()
    if not dbs:
        print("   [cookies] No se encontró la BD de cookies de Brave.")
        return None

    # --- Método 1: copia directa + descifrado propio (sin admin) ---
    tmp = Path(tempfile.gettempdir()) / "brave_cookies_ro.db"
    for src in dbs:
        try:
            shutil.copy2(src, tmp)
        except PermissionError:
            print("   [cookies] BD de Brave bloqueada (Brave abierto); "
                  "se sigue sin cookies (no suelen hacer falta).")
            continue
        except Exception as e:
            print(f"   [cookies] Error copiando: {e}")
            continue
        try:
            master = _master_key()
            con = sqlite3.connect(tmp)
            rows = con.execute(
                "SELECT name, host_key, encrypted_value, value FROM cookies "
                "WHERE host_key LIKE ?", (f"%{domain}%",)
            ).fetchall()
            con.close()
            out = {}
            for name, host, enc, val in rows:
                if val:
                    out[name] = val
                else:
                    dec = _decrypt_value(enc, master)
                    if dec is not None:
                        out[name] = dec
            if out:
                print(f"   [cookies] {len(out)} cookies leídas de {src.parent.parent.name}")
                return out
        except Exception as e:
            print(f"   [cookies] Error descifrando: {e}")
    try:
        tmp.unlink(missing_ok=True)
    except Exception:
        pass

    # --- Método 2: browser_cookie3 (puede pedir admin si Brave está abierto) ---
    try:
        import browser_cookie3
        jar = browser_cookie3.brave(domain_name=domain)
        out = {c.name: c.value for c in jar if c.name}
        if out:
            return out
    except Exception as e:
        print(f"   [cookies] browser_cookie3: {e}")

    return None


def save_cache(cookies: dict, ua: str, source: str,
               domain: str = DOMAIN) -> None:
    try:
        CACHE_FILE.write_text(
            json.dumps({"cookies": cookies, "ua": ua, "source": source,
                        "domain": domain, "ts": time.time()}, ensure_ascii=False),
            encoding="utf-8")
    except Exception:
        pass


def load_cache(domain: str = DOMAIN) -> dict | None:
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if data.get("domain", DOMAIN) != domain:
            return None                      # caché de otro sitio
        if time.time() - data.get("ts", 0) > CACHE_MAX_AGE:
            return None
        if not data.get("cookies"):
            return None
        return data
    except Exception:
        return None


def get_cookies(force_refresh: bool = False,
                allow_brave: bool = True,
                domain: str = DOMAIN) -> tuple[dict, str, str]:
    """Devuelve (cookies, user_agent, fuente).

    IMPORTANTE: las cookies son SOLO un respaldo. La mayoría de peticiones
    funcionan sin ellas, porque lo que Cloudflare comprueba sobre todo es la
    huella TLS (que curl_cffi replica). Por eso por defecto NO se lee la BD de
    Brave y puedes dejar Brave abierto con normalidad.

    - allow_brave=False: nunca toca la BD de Brave (solo caché).
    - force_refresh: ignora la caché y lee Brave (requiere Brave cerrado).
    - domain: dominio del sitio (caché y cookies propias de cada sitio)."""
    ua = brave_ua()

    if not force_refresh:
        cached = load_cache(domain)
        if cached:
            return cached["cookies"], cached.get("ua", ua), "cache"

    if not allow_brave:
        return {}, ua, "sin-cookie"

    cookies = cookies_from_brave(domain)
    if cookies:
        save_cache(cookies, ua, "brave", domain)
        return cookies, ua, "brave"

    cached = load_cache(domain)   # Brave no disponible: usa caché aunque esté vieja
    if cached:
        return cached["cookies"], cached.get("ua", ua), "cache-vieja"

    return {}, ua, "sin-cookie"


# -------------- SESIÓN Y PETICIONES --------------
def make_session(cookies: dict | None = None, ua: str | None = None) -> creq.Session:
    """Sesión HTTP con huella TLS de Chrome (impersonate).

    Por defecto NO lee la BD de Brave (solo caché si existe): las cookies son
    opcionales, la huella TLS es lo que evita el bloqueo."""
    if cookies is None or ua is None:
        cookies, ua, _ = get_cookies(allow_brave=False)
    s = creq.Session(impersonate="chrome")
    if cookies:
        s.cookies.update(cookies)
    s.headers.update({
        "User-Agent": ua,
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                   "image/avif,image/webp,*/*;q=0.8"),
        "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        "Referer": f"{BASE_URL}/",
    })
    return s


def looks_blocked(text: str, status: int) -> bool:
    if status in (401, 403, 429, 503):
        return True
    low = text[:8000].lower()
    return any(m in low for m in CHALLENGE_MARKERS)


def fetch(url: str, timeout: int = 40, force_refresh: bool = False) -> str:
    """Descarga una página. Lanza CfBlocked si Cloudflare sigue defendiéndose.

    Estrategia:
      1) Petición normal (sin tocar la BD de Brave) -> suele bastar.
      2) Si Cloudflare bloquea: leer cookies de Brave (solo si está cerrado)
         y reintentar una vez."""
    ua = brave_ua()
    dom = domain_of(url)
    ref = {"Referer": f"{origin_of(url)}/"}   # Referer del propio sitio de la URL

    def _do(ck: dict, u: str) -> str:
        s = make_session(ck, u)
        r = s.get(url, timeout=timeout, headers=ref)
        if looks_blocked(r.text, r.status_code):
            raise CfBlocked(f"HTTP {r.status_code} en {url}")
        return r.text

    cookies, u1, _ = get_cookies(force_refresh=force_refresh,
                                 allow_brave=force_refresh, domain=dom)
    try:
        return _do(cookies, u1)
    except CfBlocked:
        pass

    # Respaldo: intenta con cookies de Brave (requiere Brave cerrado)
    cookies2, u2, fuente = get_cookies(force_refresh=True, domain=dom)
    if cookies2:
        return _do(cookies2, u2)
    raise CfBlocked(f"Sigue bloqueado en {url} (cookies disponibles: {fuente})")


def fetch_image(url: str, timeout: int = 45) -> bytes:
    """Descarga una imagen (sin leer la BD de Brave)."""
    cookies, ua, _ = get_cookies(allow_brave=False)
    s = make_session(cookies, ua)
    r = s.get(url, timeout=timeout, headers={"Referer": f"{origin_of(url)}/"})
    if r.status_code != 200:
        raise CfBlocked(f"HTTP {r.status_code} en imagen")
    return r.content


if __name__ == "__main__":
    # Prueba rápida: python brave_cf.py  (NO requiere Brave cerrado)
    ck, ua, src = get_cookies(allow_brave=False)
    print(f"cookies en cache: {len(ck)} (fuente: {src}) - Brave NO se toca")
    print(f"ua: {ua}")
    html = fetch(f"{BASE_URL}/sex-comics")
    print(f"Pagina descargada: {len(html)} bytes")
    print("Bloqueado:", looks_blocked(html, 200))
    from bs4 import BeautifulSoup
    cards = BeautifulSoup(html, "html.parser").select("article.pp-card")
    print(f"Tarjetas del catalogo: {len(cards)}")
