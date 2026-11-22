# -*- coding: utf-8 -*-
"""
Configuración centralizada del proyecto.
Un único punto de verdad para DB, paths, timeouts, calidad, hilos, etc.

Lee de (en orden de prioridad):
  1. Variables de entorno (MYSQL_*, DEST_DIR, LOG_LEVEL, etc.)
  2. gui_config.json (config persistida por la GUI)
  3. Valores por defecto hardcodeados (compatibilidad XAMPP)

Uso:
    from config import cfg, DB_CONFIG, DEST_DIR, JPEG_QUALITY
    cfg.get("workers", 6)
    DB_CONFIG["password"]  # ya resuelto (env > json > default)
"""
import json
import os
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "gui_config.json"

# -------------- DEFAULTS (compatibilidad XAMPP) --------------
DEFAULTS: dict[str, Any] = {
    # Base de datos
    "db_host": "127.0.0.1",
    "db_port": 3306,
    "db_user": "root",
    "db_password": "",           # XAMPP por defecto sin password
    "db_name": "mis_comics",
    # Destino de descargas
    "dest_dir": "C:/Comics",
    # Scraper / HTTP
    "http_timeout": 40,
    "http_max_retries": 3,
    "cf_cache_max_age_hours": 6,
    "brave_user_data": str(Path(os.environ.get("LOCALAPPDATA", "")) / "BraveSoftware" / "Brave-Browser" / "User Data"),
    # Descarga
    "download_workers": 6,
    "download_timeout": 45,
    "download_max_retries": 5,
    # Optimización
    "jpeg_quality": 82,
    "min_size_bytes": 200 * 1024,   # 200 KB
    "optimize_workers": 4,
    # GUI
    "appearance": "dark",
    "limit_pages": 0,               # 0 = todas
    "mode_label": "Auto (cookies de Brave)",
    "filter_estado": "Todos",
    "dl_types": "imagen,video,archivo",
    # Logging
    "log_level": "INFO",
    "log_file": "scraper.log",
    # Pawchive
    "pch_pause_sec": 0.3,
    "pch_max_retries": 3,
    "pch_idx_max_rows": 1500,
}

# Mapeo variable de entorno -> clave de config
ENV_MAP = {
    "MYSQL_HOST": "db_host",
    "MYSQL_PORT": "db_port",
    "MYSQL_USER": "db_user",
    "MYSQL_PASSWORD": "db_password",
    "MYSQL_DATABASE": "db_name",
    "DEST_DIR": "dest_dir",
    "LOG_LEVEL": "log_level",
    "JPEG_QUALITY": "jpeg_quality",
    "DOWNLOAD_WORKERS": "download_workers",
    "OPTIMIZE_WORKERS": "optimize_workers",
}


def _load_json_config() -> dict:
    """Lee gui_config.json si existe."""
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _apply_env_overrides(cfg: dict) -> None:
    """Aplica variables de entorno sobre el dict (modifica in-place)."""
    for env_key, cfg_key in ENV_MAP.items():
        val = os.environ.get(env_key)
        if val is not None:
            # Conversión de tipos básica
            if cfg_key in ("db_port", "download_workers", "optimize_workers",
                           "jpeg_quality", "min_size_bytes", "limit_pages",
                           "http_timeout", "download_timeout", "http_max_retries",
                           "download_max_retries", "cf_cache_max_age_hours",
                           "pch_max_retries", "pch_idx_max_rows"):
                try:
                    val = int(val)
                except ValueError:
                    pass
            elif cfg_key in ("pch_pause_sec",):
                try:
                    val = float(val)
                except ValueError:
                    pass
            cfg[cfg_key] = val


def load_config() -> dict:
    """
    Carga configuración completa (defaults + json + env).
    Devuelve dict plano con todas las claves resueltas.
    """
    cfg = DEFAULTS.copy()
    cfg.update(_load_json_config())
    _apply_env_overrides(cfg)
    return cfg


# Configuración ya resuelta (módulo-level, se evalúa al importar)
_cfg = load_config()

# -------------- EXPORTS TIPIFICADOS --------------
DB_CONFIG = {
    "host": _cfg["db_host"],
    "port": _cfg["db_port"],
    "user": _cfg["db_user"],
    "password": _cfg["db_password"],
    "database": _cfg["db_name"],
    "charset": "utf8mb4",
    "collation": "utf8mb4_unicode_ci",
    "autocommit": False,
    "use_pure": False,  # usar C extension si está disponible
}

DEST_DIR = Path(_cfg["dest_dir"]).expanduser()
DEFAULT_DEST = DEST_DIR  # alias para compatibilidad

# Timeouts y reintentos
HTTP_TIMEOUT = _cfg["http_timeout"]
HTTP_MAX_RETRIES = _cfg["http_max_retries"]
CF_CACHE_MAX_AGE = _cfg["cf_cache_max_age_hours"] * 3600
BRAVE_USER_DATA = Path(_cfg["brave_user_data"])

DOWNLOAD_WORKERS = _cfg["download_workers"]
DOWNLOAD_TIMEOUT = _cfg["download_timeout"]
DOWNLOAD_MAX_RETRIES = _cfg["download_max_retries"]

JPEG_QUALITY = _cfg["jpeg_quality"]
MIN_SIZE_BYTES = _cfg["min_size_bytes"]
OPTIMIZE_WORKERS = _cfg["optimize_workers"]

APPEARANCE = _cfg["appearance"]
LIMIT_PAGES = _cfg["limit_pages"]
MODE_LABEL = _cfg["mode_label"]
FILTER_ESTADO = _cfg["filter_estado"]
DL_TYPES = _cfg["dl_types"]

LOG_LEVEL = _cfg["log_level"]
LOG_FILE = SCRIPT_DIR / _cfg["log_file"]

PCH_PAUSE = _cfg["pch_pause_sec"]
PCH_MAX_RETRIES = _cfg["pch_max_retries"]
PCH_IDX_MAX = _cfg["pch_idx_max_rows"]

# Sitios (igual que antes, pero centralizados)
SITES = {
    "vcp": {
        "nombre": "vercomicsporno",
        "base": "https://vercomicsporno.com",
        "catalogo": "https://vercomicsporno.com/sex-comics",
        "label": "VerComicsPorno",
        "icon": "\U0001f4d8",
    },
    "vmp": {
        "nombre": "vermangasporno",
        "base": "https://vermangasporno.com",
        "catalogo": "https://vermangasporno.com/manga-hentai-xxx",
        "label": "VerMangasPorno",
        "icon": "\U0001f4d9",
    },
    "pch": {
        "nombre": "pawchive",
        "base": "https://pawchive.pw",
        "catalogo": "https://pawchive.pw/api/v1",
        "label": "Pawchive",
        "icon": "\U0001f43e",
    },
}

MODE_VALUES = {
    "Auto (cookies de Brave)": "auto",
    "HTTP rápido (sin navegador)": "http",
    "Solo navegador": "browser",
}

ESTADO_FILTROS = ("Todos", "pendiente", "descargado", "duplicado")

INVALID_CHARS = r'[<>:"/\\|?*\x00-\x1f]'

# Colores (mantenidos aquí para que dashboard.py y gui.py los importen de un sitio)
COLORS = {
    "BG": "#141414",
    "PANEL": "#1e1e1e",
    "CARD_BG": "#242424",
    "BORDER": "#2e2e2e",
    "ACCENT": "#3b82f6",
    "ACCENT_HOVER": "#2563eb",
    "SUCCESS": "#22c55e",
    "WARN": "#f59e0b",
    "DANGER": "#ef4444",
    "ORANGE": "#fb923c",
    "CYAN": "#38bdf8",
    "PURPLE": "#a78bfa",
    "TEXT": "#e8e8e8",
    "MUTED": "#9a9a9a",
}

SITE_ACCENTS = {
    "vcp": {"base": "#c0392b", "hover": "#e74c3c", "bg_top": "#1a0a0a", "bg_bot": "#0d0505"},
    "vmp": {"base": "#27ae60", "hover": "#2ecc71", "bg_top": "#0a1a0a", "bg_bot": "#050d05"},
    "pch": {"base": "#8e44ad", "hover": "#9b59b6", "bg_top": "#1a0a1a", "bg_bot": "#0d050d"},
}


def save_gui_config(**kwargs) -> None:
    """
    Actualiza solo las claves indicadas en gui_config.json (persistencia GUI).
    No toca variables de entorno ni defaults.
    """
    current = _load_json_config()
    current.update(kwargs)
    try:
        CONFIG_FILE.write_text(
            json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def get(key: str, default: Any = None) -> Any:
    """Acceso rápido a cualquier clave resuelta (env > json > default)."""
    return _cfg.get(key, default)


def reload() -> None:
    """Recarga la configuración (útil si cambió gui_config.json en caliente)."""
    global _cfg, DB_CONFIG, DEST_DIR, HTTP_TIMEOUT, HTTP_MAX_RETRIES, CF_CACHE_MAX_AGE
    global BRAVE_USER_DATA, DOWNLOAD_WORKERS, DOWNLOAD_TIMEOUT, DOWNLOAD_MAX_RETRIES
    global JPEG_QUALITY, MIN_SIZE_BYTES, OPTIMIZE_WORKERS, APPEARANCE, LIMIT_PAGES
    global MODE_LABEL, FILTER_ESTADO, DL_TYPES, LOG_LEVEL, LOG_FILE
    global PCH_PAUSE, PCH_MAX_RETRIES, PCH_IDX_MAX
    _cfg = load_config()
    # Re-asignar exports (los módulos que ya importaron tendrán valores viejos;
    # para evitar eso, usar config.get() o config.DB_CONFIG siempre)
    DB_CONFIG.update({
        "host": _cfg["db_host"],
        "port": _cfg["db_port"],
        "user": _cfg["db_user"],
        "password": _cfg["db_password"],
        "database": _cfg["db_name"],
    })
    DEST_DIR = Path(_cfg["dest_dir"]).expanduser()
    HTTP_TIMEOUT = _cfg["http_timeout"]
    HTTP_MAX_RETRIES = _cfg["http_max_retries"]
    CF_CACHE_MAX_AGE = _cfg["cf_cache_max_age_hours"] * 3600
    BRAVE_USER_DATA = Path(_cfg["brave_user_data"])
    DOWNLOAD_WORKERS = _cfg["download_workers"]
    DOWNLOAD_TIMEOUT = _cfg["download_timeout"]
    DOWNLOAD_MAX_RETRIES = _cfg["download_max_retries"]
    JPEG_QUALITY = _cfg["jpeg_quality"]
    MIN_SIZE_BYTES = _cfg["min_size_bytes"]
    OPTIMIZE_WORKERS = _cfg["optimize_workers"]
    APPEARANCE = _cfg["appearance"]
    LIMIT_PAGES = _cfg["limit_pages"]
    MODE_LABEL = _cfg["mode_label"]
    FILTER_ESTADO = _cfg["filter_estado"]
    DL_TYPES = _cfg["dl_types"]
    LOG_LEVEL = _cfg["log_level"]
    LOG_FILE = SCRIPT_DIR / _cfg["log_file"]
    PCH_PAUSE = _cfg["pch_pause_sec"]
    PCH_MAX_RETRIES = _cfg["pch_max_retries"]
    PCH_IDX_MAX = _cfg["pch_idx_max_rows"]


# Para compatibilidad: permitir `from config import cfg`
cfg = _cfg