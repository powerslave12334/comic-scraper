# -*- coding: utf-8 -*-
r"""
Descarga masiva de las imágenes registradas por scraper.py.

Motor HTTP: curl_cffi con huella TLS de Chrome (brave_cf) -> NO abre navegador
ni necesita cookies de Brave (son opcionales; si Cloudflare rechaza, intenta
renovarlas como respaldo).

- Guarda en: DESTINO/<titulo_saneado>/001.jpg, 002.jpg, ...
  (vercomicsporno va en la RAÍZ de DESTINO — la colección actual ya está así;
   vermangasporno y demás sitios van en DESTINO/<sitio>/<titulo>)
- Omite archivos ya descargados (se puede reanudar en cualquier momento).
- **Optimización automática**: al terminar, las imágenes nuevas se
  re-codifican a q82 (sin pérdida visual, sin EXIF, conserva el ICC) →
  ~30% menos peso, mismo nombre de archivo. `--no-optimize` la desactiva;
  `--quality N` cambia la calidad.

Uso:
    python download.py                            # preguntará la carpeta destino
    python download.py --dest "D:\MiColeccion"    # destino directo, sin preguntar
    python download.py --dest "E:\comics" --workers 8
    python download.py --dest "C:\Comics" --no-prompt   # usa el destino sin preguntar
"""
import argparse
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import brave_cf
from config import (
    DEST_DIR, DOWNLOAD_WORKERS, DOWNLOAD_TIMEOUT, DOWNLOAD_MAX_RETRIES,
    JPEG_QUALITY, INVALID_CHARS,
)
from db import connection
from exceptions import DestinationError

# -------------- CONFIGURACIÓN --------------
# Códigos --site -> nombre del sitio en la BD
SITE_NOMBRES = {"vcp": "vercomicsporno", "vmp": "vermangasporno", "pch": "pawchive"}

# Caracteres no válidos en nombres de archivo de Windows
INVALID = re.compile(INVALID_CHARS)

OUT_DIR = DEST_DIR  # se resuelve en main() según --dest / pregunta


def safe_name(name: str) -> str:
    """Limpia el título para usarlo como nombre de carpeta."""
    clean = INVALID.sub("_", name).strip(" .")
    # strip DESPUÉS de truncar: si el corte deja espacio/punto final,
    # Windows guarda la carpeta con otro nombre y luego no se encuentra
    return clean[:120].strip(" .") or "sin_titulo"


def carpeta_comic(sitio: str, titulo: str) -> Path:
    """Carpeta de un cómic según su sitio.

    vercomicsporno  -> DESTINO/<titulo>          (compatibilidad con la
                       colección actual de ~318k archivos, no se mueve nada)
    resto de sitios -> DESTINO/<sitio>/<titulo>   (p. ej. vermangasporno/)
    """
    base = OUT_DIR
    if sitio and sitio != "vercomicsporno":
        base = OUT_DIR / safe_name(sitio)
    return base / safe_name(titulo)


def esperado_de_fila(numero: int, url_img: str) -> str:
    """Nombre del archivo que escribiría download() para esta página."""
    ext = ".jpg"
    low = url_img.split("?")[0].lower()
    for e in (".png", ".webp", ".gif", ".avif"):
        if low.endswith(e):
            ext = e
            break
    return f"{numero:03d}{ext}"


def list_files(folder: Path):
    """Archivos con tamaño >0 de la carpeta, tolerando espacios finales
    (Windows recorta los nombres al guardar). None si no se puede leer."""
    for cand in (folder, Path(str(folder).rstrip(" ."))):
        try:
            return {f.name for f in cand.iterdir()
                    if f.is_file() and f.stat().st_size > 0}
        except OSError:
            continue
    return None


def download(row, sess):
    _, comic_id, numero, url_img, _tipo, titulo, sitio = row
    folder = carpeta_comic(sitio, titulo)
    folder.mkdir(parents=True, exist_ok=True)

    # Mantener la extensión original si es posible
    file_path = folder / esperado_de_fila(numero, url_img)
    if file_path.exists() and file_path.stat().st_size > 0:
        return ("skip", file_path)

    ref = {"Referer": f"{brave_cf.origin_of(url_img)}/"}
    max_retries = DOWNLOAD_MAX_RETRIES
    base_delay = 2.0  # segundos

    for attempt in range(max_retries):
        try:
            r = sess.get(url_img, timeout=DOWNLOAD_TIMEOUT, headers=ref)
            if r.status_code == 200:
                data = r.content
                if len(data) < 1000:      # probablemente una página de error
                    return ("small", file_path)
                with open(file_path, "wb") as f:
                    f.write(data)
                return ("ok", file_path)
            elif r.status_code in (401, 403):
                # Cloudflare rechazó: renueva cookies desde Brave y reintenta
                cookies, ua, _ = brave_cf.get_cookies(
                    force_refresh=True, domain=brave_cf.domain_of(url_img))
                sess = brave_cf.make_session(cookies, ua)
                continue  # reintenta inmediatamente con cookies nuevas
            elif r.status_code == 429:
                # Rate limiting: exponential backoff
                delay = base_delay * (2 ** attempt)  # 2s, 4s, 8s, 16s, 32s
                time.sleep(delay)
                continue
            elif r.status_code in (500, 502, 503, 504):
                # Errores de servidor: reintentar con backoff
                delay = base_delay * (2 ** attempt)
                time.sleep(delay)
                continue
            else:
                # Otros códigos: no reintentar
                return (f"error: HTTP {r.status_code}", file_path)
        except Exception as e:
            # Error de red/timeout: reintentar con backoff
            if attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                time.sleep(delay)
                continue
            return (f"error: {e}", file_path)

    return (f"error: max retries ({max_retries}) exceeded", file_path)


def ask_destination(args) -> Path:
    """Decide dónde descargar: --dest > pregunta por teclado > por defecto."""
    if args.dest:
        return Path(args.dest).expanduser()
    if args.no_prompt or not sys.stdin.isatty():
        return DEST_DIR
    try:
        resp = input(f'Carpeta destino [Enter = "{DEST_DIR}"]: ').strip().strip('"')
    except EOFError:
        return DEST_DIR
    return Path(resp).expanduser() if resp else DEST_DIR


def main():
    global OUT_DIR
    parser = argparse.ArgumentParser(description="Descarga masiva de cómics")
    parser.add_argument("--workers", type=int, default=DOWNLOAD_WORKERS, help="Hilos de descarga")
    parser.add_argument("--dest", "-d", default=None,
                        help='Carpeta destino, ej. --dest "D:\\Comics"')
    parser.add_argument("--no-prompt", action="store_true",
                        help="No preguntar por teclado (usa --dest o el valor por defecto)")
    parser.add_argument("--site", choices=("vcp", "vmp", "pch", "all"),
                        default="all",
                        help="Descargar solo un sitio: vcp=vercomicsporno, "
                             "vmp=vermangasporno, pch=pawchive, "
                             "all=todos (por defecto)")
    parser.add_argument("--types", default="imagen,video,archivo",
                        help="Tipos de archivo a descargar: imagen,video,"
                             "archivo (por defecto: todos)")
    parser.add_argument("--no-optimize", action="store_true",
                        help="No optimizar (re-codificar) las imágenes nuevas")
    parser.add_argument("--quality", type=int, default=JPEG_QUALITY,
                        help="Calidad JPEG de la optimización (por defecto 82)")
    args = parser.parse_args()

    OUT_DIR = ask_destination(args)
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"[X] No se pudo crear la carpeta destino: {OUT_DIR}")
        print(f"    {e}")
        raise DestinationError(str(e)) from e
    print(f'Carpeta destino: "{OUT_DIR}"')
    print("Motor: curl_cffi con huella TLS de Chrome (sin navegador; Brave puede estar abierto)")

    tipos = [t.strip() for t in args.types.split(",") if t.strip()]
    validos = {"imagen", "video", "archivo"}
    tipos = [t for t in tipos if t in validos] or ["imagen"]
    ph = ",".join(["%s"] * len(tipos))
    sql = (f"SELECT pag.id, pag.comic_id, pag.numero, pag.url_imagen, "
           f"pag.tipo, c.titulo, c.sitio "
           f"FROM paginas pag JOIN comics c ON pag.comic_id = c.id "
           f"WHERE pag.tipo IN ({ph})")
    params: list = list(tipos)
    if args.site in SITE_NOMBRES:
        sql += " AND c.sitio = %s"
        params.append(SITE_NOMBRES[args.site])
    sql += " ORDER BY pag.comic_id, pag.numero"

    rows = []
    with connection() as conn:
        with conn.cursor(buffered=True) as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()

    if args.site in SITE_NOMBRES:
        print(f"Sitio: {SITE_NOMBRES[args.site]}")
    print(f"Tipos: {','.join(tipos)}")
    print(f"Descargando {len(rows)} páginas con {args.workers} hilos...")

    stats = {"ok": 0, "skip": 0, "small": 0, "error": 0}
    lock = threading.Lock()
    nuevos: list[Path] = []        # archivos recién escritos (a optimizar)

    # Una sesión por hilo (conexión reutilizada y mejor rendimiento)
    local = threading.local()

    def worker(row):
        if not hasattr(local, "s"):
            local.s = brave_cf.make_session()   # sesión curl_cffi con cookies de Brave
        status, path = download(row, local.s)
        msg = None
        if status == "ok":
            key = "ok"
            msg = f"[OK] {path}"
        elif status == "skip":
            key = "skip"
        elif status == "small":
            key = "small"
        else:
            key = "error"
            msg = f"[ERROR] {path.name}: {status}"
        with lock:
            stats[key] += 1
            if key == "ok":
                nuevos.append(path)
            done = stats["ok"] + stats["skip"] + stats["small"] + stats["error"]
        if key == "error":
            print(msg)
        elif done % 100 == 0:
            print(f"--- progreso: {done}/{len(rows)} ---")

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        list(ex.map(worker, rows))

    print("\n===== RESUMEN =====")
    print(f"Descargadas nuevas : {stats['ok']}")
    print(f"Ya existían        : {stats['skip']}")
    print(f"Respuesta inválida : {stats['small']}")
    print(f"Errores            : {stats['error']}")
    print(f"Carpeta destino    : {OUT_DIR}")

    # --- Optimización de imágenes nuevas (mismo nombre; q82 por defecto) ---
    if not args.no_optimize and nuevos:
        import optimize as imgopt
        print(f"\n--- Optimización de imágenes nuevas (q{args.quality}) ---")
        ost = imgopt.optimize_paths(nuevos, OUT_DIR, quality=args.quality,
                                    workers=args.workers)
        imgopt.report(ost, dry_run=False)

    # --- Estado en BD: 'descargado' SOLO si TODAS las imágenes están en disco ---
    esperados: dict[int, set[str]] = {}
    meta: dict[int, tuple[str, str]] = {}
    for _pid, comic_id, numero, url_img, _tipo, titulo, sitio in rows:
        esperados.setdefault(comic_id, set()).add(esperado_de_fila(numero, url_img))
        meta[comic_id] = (titulo, sitio)

    n_ok = n_pend = 0
    with connection() as conn:
        with conn.cursor() as cursor:
            for comic_id, (titulo, sitio) in meta.items():
                files = list_files(carpeta_comic(sitio, titulo))
                completo = bool(files) and not (esperados[comic_id] - files)
                if completo:
                    cursor.execute(
                        "UPDATE comics SET estado='descargado', "
                        "fecha_descarga=COALESCE(fecha_descarga, CURDATE()) "
                        "WHERE id=%s AND estado <> 'duplicado'", (comic_id,))
                    n_ok += 1
                else:
                    cursor.execute(
                        "UPDATE comics SET estado='pendiente', fecha_descarga=NULL "
                        "WHERE id=%s AND estado <> 'duplicado'", (comic_id,))
                    n_pend += 1
            conn.commit()
    print(f"Estado en BD (según disco): {n_ok} completos -> descargado | "
          f"{n_pend} incompletos -> pendiente")


if __name__ == "__main__":
    main()