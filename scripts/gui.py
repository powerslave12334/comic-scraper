# -*- coding: utf-8 -*-
"""
Interfaz gráfica del gestor de cómics
(vercomicsporno.com + vermangasporno.com + pawchive.pw).

Ventana LANZADOR + una ventana POR SITIO (3 ventanas posibles):

  - 🚀 Lanzador (`Launcher`): botones para abrir cada ventana de sitio
    (recuerda cuáles estaban abiertas al arrancar), recuento de cómics
    por sitio, estado del trabajo global y "Cerrar todo".
  - 📕 VerComicsPorno / 📗 VerMangasPorno / 🐾 Pawchive (`SiteWindow`):
    cada ventana tiene su propio CTkTabview con dos pestañas:
        · 📊 Dashboard: estadísticas SOLO de ese sitio, estado del
          sistema, controles agrupados, historial y registro — frame
          embebible de `dashboard.py`.
        · 📚 Colección: tabla de ese sitio, buscador y filtro de estado
          (el filtro/sitio sobra: la ventana ya es de un sitio).
    La barra de estado + progreso + ⏹ Parar queda FUERA de las pestañas
    (abajo), siempre visible.

Trabajos (subprocesos) GLOBALES: solo puede haber UN trabajo a la vez en
toda la aplicación. El módulo mantiene `ACTIVE_JOB` (dueño + nombre); la
ventana dueña ve log y progreso y puede pulsar ⏹ Parar, el resto de
ventanas muestran sus botones deshabilitados con el aviso "trabajo en
otra ventana" y recargan tabla + historial al terminar.

Uso:
    python gui.py
"""
import json
import contextlib
import os
import base64
import io
import queue
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path
from urllib.parse import quote

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import mysql.connector
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import customtkinter as ctk
from PIL import Image, ImageTk

import pawchive_api

# Directorio de assets (iconos SVG, patterns)
ASSETS_DIR = Path(__file__).resolve().parent / "assets"
ICON_DIR = ASSETS_DIR / "icons"
PATTERN_DIR = ASSETS_DIR / "patterns"

# Constantes y auxiliares compartidos (viven en config.py y dashboard.py)
from config import (
    SCRIPT_DIR, SITES, MODE_VALUES, ESTADO_FILTROS,
    COLORS, SITE_ACCENTS, DEFAULT_DEST,
    DB_CONFIG,
)
from dashboard import (
    Dashboard,
    load_config, update_config, fmt_num,
    carpeta_comic, RE_PROGRESS,
)

# Índice de cada columna del Treeview dentro de la fila de la BD.
# fila = (id, titulo, estado, sitio, n_web, paginas, url_comic)
COL_IDX = {"id": 0, "titulo": 1, "estado": 2, "sitio": 3,
           "paginas": 5, "web": 4}

# Filas pintadas de golpe (el resto se cargan al bajar el scroll)
CHUNK = 500

# -------------- PESTAÑA 🐾 EXPLORAR (solo ventana Pawchive) --------------
# Índice de artistas: la API /creators devuelve TODOS (sin filtro de
# servidor) -> se guarda en caché y se filtra en cliente.
PCH_INDEX_FILE = SCRIPT_DIR / "pawchive_index_cache.json"
PCH_IDX_MAX = 1500          # artistas máx. en la lista filtrada
PCH_ART_POR_PAGINA = 9      # tarjetas de artistas por página (3x3)
PCH_ART_COLS = 3            # columnas del grid de artistas
PCH_POST_POR_PAGINA = 9     # tarjetas de posts por página (3x3)
PCH_POST_COLS = 3           # columnas del grid de posts
PCH_POST_LOTE = 50          # posts que devuelve la API por petición (o=50)
# Páginas de tarjetas que tiene un bloque completo de 50 posts
PCH_POST_PAGS_BLOQUE = -(-PCH_POST_LOTE // PCH_POST_POR_PAGINA)   # = 6
PCH_SVC_LABELS = {"Todos": "", "Patreon": "patreon",
                  "Fanbox": "fanbox", "Discord": "discord"}

# Índice ya convertido y ordenado, en MEMORIA: se reutiliza entre ventanas
# para que abrirla sea instantáneo (evita releer el JSON de 4 MB).
_PCH_IDX_MEM: dict = {"at": None, "filas": None}


def _normalizar_idx(creadores) -> list:
    """JSON crudo del índice -> tuplas (id, nombre, servicio, fav) por fav↓."""
    filas = []
    for c in creadores:
        try:
            filas.append((str(c[0]), str(c[1]), str(c[2]), int(c[3])))
        except Exception:
            continue
    filas.sort(key=lambda r: -r[3])        # más populares primero
    return filas


# Cache de iconos: (nombre, size, modo) -> CTkImage
_ICON_CACHE: dict[tuple[str, tuple[int, int], str], ctk.CTkImage] = {}

def _load_icon(nombre: str, size=(20, 20)) -> ctk.CTkImage | None:
    """Carga SVG, lo tiñe según tema actual y devuelve CTkImage (con cache)."""
    modo = ctk.get_appearance_mode()
    key = (nombre, size, modo)
    if key in _ICON_CACHE:
        return _ICON_CACHE[key]
    path = ICON_DIR / (Path(nombre).stem + ".png")
    if not path.exists():
        return None
    try:
        img = Image.open(path).convert("RGBA")
        # Color según modo actual
        color = "#ffffff" if modo == "Dark" else "#1a1a1a"
        r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
        # Tiñe pixels no transparentes (vectorizado con list comprehension)
        datas = img.get_flattened_data()
        new_data = [(r, g, b, a) if a > 0 else (0, 0, 0, 0) for *_, a in datas]
        img.putdata(new_data)
        ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=size)
        _ICON_CACHE[key] = ctk_img
        return ctk_img
    except Exception:
        return None


def _hex_to_rgb(h: str) -> tuple[int, int, int]:
    h = h.lstrip("#")
    if len(h) == 3:
        return (int(h[0]*2, 16), int(h[1]*2, 16), int(h[2]*2, 16))
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))


# ---- Utilidad para grab modal seguro ----
def _grab(win):
    """Grab modal seguro (falla si la ventana aún no es visible)."""
    try:
        win.grab_set()
    except tk.TclError:
        pass


# ---- Parche de rendimiento: update_idletasks() ----
# customtkinter llama a update_idletasks() (un repaso GLOBAL de maquetación)
# al final de CADA _draw de CTkOptionMenu y CTkScrollbar. Al construir una
# ventana eso son ~40 repasos = ~1.5 s de ventana congelada. Mientras se
# construye/recarga widgets solo se atiende 1 de cada 10 llamadas.
_IDLETASKS_ORIG = tk.Misc.update_idletasks
_IDLETASKS = {"activo": False, "n": 0, "flush": False, "en_curso": False}


def _idletasks_limitado(self):
    """Sustituto de update_idletasks(): acotado durante la construcción."""
    if _IDLETASKS["en_curso"]:
        # Repaso ANIDADO: customtkinter se llama a sí mismo.
        #   CTkScrollbar._draw → self._canvas.update_idletasks()
        #     → Tk dispara <Configure> → _update_dimensions_event
        #       → otro _draw → otro update_idletasks() → recursión.
        # Cada nivel recalculaba el layout COMPLETO de la ventana
        # (~2.5 s de congelón al abrir). Se corta aquí: el layout se
        # asienta solo cuando el bucle de Tk vuelve al control.
        return None
    if _IDLETASKS["flush"]:
        # Dentro del repaso FINAL: los _draw anidados (scrollbars, etc.) no
        # lanzan su propio repaso global. Antes cada scrollbar._draw hacía
        # update_idletasks() real = ~24 repasos extra (~1 s) al cerrar el
        # bloque _ui_rapida.
        return None
    if (type(self).__name__ == "CTkCanvas"
            and type(getattr(self, "master", None)).__name__ == "CTkScrollbar"):
        # CTkScrollbar._draw() termina con self._canvas.update_idletasks()
        # (customtkinter 5.2.2, línea 161). Es un repaso GLOBAL que ocurre
        # DESPUÉS de dibujar: no sirve para nada y arrastra la maquetación
        # de toda la ventana (~1 s por cada barra de scroll). Se anula: el
        # propio bucle de Tk se encarga de repintar la barra.
        return None
    if not _IDLETASKS["activo"]:
        _IDLETASKS["en_curso"] = True
        try:
            return _IDLETASKS_ORIG(self)
        finally:
            _IDLETASKS["en_curso"] = False
    # Durante la construcción no se hace NINGÚN repaso: customtkinter llama
    # a update_idletasks() al final de cada _draw de optionmenu/scrollbar, y
    # cada uno drenaba TODA la maquetación pendiente (~1 s de congelón).
    # Los eventos los procesa el bucle normal de Tk al volver el control.
    return None


tk.Misc.update_idletasks = _idletasks_limitado


@contextlib.contextmanager
def _ui_rapida():
    """Bloque de código con update_idletasks() acotado (UI sin congelones)."""
    anterior = _IDLETASKS["activo"]
    _IDLETASKS["activo"] = True
    _IDLETASKS["n"] = 0
    try:
        yield
    finally:
        # NOTA: no se hace ningún repaso aquí. Los eventos de maquetación
        # pendientes los procesa el bucle normal de Tk en cuanto vuelve el
        # control: así el bloque no congela la ventana acabada de abrir.
        _IDLETASKS["activo"] = anterior


# ---- Tooltips y Loading States ----
def _add_tooltip(widget: ctk.CTkBaseClass, text: str):
    """Añade tooltip a un widget (requiere customtkinter >= 5.2)."""
    try:
        from customtkinter import CTkToolTip
        CTkToolTip(widget, message=text, delay=0.3)
    except Exception:
        pass  # Tooltip opcional


def _set_loading(btn: ctk.CTkButton, loading: bool, original_text: str = ""):
    """Activa/desactiva estado de carga en botón con spinner."""
    if loading:
        btn._orig_text = btn.cget("text")
        btn.configure(text=" ⏳", state="disabled")
    else:
        txt = getattr(btn, "_orig_text", original_text)
        btn.configure(text=txt, state="normal")


# ---- Import Pawchive ----
def pch_importar_posts(posts, nombres, q):
    """Importa posts seleccionados de Pawchive a la BD (metadatos).

    Hilo aparte (nunca en el hilo de la UI): por cada post inserta la
    tarjeta (título "Creador — Título", url única) y resuelve el detalle
    -> páginas + tags en una sola transacción. Va metiendo progreso en
    la cola `q`:
        ("imp", i, total, titulo, res)
        ("imp_fin", stats)
    `nombres` = {(service, id): nombre} del índice de artistas.
    """
    from db import ComicRepository, PageRepository, fetch_all
    stats = {"nuevos": 0, "existentes": 0, "duplicados": 0,
             "sin_img": 0, "errores": 0, "fallos": []}
    try:
        # Títulos propios de pawchive (para desambiguar títulos repetidos)
        filas = fetch_all("SELECT titulo, url_comic FROM comics "
                          "WHERE sitio = %s", ("pawchive",))
    except Exception as e:
        q.put(("imp_fin", {**stats, "error": str(e)}))
        return
    try:
        propios = {pawchive_api.norm_title(t): u for t, u in filas}
        total = len(posts)
        for i, post in enumerate(posts, 1):
            titulo, res = "?", "error"
            try:
                service = str(post.get("service") or "")
                cid = str(post.get("user") or "")
                pid = str(post.get("id") or "")
                if not (service and cid and pid):
                    stats["errores"] += 1
                    q.put(("imp", i, total, "post sin identificador", "error"))
                    continue
                if pawchive_api.count_all_files_list(post) == 0:
                    stats["sin_img"] += 1
                    q.put(("imp", i, total,
                           str(post.get("title") or pid)[:60], "sin_img"))
                    continue
                nombre = nombres.get((service, cid))
                if not nombre:
                    try:
                        perfil = pawchive_api.fetch_profile(service, cid)
                        nombre = str(perfil.get("name") or cid).strip()
                    except Exception:
                        nombre = cid
                base = str(post.get("title") or "").strip() or f"post {pid}"
                titulo = f"{nombre} — {base}"[:255]
                url = pawchive_api.post_url(service, cid, pid)
                # Colisión de título dentro de pawchive -> sufijo #id
                norm = pawchive_api.norm_title(titulo)
                dueno = propios.get(norm)
                if dueno is not None and dueno != url:
                    titulo = f"{titulo} #{pid}"[:255]
                    norm = pawchive_api.norm_title(titulo)
                estado, cid_bd = ComicRepository.upsert(
                    titulo, url, "pawchive", estado="pendiente")
                if estado == "existe":
                    stats["existentes"] += 1
                    res = "existe"
                elif estado == "duplicado":
                    propios[norm] = url
                    stats["duplicados"] += 1
                    res = "duplicado"
                else:
                    propios[norm] = url
                    if cid_bd is None:
                        raise RuntimeError("no se pudo obtener el id del cómic")
                    try:
                        detalle = pawchive_api.fetch_post(service, cid, pid)
                        archivos = pawchive_api.all_files(detalle)
                        tags = pawchive_api.post_tags(detalle)
                    except Exception:
                        # queda con n_paginas_web=NULL: lo completa
                        # "Completar metadatos" (scrape_pawchive_pages)
                        stats["errores"] += 1
                        res = "sin detalle"
                        q.put(("imp", i, total, titulo[:60], res))
                        continue
                    if not archivos:
                        ComicRepository.update_metadata(cid_bd, 0, tags)
                        stats["sin_img"] += 1
                        res = "sin_img"
                    else:
                        paginas = [(n, a["url"], a["tipo"])
                                   for n, a in enumerate(archivos, 1)]
                        PageRepository.insert_batch_with_type(cid_bd, paginas)
                        ComicRepository.update_metadata(cid_bd,
                                                        len(archivos), tags)
                        stats["nuevos"] += 1
                        res = "nuevo"
            except Exception as e:
                stats["errores"] += 1
                stats["fallos"].append(f"{post.get('id')}: {e}")
                res = "error"
            q.put(("imp", i, total, titulo[:60], res))
    except Exception as e:
        stats["errores"] += 1
        stats["fallos"].append(f"importación: {e}")
    q.put(("imp_fin", stats))


# -------------- ESTADO COMPARTIDO ENTRE VENTANAS --------------
# Ventanas de sitio abiertas: código (vcp/vmp/pch) -> SiteWindow
WINDOWS: dict[str, "SiteWindow"] = {}

# Trabajo global: solo UN proceso a la vez en toda la aplicación.
# owner = ventana dueña (None = libre); name = nombre del trabajo.
ACTIVE_JOB: dict = {"owner": None, "name": ""}


def _sync_open_windows():
    """Persiste la lista de ventanas de sitio abiertas (config)."""
    try:
        update_config(open_windows=[s for s in list(WINDOWS) if s in SITES])
    except Exception:
        pass


def set_window_icon(win):
    """Icono de ventana (libro azul generado con PIL)."""
    try:
        import tempfile
        from PIL import Image, ImageDraw
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([2, 2, 62, 62], radius=14,
                            fill=(59, 130, 246, 255))
        # Libro estilizado (dos páginas blancas)
        d.polygon([(14, 16), (31, 20), (31, 50), (14, 46)],
                  fill=(255, 255, 255, 240))
        d.polygon([(50, 16), (33, 20), (33, 50), (50, 46)],
                  fill=(224, 240, 255, 240))
        ico = Path(tempfile.gettempdir()) / "gestor_comics_icon.ico"
        img.save(ico, format="ICO")
        win.iconbitmap(str(ico))
    except Exception:
        pass


# -------------- LANZADOR --------------
class Launcher(ctk.CTk):
    """Ventana pequeña con los botones para abrir cada ventana de sitio."""

    def __init__(self):
        super().__init__()
        # Cargar tema guardado ANTES de crear widgets
        cfg = load_config()
        appearance = cfg.get("appearance", "dark")
        ctk.set_appearance_mode(appearance)
        ctk.set_default_color_theme("dark-blue")

        self.title("Gestor de Cómics — Lanzador")
        self.geometry("540x500")
        self.minsize(500, 460)
        self.configure(fg_color=COLORS["BG"])

        self._counts_q: queue.Queue = queue.Queue()
        self.btns: dict[str, ctk.CTkButton] = {}

        # Barra superior con título + toggle tema
        topbar = ctk.CTkFrame(self, fg_color="transparent")
        topbar.pack(fill="x", padx=24, pady=(20, 10))
        ctk.CTkLabel(topbar, text="\U0001f4da Gestor de Cómics",
                     font=("Segoe UI", 24, "bold"),
                     text_color=COLORS["TEXT"]).pack(side="left")
        self.btn_theme = ctk.CTkButton(
            topbar, text="", width=36, height=36,
            image=_load_icon("sun-moon.svg", (20, 20)),
            fg_color="transparent", hover_color="#2e2e2e",
            command=self._toggle_theme)
        self.btn_theme.pack(side="right")
        _add_tooltip(self.btn_theme, "Alternar tema claro/oscuro")

        ctk.CTkLabel(self,
                     text="Una ventana por sitio \u00b7 elige por d\u00f3nde "
                          "quieres trabajar",
                     font=("Segoe UI", 13), text_color=COLORS["MUTED"]).pack()

        for code, info in SITES.items():
            hover = SITE_ACCENTS[code]["hover"]
            btn = ctk.CTkButton(
                self, text=f"{info['icon']} {info['label']}\n\u2026",
                height=66, font=("Segoe UI", 15, "bold"),
                fg_color=COLORS["CARD_BG"], hover_color=hover,
                text_color=COLORS["TEXT"], border_width=1, border_color="#2e2e2e",
                command=lambda c=code: self.open_site(c))
            btn.pack(fill="x", padx=30, pady=(18, 0) if code == "vcp" else
                     (10, 0))
            self.btns[code] = btn

        self.lbl_db = ctk.CTkLabel(self, text="\u23f3 Cargando recuento\u2026",
                                   font=("Segoe UI", 12), text_color=COLORS["MUTED"])
        self.lbl_db.pack(pady=(16, 0))

        self.lbl_job = ctk.CTkLabel(self, text="\u2714 Sin trabajos en curso",
                                    font=("Segoe UI", 12), text_color=COLORS["MUTED"])
        self.lbl_job.pack(pady=(4, 0))

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.pack(fill="x", padx=30, pady=(18, 20))
        ctk.CTkButton(bar, text="\u23fb Cerrar todo", width=130, height=34,
                      font=("Segoe UI", 12), fg_color="#8c2f2f",
                      hover_color="#6e2424",
                      command=self._on_close).pack(side="right")
        ctk.CTkLabel(bar,
                     text=f"BD: {DB_CONFIG['database']} @ {DB_CONFIG['host']}",
                     font=("Segoe UI", 11), text_color="#6f6f6f").pack(
            side="left")

        set_window_icon(self)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # No restaurar ventanas automáticamente al iniciar.
        # El usuario abre manualmente las que quiera con los botones del lanzador.
        # self.after(250, self._restore_windows)
        self.after(400, self._load_counts)
        self.after(600, self._poll)

    # ---- Apertura de ventanas ----
    def open_site(self, site: str):
        """Abre (o enfoca) la ventana de un sitio."""
        if site not in SITES:
            return
        w = WINDOWS.get(site)
        if w is not None and w.winfo_exists():
            try:
                w.deiconify()
                w.lift()
                w.focus_force()
            except Exception:
                pass
            return
        w = SiteWindow(self, site)
        WINDOWS[site] = w
        _sync_open_windows()

    def _restore_windows(self):
        """Reabre las ventanas que estaban abiertas al cerrar la sesión."""
        try:
            abiertas = load_config().get("open_windows", [])
        except Exception:
            abiertas = []
        for site in abiertas:
            if site in SITES and site not in WINDOWS:
                self.open_site(site)

    # ---- Recuento de cómics por sitio (hilo de fondo) ----
    def _load_counts(self):
        def work():
            try:
                conn = mysql.connector.connect(**DB_CONFIG)
                cur = conn.cursor()
                cur.execute("SELECT sitio, COUNT(*) FROM comics "
                            "GROUP BY sitio")
                data = {str(s): int(n) for s, n in cur.fetchall()}
                cur.close()
                conn.close()
            except Exception as e:
                data = {"__error__": str(e)}
            self._counts_q.put(data)

        threading.Thread(target=work, daemon=True).start()

    def _apply_counts(self, data: dict):
        if "__error__" in data:
            self.lbl_db.configure(
                text="\u274c Sin conexi\u00f3n con MySQL (\u00bfXAMPP activo?)",
                text_color=COLORS["DANGER"])
            return
        por_sitio = {info["nombre"]: code for code, info in SITES.items()}
        vistos = set()
        total = 0
        for sitio_bd, n in data.items():
            total += n
            code = por_sitio.get(sitio_bd)
            if code and code in self.btns:
                vistos.add(code)
                info = SITES[code]
                self.btns[code].configure(
                    text=f"{info['icon']} {info['label']}\n"
                         f"{fmt_num(n)} c\u00f3mics")
        # Sitios sin cómics aún (sin fila en GROUP BY): muestran 0
        for code, info in SITES.items():
            if code not in vistos and code in self.btns:
                self.btns[code].configure(
                    text=f"{info['icon']} {info['label']}\n0 c\u00f3mics")
        self.lbl_db.configure(
            text=f"\U0001f4c1 {fmt_num(total)} c\u00f3mics en la BD "
                 f"({DB_CONFIG['database']})",
            text_color=COLORS["SUCCESS"])

    # ---- Estado del trabajo global ----
    def _poll(self):
        try:
            while True:
                self._apply_counts(self._counts_q.get_nowait())
        except queue.Empty:
            pass

        owner = ACTIVE_JOB.get("owner")
        if owner is not None:
            try:
                vivo = owner.winfo_exists()
            except Exception:
                vivo = False
        else:
            vivo = False
        if vivo:
            self.lbl_job.configure(
                text=f"\u23f3 Trabajo en curso: {ACTIVE_JOB.get('name', '')} "
                     f"\u2014 ventana {owner.site_label}",
                text_color=COLORS["WARN"])
        else:
            self.lbl_job.configure(text="\u2714 Sin trabajos en curso",
                                   text_color=COLORS["MUTED"])
        self.after(600, self._poll)

    def _on_close(self):
        if WINDOWS:
            if not messagebox.askyesno(
                    "Cerrar todo",
                    f"Hay {len(WINDOWS)} ventana(s) de sitio abierta(s).\n"
                    "\u00bfCerrar el lanzador y todas las ventanas?"):
                return
            for w in list(WINDOWS.values()):
                try:
                    w.close(force=True)
                except Exception:
                    pass
        self.destroy()

    def _toggle_theme(self):
        """Alterna modo claro/oscuro global y persiste en config."""
        cfg = load_config()
        nuevo = "light" if cfg.get("appearance", "dark") == "dark" else "dark"
        cfg["appearance"] = nuevo
        update_config(**cfg)
        ctk.set_appearance_mode(nuevo)
        # Propagar a ventanas hijas
        for w in WINDOWS.values():
            w._apply_theme(nuevo)
        # Recargar iconos del botón toggle
        self.btn_theme.configure(image=_load_icon("sun-moon.svg", (20, 20)))


# -------------- VENTANA DE UN SITIO --------------
class SiteWindow(ctk.CTkToplevel):
    """Ventana completa (Dashboard + Colección) de UN solo sitio."""

    def __init__(self, launcher: Launcher, site: str):
        super().__init__(launcher)
        # Cargar tema desde config (global, compartido con Launcher)
        cfg = load_config()
        appearance = cfg.get("appearance", "dark")
        ctk.set_appearance_mode(appearance)
        ctk.set_default_color_theme("dark-blue")

        self.launcher = launcher
        self.site = site                     # vcp | vmp | pch
        info = SITES[site]
        self.site_label = info["label"]      # VerComicsPorno / … / Pawchive
        self.sitio = info["nombre"]           # valor en comics.sitio

        self.title(f"Gestor de Cómics — {self.site_label}")
        # Geometría propia por sitio (recuerda tamaño y posición)
        defaults = {"vcp": "1280x820+30+20",
                    "vmp": "1280x820+70+55",
                    "pch": "1280x820+110+90"}
        self.geometry(cfg.get(f"geo_{site}", defaults.get(site, "1280x820")))
        self.minsize(980, 620)
        self.configure(fg_color=COLORS["BG"])

        # Canvas de fondo (gradiente + ruido) — detrás de todo
        self.bg_canvas = tk.Canvas(self, highlightthickness=0, bd=0)
        self.bg_canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.bg_canvas.lower("all")
        self.bind("<Configure>", lambda e: self._draw_bg())

        self.dest_var = tk.StringVar(value=cfg.get("dest", str(DEFAULT_DEST)))
        self.workers_var = tk.StringVar(value=str(cfg.get("workers", 6)))
        self.limit_var = tk.StringVar(value=str(cfg.get("limit", 0)))
        self.mode_var = tk.StringVar(
            value=cfg.get("mode_label", "Auto (cookies de Brave)"))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._schedule_fill())
        estado_f = cfg.get("filter_estado", "Todos")
        self.filter_estado_var = tk.StringVar(
            value=estado_f if estado_f in ESTADO_FILTROS else "Todos")
        self.filter_estado_var.trace_add(
            "write", lambda *_: self._schedule_fill())
        self.status_var = tk.StringVar(value="Listo")

        self.proc: subprocess.Popen | None = None
        self.job_queue: queue.Queue = queue.Queue()
        self.job_name = ""
        self.job_t0 = 0.0
        self.all_rows: list[tuple] = []      # filas crudas de la BD
        self.filtered: list[tuple] = []      # tras filtros + orden
        self.sort_col = "id"                 # columna de orden actual
        self.sort_desc = True                # descendente por defecto (id)
        self._rendered = 0                   # filas pintadas en la tabla
        self._fill_after = None              # retardo (debounce) del filtro
        self._filling = False                # True mientras se repinta la tabla
        self._rendering = False              # guardia anti-reentrancia del scroll
        self._db_ok: bool | None = None      # None = sin comprobar aún
        self._sel_prev: set[str] = set()     # selección anterior (para tags)
        self._row_tags: dict[str, tuple] = {}  # iid -> tags originales
        self._ctx_iid: str | None = None     # fila bajo el clic derecho
        self._empty_shown = False            # panel vacío visible (flag propio:
                                             # winfo_ismapped no sirve si la
                                             # pestaña Colección está oculta)

        self.dash: Dashboard | None = None  # se crea en _build_ui (pestaña)
        self._dash_pasos = None              # iterador de pasos de construcción

        # Estado de la pestaña 🐾 Explorar (solo la ventana de Pawchive)
        self._pch_q: queue.Queue = queue.Queue()
        self._pch_idx: list = []         # (id, nombre, servicio, fav) por fav↓
        self._pch_nom: dict = {}         # (servicio, id) -> nombre
        self._pch_view: list = []        # [(servicio, id, nombre)] en la lista
        self._pch_posts: list = []       # posts del bloque actual (hasta 50)
        self._pch_page = 0               # bloque del servidor (o = page*50)
        self._pch_post_pag = 0           # página local (0-based) del bloque
        self._pch_post_pags = 1          # páginas locales del bloque actual
        self._pch_post_objetivo = None   # página local a la que saltar
        self.pch_post_cards: dict = {}   # idx local -> datos de la tarjeta
        self._pch_busy = False           # cargando índice/posts/importando
        self._pch_idx_loading = False    # leyendo caché del índice (hilo)
        self._pch_art_after = None       # debounce de la búsqueda de artistas
        self._pch_pagina = 0             # página actual del grid (0-based)
        self._pch_paginas = 1            # páginas del filtro actual
        self._pch_creator: tuple | None = None   # (svc, id, nombre) = modo
        self._pch_qglobal = ""                   # query global guardada
        self._pch_acum: dict = {}                # pid -> post (selección
                                                   #   acumulada entre páginas)

        # ---- miniaturas (avatares artistas + thumbs posts) ----
        self._pch_thumb_imgs: dict[str, tk.PhotoImage] = {}  # clave -> imagen
        self._pch_thumb_ctk: dict[str, ctk.CTkImage] = {}     # clave -> CTkImage
        self._pch_thumb_pil: dict = {}                         # clave -> imagen PIL
        self._pch_thumb_sizes: dict[str, tuple[int, int]] = {}
        self._pch_thumb_pending: dict[str, list[str]] = {}   # clave -> iids
        self._pch_thumb_pendientes: set[str] = set()         # descargas en curso
        self._pch_thumb_errores: dict[str, str] = {}          # clave -> error
        self._pch_thumb_lock = threading.Lock()
        self._pch_thumb_hilos: list[threading.Thread] = []

        with _ui_rapida():          # construcción sin congelón (ver arriba)
            self._build_ui()
            set_window_icon(self)
            # refresh_table() se llama al terminar la fase 3 (ver abajo)

        self.after(120, self._poll_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- Tema y fondo ----
    def _apply_theme(self, modo: str):
        """Aplica cambio de tema (light/dark) a esta ventana."""
        ctk.set_appearance_mode(modo)
        # Limpiar cache de gradientes e iconos para forzar regeneracion
        self._BG_CACHE.clear()
        _ICON_CACHE.clear()
        # Re-dibujar gradiente de fondo
        self._draw_bg()
        # Recargar iconos SVG en botones (si los hay)
        self._reload_icons()

    def _reload_icons(self):
        """Recarga iconos SVG en botones conocidos tras cambio de tema."""
        # Los iconos en Treeview se actualizan solos con _draw_bg
        pass

        # Cache de gradientes a nivel de clase: (w, h, modo, site) -> PhotoImage
    _BG_CACHE: dict[tuple[int, int, str, str], ImageTk.PhotoImage] = {}

    def _draw_bg(self):
        """Dibuja gradiente vertical + ruido en canvas de fondo (optimizado con cache)."""
        w, h = self.winfo_width(), self.winfo_height()
        if w < 10 or h < 10:
            return
        acc = SITE_ACCENTS.get(self.site, SITE_ACCENTS["vcp"])
        modo = ctk.get_appearance_mode()
        if modo == "Light":
            c1 = self._lighten(acc["bg_top"], 0.35)
            c2 = self._lighten(acc["bg_bot"], 0.35)
        else:
            c1, c2 = acc["bg_top"], acc["bg_bot"]

        cache_key = (w, h, modo, self.site)
        if cache_key in self._BG_CACHE:
            self._bg_photo = self._BG_CACHE[cache_key]
        else:
            # Gradiente rapido: crea imagen 1px de ancho y redimensiona
            grad = Image.new("RGB", (1, h), c1)
            r1, g1, b1 = _hex_to_rgb(c1)
            r2, g2, b2 = _hex_to_rgb(c2)
            # Genera columna de pixels
            column = [(int(r1 + (r2 - r1) * (y / h)),
                       int(g1 + (g2 - g1) * (y / h)),
                       int(b1 + (b2 - b1) * (y / h))) for y in range(h)]
            grad.putdata(column)
            # Redimensiona horizontalmente (muy rapido en C)
            grad = grad.resize((w, h), Image.NEAREST)

            # Overlay ruido
            noise_path = PATTERN_DIR / "noise.png"
            if noise_path.exists():
                noise = Image.open(noise_path).convert("RGBA").resize((w, h), Image.NEAREST)
                noise.putalpha(int(255 * 0.03))
                grad = Image.alpha_composite(grad.convert("RGBA"), noise)

            self._bg_photo = ImageTk.PhotoImage(grad)
            self._BG_CACHE[cache_key] = self._bg_photo

        try:
            self.bg_canvas.delete("bg")
            self.bg_canvas.create_image(0, 0, anchor="nw",
                                        image=self._bg_photo, tags="bg")
        except tk.TclError:
            # la ventana ya se destruyó (el <Configure> llegó tarde)
            pass

    def _lighten(self, hex_color: str, factor: float) -> str:
        """Aclara un color hex (0.0-1.0)."""
        r, g, b = _hex_to_rgb(hex_color)
        r = min(255, int(r + (255 - r) * factor))
        g = min(255, int(g + (255 - g) * factor))
        b = min(255, int(b + (255 - b) * factor))
        return f"#{r:02x}{g:02x}{b:02x}"

    # ---- Helper tarjetas elevadas ----
    def _card(self, parent, **kwargs) -> ctk.CTkFrame:
        """Frame con borde sutil + sombra simulada (frame detrás)."""
        radius = kwargs.pop("corner_radius", 12)
        # Sombra (frame hijo un pelín mayor, color más oscuro)
        shadow = ctk.CTkFrame(parent, fg_color="#0a0a0a", corner_radius=radius)
        shadow.place(**{k: v for k, v in kwargs.items() if k in ("relx","rely","relwidth","relheight","x","y","width","height","anchor")})
        card = ctk.CTkFrame(parent,
                            fg_color=COLORS["CARD_BG"],
                            corner_radius=radius,
                            border_width=1,
                            border_color=COLORS["BORDER"])
        card.place(**{k: v for k, v in kwargs.items() if k in ("relx","rely","relwidth","relheight","x","y","width","height","anchor")})
        card.lift(shadow)
        return card

    # ---------------- INTERFAZ ----------------
    def _build_ui(self):
        small = ("Segoe UI", 12)

        # --- Pestañas: 📊 Dashboard / 📚 Colección ---
        self.tabview = ctk.CTkTabview(
            self, fg_color=COLORS["BG"], corner_radius=0,
            segmented_button_fg_color=COLORS["PANEL"],
            segmented_button_selected_color=COLORS["ACCENT"],
            segmented_button_selected_hover_color=COLORS["ACCENT_HOVER"],
            segmented_button_unselected_color=COLORS["CARD_BG"],
            segmented_button_unselected_hover_color="#2e2e2e",
            text_color=COLORS["MUTED"])
        self.tabview.pack(fill="both", expand=True)

        tab_dash = self.tabview.add("\U0001f4ca Dashboard")
        tab_col = self.tabview.add("\U0001f4da Colecci\u00f3n")

        # --- Pestaña 3 (solo Pawchive): 🐾 Explorar (artistas + posts) ---
        if self.site == "pch":
            tab_exp = self.tabview.add("\U0001f43e Explorar")
            self._build_explorar(tab_exp)
            self.after(300, self._pch_load_index)
            self.after(250, self._pch_poll)

        self._tab_dash = tab_dash
        self._tab_col = tab_col

        # --- Placeholder: el Dashboard es lo más pesado (~1.2 s) y se
        #     construye DESPUÉS de que la ventana sea visible ---
        self._dash_ph = ctk.CTkLabel(
            tab_dash, text="\u23f3 Cargando\u2026",
            font=("Segoe UI", 15), text_color=COLORS["MUTED"])
        self._dash_ph.pack(expand=True)

        # --- Barra inferior FUERA de las pestañas (siempre visible) ---
        bottom = ctk.CTkFrame(self, fg_color=COLORS["PANEL"], corner_radius=0, height=52)
        bottom.pack(fill="x")
        bottom.pack_propagate(False)
        self.status_dot = ctk.CTkLabel(bottom, text="●", font=("Segoe UI", 14),
                                       text_color=COLORS["MUTED"], width=18)
        self.status_dot.pack(side="left", padx=(14, 0), pady=13)
        ctk.CTkLabel(bottom, textvariable=self.status_var, font=small,
                     text_color=COLORS["TEXT"], anchor="w").pack(
            side="left", fill="x", expand=True, padx=(4, 10), pady=13)
        self.pbar = ctk.CTkProgressBar(bottom, width=280, height=12)
        self.pbar.pack(side="right", padx=(10, 12), pady=19)
        self.pbar.set(0)
        self.btn_stop = ctk.CTkButton(
            bottom, text="⏹ Parar", width=92, height=32, font=small,
            state="disabled", fg_color="#8c2f2f", hover_color="#6e2424",
            command=self.stop_job)
        self.btn_stop.pack(side="right", pady=10)

        # Fases siguientes: se arrancan cuando la ventana ya está MAPEDA y
        # pintada (evento <Map>), para que el usuario la vea al instante.
        # OJO: <Map> salta también por cada widget hijo que se mapea, así que
        # el binding se quita en cuanto se usa (si no, ~500 callbacks).
        self._fase2_puesta = False
        self._map_funcid = self.bind("<Map>", self._arrancar_fases, add="+")
        self.after(600, self._arrancar_fases)     # red de seguridad

    def _arrancar_fases(self, _e=None):
        """Arranca la fase 2 (Dashboard) la primera vez que se ve la ventana."""
        if self._fase2_puesta or not self.winfo_exists():
            return
        self._fase2_puesta = True
        try:
            self.unbind("<Map>", self._map_funcid)
        except Exception:
            pass
        self.after(30, self._build_ui_dashboard)

    def _build_ui_dashboard(self):
        """Fase 2: Dashboard (frame embebido de dashboard.py).

        Es la parte más pesada de la ventana, así que se construye cuando la
        ventana ya se ha pintado: el usuario la ve al instante en vez de
        esperarla congelada."""
        if not self.winfo_exists():
            return
        with _ui_rapida():
            if self._dash_ph is not None:
                try:
                    self._dash_ph.destroy()
                except Exception:
                    pass
                self._dash_ph = None
            self.dash = Dashboard(self._tab_dash, self, auto=False)
            self.dash.pack(fill="both", expand=True)
        # El dashboard tiene ~90 widgets: construirlo de una vez congela la
        # ventana ~1 s. Se hace por pasos, cediendo el control a Tk entre
        # uno y otro para que la ventana siga respondiendo.
        self._dash_pasos = self.dash.pasos_build()
        self.after(1, self._dash_paso)

    def _dash_paso(self):
        """Ejecuta el siguiente paso de construcción del dashboard."""
        if not self.winfo_exists():
            return
        try:
            paso = next(self._dash_pasos)
        except StopIteration:
            self._dash_pasos = None
            self.after(1, self._build_ui_coleccion)
            return
        with _ui_rapida():
            paso()
        self.after(1, self._dash_paso)

    def _build_ui_coleccion(self, *a, **k):
        """Fase 3 con update_idletasks() acotado (sin congelón al rellenar)."""
        with _ui_rapida():
            return self._build_ui_coleccion_raw(*a, **k)

    def _build_ui_coleccion_raw(self):
        """Fase 3: pestaña Colección + primera carga de filas desde la BD."""
        if not self.winfo_exists():
            return
        tab_col = self._tab_col
        small = ("Segoe UI", 12)

        # --- Pestaña 2: Colección ---
        # Barra superior: buscador + filtro estado + refrescar + contador
        top = ctk.CTkFrame(tab_col, fg_color=COLORS["PANEL"], corner_radius=0, height=60)
        top.pack(fill="x")
        top.pack_propagate(False)

        ctk.CTkLabel(top, text="\U0001f50d", font=("Segoe UI", 15)).pack(
            side="left", padx=(14, 4))
        ctk.CTkEntry(top, textvariable=self.search_var, width=320, height=34,
                     placeholder_text="Buscar cómic por título…",
                     font=small).pack(side="left", padx=(0, 14), pady=13)

        ctk.CTkLabel(top, text="Estado", font=small,
                     text_color=COLORS["MUTED"]).pack(side="left", padx=(0, 4))
        ctk.CTkOptionMenu(top, variable=self.filter_estado_var,
                          values=list(ESTADO_FILTROS), width=140, height=30,
                          font=("Segoe UI", 12),
                          dynamic_resizing=False).pack(side="left",
                                                       padx=(0, 14))

        ctk.CTkButton(top, text="\U0001f504 Refrescar", width=110, height=34,
                      font=small, fg_color="#3a3a3a", hover_color="#4a4a4a",
                      command=self.refresh_table).pack(side="left",
                                                       padx=(0, 10), pady=13)
        self.lbl_count = ctk.CTkLabel(top, text="", font=small,
                                      text_color=COLORS["MUTED"])
        self.lbl_count.pack(side="right", padx=16)

        # --- Barra de selección (acciones sobre las filas marcadas) ---
        self.sel_bar = ctk.CTkFrame(tab_col, fg_color="#191919",
                                    corner_radius=0, height=42)
        self.sel_bar.pack(fill="x")
        self.sel_bar.pack_propagate(False)
        self.lbl_sel = ctk.CTkLabel(self.sel_bar, text="Sin selección",
                                    font=small, text_color=COLORS["MUTED"])
        self.lbl_sel.pack(side="left", padx=(14, 10))
        ctk.CTkButton(
            self.sel_bar, text="\U0001f4c2 Abrir carpeta", width=140,
            height=28, font=("Segoe UI", 12), fg_color="#3a3a3a",
            hover_color="#4a4a4a",
            command=self._sel_open_folder).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            self.sel_bar, text="\U0001f4cb Copiar t\u00edtulo", width=132,
            height=28, font=("Segoe UI", 12), fg_color="#3a3a3a",
            hover_color="#4a4a4a",
            command=self._sel_copy_titles).pack(side="left", padx=(0, 8))
        ctk.CTkButton(
            self.sel_bar, text="\U0001f517 Copiar URL", width=124,
            height=28, font=("Segoe UI", 12), fg_color="#3a3a3a",
            hover_color="#4a4a4a",
            command=self._sel_copy_urls).pack(side="left", padx=(0, 8))
        ctk.CTkLabel(self.sel_bar,
                     text="Clic derecho en la tabla para m\u00e1s acciones",
                     font=("Segoe UI", 11), text_color="#6f6f6f").pack(
            side="right", padx=14)

        # Tabla
        tbl_frame = ctk.CTkFrame(tab_col, fg_color=COLORS["PANEL"], corner_radius=0)
        tbl_frame.pack(fill="both", expand=True)

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Dark.Treeview", background="#1a1a1a",
                        fieldbackground="#1a1a1a", foreground=COLORS["TEXT"],
                        rowheight=28, font=("Segoe UI", 12), borderwidth=0)
        style.configure("Dark.Treeview.Heading", background="#2c2c2c",
                        foreground=COLORS["TEXT"], font=("Segoe UI", 12, "bold"),
                        borderwidth=0, relief="flat")

        style.configure("PchArt.Treeview", background="#1a1a1a",
                        fieldbackground="#1a1a1a", foreground=COLORS["TEXT"],
                        rowheight=46, font=("Segoe UI", 12), borderwidth=0)
        style.configure("PchArt.Treeview.Heading", background="#2c2c2c",
                        foreground=COLORS["TEXT"], font=("Segoe UI", 12, "bold"),
                        borderwidth=0, relief="flat")
        style.configure("PchPost.Treeview", background="#1a1a1a",
                        fieldbackground="#1a1a1a", foreground=COLORS["TEXT"],
                        rowheight=74, font=("Segoe UI", 12), borderwidth=0)
        style.configure("PchPost.Treeview.Heading", background="#2c2c2c",
                        foreground=COLORS["TEXT"], font=("Segoe UI", 12, "bold"),
                        borderwidth=0, relief="flat")
        style.map("Dark.Treeview",
                  background=[("selected", COLORS["ACCENT"])],
                  foreground=[("selected", "#ffffff")])
        style.map("Dark.Treeview.Heading", background=[("selected", "#3a3a3a")])
        # Scrollbar oscura (la ttk por defecto es gris claro y choca)
        style.configure("Dark.Vertical.TScrollbar", background="#3a3a3a",
                        troughcolor="#161616", bordercolor="#161616",
                        arrowcolor=COLORS["TEXT"])
        style.map("Dark.Vertical.TScrollbar",
                  background=[("active", "#4a4a4a")])

        cols = ("id", "titulo", "estado", "sitio", "paginas", "web")
        self.tree = ttk.Treeview(tbl_frame, columns=cols, show="headings",
                                 style="Dark.Treeview",
                                 selectmode="extended")
        # Cabeceras ordenables: clic para ordenar asc/desc (▲/▼)
        self._head_labels = {"id": "ID", "titulo": "T\u00edtulo",
                             "estado": "Estado", "sitio": "Sitio",
                             "paginas": "P\u00e1g.", "web": "P\u00e1g. web"}
        for c in cols:
            self.tree.heading(c, text=self._head_labels[c],
                              command=lambda cc=c: self._sort_by(cc))
        # Orden inicial (id desc): reflejado en la cabecera
        self.tree.heading("id", text=self._head_labels["id"] + " ▼")
        self.tree.column("id", width=50, minwidth=40, anchor="center")
        self.tree.column("titulo", width=510, minwidth=200, anchor="w")
        self.tree.column("estado", width=115, minwidth=95, anchor="center")
        # Columna "sitio" OCULTA: cada ventana es de un solo sitio
        self.tree.column("sitio", width=0, minwidth=0, stretch=False)
        self.tree.heading("sitio", text="")
        self.tree.column("paginas", width=55, minwidth=45, anchor="center")
        self.tree.column("web", width=70, minwidth=55, anchor="center")

        # Filas rayadas + Estado en color (tags que no pisan la selección)
        self.tree.tag_configure("odd", background="#1a1a1a")
        self.tree.tag_configure("even", background="#202020")
        self.tree.tag_configure("e_ok", foreground=COLORS["SUCCESS"])
        self.tree.tag_configure("e_pend", foreground=COLORS["WARN"])
        self.tree.tag_configure("e_dup", foreground=COLORS["ORANGE"])
        # Selección: los tags de fila tienen prioridad sobre el estilo,
        # así que la selección se pinta también con un tag explícito.
        self.tree.tag_configure("sel", background=COLORS["ACCENT"],
                                foreground="#ffffff")

        self._sb = ttk.Scrollbar(tbl_frame, orient="vertical",
                                 style="Dark.Vertical.TScrollbar",
                                 command=self.tree.yview)
        self.tree.configure(yscrollcommand=self._on_yscroll)
        self.tree.pack(side="left", fill="both", expand=True,
                       padx=(6, 0), pady=6)
        self._sb.pack(side="right", fill="y", padx=(0, 6), pady=6)
        self.tree.bind("<Double-1>", self._open_selected)
        self.tree.bind("<Button-3>", self._ctx_menu)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        # --- Menú contextual (clic derecho) ---
        self.ctx_menu = tk.Menu(self, tearoff=0, bg="#1e1e1e", fg=COLORS["TEXT"],
                                activebackground=COLORS["ACCENT"], activeforeground="#ffffff",
                                font=("Segoe UI", 11), borderwidth=0)
        self.ctx_menu.add_command(label="\U0001f4c2 Abrir carpeta",
                                  command=self._ctx_open_folder)
        self.ctx_menu.add_command(label="\U0001f4cb Copiar t\u00edtulo",
                                  command=self._ctx_copy_title)
        self.ctx_menu.add_command(label="\U0001f517 Copiar URL",
                                  command=self._ctx_copy_url)
        self.ctx_menu.add_separator()
        self.ctx_menu.add_command(label="\U0001f310 Abrir en el navegador",
                                  command=self._ctx_open_browser)

        # --- Estado vacío (panel centrado sobre la tabla) ---
        self.empty = ctk.CTkFrame(tbl_frame, fg_color="#171717",
                                  corner_radius=0)
        self._empty_icon = ctk.CTkLabel(self.empty, text="",
                                        font=("Segoe UI", 44))
        self._empty_icon.pack(pady=(0, 6))
        self._empty_title = ctk.CTkLabel(self.empty, text="",
                                         font=("Segoe UI", 17, "bold"),
                                         text_color=COLORS["TEXT"])
        self._empty_title.pack()
        self._empty_hint = ctk.CTkLabel(self.empty, text="",
                                        font=small, text_color=COLORS["MUTED"])
        self._empty_hint.pack(pady=(4, 14))
        self.empty_btn = ctk.CTkButton(self.empty, text="", width=270,
                                       height=36, font=small,
                                       fg_color=COLORS["ACCENT"],
                                       hover_color=COLORS["ACCENT_HOVER"])
        self.empty_btn.pack()

        # Primera carga de filas (la tabla ya existe)
        self.refresh_table()

    # ---------------- PESTAÑA 🐾 EXPLORAR ----------------
    def _build_explorar(self, parent):
        """Pestaña 🐾 Explorar (solo Pawchive): artistas + buscador de posts."""
        small = ("Segoe UI", 12)
        root = ctk.CTkFrame(parent, fg_color=COLORS["BG"], corner_radius=0)
        root.pack(fill="both", expand=True)
        # Columnas con GRID (no pack): así el panel de artistas tiene un
        # ancho mínimo decente (3 tarjetas de 170 px + separaciones) y el
        # de posts se queda con el resto, sin que el Treeview se lo coma.
        root.grid_columnconfigure(0, weight=1, minsize=500)
        root.grid_columnconfigure(1, weight=1)
        root.grid_rowconfigure(0, weight=1)

        # ---------- IZQUIERDA: ARTISTAS ----------
        iz = ctk.CTkFrame(root, fg_color=COLORS["PANEL"], corner_radius=10)
        iz.grid(row=0, column=0, sticky="nsew", padx=(12, 6), pady=12)

        hdr = ctk.CTkFrame(iz, fg_color="transparent")
        hdr.pack(fill="x", padx=12, pady=(12, 6))
        ctk.CTkLabel(hdr, text="\U0001f3a8 Artistas",
                     font=("Segoe UI", 15, "bold"),
                     text_color=COLORS["ACCENT"]).pack(side="left")
        self.lbl_pch_idx = ctk.CTkLabel(hdr, text="índice: …",
                                        font=small, text_color=COLORS["MUTED"])
        self.lbl_pch_idx.pack(side="right")

        fila1 = ctk.CTkFrame(iz, fg_color="transparent")
        fila1.pack(fill="x", padx=12, pady=(0, 6))
        self.pch_entry_art = ctk.CTkEntry(
            fila1, height=32, font=small,
            placeholder_text="Buscar artista por nombre o id…")
        self.pch_entry_art.pack(side="left", fill="x", expand=True)
        self.btn_refresh_idx = ctk.CTkButton(fila1, text="\U0001f504", width=36, height=32,
                      font=small, fg_color="#3a3a3a", hover_color="#4a4a4a",
                      command=lambda: self._pch_load_index(forzar=True))
        self.btn_refresh_idx.pack(side="right", padx=(8, 0))
        _add_tooltip(self.btn_refresh_idx, "Recargar índice de artistas (forzar)")

        fila2 = ctk.CTkFrame(iz, fg_color="transparent")
        fila2.pack(fill="x", padx=12, pady=(0, 6))
        self.pch_svc_var = tk.StringVar(value="Todos")
        ctk.CTkOptionMenu(fila2, variable=self.pch_svc_var,
                          values=list(PCH_SVC_LABELS), width=110, height=30,
                          font=("Segoe UI", 12),
                          dynamic_resizing=False).pack(side="left")
        self.pch_solo_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(fila2, text="\u2605 Solo mis creadores",
                        variable=self.pch_solo_var, font=small,
                        fg_color=COLORS["ACCENT"], hover_color=COLORS["ACCENT_HOVER"],
                        command=self._pch_fill_artistas).pack(
            side="left", padx=(12, 0))

        # Grid de artistas PAGINADO: 9 tarjetas (3x3) por página.
        # Antes era un grid virtualizado con scroll infinito; con la
        # paginación solo se crean 9 tarjetas -> la ventana responde
        # enseguida y no hay que hacer scroll para llegar al final.
        self.pch_artists_frame = ctk.CTkScrollableFrame(
            iz, fg_color="#1a1a1a", corner_radius=8,
            label_text="", label_font=("Segoe UI", 12))
        self.pch_artists_frame.pack(fill="both", expand=True, padx=12, pady=(2, 4))
        self.pch_artists_frame._scrollbar.configure(width=8)
        # Configurar grid: 3 columnas
        for c in range(PCH_ART_COLS):
            self.pch_artists_frame.grid_columnconfigure(c, weight=1, minsize=170)

        self.pch_artist_cards = {}  # idx -> card data
        self.pch_selected_indices = set()

        # ---- barra de paginación ----
        pag = ctk.CTkFrame(iz, fg_color="transparent")
        pag.pack(fill="x", padx=12, pady=(0, 4))
        self.btn_pch_art_prev = ctk.CTkButton(
            pag, text="‹ Anterior", width=92, height=28, font=small,
            fg_color="#3a3a3a", hover_color="#4a4a4a",
            command=lambda: self._pch_cambiar_pagina(-1))
        self.btn_pch_art_prev.pack(side="left")
        self.lbl_pch_art_pag = ctk.CTkLabel(
            pag, text="Página 1 de 1", font=small, text_color=COLORS["MUTED"])
        self.lbl_pch_art_pag.pack(side="left", expand=True)
        self.btn_pch_art_next = ctk.CTkButton(
            pag, text="Siguiente ›", width=92, height=28, font=small,
            fg_color="#3a3a3a", hover_color="#4a4a4a",
            command=lambda: self._pch_cambiar_pagina(1))
        self.btn_pch_art_next.pack(side="right")
        # Salto directo a una página (con 1500 artistas son 167 páginas)
        self.pch_entry_pag = ctk.CTkEntry(pag, width=52, height=28, font=small,
                                          placeholder_text="nº")
        self.pch_entry_pag.pack(side="right", padx=(6, 0))
        self.pch_entry_pag.bind("<Return>", lambda _e: self._pch_ir_a_pagina())
        _add_tooltip(self.pch_entry_pag, "Ir a la página (y Enter)")

        self.lbl_pch_art_sel = ctk.CTkLabel(iz, text="Sin selección",
                                            font=small, text_color=COLORS["MUTED"])
        self.lbl_pch_art_sel.pack(anchor="w", padx=14, pady=(0, 6))
        bots = ctk.CTkFrame(iz, fg_color="transparent")
        bots.pack(fill="x", padx=12, pady=(0, 12))
        self.btn_pch_add = ctk.CTkButton(
            bots, text=" Añadir a mis creadores", height=34,
            font=small, fg_color=COLORS["ACCENT"], hover_color=COLORS["ACCENT_HOVER"],
            image=_load_icon("plus.svg", (18, 18)), compound="left",
            command=self._pch_add_creators)
        self.btn_pch_add.pack(side="left", fill="x", expand=True,
                              padx=(0, 6))
        _add_tooltip(self.btn_pch_add, "Añadir a mis creadores")
        self.btn_pch_rem = ctk.CTkButton(
            bots, text=" Quitar", width=110, height=34, font=small,
            fg_color="#3a3a3a", hover_color="#5a2a2a",
            image=_load_icon("trash.svg", (18, 18)), compound="left",
            command=self._pch_rem_creators)
        self.btn_pch_rem.pack(side="right")
        _add_tooltip(self.btn_pch_rem, "Quitar de mis creadores")

        # ---------- DERECHA: BUSCADOR DE POSTS ----------
        de = ctk.CTkFrame(root, fg_color=COLORS["PANEL"], corner_radius=10)
        de.grid(row=0, column=1, sticky="nsew", padx=(6, 12), pady=12)

        hdr2 = ctk.CTkFrame(de, fg_color="transparent")
        hdr2.pack(fill="x", padx=12, pady=(12, 6))
        self.lbl_pch_titulo = ctk.CTkLabel(
            hdr2, text="\U0001f50d Buscador de posts",
            font=("Segoe UI", 15, "bold"), text_color=COLORS["ACCENT"])
        self.lbl_pch_titulo.pack(side="left")
        # Solo se enseña en modo "posts de un creador"
        self.btn_pch_volver = ctk.CTkButton(
            hdr2, text=" Volver", width=84, height=28, font=small,
            fg_color="#3a3a3a", hover_color="#4a4a4a",
            image=_load_icon("chevron-left.svg", (16, 16)), compound="left",
            command=self._pch_volver)
        self.btn_pch_volver.pack(side="left", padx=(10, 0))
        _add_tooltip(self.btn_pch_volver, "Volver a búsqueda global")
        self.lbl_pch_posts = ctk.CTkLabel(hdr2, text="\u2014",
                                          font=small, text_color=COLORS["MUTED"])
        self.lbl_pch_posts.pack(side="right")

        fila3 = ctk.CTkFrame(de, fg_color="transparent")
        fila3.pack(fill="x", padx=12, pady=(0, 6))
        self.pch_entry_posts = ctk.CTkEntry(
            fila3, height=32, font=small,
            placeholder_text="Buscar por título o texto…")
        self.pch_entry_posts.pack(side="left", fill="x", expand=True)
        self.pch_entry_posts.bind("<Return>",
                                  lambda _e: self._pch_buscar(0))
        self.pch_svc_posts_var = tk.StringVar(value="Todos")
        self.pch_svc_posts_menu = ctk.CTkOptionMenu(
            fila3, variable=self.pch_svc_posts_var,
            values=list(PCH_SVC_LABELS), width=110, height=30,
            font=("Segoe UI", 12),
            dynamic_resizing=False)
        self.pch_svc_posts_menu.pack(side="right", padx=(8, 0))
        self.btn_pch_buscar = ctk.CTkButton(
            fila3, text=" Buscar", width=100, height=32,
            font=small, fg_color=COLORS["ACCENT"], hover_color=COLORS["ACCENT_HOVER"],
            image=_load_icon("search.svg", (18, 18)), compound="left",
            command=lambda: self._pch_buscar(0))
        self.btn_pch_buscar.pack(side="right", padx=(8, 0))
        _add_tooltip(self.btn_pch_buscar, "Buscar posts")

        # Grid de posts como TARJETAS (3x3) paginadas, igual que los
        # artistas: solo se crean 9 tarjetas a la vez -> la ventana responde
        # al instante y no hay que hacer scroll por 50 filas.
        self.pch_posts_frame = ctk.CTkScrollableFrame(
            de, fg_color="#1a1a1a", corner_radius=8,
            label_text="", label_font=("Segoe UI", 12))
        self.pch_posts_frame.pack(fill="both", expand=True, padx=12, pady=(2, 4))
        self.pch_posts_frame._scrollbar.configure(width=8)
        for c in range(PCH_POST_COLS):
            self.pch_posts_frame.grid_columnconfigure(c, weight=1, minsize=170)

        # ---- barra de paginación de posts ----
        pag = ctk.CTkFrame(de, fg_color="transparent")
        pag.pack(fill="x", padx=12, pady=(0, 6))
        self.btn_pch_post_prev = ctk.CTkButton(
            pag, text="‹ Anterior", width=92, height=28, font=small,
            fg_color="#3a3a3a", hover_color="#4a4a4a", state="disabled",
            command=lambda: self._pch_post_cambiar_pagina(-1))
        self.btn_pch_post_prev.pack(side="left")
        _add_tooltip(self.btn_pch_post_prev, "Página anterior")
        self.lbl_pch_post_pag = ctk.CTkLabel(
            pag, text="Página 1 de 1", font=small, text_color=COLORS["MUTED"])
        self.lbl_pch_post_pag.pack(side="left", expand=True, padx=6)
        self.btn_pch_post_next = ctk.CTkButton(
            pag, text="Siguiente ›", width=92, height=28, font=small,
            fg_color="#3a3a3a", hover_color="#4a4a4a", state="disabled",
            command=lambda: self._pch_post_cambiar_pagina(1))
        self.btn_pch_post_next.pack(side="right")
        _add_tooltip(self.btn_pch_post_next, "Página siguiente")
        # Salto directo a una página
        self.pch_entry_post_pag = ctk.CTkEntry(
            pag, width=52, height=28, font=small, placeholder_text="nº")
        self.pch_entry_post_pag.pack(side="right", padx=(6, 0))
        self.pch_entry_post_pag.bind("<Return>",
                                     lambda _e: self._pch_post_ir_a_pagina())
        _add_tooltip(self.pch_entry_post_pag, "Ir a la página (y Enter)")
        btn_todo = ctk.CTkButton(pag, text=" Todo", width=70, height=30,
                      font=small, fg_color="#3a3a3a", hover_color="#4a4a4a",
                      image=_load_icon("plus.svg", (16, 16)), compound="left",
                      command=self._pch_todo_posts)
        btn_todo.pack(side="right", padx=(6, 0))
        _add_tooltip(btn_todo, "Seleccionar todos los de la página")
        btn_limpiar = ctk.CTkButton(pag, text=" Limpiar", width=90, height=30,
                      font=small, fg_color="#3a3a3a", hover_color="#4a4a4a",
                      image=_load_icon("trash.svg", (16, 16)), compound="left",
                      command=self._pch_limpiar_posts)
        btn_limpiar.pack(side="right")
        _add_tooltip(btn_limpiar, "Limpiar busqueda y seleccion")

        imp = ctk.CTkFrame(de, fg_color="transparent")
        imp.pack(fill="x", padx=12, pady=(0, 6))
        self.btn_pch_imp = ctk.CTkButton(
            imp, text=" Importar seleccionados a la BD", height=36,
            font=("Segoe UI", 13, "bold"), fg_color="#1f8a4c",
            hover_color="#166b3a", state="disabled",
            image=_load_icon("download.svg", (20, 20)), compound="left",
            command=self._pch_importar)
        self.btn_pch_imp.pack(side="left", fill="x", expand=True)
        _add_tooltip(self.btn_pch_imp, "Importar seleccionados a la BD")
        # Botón de descarga para este sitio (pawchive)
        self.btn_pch_dl = ctk.CTkButton(
            imp, text=" Descargar", height=36,
            font=("Segoe UI", 13, "bold"), fg_color="#1f8a4c",
            hover_color="#166b3a",
            image=_load_icon("download.svg", (20, 20)), compound="left",
            command=lambda: self.run_downloader(self.pch_dl_types_var.get()))
        self.btn_pch_dl.pack(side="left", fill="x", expand=True, padx=(8, 0))
        _add_tooltip(self.btn_pch_dl, "Descargar imágenes de este sitio")
        # Selector de tipos a descargar
        self.pch_dl_types_var = tk.StringVar(value="imagen,video,archivo")
        self.pch_dl_types_menu = ctk.CTkOptionMenu(
            imp, variable=self.pch_dl_types_var,
            values=["imagen", "video", "archivo",
                    "imagen,video", "imagen,archivo", "video,archivo",
                    "imagen,video,archivo"],
            width=160, height=36, font=small,
            dynamic_resizing=False)
        self.pch_dl_types_menu.pack(side="left", padx=(8, 0))
        _add_tooltip(self.pch_dl_types_menu, "Tipos de archivo a descargar")
        self.lbl_pch_imp = ctk.CTkLabel(imp, text="Sin selección",
                                        font=small, text_color=COLORS["MUTED"])
        self.lbl_pch_imp.pack(side="right", padx=(10, 0))

        ctk.CTkLabel(de,
                     text="La importación guarda solo METADATOS (título, "
                          "URL, páginas y tags); las imágenes se descargan "
                          "desde \U0001f4da Colección.",
                     font=("Segoe UI", 11), text_color="#6f6f6f").pack(
            anchor="w", padx=14, pady=(0, 10))

        # Traces/binds al final: los widgets ya existen
        # (el campo usa placeholder, no textvariable: CTkEntry desactiva
        #  el placeholder en cuanto se le asigna una Variable)
        self.pch_entry_art.bind("<KeyRelease>",
                                lambda _e: self._pch_schedule_art())
        self.pch_svc_var.trace_add(
            "write", lambda *_: self._pch_fill_artistas())
        # Preview panel en hover (para posts): se enlaza en cada tarjeta
        self._pch_preview_win = None
        self._pch_preview_idx = None
        self._pch_preview_after = None
        self._pch_preview_card = None
        self._pch_preview_busy: set[int] = set()

    # ---- cola de mensajes (hilo de fondo -> UI) ----
    def _pch_poll(self):
        if not self.winfo_exists():
            return
        try:
            while True:
                self._pch_msg(self._pch_q.get_nowait())
        except queue.Empty:
            pass
        self.after(250, self._pch_poll)

    def _pch_msg(self, m):
        tipo = m[0]
        if tipo == "idx":
            self._pch_set_idx(m[1], m[2])
        elif tipo == "thumb":
            # miniatura lista: se crea el PhotoImage en el hilo principal
            self._pch_thumb_listo(m[1], m[2])
        elif tipo == "thumb_err":
            self._pch_thumb_error(m[1], m[2])
        elif tipo == "preview":
            # detalle del post listo -> se pinta el preview (hilo principal)
            self._pch_crear_preview(m[1], m[2])
        elif tipo == "idx_err":
            self._pch_busy = False
            self._pch_idx_loading = False
            _set_loading(self.btn_refresh_idx, False, "\U0001f504")
            self.lbl_pch_idx.configure(text=f"\u274c {m[1]}",
                                       text_color=COLORS["DANGER"])
        elif tipo == "posts":
            self._pch_fill_posts(m[1], m[2])
        elif tipo == "posts_err":
            self._pch_busy = False
            self._pch_post_objetivo = None
            self.btn_pch_buscar.configure(state="normal")
            self.lbl_pch_posts.configure(text=f"\u274c {m[1]}",
                                         text_color=COLORS["DANGER"])
        elif tipo == "imp":
            _, i, total, titulo, res = m
            ico = {"nuevo": "\u2705", "existe": "\u21a9\ufe0f",
                   "duplicado": "\u26a0\ufe0f", "sin_img": "\U0001f6ab",
                   "sin detalle": "\u26a0\ufe0f"}.get(res, "\u274c")
            self.log(f"  [{i}/{total}] {ico} {res}: {titulo}")
            self.lbl_pch_imp.configure(
                text=f"\u23f3 Importando {i}/{total}\u2026",
                text_color=COLORS["WARN"])
        elif tipo == "imp_fin":
            self._pch_import_fin(m[1])

    # ---- índice de artistas (caché + API) ----
    def _pch_load_index(self, forzar=False):
        if self._pch_busy or self._pch_idx_loading:
            return
        # 1) caché en memoria (ya convertida y ordenada) -> instantáneo
        if not forzar and _PCH_IDX_MEM["filas"] is not None:
            self._pch_set_idx(_PCH_IDX_MEM["filas"], _PCH_IDX_MEM["at"])
            return
        if not forzar:
            # 2) caché en disco (4 MB): se lee y parsea en un Hilo para que
            #    la ventana se abra al instante y no se congele la UI.
            self._pch_idx_loading = True
            _set_loading(self.btn_refresh_idx, True, "\U0001f504")
            self.lbl_pch_idx.configure(
                text="\u23f3 Cargando índice\u2026",
                text_color=COLORS["WARN"])
            threading.Thread(target=self._pch_cache_worker,
                             daemon=True).start()
            return
        self._pch_busy = True
        _set_loading(self.btn_refresh_idx, True, "\U0001f504")
        self.lbl_pch_idx.configure(
            text="\u23f3 Descargando índice de artistas\u2026",
            text_color=COLORS["WARN"])
        threading.Thread(target=self._pch_idx_worker, daemon=True).start()

    def _pch_cache_worker(self):
        """Lee la caché del índice (4 MB) y la convierte/ordena SIN bloquear
        la UI. Si la caché no sirve, cae a descargar el índice de la API."""
        try:
            data = json.loads(
                PCH_INDEX_FILE.read_text(encoding="utf-8"))
            creadores = data.get("creators")
            if not creadores:
                raise ValueError("caché vacía")
            filas = _normalizar_idx(creadores)
            at = str(data.get("at") or "caché")
            _PCH_IDX_MEM["at"] = at
            _PCH_IDX_MEM["filas"] = filas
            self._pch_q.put(("idx", filas, at))
        except Exception:
            self._pch_idx_worker()        # sin caché útil -> red

    def _pch_idx_worker(self):
        try:
            d = pawchive_api.fetch_json("/creators")
            creadores = [[str(c.get("id") or ""), str(c.get("name") or ""),
                          str(c.get("service") or ""),
                          int(c.get("favorited") or 0)]
                         for c in d if isinstance(c, dict)]
            filas = _normalizar_idx(creadores)   # convierte + ordena (hilo)
            at = time.strftime("%d/%m/%Y %H:%M")
            try:
                PCH_INDEX_FILE.write_text(
                    json.dumps({"at": at, "creators": filas},
                               ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass
            _PCH_IDX_MEM["at"] = at
            _PCH_IDX_MEM["filas"] = filas
            self._pch_q.put(("idx", filas, at))
        except Exception as e:
            self._pch_q.put(("idx_err", str(e)))

    def _pch_set_idx(self, creadores, at):
        if creadores and isinstance(creadores[0], tuple):
            filas = creadores          # ya convertido y ordenado (hilo)
        else:
            filas = _normalizar_idx(creadores)
        self._pch_idx = filas
        self._pch_nom = {(s, i): n for i, n, s, _f in filas}
        _PCH_IDX_MEM["at"] = at
        _PCH_IDX_MEM["filas"] = filas
        self._pch_busy = False
        self._pch_idx_loading = False
        _set_loading(self.btn_refresh_idx, False, "\U0001f504")
        self.lbl_pch_idx.configure(
            text=f"índice: {fmt_num(len(filas))} artistas · {at}",
            text_color=COLORS["MUTED"])
        self._pch_fill_artistas()

    # ---- lista de artistas ----
    def _pch_schedule_art(self):
        if self._pch_art_after is not None:
            try:
                self.after_cancel(self._pch_art_after)
            except Exception:
                pass
        self._pch_art_after = self.after(300, self._pch_fill_artistas)

    def _pch_fill_artistas(self, *args, **kwargs):
        """Rellena el grid de artistas con update_idletasks() acotado."""
        with _ui_rapida():
            return self._pch_fill_artistas_raw(*args, **kwargs)

    def _pch_fill_artistas_raw(self, *_):
        if getattr(self, "pch_artists_frame", None) is None:
            return
        q = self.pch_entry_art.get().strip().lower()
        svc = PCH_SVC_LABELS.get(self.pch_svc_var.get(), "")
        solo = self.pch_solo_var.get()
        cfg = set()
        if solo:
            cfg = {(c["service"], c["id"])
                   for c in pawchive_api.load_creators()}
        out = []
        for i, n, s, _f in self._pch_idx:
            if svc and s != svc:
                continue
            if q and q not in n.lower() and q != i:
                continue
            if solo and (s, i) not in cfg:
                continue
            out.append((s, i, n))
            if len(out) >= PCH_IDX_MAX:
                break
        self._pch_view = out

        # El filtro cambió -> la selección anterior deja de tener sentido y
        # se vuelve a la primera página.
        self.pch_selected_indices.clear()
        self._pch_pagina = 0
        self._pch_paginas = max(1, -(-len(out) // PCH_ART_POR_PAGINA))
        self._pch_render_pagina()

    # ---------------- PAGINACIÓN DEL GRID DE ARTISTAS ----------------
    def _pch_render_pagina(self):
        """Pinta las 9 tarjetas de la página actual (update_idletasks acotado)."""
        with _ui_rapida():
            return self._pch_render_pagina_raw()

    def _pch_render_pagina_raw(self):
        frame = getattr(self, "pch_artists_frame", None)
        if frame is None or not frame.winfo_exists():
            return
        # Limpiar las tarjetas de la página anterior
        for widget in frame.winfo_children():
            widget.destroy()
        self.pch_artist_cards.clear()

        total = len(self._pch_view)
        self._pch_paginas = max(1, -(-total // PCH_ART_POR_PAGINA))
        self._pch_pagina = max(0, min(self._pch_pagina, self._pch_paginas - 1))
        ini = self._pch_pagina * PCH_ART_POR_PAGINA
        fin = min(ini + PCH_ART_POR_PAGINA, total)

        for k, idx in enumerate(range(ini, fin)):
            s, i, n = self._pch_view[idx]
            self._create_artist_card(idx, s, i, n,
                                     k // PCH_ART_COLS, k % PCH_ART_COLS)

        # Barra de paginación
        self.lbl_pch_art_pag.configure(
            text=f"Página {self._pch_pagina + 1} de {self._pch_paginas}")
        self.btn_pch_art_prev.configure(
            state="normal" if self._pch_pagina > 0 else "disabled")
        self.btn_pch_art_next.configure(
            state="normal" if self._pch_pagina < self._pch_paginas - 1 else "disabled")

        self._pch_pedir_thumbs_artistas()
        self._pch_sel_artistas()

    def _pch_cambiar_pagina(self, delta: int):
        """Avanza o retrocede páginas del grid de artistas."""
        nueva = self._pch_pagina + delta
        if nueva < 0 or nueva >= self._pch_paginas:
            return
        self._pch_pagina = nueva
        self._pch_render_pagina()

    def _pch_ir_a_pagina(self):
        """Salta a la página escrita en el campo (Enter o botón)."""
        try:
            n = int(self.pch_entry_pag.get().strip())
        except Exception:
            return
        if n < 1 or n > self._pch_paginas:
            return
        self._pch_pagina = n - 1
        self.pch_entry_pag.delete(0, "end")
        self._pch_render_pagina()

    def _create_artist_card(self, idx, service, cid, name, row, col):
        """Crea una tarjeta de artista en el grid."""
        clave = f"creator:{service}:{cid}"
        ph = self._pch_thumb_ctk.get(clave) or self._pch_thumb_imgs.get(clave)

        # Frame de la tarjeta
        card = ctk.CTkFrame(
            self.pch_artists_frame,
            fg_color="#222222",
            corner_radius=10,
            border_width=2,
            border_color="#333333",
            width=170,
            height=210)
        card.grid(row=row, column=col, padx=8, pady=8, sticky="nsew")
        card.grid_propagate(False)
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(0, weight=1)   # la imagen toma el alto libre

        # Imagen de fondo (avatar). Si no hay miniatura se ve la inicial.
        img_label = ctk.CTkLabel(
            card, text=("" if ph is not None else (name[:1] or "?")),
            image=ph, text_color="#555555",
            font=("Segoe UI", 30, "bold"))
        img_label.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)

        # Banda de información (dentro de la tarjeta, encima de la imagen)
        overlay = ctk.CTkFrame(card, fg_color="#111111", corner_radius=8)
        overlay.grid(row=1, column=0, sticky="ew", padx=4, pady=(0, 4))
        overlay.grid_columnconfigure(0, weight=1)

        # Nombre del artista
        name_label = ctk.CTkLabel(
            overlay, text=name[:25], font=("Segoe UI", 13, "bold"),
            text_color="white", wraplength=150, justify="left")
        name_label.grid(row=0, column=0, padx=8, pady=(6, 0), sticky="ew")

        # Servicio e ID
        info_text = f"{service} · {cid}"
        info_label = ctk.CTkLabel(
            overlay, text=info_text, font=("Segoe UI", 10),
            text_color="#cccccc")
        info_label.grid(row=1, column=0, padx=8, pady=(0, 6), sticky="w")

        # Guardar referencia
        self.pch_artist_cards[idx] = {
            "frame": card,
            "img_label": img_label,
            "overlay": overlay,
            "name_label": name_label,
            "info_label": info_label,
            "service": service,
            "cid": cid,
            "name": name,
            "selected": idx in self.pch_selected_indices}
        card.bind("<Button-1>", lambda e, i=idx: self._on_artist_card_click(i, e))
        card.bind("<Double-1>", lambda e, i=idx: self._on_artist_card_double(i, e))
        img_label.bind("<Button-1>", lambda e, i=idx: self._on_artist_card_click(i, e))
        overlay.bind("<Button-1>", lambda e, i=idx: self._on_artist_card_click(i, e))
        name_label.bind("<Button-1>", lambda e, i=idx: self._on_artist_card_click(i, e))
        info_label.bind("<Button-1>", lambda e, i=idx: self._on_artist_card_click(i, e))

        # Programar descarga de miniatura si falta
        if clave not in self._pch_thumb_imgs:
            url = pawchive_api.thumb_creator_url(service, cid)
            self._pch_bajar_thumb(clave, url, 172, 172)

    def _pch_pedir_thumbs_artistas(self):
        """Pide los avatares de las tarjetas de la página actual."""
        for card_data in self.pch_artist_cards.values():
            clave = f"creator:{card_data['service']}:{card_data['cid']}"
            if clave in self._pch_thumb_imgs:
                continue
            url = pawchive_api.thumb_creator_url(card_data["service"],
                                                 card_data["cid"])
            self._pch_bajar_thumb(clave, url, 172, 172)

    def _pch_update_card_style(self, idx):
        """Actualiza el estilo visual de una tarjeta según selección."""
        if idx not in self.pch_artist_cards:
            return
        card_data = self.pch_artist_cards[idx]
        selected = idx in self.pch_selected_indices
        card_data["selected"] = selected
        frame = card_data["frame"]
        overlay = card_data["overlay"]
        if selected:
            frame.configure(border_color=COLORS["ACCENT"], border_width=3)
            overlay.configure(fg_color=COLORS["ACCENT"])
        else:
            frame.configure(border_color="#333333", border_width=2)
            overlay.configure(fg_color="#111111")

    def _on_artist_card_click(self, idx, event=None):
        """Clic en una tarjeta de artista del grid.

        Selecciona la tarjeta y carga sus posts en el panel derecho.
        Con Ctrl/Cmd se añade a la selección (selección múltiple)."""
        if idx < 0 or idx >= len(self._pch_view):
            return
        # Ctrl/Cmd -> selección múltiple; en el resto, solo la tarjeta clicada
        multi = bool(event is not None and (event.state & 0x0004))
        if multi:
            if idx in self.pch_selected_indices:
                self.pch_selected_indices.discard(idx)
            else:
                self.pch_selected_indices.add(idx)
        else:
            self.pch_selected_indices = {idx}
        self._pch_sel_artistas()
        # Con una sola selección cargamos directamente sus posts
        if len(self.pch_selected_indices) == 1:
            self._pch_ver_posts_from_selection()

    def _on_artist_card_double(self, idx, event=None):
        """Doble clic: abre perfil del artista en navegador."""
        if idx not in self.pch_artist_cards:
            return
        card = self.pch_artist_cards[idx]
        webbrowser.open(pawchive_api.creator_url(card["service"], card["cid"]))

    def _pch_ver_posts_from_selection(self):
        """Carga posts del artista seleccionado (desde selección actual)."""
        if len(self.pch_selected_indices) != 1:
            return
        idx = next(iter(self.pch_selected_indices))
        if idx >= len(self._pch_view):
            return
        s, i, n = self._pch_view[idx]
        if self._pch_creator and self._pch_creator[:2] == (s, i):
            return
        self._pch_qglobal = self.pch_entry_posts.get()
        self._pch_creator = (s, i, n)
        self._pch_aplicar_modo()
        self._pch_buscar(0)

    def _pch_sel_artistas(self, _e=None):
        """Actualiza UI según selección actual."""
        for idx in self.pch_artist_cards:
            self._pch_update_card_style(idx)

        n = len(self.pch_selected_indices)
        self.lbl_pch_art_sel.configure(
            text="Sin selección" if not n else
            f"{n} seleccionado{'s' if n != 1 else ''}",
            text_color=COLORS["MUTED"] if not n else COLORS["ACCENT"])

    def _pch_open_artista(self, _e=None):
        """Abre perfil del artista (para compatibilidad, no se usa en grid)."""
        if not self.pch_selected_indices:
            return
        idx = next(iter(self.pch_selected_indices))
        if idx >= len(self._pch_view):
            return
        s, i, _n = self._pch_view[idx]
        webbrowser.open(pawchive_api.creator_url(s, i))

    # ---- modo "posts de un creador" (clic en la lista de artistas) ----
    def _pch_ver_posts(self, _e=None):
        """Clic en un artista -> carga SUS posts en el panel derecho."""
        # Para compatibilidad con el bind antiguo
        self._pch_ver_posts_from_selection()

    def _pch_volver(self):
        """Vuelve a la búsqueda global (recupera la query anterior)."""
        if not self._pch_creator or self._pch_busy:
            return
        self._pch_creator = None
        self._pch_aplicar_modo()
        self._pch_buscar(0)

    def _pch_aplicar_modo(self):
        """Cabecera, desplegable de servicio y campo según el modo."""
        if self._pch_creator:
            nombre = self._pch_creator[2]
            self.lbl_pch_titulo.configure(
                text=f"\U0001f3a8 Posts de {nombre}")
            self.btn_pch_volver.pack(side="left", padx=(10, 0))
            self.pch_svc_posts_menu.configure(state="disabled")
            self.pch_entry_posts.configure(
                placeholder_text=f"Buscar en los posts de {nombre}…")
            self.pch_entry_posts.delete(0, "end")
        else:
            self.lbl_pch_titulo.configure(
                text="\U0001f50d Buscador de posts")
            self.btn_pch_volver.pack_forget()
            self.pch_svc_posts_menu.configure(state="normal")
            self.pch_entry_posts.configure(
                placeholder_text="Buscar por título o texto…")
            self.pch_entry_posts.delete(0, "end")
            if self._pch_qglobal:
                self.pch_entry_posts.insert(0, self._pch_qglobal)

    def _pch_add_creators(self):
        if not self.pch_selected_indices:
            messagebox.showinfo("Artistas",
                                "Selecciona al menos un artista de la lista.",
                                parent=self)
            return
        sel = [self._pch_view[idx] for idx in self.pch_selected_indices
               if 0 <= idx < len(self._pch_view)]
        if not sel:
            messagebox.showinfo("Artistas",
                                "Selecciona al menos un artista de la lista.",
                                parent=self)
            return
        actuales = pawchive_api.load_creators()
        antes = {(c["service"], c["id"]) for c in actuales}
        nuevos = [{"service": s, "id": i} for s, i, _n in sel
                  if (s, i) not in antes]
        if not nuevos:
            messagebox.showinfo("Artistas",
                                "Ya están todos en tu lista.",
                                parent=self)
            return
        pawchive_api.save_creators(actuales + nuevos)
        self._pch_creadores_cambio(
            f"\U0001f43e {len(nuevos)} artista(s) a\u00f1adido(s)")
        if self.pch_solo_var.get():
            self._pch_fill_artistas()

    def _pch_rem_creators(self):
        if not self.pch_selected_indices:
            messagebox.showinfo("Artistas",
                                "Selecciona al menos un artista de la lista.",
                                parent=self)
            return
        sel = set()
        for idx in self.pch_selected_indices:
            if 0 <= idx < len(self._pch_view):
                s, i, _n = self._pch_view[idx]
                sel.add((s, i))
        if not sel:
            messagebox.showinfo("Artistas",
                                "Ninguno de los seleccionados está en tu lista.",
                                parent=self)
            return
        actuales = pawchive_api.load_creators()
        restantes = [c for c in actuales
                     if (c["service"], c["id"]) not in sel]
        if len(restantes) == len(actuales):
            messagebox.showinfo(
                "Artistas",
                "Ninguno de los seleccionados está en tu lista.",
                parent=self)
            return
        pawchive_api.save_creators(restantes)
        self._pch_creadores_cambio(
            f"\U0001f43e {len(actuales) - len(restantes)} artista(s) "
            "quitado(s)")
        if self.pch_solo_var.get():
            self._pch_fill_artistas()

    def _pch_creadores_cambio(self, que):
        n = len(pawchive_api.load_creators())
        if self.dash and getattr(self.dash, "btn_creators", None):
            self.dash.btn_creators.configure(
                text=f"\U0001f43e Creadores Pawchive ({n})")
        self.log(f"{que} \u2014 tienes {n} creador(es).", "ok")

    # ---- buscador de posts (global o de UN creador) ----
    def _pch_buscar(self, page=0):
        if self._pch_busy or page < 0:
            return
        q = self.pch_entry_posts.get().strip()
        self._pch_busy = True
        _set_loading(self.btn_pch_buscar, True, " Buscar")
        if self._pch_creator:
            # Dentro de un creador: /{service}/user/{id}?q=…&o=…
            svc, cid, nombre = self._pch_creator
            self.lbl_pch_posts.configure(
                text=f"\u23f3 Cargando posts de {nombre} "
                     f"(p\u00e1gina {page + 1})\u2026",
                text_color=COLORS["WARN"])
            params = []
            if q:
                params.append("q=" + quote(q))
            params.append("o=" + str(page * PCH_POST_LOTE))
            rel = f"/{svc}/user/{cid}?" + "&".join(params)
        else:
            self.lbl_pch_posts.configure(
                text=f"\u23f3 Buscando\u2026 (p\u00e1gina {page + 1})",
                text_color=COLORS["WARN"])
            svc = PCH_SVC_LABELS.get(self.pch_svc_posts_var.get(), "")
            params = []
            if q:
                params.append("q=" + quote(q))
            if svc:
                params.append("service=" + svc)
            params.append("o=" + str(page * PCH_POST_LOTE))
            rel = "/posts?" + "&".join(params)
        threading.Thread(target=self._pch_posts_worker,
                         args=(page, rel), daemon=True).start()

    def _pch_posts_worker(self, page, rel):
        try:
            d = pawchive_api.fetch_json(rel)
            self._pch_q.put(("posts", page,
                             d if isinstance(d, list) else []))
        except Exception as e:
            self._pch_q.put(("posts_err", str(e)))

    def _pch_fill_posts(self, page, posts):
        """Llega un bloque de posts (hasta 50) del servidor -> se pintan sus
        tarjetas por páginas de 9."""
        self._pch_busy = False
        self._pch_posts = posts
        self._pch_page = page
        _set_loading(self.btn_pch_buscar, False, " Buscar")
        self.lbl_pch_posts.configure(
            text=f"{len(posts)} posts", text_color=COLORS["MUTED"])
        # ¿veníamos de un salto a una página concreta?
        objetivo = self._pch_post_objetivo
        self._pch_post_objetivo = None
        if objetivo is None:
            self._pch_post_pag = 0      # bloque nuevo -> primera página local
        self._pch_render_posts(objetivo)

    # ---------------- PAGINACIÓN DEL GRID DE POSTS ----------------
    def _pch_render_posts(self, objetivo=None):
        """Pinta las 9 tarjetas de posts de la página local actual.

        `objetivo` = página local (0-based) a la que saltar; si es None se
        respeta la página actual."""
        frame = getattr(self, "pch_posts_frame", None)
        if frame is None or not frame.winfo_exists():
            return
        with _ui_rapida():
            self._pch_render_posts_raw(objetivo)

    def _pch_render_posts_raw(self, objetivo=None):
        frame = self.pch_posts_frame
        for widget in frame.winfo_children():
            widget.destroy()
        self.pch_post_cards.clear()

        total = len(self._pch_posts)
        self._pch_post_pags = max(1, -(-total // PCH_POST_POR_PAGINA))
        if objetivo is not None:
            if objetivo < 0:
                # -1 = última página del bloque (al retroceder de bloque)
                self._pch_post_pag = self._pch_post_pags - 1
            else:
                self._pch_post_pag = max(0, min(int(objetivo),
                                                self._pch_post_pags - 1))
        else:
            self._pch_post_pag = max(0, min(self._pch_post_pag,
                                            self._pch_post_pags - 1))

        ini = self._pch_post_pag * PCH_POST_POR_PAGINA
        fin = min(ini + PCH_POST_POR_PAGINA, total)
        for k, idx in enumerate(range(ini, fin)):
            self._create_post_card(idx, self._pch_posts[idx],
                                   k // PCH_POST_COLS, k % PCH_POST_COLS)

        # Barra de paginación: numeración global (bloque*6 + local)
        num = self._pch_page * PCH_POST_PAGS_BLOQUE + self._pch_post_pag + 1
        total_pags = self._pch_page * PCH_POST_PAGS_BLOQUE + self._pch_post_pags
        if total == 0:
            self.lbl_pch_post_pag.configure(text="—")
        else:
            self.lbl_pch_post_pag.configure(
                text=f"Página {num} de {total_pags}")
        hay_mas = (self._pch_post_pag < self._pch_post_pags - 1
                   or len(self._pch_posts) >= PCH_POST_LOTE)
        self.btn_pch_post_prev.configure(
            state="normal" if (self._pch_post_pag > 0
                               or self._pch_page > 0) else "disabled")
        self.btn_pch_post_next.configure(
            state="normal" if hay_mas else "disabled")

        self._pch_sel_posts()
        self._pch_descargar_posts_faltantes()

    def _pch_post_cambiar_pagina(self, delta: int):
        """Avanza o retrocede páginas de tarjetas de posts.

        Al llegar al final de un bloque se pide el siguiente bloque al
        servidor (la API devuelve 50 posts por petición)."""
        if self._pch_busy:
            return
        if delta > 0:
            if self._pch_post_pag + 1 < self._pch_post_pags:
                self._pch_post_pag += 1
                self._pch_render_posts()
                return
            if len(self._pch_posts) >= PCH_POST_LOTE:
                self._pch_post_objetivo = 0
                self._pch_buscar(self._pch_page + 1)
            return
        # delta < 0
        if self._pch_post_pag > 0:
            self._pch_post_pag -= 1
            self._pch_render_posts()
            return
        if self._pch_page > 0:
            self._pch_post_objetivo = -1      # última página del bloque
            self._pch_buscar(self._pch_page - 1)

    def _pch_post_ir_a_pagina(self):
        """Salta a la página escrita en el campo (Enter)."""
        try:
            n = int(self.pch_entry_post_pag.get().strip())
        except Exception:
            return
        if n < 1:
            return
        bloque = (n - 1) // PCH_POST_PAGS_BLOQUE
        local = (n - 1) % PCH_POST_PAGS_BLOQUE
        self.pch_entry_post_pag.delete(0, "end")
        if bloque == self._pch_page:
            self._pch_post_pag = local
            self._pch_render_posts()
            return
        self._pch_post_objetivo = local
        self._pch_buscar(bloque)

    def _create_post_card(self, idx, post, row, col):
        """Crea una tarjeta de post en el grid (3x3)."""
        svc = str(post.get("service") or "")
        cid = str(post.get("user") or "")
        pid = str(post.get("id") or "")
        clave = f"post:{svc}:{cid}:{pid}"
        titulo = (str(post.get("title") or "").strip() or f"post {pid}")
        creador = self._pch_nom.get((svc, cid), cid)
        fecha = str(post.get("published") or "")[:10]
        counts = pawchive_api.count_by_type_list(post)
        ph = self._pch_thumb_ctk.get(clave) or self._pch_thumb_imgs.get(clave)

        card = ctk.CTkFrame(
            self.pch_posts_frame, fg_color="#222222", corner_radius=10,
            border_width=2, border_color="#333333", width=170, height=230)
        card.grid(row=row, column=col, padx=8, pady=8, sticky="nsew")
        card.grid_propagate(False)
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(0, weight=1)   # la imagen toma el alto libre

        # Portada del post (si no hay miniatura: la inicial del título)
        img_label = ctk.CTkLabel(
            card, text=("" if ph is not None else (titulo[:1] or "?")),
            image=ph, text_color="#555555",
            font=("Segoe UI", 30, "bold"))
        img_label.grid(row=0, column=0, sticky="nsew", padx=4, pady=4)

        # Banda de información
        overlay = ctk.CTkFrame(card, fg_color="#111111", corner_radius=8)
        overlay.grid(row=1, column=0, sticky="ew", padx=4, pady=(0, 4))
        overlay.grid_columnconfigure(0, weight=1)

        title_label = ctk.CTkLabel(
            overlay, text=titulo[:60], font=("Segoe UI", 12, "bold"),
            text_color="white", wraplength=150, justify="left",
            anchor="w")
        title_label.grid(row=0, column=0, padx=8, pady=(6, 0), sticky="ew")

        info_label = ctk.CTkLabel(
            overlay, text=f"{creador[:18]} · {fecha}",
            font=("Segoe UI", 10), text_color="#cccccc", anchor="w")
        info_label.grid(row=1, column=0, padx=8, pady=(0, 0), sticky="ew")

        counts_label = ctk.CTkLabel(
            overlay,
            text=(f"{svc} · 🖼{counts['imagen']}  🎬{counts['video']}"
                  f"  📁{counts['archivo']}"),
            font=("Segoe UI", 10), text_color="#9a9a9a", anchor="w")
        counts_label.grid(row=2, column=0, padx=8, pady=(0, 6), sticky="ew")

        self.pch_post_cards[idx] = {
            "frame": card,
            "img_label": img_label,
            "overlay": overlay,
            "title_label": title_label,
            "info_label": info_label,
            "counts_label": counts_label,
            "service": svc,
            "cid": cid,
            "pid": pid,
            "titulo": titulo,
            "selected": pid in self._pch_acum,
        }
        for w in (card, img_label, overlay, title_label, info_label,
                  counts_label):
            w.bind("<Button-1>", lambda e, i=idx: self._pch_post_click(i))
            w.bind("<Double-1>", lambda e, i=idx: self._pch_open_post(i))
            w.bind("<Motion>", lambda e, i=idx: self._pch_on_post_motion(i))
            w.bind("<Leave>", self._pch_hide_preview)
        # La portada se recorta al tamaño real de la tarjeta (al redimensionar)
        img_label.bind("<Configure>",
                       lambda e, i=idx: self._pch_post_ajustar_imagen(i))

        # Descargar la portada si falta (en hilo aparte)
        if clave not in self._pch_thumb_imgs:
            url = pawchive_api.thumb_post_url(post)
            if url:
                self._pch_bajar_thumb(clave, url, 150, 150)

    def _pch_post_click(self, idx):
        """Clic en una tarjeta de post -> la marca/desmarca para importar."""
        if idx not in self.pch_post_cards:
            return
        card = self.pch_post_cards[idx]
        pid = card["pid"]
        if pid in self._pch_acum:
            self._pch_acum.pop(pid, None)
        else:
            self._pch_acum[pid] = self._pch_posts[idx]
        self._pch_sel_posts()
        # Después de cambiar la selección, ajustar la imagen al tamaño
        # actual de la tarjeta (por si la ventana se redimensionó o la
        # tarjeta cambió de ancho por cualquier motivo).
        self._pch_post_ajustar_imagen(idx)

    def _pch_post_style(self, idx):
        """Pinta el estilo de la tarjeta según esté seleccionada o no."""
        if idx not in self.pch_post_cards:
            return
        card = self.pch_post_cards[idx]
        sel = card["pid"] in self._pch_acum
        card["selected"] = sel
        if sel:
            card["frame"].configure(border_color=COLORS["ACCENT"],
                                    border_width=3)
            card["overlay"].configure(fg_color=COLORS["ACCENT"])
        else:
            card["frame"].configure(border_color="#333333", border_width=2)
            card["overlay"].configure(fg_color="#111111")

    # ---- descarga de miniaturas (avatares artistas + thumbs posts) ----
    def _pch_descargar_artistas_faltantes(self, out):
        """Dispara la descarga en segundo plano de los avatares que faltan."""
        for s, i, _n in out:
            clave = f"creator:{s}:{i}"
            if clave in self._pch_thumb_imgs:
                continue
            url = pawchive_api.thumb_creator_url(s, i)
            self._pch_bajar_thumb(clave, url, 172, 172)

    def _pch_descargar_posts_faltantes(self):
        """Dispara la descarga en segundo plano de las portadas de los posts
        que faltan en la página actual."""
        for idx, card in self.pch_post_cards.items():
            clave = f"post:{card['service']}:{card['cid']}:{card['pid']}"
            if clave in self._pch_thumb_imgs:
                continue
            if not (0 <= idx < len(self._pch_posts)):
                continue
            url = pawchive_api.thumb_post_url(self._pch_posts[idx])
            if url:
                self._pch_bajar_thumb(clave, url, 150, 150)

    def _pch_bajar_thumb(self, clave, url, w, h):
        """Pide una miniatura: primero la caché en disco, si no, la red.

        La descarga va en un hilo aparte, pero la creación del `PhotoImage`
        SIEMPRE en el hilo principal de Tk (crear objetos de Tk desde otro
        hilo es ilegal y Tk lo rechaza en silencio).
        """
        with self._pch_thumb_lock:
            if clave in self._pch_thumb_imgs:
                return
            if clave in self._pch_thumb_pendientes:
                return          # ya hay una descarga en curso para esa clave
            self._pch_thumb_pendientes.add(clave)
            self._pch_thumb_sizes[clave] = (w, h)

        # 1) ¿ya la tenemos en la caché de disco? -> al instante y sin red
        try:
            ruta = pawchive_api.thumb_path(clave)
        except Exception:
            ruta = None
        if ruta is not None and ruta.exists():
            try:
                png = ruta.read_bytes()
            except Exception:
                png = b""
            if png:
                self._pch_thumb_listo(clave, png)
                return

        # 2) red (máx. 6 descargas simultáneas)
        with self._pch_thumb_lock:
            activos = sum(1 for t in self._pch_thumb_hilos if t.is_alive())
        if activos >= 6:
            with self._pch_thumb_lock:
                self._pch_thumb_pendientes.discard(clave)
            return
        hilo = threading.Thread(target=self._pch_bajar_thumb_worker,
                                args=(clave, url, w, h), daemon=True)
        self._pch_thumb_hilos.append(hilo)
        hilo.start()

    def _pch_bajar_thumb_worker(self, clave, url, w, h):
        """Hilo auxiliar: descarga + recorta la miniatura (nada de Tk)."""
        try:
            png = self._pch_thumb_preparar(clave, url, w, h)
        except Exception as e:
            # La cola es segura entre hilos: el PhotoImage se crea después
            # en el hilo principal (crearlo aquí sería ilegal en Tk).
            self._pch_q.put(("thumb_err", clave, str(e)))
            return
        self._pch_q.put(("thumb", clave, png))

    @staticmethod
    def _pch_thumb_preparar(clave: str, url: str, w: int, h: int) -> bytes:
        """Descarga la miniatura, la recorta y la devuelve como PNG.

        Solo trabaja con Pillow (sin Tk): se puede ejecutar en un hilo.

        - Creadores: se ajustan en un cuadrado con fondo (el avatar se ve
          completo).
        - Posts: **no se fuerza cuadrado**. Se thumbnailean al ancho `w`
          manteniendo la relación de aspecto original. El recorte al tamaño
          exacto de la tarjeta se hace al pintar (_pch_post_ajustar_imagen),
          así la imagen Llena la tarjeta sin deformarse.
        """
        from PIL import Image, ImageOps   # PIL es dependencia del proyecto
        datos = pawchive_api.fetch_thumb(url, timeout=30)
        img = Image.open(io.BytesIO(datos)).convert("RGB")
        img = ImageOps.exif_transpose(img) or img
        if clave.startswith("creator:"):
            img.thumbnail((w, h))
            fondo = Image.new("RGB", (w, h), (26, 26, 26))
            x = (w - img.width) // 2
            y = (h - img.height) // 2
            fondo.paste(img, (x, y))
            img = fondo
        # Para posts: thumbnail simple, sin fondo cuadrado.
        # El ajuste al rectángulo de la tarjeta se hará al pintar.
        else:
            img.thumbnail((w, h))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        png = buf.getvalue()
        # caché en disco: la próxima vez se pinta sin volver a la red
        try:
            pawchive_api.THUMB_DIR.mkdir(parents=True, exist_ok=True)
            pawchive_api.thumb_path(clave).write_bytes(png)
        except Exception:
            pass
        return png

    def _pch_thumb_listo(self, clave, png):
        """Hilo principal: crea el PhotoImage y lo pinta."""
        with self._pch_thumb_lock:
            self._pch_thumb_pendientes.discard(clave)
            self._pch_thumb_errores.pop(clave, None)
        try:
            ph = tk.PhotoImage(master=self, data=base64.b64encode(png))
        except Exception as e:
            self._pch_thumb_error(clave, e)
            return
        with self._pch_thumb_lock:
            self._pch_thumb_imgs[clave] = ph
        # Las tarjetas usan CTkImage (escala bien en pantallas HiDPI).
        try:
            from PIL import Image
            w, h = self._pch_thumb_sizes.get(clave, (172, 172))
            img = Image.open(io.BytesIO(png)).convert("RGB")
            with self._pch_thumb_lock:
                self._pch_thumb_pil[clave] = img
                self._pch_thumb_ctk[clave] = ctk.CTkImage(
                    light_image=img, dark_image=img, size=(w, h))
        except Exception as e:
            self._pch_thumb_error(clave, e)
        self._pch_aplicar_thumb(clave)

    def _pch_thumb_error(self, clave, e):
        """La miniatura no se pudo cargar: se avisa (una vez por clave)."""
        with self._pch_thumb_lock:
            self._pch_thumb_pendientes.discard(clave)
            nuevo = clave not in self._pch_thumb_errores
            self._pch_thumb_errores[clave] = str(e)
        if not nuevo:
            return
        try:
            self.log(f"[miniatura] {clave}: {e}", "warn")
        except Exception:
            pass
        # en la tarjeta se ve la inicial (el placeholder) en vez de la imagen
        if clave.startswith("creator:"):
            for card_data in self.pch_artist_cards.values():
                card_clave = f"creator:{card_data['service']}:{card_data['cid']}"
                if card_clave == clave:
                    card_data["img_label"].configure(
                        image=None, text=(card_data["name"][:1] or "?"))
        elif clave.startswith("post:"):
            for card_data in self.pch_post_cards.values():
                card_clave = (f"post:{card_data['service']}:"
                              f"{card_data['cid']}:{card_data['pid']}")
                if card_clave == clave:
                    card_data["img_label"].configure(
                        image=None, text=(card_data["titulo"][:1] or "?"))

    def _pch_aplicar_thumb(self, clave):
        """Asigna la miniatura ya descargada a las tarjetas que la usan."""
        ph = self._pch_thumb_imgs.get(clave)
        if ph is None:
            return
        # Actualizar tarjetas de artistas
        if clave.startswith("creator:"):
            ctk_img = self._pch_thumb_ctk.get(clave)
            for idx, card_data in self.pch_artist_cards.items():
                card_clave = f"creator:{card_data['service']}:{card_data['cid']}"
                if card_clave == clave:
                    try:
                        card_data["img_label"].configure(
                            image=ctk_img if ctk_img is not None else ph,
                            text="")
                    except Exception:
                        pass        # la tarjeta ya no existe (página cambiada)
        # Actualizar portadas de los posts
        elif clave.startswith("post:"):
            for idx, card_data in self.pch_post_cards.items():
                card_clave = (f"post:{card_data['service']}:"
                              f"{card_data['cid']}:{card_data['pid']}")
                if card_clave == clave:
                    self._pch_post_ajustar_imagen(idx)

    def _pch_post_ajustar_imagen(self, idx):
        """Recorta la portada al tamaño real de la tarjeta (sin deformar).

        La imagen se guarda cuadrada en la caché; aquí se recorta al ancho
        y alto que tiene la etiqueta, así la portada LLENA la tarjeta."""
        card = self.pch_post_cards.get(idx)
        if card is None:
            return
        lab = card["img_label"]
        try:
            if not lab.winfo_exists():
                return
            w = lab.winfo_width()
            h = lab.winfo_height()
        except Exception:
            return
        if w < 20 or h < 20:
            return
        if card.get("img_w") == w and card.get("img_h") == h:
            return
        card["img_w"], card["img_h"] = w, h
        clave = (f"post:{card['service']}:{card['cid']}:{card['pid']}")
        pil = self._pch_thumb_pil.get(clave)
        if pil is None:
            return
        try:
            from PIL import ImageOps
            escala = ctk.ScalingTracker.get_widget_scaling(lab) or 1.0
            destino = (max(1, int(w / escala)), max(1, int(h / escala)))
            recortada = ImageOps.fit(pil, destino, method=Image.LANCZOS,
                                     centering=(0.5, 0.3))
            ctk_img = ctk.CTkImage(light_image=recortada,
                                   dark_image=recortada, size=destino)
            card["ctk"] = ctk_img
            lab.configure(image=ctk_img, text="")
        except Exception:
            pass

    def _pch_sel_posts(self, _e=None):
        """Refleja la selección ACUMULADA (`_pch_acum`) en las tarjetas.

        `acum` es la fuente de verdad: sobrevive a los cambios de página y
        de búsqueda (clave: id del post)."""
        for idx in list(self.pch_post_cards):
            self._pch_post_style(idx)
        n = len(self._pch_acum)
        self.lbl_pch_imp.configure(
            text="Sin selección" if not n else
            f"{n} seleccionado{'s' if n != 1 else ''}",
            text_color=COLORS["MUTED"] if not n else COLORS["ACCENT"])
        self.btn_pch_imp.configure(
            state="normal" if (n and not self._pch_busy) else "disabled")

    def _pch_open_post(self, idx):
        """Doble clic en una tarjeta -> abre el post en el navegador."""
        if not isinstance(idx, int) or not (0 <= idx < len(self._pch_posts)):
            return
        p = self._pch_posts[idx]
        webbrowser.open(pawchive_api.post_url(
            str(p.get("service") or ""), str(p.get("user") or ""),
            str(p.get("id") or "")))

    # ---- Panel de preview flotante (hover en tarjeta de post) ----
    def _pch_on_post_motion(self, idx):
        """Muestra el preview al pasar el ratón por una tarjeta."""
        if not (0 <= idx < len(self._pch_posts)):
            self._pch_hide_preview()
            return
        if (self._pch_preview_idx == idx and self._pch_preview_win
                and self._pch_preview_win.winfo_exists()):
            return  # ya visible para esta tarjeta
        if self._pch_preview_after:
            try:
                self.after_cancel(self._pch_preview_after)
            except Exception:
                pass
        self._pch_hide_preview()
        # El detalle del post se pide en un hilo (la API va lento): la
        # ventana se pinta en el hilo principal cuando llega la respuesta.
        self._pch_preview_after = self.after(
            150, lambda: self._pch_show_preview(idx))

    def _pch_show_preview(self, idx):
        """Pide el detalle del post (en hilo) y pinta el preview."""
        if not (0 <= idx < len(self._pch_posts)):
            return
        if idx in self._pch_preview_busy:
            return          # ya hay una petición en curso para esta tarjeta
        p = self._pch_posts[idx]
        svc = str(p.get("service") or "")
        cid = str(p.get("user") or "")
        pid = str(p.get("id") or "")
        self._pch_preview_idx = idx
        self._pch_preview_busy.add(idx)
        threading.Thread(target=self._pch_preview_worker,
                         args=(idx, svc, cid, pid), daemon=True).start()

    def _pch_preview_worker(self, idx, svc, cid, pid):
        """Hilo: descarga el detalle del post (nada de Tk aquí)."""
        try:
            detalle = pawchive_api.fetch_post(svc, cid, pid)
            archivos = pawchive_api.all_files(detalle)
        except Exception:
            archivos = []
        self._pch_q.put(("preview", idx, archivos))

    def _pch_crear_preview(self, idx, archivos):
        """Hilo principal: crea la ventana flotante de preview del post."""
        self._pch_preview_busy.discard(idx)
        if not (0 <= idx < len(self._pch_posts)):
            return
        # Si el puntero ya no está sobre la tarjeta, no se abre
        if self._pch_preview_idx != idx:
            return
        p = self._pch_posts[idx]
        svc = str(p.get("service") or "")
        cid = str(p.get("user") or "")
        pid = str(p.get("id") or "")
        titulo = (str(p.get("title") or "").strip() or f"post {pid}")
        # Destruir el preview anterior si existe
        self._pch_hide_preview_ventana()
        self._pch_preview_idx = idx
        win = ctk.CTkToplevel(self)
        self._pch_preview_win = win
        win.wm_overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(fg_color=COLORS["CARD_BG"])
        frame = ctk.CTkFrame(win, fg_color=COLORS["CARD_BG"], corner_radius=8,
                             border_width=1, border_color=COLORS["BORDER"])
        frame.pack(padx=1, pady=1)
        ctk.CTkLabel(frame, text=titulo[:80], font=("Segoe UI", 12, "bold"),
                     text_color=COLORS["TEXT"], wraplength=300).pack(
            padx=12, pady=(10, 6), anchor="w")
        creador = self._pch_nom.get((svc, cid), cid)
        fecha = str(p.get("published") or "")[:10]
        counts = pawchive_api.count_by_type_list(p)
        info = f"{creador}  •  {fecha}  •  {svc}"
        info += f"  •  🖼 {counts['imagen']}  🎬 {counts['video']}  📁 {counts['archivo']}"
        ctk.CTkLabel(frame, text=info, font=("Segoe UI", 11),
                     text_color=COLORS["MUTED"]).pack(padx=12, anchor="w")
        ctk.CTkFrame(frame, height=1, fg_color=COLORS["BORDER"]).pack(
            fill="x", padx=8, pady=6)
        if archivos:
            for a in archivos[:12]:      # máx 12
                icon = {"imagen": "🖼", "video": "🎬",
                        "archivo": "📁"}.get(a["tipo"], "📄")
                name = a["nombre"][:50]
                ctk.CTkLabel(frame, text=f"{icon}  {name}", font=("Segoe UI", 11),
                             text_color=COLORS["TEXT"], anchor="w").pack(
                    padx=12, pady=1, anchor="w", fill="x")
            if len(archivos) > 12:
                ctk.CTkLabel(frame, text=f"... y {len(archivos) - 12} más",
                             font=("Segoe UI", 10),
                             text_color=COLORS["MUTED"]).pack(
                    padx=12, pady=2, anchor="w")
        else:
            ctk.CTkLabel(frame, text="(sin archivos accesibles)",
                         font=("Segoe UI", 11),
                         text_color=COLORS["MUTED"]).pack(
                padx=12, pady=4, anchor="w")
        ctk.CTkFrame(frame, height=1, fg_color=COLORS["BORDER"]).pack(
            fill="x", padx=8, pady=6)
        ctk.CTkButton(frame, text=" Abrir en pawchive", height=28,
                      font=("Segoe UI", 11), fg_color="#3a3a3a",
                      hover_color="#4a4a4a",
                      command=lambda: webbrowser.open(
                          pawchive_api.post_url(svc, cid, pid))).pack(
            padx=12, pady=(0, 10))
        # Posicionar a la derecha de la tarjeta
        win.update_idletasks()
        card = self.pch_post_cards.get(idx)
        if card is not None:
            try:
                x = card["frame"].winfo_rootx() + card["frame"].winfo_width() + 10
                y = card["frame"].winfo_rooty()
            except Exception:
                x = self.winfo_pointerx() + 20
                y = self.winfo_pointery() + 10
        else:
            x = self.winfo_pointerx() + 20
            y = self.winfo_pointery() + 10
        pw, ph = win.winfo_width(), win.winfo_height()
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        if x + pw > sw:
            x = max(0, sw - pw - 10)
        if y + ph > sh:
            y = max(0, sh - ph - 10)
        win.geometry(f"+{x}+{y}")

    def _pch_hide_preview_ventana(self):
        """Destruye solo la ventana de preview (sin tocar los timers)."""
        if self._pch_preview_win and self._pch_preview_win.winfo_exists():
            try:
                self._pch_preview_win.destroy()
            except Exception:
                pass
        self._pch_preview_win = None

    def _pch_hide_preview(self, event=None):
        """Oculta el panel de preview."""
        if self._pch_preview_after:
            try:
                self.after_cancel(self._pch_preview_after)
            except Exception:
                pass
            self._pch_preview_after = None
        self._pch_hide_preview_ventana()
        self._pch_preview_idx = None

    def _pch_todo_posts(self):
        """Selecciona todos los posts de la página actual."""
        for idx, card in self.pch_post_cards.items():
            if not (0 <= idx < len(self._pch_posts)):
                continue
            self._pch_acum[card["pid"]] = self._pch_posts[idx]
        self._pch_sel_posts()

    def _pch_limpiar_posts(self):
        self.pch_entry_posts.delete(0, "end")
        frame = getattr(self, "pch_posts_frame", None)
        if frame is not None and frame.winfo_exists():
            for widget in frame.winfo_children():
                widget.destroy()
        self.pch_post_cards.clear()
        self._pch_posts = []
        self._pch_page = 0
        self._pch_post_pag = 0
        self._pch_post_pags = 1
        self._pch_acum.clear()          # la selección acumulada también
        self.lbl_pch_post_pag.configure(text="Página 1 de 1")
        self.btn_pch_post_prev.configure(state="disabled")
        self.btn_pch_post_next.configure(state="disabled")
        self.lbl_pch_posts.configure(text="—", text_color=COLORS["MUTED"])
        self._pch_sel_posts()

    def _pch_importar(self):
        # Toda la selección acumulada (varias páginas/creadores)
        sel = list(self._pch_acum.values())
        if not sel:
            messagebox.showinfo("Importar",
                                "Selecciona posts en la lista.",
                                parent=self)
            return
        if self._pch_busy:
            return
        if not messagebox.askyesno(
                "Importar a la BD",
                f"Se importar\u00e1n {len(sel)} post(s) a la base de datos "
                "(t\u00edtulo, URL, p\u00e1ginas y tags \u2014 solo "
                "metadatos).\n\nLos que ya existan se omiten.\n"
                "\u00bfContinuar?", parent=self):
            return
        self._pch_busy = True
        _set_loading(self.btn_pch_imp, True, " Importar seleccionados a la BD")
        self.lbl_pch_imp.configure(
            text=f"\u23f3 Importando 0/{len(sel)}\u2026", text_color=COLORS["WARN"])
        self.log(f"\U0001f43e Importando {len(sel)} post(s) "
                 "seleccionados a la BD\u2026")
        threading.Thread(target=pch_importar_posts,
                         args=(sel, dict(self._pch_nom), self._pch_q),
                         daemon=True).start()

    def _pch_import_fin(self, stats):
        self._pch_busy = False
        # Primero el botón según la selección; el resumen se pinta DESDE
        # para que _pch_sel_posts() no lo sobrescriba con "N seleccionados".
        self._pch_sel_posts()
        _set_loading(self.btn_pch_imp, False, " Importar seleccionados a la BD")
        if stats.get("error"):
            self.lbl_pch_imp.configure(text=f"\u274c {stats['error']}",
                                       text_color=COLORS["DANGER"])
            self.log(f"[X] Importaci\u00f3n fallida: {stats['error']}")
        else:
            resumen = (f"\u2705 {stats['nuevos']} nuevos \u00b7 "
                       f"\u21a9\ufe0f {stats['existentes']} existentes \u00b7 "
                       f"\u26a0\ufe0f {stats['duplicados']} duplicados \u00b7 "
                       f"\U0001f6ab {stats['sin_img']} sin im\u00e1genes \u00b7 "
                       f"\u274c {stats['errores']} errores")
            ok = stats["nuevos"] or stats["existentes"]
            self.lbl_pch_imp.configure(
                text=resumen,
                text_color=COLORS["SUCCESS"] if ok and not stats["errores"] else COLORS["WARN"])
            self.log(f"\U0001f43e Importaci\u00f3n: {resumen}",
                     "ok" if not stats["errores"] else None)
            for f in stats.get("fallos", [])[:8]:
                self.log(f"   [X] {f}")
        self.refresh_table()
        if self.dash:
            self.dash.refresh_stats()

    # ---------------- BASE DE DATOS ----------------
    def _db(self):
        return mysql.connector.connect(**DB_CONFIG)

    def refresh_table(self):
        if not hasattr(self, "tree"):
            return          # la pestaña Colección aún no está construida
        try:
            conn = self._db()
            cur = conn.cursor()
            # Un solo pase: LEFT JOIN con el recuento de páginas por cómic
            # (antes: subconsulta correlacionada = un escaneo por cómic).
            # Solo los cómics de ESTE sitio (la ventana es de un sitio).
            cur.execute(
                "SELECT c.id, c.titulo, c.estado, c.sitio, "
                "c.n_paginas_web, COALESCE(p.n, 0), c.url_comic "
                "FROM comics c "
                "LEFT JOIN (SELECT comic_id, COUNT(*) AS n FROM paginas "
                "           GROUP BY comic_id) p ON p.comic_id = c.id "
                "WHERE c.sitio = %s "
                "ORDER BY c.id DESC",
                (self.sitio,)
            )
            self.all_rows = cur.fetchall()
            cur.close()
            conn.close()
        except Exception as e:
            self.log(f"[BD] Error: {e}")
            self.status_var.set("Sin conexión con MySQL (¿XAMPP activo?)")
            self._db_ok = False
            self._update_empty()
            return
        self._db_ok = True
        self.fill_table()

    def fetch_stats(self) -> dict:
        """Estadísticas de ESTE sitio (tarjetas + barras + gráfico)."""
        t0 = time.perf_counter()
        try:
            conn = self._db()
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*), "
                "COALESCE(SUM(estado='descargado'),0), "
                "COALESCE(SUM(estado='pendiente'),0), "
                "COALESCE(SUM(estado='duplicado'),0) "
                "FROM comics WHERE sitio = %s", (self.sitio,))
            total, desc, pend, dup = cur.fetchone()
            cur.execute(
                "SELECT COUNT(*) FROM paginas p "
                "JOIN comics c ON c.id = p.comic_id "
                "WHERE c.sitio = %s", (self.sitio,))
            paginas = cur.fetchone()[0]
            cur.execute(
                "SELECT COUNT(DISTINCT ce.etiqueta_id) "
                "FROM comic_etiquetas ce "
                "JOIN comics c ON c.id = ce.comic_id "
                "WHERE c.sitio = %s", (self.sitio,))
            etiquetas = cur.fetchone()[0]
            cur.execute(
                "SELECT sitio, COUNT(*), "
                "COALESCE(SUM(n_paginas_web IS NOT NULL),0) "
                "FROM comics WHERE sitio = %s "
                "GROUP BY sitio", (self.sitio,))
            sitios = [(s, int(n), int(c)) for s, n, c in cur.fetchall()]
            # Descargas de los últimos 14 días (gráfico de actividad)
            cur.execute(
                "SELECT fecha_descarga, COUNT(*) FROM comics "
                "WHERE sitio = %s "
                "AND fecha_descarga >= CURDATE() - INTERVAL 13 DAY "
                "GROUP BY fecha_descarga ORDER BY fecha_descarga",
                (self.sitio,))
            descargas_dias = [(str(f), int(n)) for f, n in cur.fetchall()]
            cur.close()
            conn.close()
            return {
                "ok": True,
                "ms": int((time.perf_counter() - t0) * 1000),
                "total": int(total),
                "descargados": int(desc),
                "pendientes": int(pend),
                "duplicados": int(dup),
                "paginas": int(paginas),
                "etiquetas": int(etiquetas),
                "sitios": sitios,
                "descargas_dias": descargas_dias,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---------------- TABLA (filtro, orden, bloques) ----------------
    def _schedule_fill(self):
        """Debounce: reprograma el relleno 250 ms después de cada tecla."""
        if self._fill_after is not None:
            try:
                self.after_cancel(self._fill_after)
            except Exception:
                pass
        self._fill_after = self.after(250, self.fill_table)

    def fill_table(self):
        """Filtra + ordena `all_rows` y pinta el primer bloque de filas."""
        if not hasattr(self, "tree"):
            return          # la pestaña Colección aún no está construida
        # Cancela el debounce pendiente (si vino de una tecla, se cancela el
        # timer; si vino del propio timer, after_cancel es inofensivo)
        if self._fill_after is not None:
            try:
                self.after_cancel(self._fill_after)
            except Exception:
                pass
            self._fill_after = None
        text = self.search_var.get().strip().lower()
        estado_f = self.filter_estado_var.get()
        rows = []
        for r in self.all_rows:
            if text and text not in str(r[1]).lower() \
                    and text not in str(r[3]).lower() \
                    and text not in str(r[6]).lower():
                continue
            if estado_f != "Todos" and r[2] != estado_f:
                continue
            rows.append(r)

        # Orden en memoria sobre la columna elegida en la cabecera
        idx = COL_IDX.get(self.sort_col, 0)
        if self.sort_col in ("id", "paginas", "web"):
            # Los None (sin dato) siempre al final, en ambos sentidos
            con_dato = [r for r in rows if r[idx] is not None]
            sin_dato = [r for r in rows if r[idx] is None]
            con_dato.sort(key=lambda r: r[idx], reverse=self.sort_desc)
            rows = con_dato + sin_dato
        else:
            rows.sort(key=lambda r: str(r[idx] or "").lower(),
                      reverse=self.sort_desc)

        self.filtered = rows
        self._rendered = 0
        self._sel_prev = set()
        self._row_tags = {}
        self._filling = True
        try:
            self.tree.delete(*self.tree.get_children())
            self._render_chunk()
        finally:
            self._filling = False
        self.lbl_count.configure(
            text=f"{fmt_num(len(rows))} de {fmt_num(len(self.all_rows))}")
        self._update_empty()

    def _render_chunk(self):
        """Pinta el siguiente bloque de filas (CHUNK a la vez)."""
        start = self._rendered
        end = min(len(self.filtered), start + CHUNK)
        for i, r in enumerate(self.filtered[start:end], start):
            cid, titulo, estado, sitio, n_web, pag, _url = r
            if estado == "descargado":
                estado_txt, tag = "✅ Descargado", "e_ok"
            elif estado == "duplicado":
                estado_txt, tag = "⚠️ Duplicado", "e_dup"
            else:
                estado_txt, tag = "⏳ Pendiente", "e_pend"
            stripe = "odd" if i % 2 else "even"
            iid = str(cid)
            self.tree.insert("", "end", iid=iid,
                             values=(cid, titulo, estado_txt, sitio,
                                     pag, n_web if n_web else "—"),
                             tags=(stripe, tag))
            self._row_tags[iid] = (stripe, tag)
        self._rendered = end

    def _on_yscroll(self, first, last):
        """Auto-carga el siguiente bloque al bajar al 98% del scroll."""
        self._sb.set(first, last)
        if self._filling or self._rendering:
            return
        try:
            if float(last) >= 0.98 and self._rendered < len(self.filtered):
                self._rendering = True
                try:
                    self._render_chunk()
                finally:
                    self._rendering = False
        except (ValueError, TypeError):
            pass

    def _sort_by(self, col: str):
        """Alterna ascendente/descendente al pinchar una cabecera."""
        if self.sort_col == col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col = col
            self.sort_desc = col in ("id", "paginas", "web")
        for c, label in self._head_labels.items():
            arrow = ""
            if c == self.sort_col:
                arrow = " ▼" if self.sort_desc else " ▲"
            self.tree.heading(c, text=label + arrow)
        self.fill_table()

    def _update_empty(self):
        """Muestra u oculta el panel centrado según el estado de la tabla."""
        if not hasattr(self, "empty"):
            return          # la pestaña Colección aún no está construida
        if self._db_ok is False and not self.all_rows:
            icon, title, hint = ("❌", "Sin conexión con MySQL",
                                 "¿Está XAMPP activo? Pulsa para reintentar.")
            action, cmd = "🔄 Reintentar", self.refresh_table
        elif not self.all_rows:
            icon, title, hint = ("📚", "Colección vacía",
                                 "Todavía no hay cómics de este sitio en la "
                                 "base de datos.")
            action, cmd = "🔄 Sincronizar catálogo", self.run_scraper
        elif not self.filtered:
            icon, title, hint = ("🔍", "Sin resultados",
                                 "Ningún cómic coincide con la búsqueda "
                                 "o los filtros.")
            action, cmd = "🧹 Limpiar filtros", self._clear_filters
        else:
            if self._empty_shown:
                self.empty.pack_forget()
                self.tree.pack(side="left", fill="both", expand=True,
                               padx=(6, 0), pady=6)
                self._sb.pack(side="right", fill="y", padx=(0, 6), pady=6)
                self._empty_shown = False
            return
        self.empty_btn.configure(text=action, command=cmd)
        self._empty_icon.configure(text=icon)
        self._empty_title.configure(text=title)
        self._empty_hint.configure(text=hint)
        if not self._empty_shown:
            self._sb.pack_forget()
            self.tree.pack_forget()
            self.empty.pack(fill="both", expand=True)
            self._empty_shown = True

    def _clear_filters(self):
        self.search_var.set("")
        self.filter_estado_var.set("Todos")
        # Los traces lanzan el fill con debounce; forzamos inmediato:
        if self._fill_after is not None:
            try:
                self.after_cancel(self._fill_after)
            except Exception:
                pass
            self._fill_after = None
        self.fill_table()

    def clear_db(self):
        if ACTIVE_JOB.get("owner") is not None:
            messagebox.showwarning(
                "Ocupado",
                "Hay un trabajo activo (en esta o en otra ventana).\n"
                "Espera a que termine antes de vaciar.")
            return
        if not messagebox.askyesno(
                f"Vaciar sitio: {self.site_label}",
                "Se borrarán TODOS los cómics y URLs de ESTE sitio "
                f"({self.site_label}) de la base de datos.\n"
                "Los archivos ya descargados NO se eliminan.\n"
                "El resto de sitios no se toca.\n\n¿Continuar?"):
            return
        try:
            conn = self._db()
            cur = conn.cursor()
            # Borra en orden de dependencia (sin TRUNCATE: solo este sitio)
            cur.execute(
                "DELETE ce FROM comic_etiquetas ce "
                "JOIN comics c ON c.id = ce.comic_id "
                "WHERE c.sitio = %s", (self.sitio,))
            cur.execute(
                "DELETE p FROM paginas p "
                "JOIN comics c ON c.id = p.comic_id "
                "WHERE c.sitio = %s", (self.sitio,))
            cur.execute("DELETE FROM comics WHERE sitio = %s", (self.sitio,))
            conn.commit()
            cur.close()
            conn.close()
            self.log(f"[BD] Cómics de {self.site_label} vaciados.")
            self.refresh_table()
            if self.dash:
                self.dash.refresh_stats()
        except Exception as e:
            messagebox.showerror("Error", str(e))

    # ---------------- TRABAJOS ----------------
    def _start_job(self, args, name, determinate=True):
        if self.proc:
            messagebox.showinfo("Ya hay un trabajo",
                                "Espera a que termine o pulsa Parar.")
            return
        owner = ACTIVE_JOB.get("owner")
        if owner is not None and owner is not self:
            messagebox.showinfo(
                "Trabajo en otra ventana",
                f"Ya hay un trabajo en marcha:\n\n"
                f"  {ACTIVE_JOB.get('name', '')}\n"
                f"  (ventana {getattr(owner, 'site_label', '?')})\n\n"
                "Solo puede haber UN trabajo a la vez.\n"
                "Cierra o espera ese trabajo para lanzar otro.")
            return

        dest = self.dest_var.get().strip().strip('"')
        if dest:
            self.dest_var.set(dest)
        self._save_settings()

        self.job_name = name
        self.job_t0 = time.time()
        self.log(f"\n>>> {name}")
        self.status_var.set(f"Ejecutando: {name}")

        try:
            self.proc = subprocess.Popen(
                [sys.executable, "-u", *args],
                cwd=str(SCRIPT_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception as e:
            self.log(f"[ERROR] No se pudo lanzar: {e}")
            self.proc = None
            self.status_var.set("Listo")
            return

        # Trabajo global en marcha: esta ventana es la dueña
        ACTIVE_JOB["owner"] = self
        ACTIVE_JOB["name"] = name
        self._broadcast("set_external_busy", True, name)

        self._set_progress_mode(determinate)
        if self.dash:
            self.dash.set_busy(True)
        self.btn_stop.configure(state="normal")
        self.status_dot.configure(text_color=COLORS["ACCENT"])

        threading.Thread(target=self._reader, daemon=True).start()
        self._refresh_tick()

    def _broadcast(self, method: str, *args):
        """Llama a `method(*args)` en las demás ventanas de sitio."""
        for code, w in list(WINDOWS.items()):
            if w is self:
                continue
            try:
                if not w.winfo_exists():
                    continue
                getattr(w, method)(*args)
            except Exception:
                pass

    def set_external_busy(self, busy: bool, name: str = ""):
        """Botones/estado cuando el trabajo lo lleva OTRA ventana."""
        if not self.winfo_exists():
            return
        if self.dash:
            self.dash.set_busy(busy)
            if busy:
                # ⏹ Parar solo actúa en la ventana dueña del trabajo
                try:
                    self.dash.btn_stop.configure(state="disabled")
                except Exception:
                    pass
        try:
            self.btn_stop.configure(state="disabled")
        except Exception:
            pass
        if busy:
            self.status_var.set(f"⏳ Trabajo en otra ventana: {name}")
            self.status_dot.configure(text_color=COLORS["WARN"])
        else:
            self.status_var.set("Listo")
            self.status_dot.configure(text_color=COLORS["MUTED"])

    def external_job_done(self):
        """Al terminar el trabajo de OTRA ventana: estado + tabla + hist."""
        if not self.winfo_exists():
            return
        self.set_external_busy(False)
        if self.dash:
            self.dash.reload_jobs()
        self.refresh_table()

    def run_scraper(self):
        args = ["scraper.py"]
        try:
            limit = int(self.limit_var.get())
            if limit > 0:
                args += ["--limit", str(limit)]
        except ValueError:
            pass
        mode = MODE_VALUES.get(self.mode_var.get(), "auto")
        args += ["--mode", mode, "--site", self.site]
        self._start_job(args,
                        f"Sincronización ({mode}, {self.site_label})",
                        determinate=False)

    def start_backfill(self):
        """📚 Completar metadatos: backfill sin revisar el catálogo (HTTP)."""
        args = ["scraper.py", "--site", self.site, "--mode", "http",
                "--skip-catalog"]
        self._start_job(args,
                        f"Completar metadatos (http, {self.site_label})",
                        determinate=False)

    def edit_pawchive_creators(self):
        """🐾 Diálogo modal para editar la lista de creadores de Pawchive
        (pawchive_creators.json): se pega una URL por línea."""
        dlg = ctk.CTkToplevel(self)
        dlg.title("Creadores Pawchive")
        dlg.geometry("620x440")
        dlg.after(80, lambda: (dlg.transient(self), _grab(dlg)))
        ctk.CTkLabel(dlg,
                     text="Pega las URLs de tus creadores (una por línea):",
                     font=("Segoe UI", 13)).pack(anchor="w", padx=14,
                                                 pady=(14, 2))
        ctk.CTkLabel(dlg,
                     text="https://pawchive.pw/patreon/user/12345   ·   "
                          "https://pawchive.pw/fanbox/user/67890",
                     font=("Segoe UI", 12), text_color=COLORS["MUTED"]).pack(
            anchor="w", padx=14)
        box = ctk.CTkTextbox(dlg, font=("Segoe UI", 13))
        box.pack(fill="both", expand=True, padx=14, pady=(8, 8))
        actuales = [pawchive_api.creator_url(c["service"], c["id"])
                    for c in pawchive_api.load_creators()]
        box.insert("1.0", "\n".join(actuales))

        def guardar():
            lineas = [l.strip()
                      for l in box.get("1.0", "end").splitlines()]
            lineas = [l for l in lineas if l and not l.startswith("#")]
            creadores, malos = [], []
            for l in lineas:
                c = pawchive_api.parse_creator_url(l)
                if c:
                    creadores.append(c)
                else:
                    malos.append(l)
            if malos:
                messagebox.showwarning(
                    "URLs no válidas",
                    "No se reconocen:\n" + "\n".join(malos[:8]) +
                    ("\n…" if len(malos) > 8 else ""),
                    parent=dlg)
                return
            pawchive_api.save_creators(creadores)
            n = len(pawchive_api.load_creators())   # tras deduplicar
            if self.dash and getattr(self.dash, "btn_creators", None):
                self.dash.btn_creators.configure(
                    text=f"\U0001f43e Creadores Pawchive ({n})")
            self.log(f"\U0001f43e Creadores Pawchive: {n} guardados.")
            dlg.destroy()

        bar = ctk.CTkFrame(dlg, fg_color="transparent")
        bar.pack(fill="x", padx=14, pady=(0, 14))
        ctk.CTkButton(bar, text="\U0001f4be Guardar", width=140, height=34,
                      font=("Segoe UI", 13), fg_color=COLORS["ACCENT"],
                      hover_color=COLORS["ACCENT_HOVER"],
                      command=guardar).pack(side="right", padx=(8, 0))
        ctk.CTkButton(bar, text="Cancelar", width=110, height=34,
                      font=("Segoe UI", 13), fg_color="#3a3a3a",
                      hover_color="#4a4a4a",
                      command=dlg.destroy).pack(side="right")
        dlg.focus()

    def run_verify(self):
        """🎞 Verificar disco: 'descargado' solo si el cómic está completo en disco."""
        dest = self.dest_var.get().strip().strip('"') or str(DEFAULT_DEST)
        args = ["verify_dl.py", "--sync-estado", "--dest", dest]
        self._start_job(args, "Verificar disco (estado según disco)",
                        determinate=False)

    def run_optimizer(self):
        """🗜 Optimizar imágenes: re-codifica a q82 en el mismo archivo."""
        dest = self.dest_var.get().strip().strip('"') or str(DEFAULT_DEST)
        args = ["optimize.py", "--dest", dest]
        self._start_job(args, "Optimizar imágenes (q82)",
                        determinate=False)

    def run_downloader(self, types: str = "imagen,video,archivo"):
        args = ["download.py", "--no-prompt",
                "--dest", self.dest_var.get().strip().strip('"') or str(DEFAULT_DEST)]
        try:
            args += ["--workers", str(max(1, int(self.workers_var.get())))]
        except ValueError:
            pass
        args += ["--site", self.site, "--types", types]
        self._start_job(args,
                        f"Descarga ({types}) ({self.site_label})",
                        determinate=True)

    def stop_job(self):
        if self.proc:
            self.proc.terminate()
            self.log(">>> Parada solicitada…")

    def _set_progress_mode(self, determinate: bool):
        if determinate:
            self.pbar.configure(mode="determinate")
            self.pbar.set(0)
        else:
            self.pbar.configure(mode="indeterminate")
            self.pbar.start()

    def _set_progress(self, frac: float):
        self.pbar.set(frac)

    def _stop_progress(self):
        self.pbar.stop()
        self.pbar.configure(mode="determinate")

    def _reader(self):
        proc = self.proc
        for line in proc.stdout:
            self.job_queue.put(line.rstrip("\n"))
        code = proc.wait()
        self.job_queue.put(("__done__", code))

    def _poll_queue(self):
        try:
            while True:
                item = self.job_queue.get_nowait()
                if isinstance(item, tuple) and item[0] == "__done__":
                    self._job_finished(item[1])
                else:
                    self.log(item)
                    self._handle_line(item)
        except queue.Empty:
            pass
        self.after(120, self._poll_queue)

    def _handle_line(self, line: str):
        m = RE_PROGRESS.search(line)
        if m:
            done, total = int(m.group(1)), int(m.group(2))
            if total > 0:
                self._set_progress(done / total)
                self.status_var.set(f"{self.job_name}: {done}/{total}")

    def _job_finished(self, code: int):
        dur_s = int(time.time() - self.job_t0) if self.job_t0 else 0
        dur = f"{dur_s // 60}m {dur_s % 60:02d}s" if dur_s >= 60 else f"{dur_s}s"
        self._stop_progress()
        if code == 0:
            self.pbar.set(1)
            self.status_var.set(f"{self.job_name}: completado ✅")
            self.log(f"<<< {self.job_name} finalizado correctamente.")
        else:
            self.status_var.set(f"{self.job_name}: error (código {code}) ⚠️")
            self.log(f"<<< {self.job_name} terminó con error {code}.")
        self.proc = None
        self.btn_stop.configure(state="disabled")
        self.status_dot.configure(
            text_color=COLORS["SUCCESS"] if code == 0 else COLORS["DANGER"])
        if self.dash:
            self.dash.set_busy(False)
            self.dash.job_finished(self.job_name, code, dur)
        # Libera el trabajo global y avisa al resto de ventanas
        if ACTIVE_JOB.get("owner") is self:
            ACTIVE_JOB["owner"] = None
            ACTIVE_JOB["name"] = ""
        self._broadcast("external_job_done")
        self.refresh_table()

    def _refresh_tick(self):
        """Refresca la tabla mientras hay un trabajo en marcha."""
        if self.proc:
            self.refresh_table()
            self.after(4000, self._refresh_tick)

    # ---------------- UTILIDADES ----------------
    def log(self, text: str, tag: str | None = None):
        if self.dash is not None:
            self.dash.log(text, tag)

    def pick_dest(self):
        path = filedialog.askdirectory(initialdir=self.dest_var.get() or "C:\\")
        if path:
            self.dest_var.set(path)
            self._save_settings()

    def open_dest(self):
        path = self.dest_var.get().strip() or str(DEFAULT_DEST)
        os.makedirs(path, exist_ok=True)
        os.startfile(path)

    def _row_by_iid(self, iid: str) -> tuple | None:
        try:
            cid = int(iid)
        except ValueError:
            return None
        return next((r for r in self.all_rows if r[0] == cid), None)

    def _selected_rows(self) -> list[tuple]:
        out = []
        for iid in self.tree.selection():
            row = self._row_by_iid(iid)
            if row:
                out.append(row)
        return out

    def _comic_folder(self, row: tuple) -> Path:
        _cid, titulo, _estado, sitio = row[0], row[1], row[2], row[3]
        return carpeta_comic(self.dest_var.get().strip() or str(DEFAULT_DEST),
                             sitio, titulo)

    def _open_selected(self, _event=None):
        rows = self._selected_rows()
        if not rows:
            return
        self._open_folder_of(rows[0])

    def _open_folder_of(self, row: tuple):
        path = self._comic_folder(row)
        if path.is_dir():
            os.startfile(path)
        else:
            messagebox.showinfo(
                "Sin carpeta",
                f"Todavía no se descargó este cómic.\n\n{path}")

    def _copy(self, text: str, what: str):
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update_idletasks()
            self.log(f"\U0001f4cb {what} copiado al portapapeles.", "ok")
        except Exception as e:
            self.log(f"[X] No se pudo copiar: {e}")

    # ---- Barra de selección ----
    def _on_select(self, _event=None):
        sel = set(self.tree.selection())
        # Resalta la selección con el color de acento (tag "sel")
        for iid in sel - self._sel_prev:
            try:
                self.tree.item(iid, tags=("sel",))
            except tk.TclError:
                pass
        for iid in self._sel_prev - sel:
            orig = self._row_tags.get(iid)
            if orig:
                try:
                    self.tree.item(iid, tags=orig)
                except tk.TclError:
                    pass
        self._sel_prev = sel
        self._update_sel_bar()

    def _update_sel_bar(self):
        n = len(self._sel_prev)
        if n:
            self.lbl_sel.configure(
                text=f"{n} seleccionado{'s' if n != 1 else ''}",
                text_color=COLORS["ACCENT"])
        else:
            self.lbl_sel.configure(text="Sin selección", text_color=COLORS["MUTED"])

    def _sel_open_folder(self):
        rows = self._selected_rows()
        if rows:
            self._open_folder_of(rows[0])
        else:
            messagebox.showinfo("Selección",
                                "Selecciona al menos una fila en la tabla.")

    def _sel_copy_titles(self):
        rows = self._selected_rows()
        if not rows:
            messagebox.showinfo("Selección",
                                "Selecciona al menos una fila en la tabla.")
            return
        self._copy("\n".join(str(r[1]) for r in rows),
                   f"Títulos ({len(rows)})")

    def _sel_copy_urls(self):
        rows = self._selected_rows()
        if not rows:
            messagebox.showinfo("Selección",
                                "Selecciona al menos una fila en la tabla.")
            return
        self._copy("\n".join(str(r[6]) for r in rows),
                   f"URLs ({len(rows)})")

    # ---- Menú contextual (clic derecho) ----
    def _ctx_menu(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        if iid not in self.tree.selection():
            self.tree.selection_set(iid)
        self._ctx_iid = iid
        try:
            self.ctx_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.ctx_menu.grab_release()

    def _ctx_row(self) -> tuple | None:
        return self._row_by_iid(self._ctx_iid) if self._ctx_iid else None

    def _ctx_open_folder(self):
        row = self._ctx_row()
        if row:
            self._open_folder_of(row)

    def _ctx_copy_title(self):
        row = self._ctx_row()
        if row:
            self._copy(str(row[1]), "Título")

    def _ctx_copy_url(self):
        row = self._ctx_row()
        if row:
            self._copy(str(row[6]), "URL")

    def _ctx_open_browser(self):
        row = self._ctx_row()
        if row and row[6]:
            webbrowser.open(str(row[6]))

    # ---------------- AJUSTES / CIERRE ----------------
    def _save_settings(self):
        try:
            workers = int(self.workers_var.get())
        except ValueError:
            workers = 6
        try:
            limit = int(self.limit_var.get())
        except ValueError:
            limit = 0
        # update_config conserva las demás claves (caché de escaneo, etc.)
        update_config(dest=self.dest_var.get().strip(),
                      workers=workers,
                      limit=limit,
                      mode_label=self.mode_var.get(),
                      filter_estado=self.filter_estado_var.get())

    def _on_close(self):
        self.close(force=False)

    def close(self, force: bool = False):
        """Cierra la ventana guardando geometría y estado.

        force=True (lanzador "Cerrar todo"): no pregunta, pero si había
        un trabajo propio lo detiene y libera el candado global.
        """
        if self.proc and self.proc.poll() is None:
            if not force and not messagebox.askyesno(
                    "Trabajo en marcha",
                    f"Hay un trabajo activo en la ventana "
                    f"{self.site_label}.\n¿Detenerlo y cerrar la ventana?"):
                return
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except Exception:
                    pass
            except Exception:
                pass
            dur_s = int(time.time() - self.job_t0) if self.job_t0 else 0
            dur = (f"{dur_s // 60}m {dur_s % 60:02d}s" if dur_s >= 60
                   else f"{dur_s}s")
            if self.dash:
                self.dash.job_finished(f"{self.job_name} (cancelado)",
                                       1, dur)
            self.proc = None
            if ACTIVE_JOB.get("owner") is self:
                ACTIVE_JOB["owner"] = None
                ACTIVE_JOB["name"] = ""
                # El resto de ventanas vuelve a estado libre
                for w in list(WINDOWS.values()):
                    if w is self:
                        continue
                    try:
                        if w.winfo_exists():
                            w.external_job_done()
                    except Exception:
                        pass
        elif ACTIVE_JOB.get("owner") is self:
            ACTIVE_JOB["owner"] = None
            ACTIVE_JOB["name"] = ""
        self._save_settings()
        try:
            update_config(**{f"geo_{self.site}": self.geometry()})
        except Exception:
            pass
        WINDOWS.pop(self.site, None)
        _sync_open_windows()
        self.destroy()


if __name__ == "__main__":
    launcher = Launcher()
    print("Lanzador iniciado — ventanas: "
          "📕 VerComicsPorno / 📗 VerMangasPorno / 🐾 Pawchive")
    print(f"Base de datos: {DB_CONFIG['database']} @ {DB_CONFIG['host']}")
    launcher.mainloop()