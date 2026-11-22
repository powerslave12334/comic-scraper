# -*- coding: utf-8 -*-
"""
Pestaña 📊 Dashboard del gestor de cómics (una ventana por sitio).

Widget CTkFrame embebible en la pestaña "📊 Dashboard" de cada ventana
`SiteWindow` de `gui.py` (una ventana por sitio: vcp/vmp/pch), con las
seis mejoras aprobadas:

  1. Tarjetas de estadísticas (cómics, descargados, pendientes, duplicados,
     URLs de páginas, etiquetas).
  2. Progreso de metadatos por sitio (barras con cómics consultados / total).
  3. Estado del sistema (MySQL, disco libre, tamaño de la colección con
     escaneo en segundo plano + botón "Recalcular").
  4. Controles agrupados: Scraping (sincronizar + completar metadatos),
     Descarga y Utilidades.
  5. Historial de trabajos (últimos 20, persistido en `jobs_history.json`).
  6. Registro de actividad con marcas de tiempo, colores por tipo de línea
     y botones 🧹 Limpiar / 📋 Copiar.

El contenido superior (tarjetas + cuerpo) va en un `CTkScrollableFrame` para
que quepa en ventanas bajas; el registro queda fijo abajo. La barra de
progreso/estado vive fuera, en la barra inferior de `gui.py`.

Este módulo NO importa `gui.py` (evita importaciones circulares): la ventana
principal importa de aquí las constantes y auxiliares compartidos, y crea el
`Dashboard` dentro de su pestaña pasándole la instancia de la app. La
comunicación es por métodos:

  - dashboard → app:  self.app.run_scraper() / run_downloader() /
    start_backfill() / stop_job() / pick_dest() / open_dest() / clear_db() /
    refresh_stats() / fetch_stats()
  - app → dashboard:   log() / set_busy() / job_finished()

Uso: lo crea `gui.py` al montar la pestaña; no se ejecuta solo.
"""
import json
import os
import queue
import re
import shutil
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import customtkinter as ctk
import tkinter as tk

from config import (
    SCRIPT_DIR, SITES, MODE_VALUES, ESTADO_FILTROS, INVALID_CHARS,
    COLORS, SITE_ACCENTS, DEFAULT_DEST,
)
from db import ComicRepository, TagRepository, PageRepository, connection  # noqa: F401
from exceptions import DatabaseError  # noqa: F401

# SITES / ESTADO_FILTROS / SITE_ACCENTS se re-exportan desde aquí:
# gui.py y otros módulos los importan de dashboard (compatibilidad).
__all_reexports__ = (SITES, ESTADO_FILTROS, SITE_ACCENTS)

# -------------- CONFIGURACIÓN COMPARTIDA (la usa gui.py) --------------
CONFIG_FILE = SCRIPT_DIR / "gui_config.json"
JOBS_FILE = SCRIPT_DIR / "jobs_history.json"

# Motor de scraping ofrecido en la interfaz
MODE_LABELS = ("Auto (cookies de Brave)", "HTTP rápido (sin navegador)", "Solo navegador")

# Filtros de la tabla principal (gui.py)
SITIO_FILTROS = ("Todos", "vercomicsporno", "vermangasporno", "pawchive")

RE_PROGRESS = re.compile(r"progreso:\s*(\d+)/(\d+)")

# Colores (desde config)
BG = COLORS["BG"]
PANEL = COLORS["PANEL"]
CARD_BG = COLORS["CARD_BG"]
BORDER = COLORS["BORDER"]
ACCENT = COLORS["ACCENT"]
ACCENT_HOVER = COLORS["ACCENT_HOVER"]
SUCCESS = COLORS["SUCCESS"]
WARN = COLORS["WARN"]
DANGER = COLORS["DANGER"]
ORANGE = COLORS["ORANGE"]
CYAN = COLORS["CYAN"]
PURPLE = COLORS["PURPLE"]
TEXT = COLORS["TEXT"]
MUTED = COLORS["MUTED"]

INVALID = re.compile(INVALID_CHARS)


def blend(color: str, bg: str, t: float = 0.2) -> str:
    """Mezcla dos colores hex: t=0 -> fondo, t=1 -> color."""
    c = color.lstrip("#")
    b = bg.lstrip("#")
    out = []
    for i in range(3):
        cc = int(c[i * 2:i * 2 + 2], 16)
        bb = int(b[i * 2:i * 2 + 2], 16)
        out.append(f"{round(cc * t + bb * (1 - t)):02x}")
    return "#" + "".join(out)


def safe_name(name: str) -> str:
    """Mismo criterio que download.py para nombres de carpeta."""
    clean = INVALID.sub("_", name).strip(" .")
    return clean[:120].strip(" .") or "sin_titulo"


def carpeta_comic(dest, sitio, titulo) -> Path:
    """Mismo criterio que download.py: vercomicsporno va en la raíz del destino
    (compatibilidad con la colección actual); el resto de sitios en
    DESTINO/<sitio>/<titulo>."""
    base = Path(dest)
    if sitio and sitio != "vercomicsporno":
        base = base / safe_name(sitio)
    return base / safe_name(titulo)


def load_config() -> dict:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except Exception:
        pass


def update_config(**kw) -> None:
    """Actualiza solo las claves indicadas sin borrar el resto."""
    cfg = load_config()
    cfg.update(kw)
    save_config(cfg)


def fmt_num(n) -> str:
    try:
        return f"{int(n):,}".replace(",", ".")
    except (TypeError, ValueError):
        return str(n)


def fmt_bytes(b: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if b < 1024:
            return f"{b:.0f} {unit}" if unit == "B" else f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


# -------------- CLASIFICACIÓN DEL REGISTRO --------------
def classify_line(line: str):
    """Devuelve la etiqueta de color para una línea del registro."""
    if re.search(r"\[X\]|\bERROR\b|Error:|Traceback|\[ERROR\]", line):
        return "err"
    if "[!]" in line or "\u26a0" in line:
        return "warn"
    if "[OK]" in line or "\u2705" in line or "correctamente" in line:
        return "ok"
    if "progreso:" in line or line.startswith("[HTTP] ["):
        return "prog"
    if line.startswith((">>>", "<<<", "===========")):
        return "head"
    return None


class Dashboard(ctk.CTkFrame):
    """Pestaña del dashboard: estadísticas, sistema, controles, historial y log."""

    def __init__(self, master, app, auto=True):
        super().__init__(master, fg_color=BG, corner_radius=0)
        self.app = app

        self._scan_queue: queue.Queue = queue.Queue()
        self._scan_running = False
        self._mysql_ok = None          # None = aún sin comprobar
        self.jobs: list[dict] = self._load_jobs()
        self._meta_rows: dict = {}     # sitio -> (label, bar, pct)
        self._built = False

        if auto:
            self.construir()

    # ---------------- CONSTRUCCIÓN ----------------
    def construir(self):
        """Construye el dashboard entero de una sola vez."""
        for paso in self.pasos_build():
            paso()
        self._built = True

    def pasos_build(self):
        """Pasos de construcción, troceados.

        El dashboard tiene ~90 widgets; crearlos todos de golpe congela la
        ventana casi un segundo.gui.py va llamando a estos pasos de uno en
        uno (con `after`) para que Tk repinte entre medias."""
        yield self._paso_scroll
        yield self._paso_cartas
        yield self._paso_grafico
        yield self._paso_cuerpo
        yield self._paso_scraping
        yield self._paso_descarga
        yield self._paso_utilidades
        yield self._paso_info
        yield self._paso_log
        yield self._paso_final

    def _build(self):
        """Construcción completa de una vez (compatibilidad)."""
        self.construir()

    # --- Paso 1: marco con scroll ---
    def _paso_scroll(self):
        scroll = ctk.CTkScrollableFrame(self, fg_color=BG, corner_radius=0)
        scroll.pack(fill="both", expand=True)
        self._scroll = scroll

    # --- Paso 2: tarjetas de estadísticas (elevadas con sombra) ---
    def _paso_cartas(self):
        scroll = self._scroll
        cards = ctk.CTkFrame(scroll, fg_color="transparent")
        cards.pack(fill="x", padx=16, pady=(12, 8))
        for i in range(6):
            cards.columnconfigure(i, weight=1, uniform="card")
        # (icono, título, clave de stats, color de acento de la tarjeta)
        self._card_defs = (
            ("\U0001f4da", "C\u00f3mics", "total", ACCENT),
            ("\u2705", "Descargados", "descargados", SUCCESS),
            ("\u23f3", "Pendientes", "pendientes", WARN),
            ("\u26a0\ufe0f", "Duplicados", "duplicados", ORANGE),
            ("\U0001f5bc", "URLs de p\u00e1ginas", "paginas", CYAN),
            ("\U0001f3f7", "Etiquetas", "etiquetas", PURPLE),
        )
        self._card_vals: dict = {}
        for col, (icon, title, key, color) in enumerate(self._card_defs):
            # Sombra (frame detrás, un pelín mayor)
            shadow = ctk.CTkFrame(cards, fg_color="#0a0a0a", corner_radius=12)
            shadow.grid(row=0, column=col, sticky="nsew", padx=4, pady=2)
            # Tarjeta principal
            card = ctk.CTkFrame(cards, fg_color=CARD_BG, corner_radius=12,
                                height=92, border_width=1, border_color=BORDER)
            card.grid(row=0, column=col, sticky="nsew", padx=4)
            card.grid_propagate(False)
            card.lift(shadow)
            hdr = ctk.CTkFrame(card, fg_color="transparent")
            hdr.pack(fill="x", padx=12, pady=(12, 0))
            # chip de icono con fondo teñido por el color de la tarjeta
            ctk.CTkLabel(hdr, text=icon, width=26, height=26,
                         corner_radius=7, fg_color=blend(color, CARD_BG, 0.20),
                         text_color=color, font=("Segoe UI", 13)).pack(
                side="left")
            ctk.CTkLabel(hdr, text=title, font=("Segoe UI", 11),
                         text_color=MUTED).pack(side="left", padx=(8, 0))
            val = ctk.CTkLabel(card, text="\u2014",
                               font=("Segoe UI", 26, "bold"),
                               text_color=TEXT, anchor="w")
            val.pack(anchor="w", padx=12, pady=(4, 10))
            self._card_vals[key] = val

    # --- Paso 3: gráfico de descargas de los últimos 14 días ---
    def _paso_grafico(self):
        small = ("Segoe UI", 12)
        scroll = self._scroll
        chart_sec = ctk.CTkFrame(scroll, fg_color=PANEL, corner_radius=10)
        chart_sec.pack(fill="x", padx=16, pady=(0, 8))
        ch_hdr = ctk.CTkFrame(chart_sec, fg_color="transparent")
        ch_hdr.pack(fill="x", padx=14, pady=(10, 0))
        ctk.CTkLabel(ch_hdr,
                     text="\U0001f4c8 Descargas \u00b7 \u00faltimos 14 d\u00edas",
                     font=("Segoe UI", 14, "bold"),
                     text_color=ACCENT).pack(side="left")
        self.lbl_chart_sum = ctk.CTkLabel(ch_hdr, text="", font=small,
                                          text_color=MUTED)
        self.lbl_chart_sum.pack(side="right")
        self._chart_body = ctk.CTkFrame(chart_sec, fg_color="transparent")
        self._chart_body.pack(fill="x", padx=14, pady=(6, 12))
        self._chart_data = None      # últimos datos dibujados (evita repintar)

    # --- Paso 4: cuerpo (columna de controles / columna de info) ---
    def _paso_cuerpo(self):
        scroll = self._scroll
        body = ctk.CTkFrame(scroll, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=16, pady=(0, 4))
        # Columna 0 = ancho fijo del panel de controles (384 px);
        # columna 1 = info, ocupa el resto.
        body.grid_columnconfigure(0, weight=0, minsize=384)
        body.grid_columnconfigure(1, weight=1)
        body.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(body, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        # OJO: sin pack_propagate(False). Con él, el panel declaraba un alto
        # de 0 y el packer repartía el espacio de la ventana entre las 3
        # secciones: "Descarga" y "Utilidades" quedaban aplastadas a 1x1 y el
        # botón "Descargar" era invisible.

        right = ctk.CTkFrame(body, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")
        self._body = body
        self._left = left
        self._right = right

    # --- Paso 5: controles de Scraping ---
    def _paso_scraping(self):
        font = ("Segoe UI", 13)
        small = ("Segoe UI", 12)
        left = self._left
        s1 = self._section(left, "\U0001f504 Scraping")
        sitio = getattr(self.app, "site", "vcp")
        if sitio == "pch":
            # Pawchive va por API JSON: el motor HTTP/navegador no aplica
            r = ctk.CTkFrame(s1, fg_color="transparent")
            r.pack(fill="x", pady=(0, 6))
            ctk.CTkLabel(r, text="Motor de scraping",
                         font=("Segoe UI", 12),
                         text_color=MUTED).pack(side="left")
            ctk.CTkLabel(r, text="API JSON (sin navegador)",
                         font=("Segoe UI", 12)).pack(side="right")
        else:
            self._opt_row(s1, "Motor de scraping", self.app.mode_var,
                          list(MODE_VALUES.keys()), 210)
        # Sitio fijo de esta ventana (una ventana por sitio)
        r = ctk.CTkFrame(s1, fg_color="transparent")
        r.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(r, text="Sitio", font=("Segoe UI", 12),
                     text_color=MUTED).pack(side="left")
        ctk.CTkLabel(r, text=getattr(self.app, "site_label", "\u2014"),
                     font=("Segoe UI", 12, "bold"),
                     text_color=ACCENT).pack(side="right")
        r = ctk.CTkFrame(s1, fg_color="transparent")
        r.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(r, text="P\u00e1g. cat\u00e1logo (0=todas)", font=small,
                     text_color=MUTED).pack(side="left")
        ctk.CTkEntry(r, textvariable=self.app.limit_var, width=70, height=30,
                     font=small).pack(side="right")
        self.btn_sync = ctk.CTkButton(
            s1, text="\U0001f504  Sincronizar cat\u00e1logo", height=38,
            font=("Segoe UI", 13, "bold"), fg_color=ACCENT,
            hover_color=ACCENT_HOVER, command=self.app.run_scraper)
        self.btn_sync.pack(fill="x", pady=(4, 6))
        self.btn_backfill = ctk.CTkButton(
            s1, text="\U0001f4da  Completar metadatos", height=36, font=font,
            fg_color="#8a5cd6", hover_color="#6e46b3",
            command=self.app.start_backfill)
        self.btn_backfill.pack(fill="x", pady=(0, 2))
        ctk.CTkLabel(s1,
                     text="Sin cat\u00e1logo: rellena n\u00ba de p\u00e1ginas web\n"
                          "y tags de los c\u00f3mics pendientes (HTTP r\u00e1pido).",
                     font=("Segoe UI", 11), text_color=MUTED,
                     justify="left").pack(anchor="w", pady=(2, 0))
        # Creadores de Pawchive (pawchive_creators.json) — solo en la
        # ventana de Pawchive
        self.btn_creators = None
        if sitio == "pch":
            try:
                import pawchive_api
                n_pch = len(pawchive_api.load_creators())
            except Exception:
                n_pch = 0
            self.btn_creators = ctk.CTkButton(
                s1, text=f"\U0001f43e Creadores Pawchive ({n_pch})",
                height=34, font=small, fg_color="#3a3a3a",
                hover_color="#4a4a4a",
                command=self.app.edit_pawchive_creators)
            self.btn_creators.pack(fill="x", pady=(8, 0))

    # --- Paso 6: controles de Descarga ---
    def _paso_descarga(self):
        font = ("Segoe UI", 13)
        small = ("Segoe UI", 12)
        left = self._left
        s2 = self._section(left, "\u2b07\ufe0f Descarga")
        ctk.CTkLabel(s2, text="Carpeta destino", font=small,
                     text_color=MUTED).pack(anchor="w")
        row = ctk.CTkFrame(s2, fg_color="transparent")
        row.pack(fill="x", pady=(2, 6))
        ctk.CTkEntry(row, textvariable=self.app.dest_var, height=32,
                     font=small).pack(side="left", fill="x", expand=True)
        ctk.CTkButton(row, text="\u2026", width=38, height=32, font=font,
                      command=self.app.pick_dest).pack(side="right",
                                                       padx=(6, 0))
        r = ctk.CTkFrame(s2, fg_color="transparent")
        r.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(r, text="Hilos de descarga", font=small,
                     text_color=MUTED).pack(side="left")
        ctk.CTkEntry(r, textvariable=self.app.workers_var, width=70, height=30,
                     font=small).pack(side="right")
        # Selector de tipos a descargar
        self.app.dl_types_var = tk.StringVar(value="imagen,video,archivo")
        ctk.CTkOptionMenu(r, variable=self.app.dl_types_var,
                          values=["imagen", "video", "archivo",
                                  "imagen,video", "imagen,archivo", "video,archivo",
                                  "imagen,video,archivo"],
                          width=160, height=30, font=small,
                          dynamic_resizing=False).pack(side="right", padx=(8, 0))
        self.btn_dl = ctk.CTkButton(
            s2, text="\u2b07\ufe0f  Descargar", height=38,
            font=("Segoe UI", 13, "bold"), fg_color="#1f8a4c",
            hover_color="#166b3a",
            command=lambda: self.app.run_downloader(self.app.dl_types_var.get()))
        self.btn_dl.pack(fill="x", pady=(4, 6))
        self.btn_stop = ctk.CTkButton(
            s2, text="\u23f9  Parar", height=34, font=font, state="disabled",
            fg_color="#8c2f2f", hover_color="#6e2424",
            command=self.app.stop_job)
        self.btn_stop.pack(fill="x")

    # --- Paso 7: controles de Utilidades ---
    def _paso_utilidades(self):
        small = ("Segoe UI", 12)
        left = self._left
        s3 = self._section(left, "\U0001f527 Utilidades")
        ctk.CTkButton(s3, text="\U0001f4c1  Abrir carpeta destino", height=34,
                      font=small, fg_color="#3a3a3a", hover_color="#4a4a4a",
                      command=self.app.open_dest).pack(fill="x", pady=(0, 6))
        self.btn_verify = ctk.CTkButton(
            s3, text="\U0001f39a  Verificar disco", height=34, font=small,
            fg_color="#3a3a3a", hover_color="#4a4a4a",
            command=self.app.run_verify)
        self.btn_verify.pack(fill="x", pady=(0, 6))
        self.btn_opt = ctk.CTkButton(
            s3, text="\U0001f5dc  Optimizar im\u00e1genes", height=34,
            font=small, fg_color="#3a3a3a", hover_color="#4a4a4a",
            command=self.app.run_optimizer)
        self.btn_opt.pack(fill="x", pady=(0, 6))
        ctk.CTkButton(s3, text="\U0001f5d1\ufe0f  Vaciar solo este sitio",
                      height=34, font=small, fg_color="#3a3a3a",
                      hover_color="#5a2a2a",
                      command=self.app.clear_db).pack(fill="x", pady=(0, 6))
        ctk.CTkButton(s3, text="\U0001f504  Refrescar estad\u00edsticas",
                      height=34, font=small, fg_color="#3a3a3a",
                      hover_color="#4a4a4a",
                      command=self.refresh_stats).pack(fill="x")

    # --- Paso 8: panel derecho (metadatos, estado e historial) ---
    def _paso_info(self):
        right = self._right
        # --- Progreso de metadatos del sitio ---
        s4 = self._section(right, "\U0001f4c8 Metadatos del sitio")
        self.meta_body = ctk.CTkFrame(s4, fg_color="transparent")
        self.meta_body.pack(fill="x")
        ctk.CTkLabel(s4,
                     text="C\u00f3mics con n\u00ba de p\u00e1ginas web consultado "
                          "(backfill de metadatos).",
                     font=("Segoe UI", 11), text_color=MUTED).pack(
            anchor="w", pady=(4, 0))

        # --- 3. Estado del sistema ---
        s5 = self._section(right, "\U0001f5a5\ufe0f Estado del sistema")
        tiles = ctk.CTkFrame(s5, fg_color="transparent")
        tiles.pack(fill="x")
        for i in range(3):
            tiles.columnconfigure(i, weight=1, uniform="tile")
        self.lbl_mysql = self._tile(tiles, 0, "\U0001f418 MySQL")
        self.lbl_disk = self._tile(tiles, 1, "\U0001f4be Disco")
        coll_tile, self.lbl_coll = self._tile(tiles, 2, "\U0001f4c1 Colecci\u00f3n",
                                              with_frame=True)
        self.btn_recalc = ctk.CTkButton(
            coll_tile, text="\U0001f504 Recalcular", width=104, height=24,
            font=("Segoe UI", 11), fg_color="#3a3a3a", hover_color="#4a4a4a",
            command=self._start_scan)
        self.btn_recalc.pack(anchor="e", padx=10, pady=(0, 8))

        # --- 5. Historial de trabajos ---
        s6 = self._section(right, "\U0001f552 Historial de trabajos (20)")
        self.hist_box = ctk.CTkTextbox(s6, height=118, font=("Consolas", 12),
                                       fg_color="#101010", text_color="#c8e6c9")
        self.hist_box.pack(fill="both", expand=True)
        self.hist_box.tag_config("ok", foreground="#7bd88f")
        self.hist_box.tag_config("err", foreground="#ff6b6b")
        self._render_jobs()

    # --- Paso 9: registro de actividad (fijo abajo, siempre visible) ---
    def _paso_log(self):
        small = ("Segoe UI", 12)
        logf = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=10, height=206)
        logf.pack(fill="x", padx=16, pady=(4, 10))
        logf.pack_propagate(False)
        lhdr = ctk.CTkFrame(logf, fg_color="transparent")
        lhdr.pack(fill="x", padx=10, pady=(6, 0))
        ctk.CTkLabel(lhdr, text="\U0001f4dd Registro de actividad", font=small,
                     text_color=MUTED).pack(side="left")
        ctk.CTkButton(lhdr, text="\U0001f9f9 Limpiar", width=86, height=26,
                      font=("Segoe UI", 11), fg_color="#3a3a3a",
                      hover_color="#4a4a4a",
                      command=self.clear_log).pack(side="right", padx=(6, 0))
        ctk.CTkButton(lhdr, text="\U0001f4cb Copiar", width=86, height=26,
                      font=("Segoe UI", 11), fg_color="#3a3a3a",
                      hover_color="#4a4a4a",
                      command=self.copy_log).pack(side="right")
        self.log_box = ctk.CTkTextbox(logf, font=("Consolas", 12),
                                      fg_color="#101010", text_color="#c8e6c9")
        self.log_box.pack(fill="both", expand=True, padx=10, pady=(4, 8))
        self.log_box.tag_config("ts", foreground="#6f6f6f")
        self.log_box.tag_config("err", foreground="#ff6b6b")
        self.log_box.tag_config("warn", foreground="#ffb86b")
        self.log_box.tag_config("ok", foreground="#7bd88f")
        self.log_box.tag_config("prog", foreground="#6bd6ff")
        self.log_box.tag_config("head", foreground="#a78bfa")

    # --- Paso 10: datos iniciales (BD) y temporizador ---
    def _paso_final(self):
        self._built = True
        self._load_cache()
        self.refresh_stats()
        self.after(5000, self._tick)

    def _section(self, parent, title):
        f = ctk.CTkFrame(parent, fg_color=PANEL, corner_radius=10)
        f.pack(fill="x", pady=(0, 10))
        ctk.CTkLabel(f, text=title, font=("Segoe UI", 14, "bold"),
                     text_color=ACCENT).pack(anchor="w", padx=14,
                                             pady=(10, 4))
        inner = ctk.CTkFrame(f, fg_color="transparent")
        inner.pack(fill="x", padx=14, pady=(0, 12))
        return inner

    def _opt_row(self, parent, label, variable, values, width):
        r = ctk.CTkFrame(parent, fg_color="transparent")
        r.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(r, text=label, font=("Segoe UI", 12),
                     text_color=MUTED).pack(side="left")
        ctk.CTkOptionMenu(r, variable=variable, values=values, width=width,
                          height=30, font=("Segoe UI", 12),
                          dynamic_resizing=False).pack(side="right")

    def _tile(self, parent, col, title, with_frame=False):
        tile = ctk.CTkFrame(parent, fg_color=CARD_BG, corner_radius=10,
                            height=86)
        tile.grid(row=0, column=col, sticky="nsew", padx=4)
        tile.grid_propagate(False)
        ctk.CTkLabel(tile, text=title, font=("Segoe UI", 12),
                     text_color=MUTED).pack(anchor="w", padx=12,
                                            pady=(9, 0))
        val = ctk.CTkLabel(tile, text="\u2014", font=("Segoe UI", 13, "bold"),
                           text_color=TEXT, anchor="w", justify="left")
        if with_frame:
            val.pack(anchor="w", padx=12, pady=(3, 0))
        else:
            val.pack(anchor="w", padx=12, pady=(3, 10))
        if with_frame:
            return tile, val
        return val

    # ---------------- EVENTOS DESDE SiteWindow (gui.py) ----------------
    def set_busy(self, busy: bool):
        state = "disabled" if busy else "normal"
        for nom in ("btn_sync", "btn_backfill", "btn_dl", "btn_verify",
                    "btn_opt"):
            btn = getattr(self, nom, None)
            if btn is not None:
                btn.configure(state=state)
        btn_stop = getattr(self, "btn_stop", None)
        if btn_stop is not None:
            btn_stop.configure(state="normal" if busy else "disabled")

    def job_finished(self, name: str, code: int, dur: str):
        self.jobs.append({
            "t": time.strftime("%d/%m %H:%M"),
            "name": name,
            "dur": dur,
            "code": code,
        })
        self.jobs = self.jobs[-20:]
        try:
            JOBS_FILE.write_text(json.dumps(self.jobs, ensure_ascii=False,
                                            indent=2), encoding="utf-8")
        except Exception:
            pass
        self._render_jobs()

    def reload_jobs(self):
        """Vuelve a leer el historial compartido (jobs_history.json).

        Lo usan las ventanas que NO fueron la dueña del trabajo: el
        historial es único para todas las ventanas."""
        self.jobs = self._load_jobs()
        self._render_jobs()

    def log(self, text: str, tag: str | None = None):
        if not text or not self.winfo_exists():
            return
        log_box = getattr(self, "log_box", None)
        if log_box is None:
            return          # dashboard aún en construcción
        if tag is None:
            tag = classify_line(text)
        ts = time.strftime("%H:%M:%S")
        log_box.configure(state="normal")
        log_box.insert("end", f"[{ts}] ", "ts")
        if tag:
            log_box.insert("end", text + "\n", tag)
        else:
            log_box.insert("end", text + "\n")
        content = log_box.get("1.0", "end").splitlines()
        if len(content) > 600:
            log_box.delete("1.0", f"{len(content) - 600}.0")
        log_box.see("end")
        self.log_box.configure(state="disabled")

    def clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")

    def copy_log(self):
        try:
            txt = self.log_box.get("1.0", "end-1c")
            self.clipboard_clear()
            self.clipboard_append(txt)
            self.log("\U0001f4cb Registro copiado al portapapeles.", "ok")
        except Exception as e:
            self.log(f"[X] No se pudo copiar: {e}")

    # ---------------- ESTADÍSTICAS ----------------
    def refresh_stats(self):
        if not self._built:
            return          # dashboard aún en construcción
        stats = self.app.fetch_stats()
        if not stats or not stats.get("ok"):
            err = (stats or {}).get("error", "sin datos")
            for val in self._card_vals.values():
                val.configure(text="\u2014")
            for sitio, (lbl, bar, pct) in self._meta_rows.items():
                bar.set(0)
                pct.configure(text="\u2014")
            if self._mysql_ok is not False:
                self.log(f"[BD] Error: {err}", "err")
                self._mysql_ok = False
            self.lbl_mysql.configure(text="\u274c Sin conexi\u00f3n")
            self._render_chart([])
            return

        if self._mysql_ok is not True:
            self._mysql_ok = True
        self.lbl_mysql.configure(
            text=f"\u2705 MySQL \u2014 {stats.get('ms', 0)} ms")

        for _icon, _title, key, _color in self._card_defs:
            self._card_vals[key].configure(
                text=fmt_num(stats.get(key, 0)))

        # Gr\u00e1fico de descargas (repinta solo si cambi\u00f3)
        self._render_chart(stats.get("descargas_dias") or [])

        # Barras por sitio
        seen = set()
        for sitio, total, con in stats.get("sitios", []):
            seen.add(sitio)
            if sitio not in self._meta_rows:
                rowf = ctk.CTkFrame(self.meta_body, fg_color="transparent")
                rowf.pack(fill="x", pady=4)
                lbl = ctk.CTkLabel(rowf, text="", font=("Segoe UI", 12),
                                   text_color=TEXT, anchor="w")
                pct = ctk.CTkLabel(rowf, text="", width=52,
                                   font=("Segoe UI", 12, "bold"),
                                   text_color=TEXT, anchor="e")
                bar = ctk.CTkProgressBar(rowf, height=10)
                pct.pack(side="right")
                lbl.pack(side="left", padx=(0, 10))
                bar.pack(side="left", fill="x", expand=True)
                self._meta_rows[sitio] = (lbl, bar, pct)
            lbl, bar, pct = self._meta_rows[sitio]
            lbl.configure(
                text=f"{sitio} \u2014 metadatos {fmt_num(con)}/{fmt_num(total)}")
            bar.set((con / total) if total else 0)
            pct.configure(
                text=f"{100 * con // total}%" if total else "\u2014")
        for sitio in list(self._meta_rows):
            if sitio not in seen:
                lbl, bar, pct = self._meta_rows.pop(sitio)
                lbl.master.destroy()

    def _render_chart(self, rows):
        """Barras de descargas por día (últimos 14).

        `rows` = [(fecha_iso, n), ...] solo de días con actividad.
        Repinta solo si la serie cambió (se llama cada 5 s).
        """
        lookup = {}
        for f, n in rows:
            key = f.isoformat() if hasattr(f, "isoformat") else str(f)
            lookup[key[:10]] = int(n)
        hoy = datetime.now().date()
        fechas = [hoy - timedelta(days=i) for i in range(13, -1, -1)]
        serie = [lookup.get(d.isoformat(), 0) for d in fechas]
        if tuple(serie) == self._chart_data:
            return
        self._chart_data = tuple(serie)

        for w in self._chart_body.winfo_children():
            w.destroy()
        total = sum(serie)
        self.lbl_chart_sum.configure(
            text=f"{fmt_num(total)} c\u00f3mics" if total else "sin actividad")
        if total == 0:
            ctk.CTkLabel(self._chart_body,
                         text="\U0001f4ed Sin descargas en los \u00faltimos "
                              "14 d\u00edas",
                         font=("Segoe UI", 12), text_color=MUTED,
                         height=84).pack(fill="x")
            return

        maxv = max(serie) or 1
        BAR_H = 70
        row = ctk.CTkFrame(self._chart_body, fg_color="transparent")
        row.pack(fill="x")
        for d, n in zip(fechas, serie):
            col = ctk.CTkFrame(row, fg_color="transparent")
            col.pack(side="left", expand=True, fill="x")
            ctk.CTkLabel(col, text=str(n) if n else "",
                         font=("Segoe UI", 10, "bold"),
                         text_color=TEXT if n else MUTED).pack(
                anchor="center")
            holder = ctk.CTkFrame(col, fg_color="transparent", height=BAR_H)
            holder.pack(fill="x", padx=3)
            holder.pack_propagate(False)
            if n:
                h = max(6, round(BAR_H * n / maxv))
                color = ACCENT
            else:
                h = 3
                color = "#2e2e2e"
            ctk.CTkFrame(holder, fg_color=color, corner_radius=4,
                         height=h).pack(side="bottom", fill="x")
            es_hoy = (d == hoy)
            ctk.CTkLabel(col, text=d.strftime("%d"),
                         font=("Segoe UI", 9,
                               "bold" if es_hoy else "normal"),
                         text_color=ACCENT if es_hoy else MUTED).pack(
                anchor="center", pady=(2, 0))

    def _render_jobs(self):
        hist_box = getattr(self, "hist_box", None)
        if hist_box is None:
            return          # dashboard aún en construcción
        hist_box.configure(state="normal")
        hist_box.delete("1.0", "end")
        if not self.jobs:
            hist_box.insert("end", "Sin trabajos a\u00fan en esta sesi\u00f3n.\n")
        for j in reversed(self.jobs):
            ok = j.get("code") == 0
            icon = "\u2705" if ok else "\u274c"
            extra = "" if ok else f" \u2014 error {j.get('code')}"
            line = (f"{j.get('t', '')}  {icon} {j.get('name', '')}  "
                    f"\u2014 {j.get('dur', '?')}{extra}\n")
            hist_box.insert("end", line, "ok" if ok else "err")
        hist_box.see("1.0")
        hist_box.configure(state="disabled")

    # ---------------- SISTEMA ----------------
    def _tick(self):
        if not self.winfo_exists():
            return
        # Resultados del escaneo en segundo plano
        try:
            while True:
                res = self._scan_queue.get_nowait()
                self._scan_running = False
                self.btn_recalc.configure(state="normal")
                self.lbl_coll.configure(
                    text=f"\U0001f4c1 {fmt_num(res['files'])} archivos \u00b7 "
                         f"{fmt_bytes(res['bytes'])}  ({res['at']})")
                update_config(disk_scan=res)
        except queue.Empty:
            pass
        # Disco libre
        try:
            self.lbl_disk.configure(text=self._disk_info())
        except Exception:
            pass
        # Estadísticas de la BD
        self.refresh_stats()
        self.after(5000, self._tick)

    def _disk_info(self) -> str:
        dest = self.app.dest_var.get().strip() or DEFAULT_DEST
        p = Path(dest)
        while not p.exists() and p.parent != p:
            p = p.parent
        du = shutil.disk_usage(str(p))
        return (f"{p.drive or p} \u2014 {du.free / 1024 ** 3:.1f} GB libres "
                f"de {du.total / 1024 ** 3:.0f} GB")

    def _start_scan(self):
        """Escanea la colección en un hilo de fondo (nunca en el hilo UI)."""
        if self._scan_running:
            return
        self._scan_running = True
        self.btn_recalc.configure(state="disabled")
        self.lbl_coll.configure(text="\u23f3 Escaneando la colecci\u00f3n\u2026")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        dest = self.app.dest_var.get().strip() or DEFAULT_DEST
        path = Path(dest)
        while not path.exists() and path.parent != path:
            path = path.parent
        files = 0
        total = 0
        try:
            for root, _dirs, names in os.walk(str(path)):
                for n in names:
                    try:
                        total += os.path.getsize(os.path.join(root, n))
                        files += 1
                    except OSError:
                        pass
        except Exception:
            pass
        self._scan_queue.put({
            "path": str(path),
            "files": files,
            "bytes": total,
            "at": time.strftime("%d/%m %H:%M"),
        })

    # ---------------- CARGA INICIAL ----------------
    def _load_cache(self):
        """Muestra el último escaneo conocido de la colección."""
        scan = load_config().get("disk_scan")
        lbl = getattr(self, "lbl_coll", None)
        if scan and lbl is not None:
            lbl.configure(
                text=f"\U0001f4c1 {fmt_num(scan.get('files', 0))} archivos \u00b7 "
                     f"{fmt_bytes(scan.get('bytes', 0))}  ({scan.get('at', '?')})")

    def _load_jobs(self) -> list:
        try:
            data = json.loads(JOBS_FILE.read_text(encoding="utf-8"))
            return data[-20:] if isinstance(data, list) else []
        except Exception:
            return []