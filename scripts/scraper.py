# -*- coding: utf-8 -*-
"""
Scraper multi-sitio: vercomicsporno.com (vcp), vermangasporno.com (vmp)
y Pawchive API (pch). Los dos primeros usan el mismo tema WordPress "pp"
-> mismos selectores; Pawchive usa su API JSON (pawchive_api.py).

- HTTP directo con huella TLS de Chrome (curl_cffi): sin navegador, sin cookies
  obligatorias (respaldo opcional con Playwright + Chrome real).
- Extrae catálogo (títulos + URL), imágenes de cada cómic y metadatos
  (pp-pagecount y pp-taglist) -> MySQL (XAMPP) -> base de datos mis_comics.
- Pawchive: lista de posts por creador (pawchive_creators.json), solo los
  que tienen imágenes; las URLs completas se resuelven desde el detalle.
- Duplicados entre sitios: si el título ya existe en el otro sitio, se guarda
  con estado='duplicado' y no se le extraen imágenes.

Uso:
    python scraper.py                    # vcp, todo (por defecto)
    python scraper.py --site vmp         # solo vermangasporno
    python scraper.py --site all         # los dos sitios WordPress, uno tras otro
    python scraper.py --site pch         # solo Pawchive (catálogo + páginas)
    python scraper.py --limit 3          # 3 páginas de catálogo / 3 posts por creador
    python scraper.py --skip-catalog     # solo pendientes/sin metadatos
    python scraper.py --headed           # navegador visible (captcha manual)
"""
import argparse
import asyncio
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

# Consola de Windows: forzar UTF-8 para poder imprimir emojis
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import mysql.connector
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

import brave_cf
import pawchive_api

# -------------- CONFIGURACIÓN --------------
DB = {
    "user": "root",
    "password": "",       # XAMPP por defecto sin contraseña
    "host": "127.0.0.1",
    "database": "mis_comics",
}
# Sitios compatibles (ambos usan el mismo tema WordPress "pp")
SITES = {
    "vcp": {
        "nombre": "vercomicsporno",
        "base": "https://vercomicsporno.com",
        "catalogo": "https://vercomicsporno.com/sex-comics",
    },
    "vmp": {
        "nombre": "vermangasporno",
        "base": "https://vermangasporno.com",
        "catalogo": "https://vermangasporno.com/manga-hentai-xxx",
    },
    # Pawchive: API JSON (pawchive_api.py); base/catalogo solo para el banner
    "pch": {
        "nombre": "pawchive",
        "base": "https://pawchive.pw",
        "catalogo": "https://pawchive.pw/api/v1",
    },
}
SITE_KEY = "vcp"
SITE = SITES[SITE_KEY]
BASE_URL = SITE["base"]         # se recalcula con set_site()
CATALOG_URL = SITE["catalogo"]  # idem


def set_site(key: str) -> None:
    """Activa un sitio: actualiza BASE_URL/CATALOG_URL (y SITE) globales."""
    global SITE_KEY, SITE, BASE_URL, CATALOG_URL
    SITE_KEY = key
    SITE = SITES[key]
    BASE_URL = SITE["base"]
    CATALOG_URL = SITE["catalogo"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")


def get_db():
    return mysql.connector.connect(**DB)


# -------------- NAVEGACIÓN CON ESPERA DE CLOUDFLARE --------------
async def click_turnstile(page) -> bool:
    """Hace clic en el checkbox 'Verifique que es un ser humano' (Turnstile).
    Busca cualquier iframe visible (el src puede variar entre versiones)."""
    try:
        iframes = page.locator("iframe")
        n = await iframes.count()
        if n == 0:
            return False
        clicked = False
        for i in range(n):
            fr = iframes.nth(i)
            box = await fr.bounding_box()
            if not box or box["width"] < 50 or box["height"] < 40:
                continue
            src = (await fr.get_attribute("src")) or ""
            if "cloudflare" not in src and "challenges" not in src and src.startswith(("about:", "")):
                # iframe sin src útil: igualmente se intenta si está en la zona del reto
                pass
            await page.mouse.click(box["x"] + 30, box["y"] + box["height"] / 2)
            print(f"   [clic] iframe #{i} src={src[:60]}")
            clicked = True
            break
        return clicked
    except Exception as e:
        print(f"   [clic-error] {e}")
        return False


async def goto(page, url, selector, timeout_s=90):
    """Navega a `url` y espera hasta que aparezca `selector`.
    Si Cloudflare muestra el checkbox de Turnstile, hace clic en él."""
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    except PWTimeout:
        print("   [!] Timeout en la carga, reintento...")
        try:
            await page.reload(wait_until="domcontentloaded", timeout=60000)
        except PWTimeout:
            return False

    # Bucle de espera: resolver el reto (clic en Turnstile) y esperar contenido
    deadline = time.time() + timeout_s
    state = {"challenge_seen": False, "clicked": False}
    while time.time() < deadline:
        try:
            await page.wait_for_selector(selector, timeout=4000, state="attached")
            return True
        except Exception:
            # PWTimeout, o navegación al resolverse el reto (contexto destruido)
            try:
                title = (await page.title()).lower()
            except Exception:
                continue
            is_challenge = "momento" in title or "moment" in title
            if not is_challenge:
                # Superado: permitir clic en un posible reto futuro
                state["clicked"] = False
                state["challenge_seen"] = False
                continue
            if not state["challenge_seen"]:
                print("   [..] Reto de Cloudflare, esperando resolucion...")
                state["challenge_seen"] = True
            if not state["clicked"]:
                if await click_turnstile(page):
                    print("   [clic] Checkbox 'humano' pulsado")
                    state["clicked"] = True
            continue
    return False


# -------------- MODO HTTP (sin navegador, cookies de Brave) --------------
RE_PAGE = re.compile(r"/page/(\d+)")

# Imágenes que NO son páginas del cómic (iconos de embebidos, etc.)
JUNK_SRC = ("favicon", "google.com/s2", ".ico", "gravatar", "data:")


def clean_img_src(src) -> str | None:
    """Sanea la URL de una imagen; devuelve None si es basura o inválida."""
    if not src or not isinstance(src, str):
        return None
    src = re.sub(r"\s+", "", src)        # quita espacios/saltos de línea
    if src.lower().startswith("data:"):
        return None
    if not src.startswith("http"):
        src = urljoin(BASE_URL, src)
    low = src.lower()
    if any(j in low for j in JUNK_SRC):
        return None                      # favicons, iconos sueltos, etc.
    return src


def parse_metadata(soup) -> tuple[int | None, list[tuple[str, str | None]]]:
    """Lee pp-pagecount (nº de páginas que indica la web) y pp-taglist (tags)."""
    n_paginas = None
    el = soup.select_one(".pp-pagecount")
    if el:
        m = re.search(r"\d+", el.get_text())
        if m:
            n_paginas = int(m.group())

    tags: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for a in soup.select("div.pp-taglist a.pp-tag"):
        nombre = a.get_text(strip=True).lstrip("#").strip()
        if not nombre or nombre.lower() in seen:
            continue
        seen.add(nombre.lower())
        href = (a.get("href") or "").rstrip("/")
        slug = unquote(href.rsplit("/", 1)[-1]) if href else None
        tags.append((nombre, slug))
    return n_paginas, tags


def guardar_metadata(cursor, comic_id, n_paginas, tags) -> None:
    """Guarda el nº de páginas de la web y las etiquetas (relación n:m).

    n_paginas_web = NULL significa "aún no consultado"; si la web no muestra
    el dato se guarda 0."""
    cursor.execute("UPDATE comics SET n_paginas_web=%s WHERE id=%s",
                   (0 if n_paginas is None else n_paginas, comic_id))
    for nombre, slug in tags:
        cursor.execute("SELECT id FROM etiquetas WHERE nombre=%s", (nombre,))
        row = cursor.fetchone()
        if row:
            etiqueta_id = row[0]
        else:
            cursor.execute("INSERT INTO etiquetas (nombre, slug) VALUES (%s, %s)",
                           (nombre, slug))
            etiqueta_id = cursor.lastrowid
        cursor.execute(
            "INSERT IGNORE INTO comic_etiquetas (comic_id, etiqueta_id) VALUES (%s, %s)",
            (comic_id, etiqueta_id))


def cargar_titulos_otros(cursor, sitio_propio: str) -> dict[str, int]:
    """Títulos normalizados de los cómics de OTROS sitios (para detectar
    duplicados entre sitios: mismo título publicado en las dos webs).

    Normalización compartida con insertar_comic: minúsculas + espacios
    colapsados (MySQL LOWER(TRIM(REGEXP_REPLACE(...))) ≈ Python split/join)."""
    cursor.execute(
        "SELECT id, LOWER(TRIM(REGEXP_REPLACE(titulo, '[[:space:]]+', ' '))) "
        "FROM comics WHERE sitio <> %s",
        (sitio_propio,))
    return {norm: cid for cid, norm in cursor.fetchall() if norm}


def insertar_comic(cursor, titulo: str, url_comic: str,
                   otros_titulos: dict) -> str:
    """Inserta una tarjeta del catálogo en la BD.

    Devuelve:
      'nuevo'     -> insertado normalmente
      'duplicado' -> el título ya existe en el OTRO sitio: se guarda marcado
                     con estado='duplicado' y NO se le extraen imágenes
                     (no se descarga dos veces el mismo cómic)
      'existe'    -> la URL ya estaba registrada (re-sincronización)
    """
    norm = " ".join(titulo.split()).lower()
    if norm in otros_titulos:
        cursor.execute(
            "INSERT IGNORE INTO comics (titulo, url_comic, sitio, estado) "
            "VALUES (%s, %s, %s, 'duplicado')",
            (titulo, url_comic, SITE["nombre"]))
        return "duplicado" if cursor.rowcount else "existe"
    cursor.execute(
        "INSERT IGNORE INTO comics (titulo, url_comic, sitio) VALUES (%s, %s, %s)",
        (titulo, url_comic, SITE["nombre"]))
    return "nuevo" if cursor.rowcount else "existe"


def detect_last_page(soup) -> int | None:
    """Última página del catálogo a partir de la paginación."""
    cat_path = urlparse(CATALOG_URL).path.rstrip("/")
    nums = []
    for a in soup.select('a[href*="/page/"]'):
        href = a.get("href", "")
        if cat_path not in href:
            continue
        m = RE_PAGE.search(href)
        if m:
            nums.append(int(m.group(1)))
    return max(nums) if nums else None


def scrape_catalog_http(db, max_pages=None):
    """Catálogo vía HTTP directo (curl_cffi + cookies de Brave)."""
    cursor = db.cursor(buffered=True)
    otros = cargar_titulos_otros(cursor, SITE["nombre"])
    if otros:
        print(f"[i] {len(otros)} titulos en otros sitios para detectar duplicados.")
    page_num = 1
    total_new = 0
    last_page = None

    while True:
        url = CATALOG_URL if page_num == 1 else f"{CATALOG_URL}/page/{page_num}"
        print(f"[HTTP] Pagina de catalogo {page_num}: {url}")
        soup = BeautifulSoup(brave_cf.fetch(url), "html.parser")

        cards = soup.select("article.pp-card")
        if not cards:
            print("   [X] Sin tarjetas (pp-card). Fin del catalogo.")
            break

        new_count = 0
        dup_count = 0
        for card in cards:
            a = card.select_one("a.pp-card-link")
            h3 = card.select_one("h3.pp-card-title")
            if not a or not h3:
                continue
            href = a.get("href", "")
            titulo = h3.get_text(strip=True)
            if not href or not titulo:
                continue
            url_comic = href if href.startswith("http") else urljoin(BASE_URL, href)
            res = insertar_comic(cursor, titulo, url_comic, otros)
            if res == "nuevo":
                new_count += 1
            elif res == "duplicado":
                dup_count += 1
                print(f"   [!] Duplicado (ya en el otro sitio): {titulo[:60]}")

        db.commit()
        total_new += new_count
        print(f"   [OK] {len(cards)} tarjetas | {new_count} nuevas | "
              f"{dup_count} duplicados")

        if page_num == 1:
            last_page = detect_last_page(soup)
            if last_page:
                print(f"   [i] Ultima pagina del catalogo: {last_page}")

        if max_pages and page_num >= max_pages:
            print("   [STOP] Limite --limit alcanzado.")
            break
        if last_page and page_num >= last_page:
            break
        if len(cards) == 0:
            break

        page_num += 1
        time.sleep(random.uniform(0.4, 1.0))

    cursor.close()
    return total_new


def scrape_comics_http(db):
    """Extrae imágenes y metadatos (páginas web + tags) de cada cómic.

    Procesa los pendientes y también los que aún no tienen n_paginas_web
    (backfill de metadatos). Los duplicados entre sitios se excluyen."""
    cursor = db.cursor(buffered=True)
    cursor.execute("SELECT id, titulo, url_comic FROM comics "
                   "WHERE sitio = %s AND estado <> 'duplicado' "
                   "AND (estado = 'pendiente' OR n_paginas_web IS NULL)",
                   (SITE["nombre"],))
    pendientes = cursor.fetchall()
    print(f"\n[HTTP] {len(pendientes)} comics de '{SITE['nombre']}' por "
          "procesar (imagenes y/o metadatos).")

    for comic_id, titulo, url_comic in pendientes:
        print(f"[HTTP] [{comic_id}] {titulo[:55]}")
        try:
            soup = BeautifulSoup(brave_cf.fetch(url_comic), "html.parser")
        except brave_cf.CfBlocked as e:
            print(f"   [X] Bloqueado: {e}")
            cursor.close()
            raise

        srcs = []
        for img in soup.select("div.pp-comic-content img"):
            src = clean_img_src(img.get("src") or img.get("data-src"))
            if src:
                srcs.append(src)

        count = 0
        for idx, src in enumerate(srcs, start=1):
            cursor.execute(
                "INSERT IGNORE INTO paginas (comic_id, numero, url_imagen) VALUES (%s, %s, %s)",
                (comic_id, idx, src),
            )
            count += cursor.rowcount

        n_paginas, tags = parse_metadata(soup)
        guardar_metadata(cursor, comic_id, n_paginas, tags)

        # NOTA: el estado NO se cambia aquí. 'descargado' solo lo ponen
        # download.py (al confirmar que TODAS las imágenes están en disco)
        # o verify_dl.py --sync-estado; extraer URLs deja el cómic 'pendiente'.
        db.commit()
        print(f"   [OK] {count} imagenes | {n_paginas or '?'} paginas web "
              f"| {len(tags)} tags")
        time.sleep(random.uniform(0.3, 0.8))

    cursor.close()


# -------------- PAWCHIVE (API JSON, sin HTML ni navegador) --------------
def scrape_pawchive(db, args):
    """Flujo Pawchive: catálogo por creadores + páginas/metadatos.

    Los creadores vienen de pawchive_creators.json (la GUI 🐾 lo edita)."""
    creadores = pawchive_api.load_creators()
    if not creadores:
        print("[i] Sin creadores de Pawchive configurados.")
        print("    Pulsa el boton Creadores Pawchive en la GUI")
        print("    (o crea pawchive_creators.json).")
        return
    print(f"[i] {len(creadores)} creador(es) configurados "
          "(API JSON: el modo --mode no aplica).")
    if not args.skip_catalog:
        scrape_pawchive_catalog(db, creadores, max_posts=args.limit)
    scrape_pawchive_pages(db)


def scrape_pawchive_catalog(db, creadores, max_posts=None):
    """Lista los posts de cada creador e inserta los que tienen imágenes.

    - Título: "Creador — Título" (evita colisiones de carpeta entre
      creadores); si aun así repite título dentro de pawchive, se añade
      el sufijo " #id_post".
    - url_comic única -> la re-sincronización es idempotente (INSERT IGNORE).
    - n_paginas_web queda NULL hasta scrape_pawchive_pages (detalle real).
    - Los posts solo-vídeo (o sin imágenes) se omiten y se contabilizan."""
    cursor = db.cursor(buffered=True)
    otros = cargar_titulos_otros(cursor, SITE["nombre"])
    cursor.execute("SELECT titulo, url_comic FROM comics WHERE sitio = %s",
                   (SITE["nombre"],))
    propios = {pawchive_api.norm_title(t): u for t, u in cursor.fetchall()}

    total_c = len(creadores)
    n_new = n_exist = n_dup = n_noimg = 0
    for i, cr in enumerate(creadores, 1):
        service, cid = str(cr["service"]), str(cr["id"])
        print(f"\n--- Creador {i}/{total_c}: {service}/{cid} ---")
        try:
            perfil = pawchive_api.fetch_profile(service, cid)
            nombre = str(perfil.get("name") or cid).strip()
        except Exception as e:
            print(f"   [X] Perfil {service}/{cid}: {e}")
            continue
        print(f"   {nombre}")
        vistas = nuevos = 0
        try:
            for post in pawchive_api.iter_posts(service, cid, max_posts):
                vistas += 1
                if pawchive_api.count_all_files_list(post) == 0:
                    n_noimg += 1
                    continue
                pid = str(post.get("id") or "")
                if not pid:
                    continue
                titulo = str(post.get("title") or "").strip() or f"post {pid}"
                titulo = f"{nombre} — {titulo}"[:255]
                url = pawchive_api.post_url(service, cid, pid)
                # ¿Colisión de título dentro de pawchive? -> sufijo #id
                norm = pawchive_api.norm_title(titulo)
                dueno = propios.get(norm)
                if dueno is not None and dueno != url:
                    titulo = f"{titulo} #{pid}"[:255]
                    norm = pawchive_api.norm_title(titulo)
                res = insertar_comic(cursor, titulo, url, otros)
                if res == "nuevo":
                    propios[norm] = url
                    n_new += 1
                    nuevos += 1
                elif res == "duplicado":
                    propios[norm] = url
                    n_dup += 1
                    print(f"   [!] Duplicado (ya en el otro sitio): "
                          f"{titulo[:60]}")
                else:
                    n_exist += 1
                db.commit()
        except Exception as e:
            print(f"   [X] Catálogo {service}/{cid}: {e}")
            db.rollback()
        print(f"   [OK] {vistas} posts | {nuevos} nuevos | "
              f"omitidos sin imágenes: {n_noimg}")
        print(f"progreso: {i}/{total_c}")

    cursor.close()
    print(f"\n[FIN] Catálogo pawchive: {n_new} nuevos | {n_exist} existentes "
          f"| {n_dup} duplicados | {n_noimg} omitidos (sin imágenes)")


def scrape_pawchive_pages(db):
    """Resuelve las URLs completas de las imágenes de cada post pendiente.

    El listado no da el servidor: hay que consultar el detalle de cada post
    (n*.pawchive.pw) y montar {server}/data{path}. También guarda los tags
    del detalle (pueden venir vacíos). Todo se confirma en una sola
    transacción por cómic (si falla, se revierte y se reintenta luego)."""
    cursor = db.cursor(buffered=True)
    cursor.execute(
        "SELECT c.id, c.url_comic FROM comics c "
        "LEFT JOIN paginas p ON p.comic_id = c.id "
        "WHERE c.sitio = %s AND c.estado <> 'duplicado' "
        "AND c.n_paginas_web IS NULL AND p.id IS NULL "
        "ORDER BY c.id",
        (SITE["nombre"],))
    filas = cursor.fetchall()
    if not filas:
        print("[i] Pawchive: no hay cómics pendientes de páginas.")
        cursor.close()
        return
    print(f"\n[Pawchive] {len(filas)} comics por completar "
          "(paginas + tags).")

    ok = sin_img = fallidos = 0
    for i, (comic_id, url_comic) in enumerate(filas, 1):
        print(f"progreso: {i}/{len(filas)}")
        m = pawchive_api.RE_POST.search(url_comic or "")
        if not m:
            print(f"   [X] URL no reconocida: {url_comic}")
            fallidos += 1
            continue
        service, cid, pid = m.group(1), m.group(2), m.group(3)
        try:
            detalle = pawchive_api.fetch_post(service, cid, pid)
            archivos = pawchive_api.all_files(detalle)
            tags = pawchive_api.post_tags(detalle)
        except Exception as e:
            print(f"   [X] Post {pid}: {e}")
            fallidos += 1
            db.rollback()
            continue
        if not archivos:
            # Listado prometía archivos pero el detalle no los da (solo
            # preview/vídeo): se marca consultado (0) para no reintentar.
            print(f"   [!] Post {pid}: sin archivos descargables "
                  "(¿solo video o preview?).")
            try:
                guardar_metadata(cursor, comic_id, 0, tags)
                db.commit()
            except Exception as e:
                db.rollback()
                print(f"   [X] BD (post {pid}): {e}")
            sin_img += 1
            continue
        try:
            for n, a in enumerate(archivos, 1):
                cursor.execute(
                    "INSERT IGNORE INTO paginas "
                    "(comic_id, numero, url_imagen, tipo) "
                    "VALUES (%s, %s, %s, %s)",
                    (comic_id, n, a["url"], a["tipo"]))
            guardar_metadata(cursor, comic_id, len(archivos), tags)
            db.commit()
            n_img = sum(1 for a in archivos if a["tipo"] == "imagen")
            n_vid = sum(1 for a in archivos if a["tipo"] == "video")
            n_arr = sum(1 for a in archivos if a["tipo"] == "archivo")
            print(f"   [OK] {n_img} imagenes | {n_vid} videos | {n_arr} archivos "
                  f"| {len(tags)} tags")
        except Exception as e:
            db.rollback()
            print(f"   [X] BD (post {pid}): {e}")
            fallidos += 1

    cursor.close()
    print(f"\n[FIN] Paginas pawchive: {ok} completados | {sin_img} sin "
          f"imagenes | {fallidos} con incidencias")


# -------------- PASO 1: CATÁLOGO --------------
async def scrape_catalog(page, db, max_pages=None):
    cursor = db.cursor(buffered=True)
    otros = cargar_titulos_otros(cursor, SITE["nombre"])
    if otros:
        print(f"[i] {len(otros)} titulos en otros sitios para detectar duplicados.")
    page_num = 1
    total_new = 0

    while True:
        url = CATALOG_URL if page_num == 1 else f"{CATALOG_URL}/page/{page_num}"
        print(f"\nPagina de catalogo {page_num}: {url}")

        ok = await goto(page, url, "article.pp-card a.pp-card-link")
        if not ok:
            # Diagnóstico: qué vio el navegador exactamente
            try:
                title = await page.title()
                html = await page.content()
                print(f"   [DEBUG] titulo: {title}")
                print(f"   [DEBUG] url: {page.url}")
                print(f"   [DEBUG] html len: {len(html)}")
                dbg = Path(__file__).with_name("debug_page.html")
                dbg.write_text(html, encoding="utf-8")
                shot = Path(__file__).with_name("debug_page.png")
                await page.screenshot(path=str(shot))
                print(f"   [DEBUG] guardado: {dbg.name}, {shot.name}")
            except Exception as e:
                print(f"   [DEBUG] error: {e}")
            print("   [X] No se encontraron tarjetas (pp-card). Fin del catalogo.")
            break

        cards = await page.eval_on_selector_all(
            "article.pp-card",
            """els => els.map(a => {
                const link = a.querySelector('a.pp-card-link');
                const title = a.querySelector('h3.pp-card-title');
                return {
                    titulo: title ? title.textContent.trim() : '',
                    url: link ? link.href : ''
                };
            })"""
        )

        new_count = 0
        dup_count = 0
        for c in cards:
            if not c["titulo"] or not c["url"]:
                continue
            res = insertar_comic(cursor, c["titulo"], c["url"], otros)
            if res == "nuevo":
                new_count += 1
            elif res == "duplicado":
                dup_count += 1
                print(f"   [!] Duplicado (ya en el otro sitio): {c['titulo'][:60]}")

        db.commit()
        total_new += new_count
        print(f"   [OK] {len(cards)} tarjetas | {new_count} nuevas | "
              f"{dup_count} duplicados")

        if max_pages and page_num >= max_pages:
            print("   [STOP] Limite --limit alcanzado.")
            break
        if len(cards) == 0:
            break

        page_num += 1
        await asyncio.sleep(random.uniform(1.5, 3.0))

    cursor.close()
    return total_new


# -------------- PASO 2: IMÁGENES DE CADA CÓMIC --------------
async def scrape_comics(page, db):
    cursor = db.cursor(buffered=True)
    cursor.execute("SELECT id, titulo, url_comic FROM comics "
                   "WHERE sitio = %s AND estado <> 'duplicado' "
                   "AND (estado = 'pendiente' OR n_paginas_web IS NULL)",
                   (SITE["nombre"],))
    pendientes = cursor.fetchall()
    print(f"\n{len(pendientes)} comics de '{SITE['nombre']}' por procesar "
          "(imagenes y/o metadatos).")

    for comic_id, titulo, url_comic in pendientes:
        print(f"[{comic_id}] {titulo[:55]}")

        ok = await goto(page, url_comic, "div.pp-comic-content img")
        if not ok:
            print("   [X] Sin imagenes (.pp-comic-content). Se omite.")
            continue

        # Todas las paginas del comic cargan en una sola vista
        srcs = await page.eval_on_selector_all(
            "div.pp-comic-content img",
            "els => els.map(i => i.currentSrc || i.src).filter(Boolean)"
        )
        srcs = [u for u in (clean_img_src(s) for s in srcs) if u]

        count = 0
        for idx, src in enumerate(srcs, start=1):
            if not src.startswith("http"):
                continue
            cursor.execute(
                "INSERT IGNORE INTO paginas (comic_id, numero, url_imagen) VALUES (%s, %s, %s)",
                (comic_id, idx, src),
            )
            count += cursor.rowcount

        n_paginas, tags = parse_metadata(BeautifulSoup(await page.content(), "html.parser"))
        guardar_metadata(cursor, comic_id, n_paginas, tags)

        # El estado 'descargado' lo pone download.py / verify_dl --sync-estado
        # (solo si las imágenes están en disco); aquí nunca.
        db.commit()
        print(f"   [OK] {count} imagenes | {n_paginas or '?'} paginas web "
              f"| {len(tags)} tags")
        await asyncio.sleep(random.uniform(0.8, 1.8))

    cursor.close()


# -------------- MAIN --------------
async def run_browser(args, db):
    """Respaldo: extracción con navegador (Playwright + Chrome real)."""
    profile = str(Path(__file__).with_name(".browser-profile"))
    async with async_playwright() as p:
        # Contexto PERSISTENTE con perfil guardado: la cookie de Cloudflare
        # se conserva entre ejecuciones y no hay que repetir el reto.
        ctx = await p.chromium.launch_persistent_context(
            user_data_dir=profile,
            headless=args.headless,
            channel="chrome",          # Chrome real instalado (mejor huella que Chromium)
            viewport={"width": 1400, "height": 900},
            args=["--disable-blink-features=AutomationControlled", "--disable-gpu"],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        try:
            if not args.skip_catalog:
                nuevos = await scrape_catalog(page, db, max_pages=args.limit)
                print(f"\nTotal comics nuevos insertados: {nuevos}")
            await scrape_comics(page, db)
            print("\n[FIN] Scraping completado (modo navegador).")
        finally:
            await ctx.close()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None,
                        help="Maximo de paginas de catalogo (para pruebas)")
    parser.add_argument("--headed", action="store_true",
                        help="Mostrar el navegador (por defecto VISIBLE por Cloudflare)")
    parser.add_argument("--headless", action="store_true",
                        help="Ocultar el navegador (más rápido pero puede ser bloqueado)")
    parser.add_argument("--mode", choices=("auto", "http", "browser"),
                        default="auto",
                        help="auto: HTTP con cookies de Brave + respaldo en navegador; "
                             "http: solo HTTP (sin abrir navegador); "
                             "browser: solo Playwright")
    parser.add_argument("--skip-catalog", action="store_true",
                        help="No revisar el catalogo; solo procesar los comics "
                             "pendientes/m sin metadatos (backfill)")
    parser.add_argument("--site", choices=("vcp", "vmp", "pch", "all"),
                        default="vcp",
                        help="Sitio a sincronizar: vcp=vercomicsporno, "
                             "vmp=vermangasporno, pch=Pawchive (API), "
                             "all=los dos WordPress (uno tras otro)")
    args = parser.parse_args()

    sitios = ["vcp", "vmp"] if args.site == "all" else [args.site]

    db = get_db()
    for key in sitios:
        set_site(key)
        print(f"\n========== Sitio {SITE['nombre']} ({SITE['base']}) ==========")
        hecho_con_http = False

        # ---- Motor API: Pawchive (JSON, sin HTML ni navegador) ----
        if key == "pch":
            scrape_pawchive(db, args)
            continue

        # ---- Motor 1: HTTP directo (rápido, sin ventana, sin tocar Brave) ----
        if args.mode in ("auto", "http"):
            try:
                print("[Motor HTTP] HTTP directo con huella TLS de Chrome "
                      "(cookies de Brave opcionales, Brave puede estar abierto)")
                if args.skip_catalog:
                    print("[i] --skip-catalog: se omite la revision del catalogo.")
                else:
                    nuevos = scrape_catalog_http(db, max_pages=args.limit)
                    print(f"\nTotal comics nuevos insertados: {nuevos}")
                scrape_comics_http(db)
                print(f"\n[FIN] Scraping de {SITE['nombre']} completado "
                      "(sin navegador).")
                hecho_con_http = True
            except brave_cf.CfBlocked as e:
                if args.mode == "http":
                    print(f"[X] Cloudflare bloqueo el modo HTTP: {e}")
                    print("    -> Cierra Brave para renovar cookies o usa --mode browser.")
                else:
                    print(f"[!] HTTP bloqueado -> respaldo en modo navegador. ({e})")

        # ---- Motor 2: navegador (respaldo) ----
        if not hecho_con_http and args.mode != "http":
            await run_browser(args, db)

    db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nDetenido por el usuario.")
        sys.exit(0)
