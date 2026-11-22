# -*- coding: utf-8 -*-
"""
Excepciones propias del proyecto.
Permite manejo granular de errores y distingue entre tipos de fallo.
"""
from __future__ import annotations
from typing import Any


class ComicManagerError(Exception):
    """Base para todos los errores del proyecto."""
    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        if self.details:
            return f"{self.message} | {self.details}"
        return self.message


# --- Base de datos ---
class DatabaseError(ComicManagerError):
    """Error genérico de base de datos (conexión, query, constraint)."""


class ConnectionError(DatabaseError):
    """No se pudo conectar a MySQL (XAMPP apagado, credenciales mal, red)."""


class QueryError(DatabaseError):
    """Error ejecutando una query (sintaxis, constraint, deadlock)."""


class MigrationError(DatabaseError):
    """Error en migración/esquema (falta tabla, índice, columna)."""


# --- Scraper / HTTP ---
class ScraperError(ComicManagerError):
    """Error genérico del scraper."""


class CloudflareBlocked(ScraperError):
    """Cloudflare sigue bloqueando tras reintentos (huella TLS, cookies caducadas)."""
    def __init__(self, url: str, status: int | None = None, *, details=None):
        msg = f"Cloudflare bloqueó la petición a {url}"
        if status:
            msg += f" (HTTP {status})"
        super().__init__(msg, details=details)
        self.url = url
        self.status = status


class HttpError(ScraperError):
    """Error HTTP genérico (4xx, 5xx, timeout, red)."""
    def __init__(self, url: str, status: int | None = None, *, details=None):
        msg = f"Error HTTP en {url}"
        if status:
            msg += f" (HTTP {status})"
        super().__init__(msg, details=details)
        self.url = url
        self.status = status


class ParseError(ScraperError):
    """No se pudo parsear la respuesta (HTML/JSON inesperado, selectores rotos)."""
    def __init__(self, message: str, *, url: str | None = None, details=None):
        super().__init__(message, details=details)
        self.url = url


class SelectorMissing(ParseError):
    """Un selector CSS esperado no encontró elementos (cambio de layout web)."""


# --- Descarga ---
class DownloadError(ComicManagerError):
    """Error genérico de descarga."""


class FileTooSmall(DownloadError):
    """Archivo descargado < umbral mínimo (probable página de error)."""


class DiskFullError(DownloadError):
    """Espacio insuficiente en disco."""


class DestinationError(DownloadError):
    """Carpeta destino no accesible (permisos, no existe, ruta inválida)."""


# --- Optimización ---
class OptimizationError(ComicManagerError):
    """Error re-codificando una imagen."""


class UnsupportedFormatError(OptimizationError):
    """Formato de imagen no soportado (solo JPEG/PNG)."""


# --- Pawchive API ---
class PawchiveError(ComicManagerError):
    """Error genérico de la API de Pawchive."""


class PawchiveApiError(PawchiveError):
    """La API devolvió {"error": ...}."""
    def __init__(self, message: str, *, endpoint: str | None = None, details=None):
        super().__init__(message, details=details)
        self.endpoint = endpoint


class PawchiveNoImages(PawchiveError):
    """Post sin imágenes descargables (solo video/preview)."""


# --- Configuración ---
class ConfigError(ComicManagerError):
    """Error de configuración (falta clave, valor inválido, archivo corrupto)."""


# --- Utilidades ---
def is_retryable_error(exc: Exception) -> bool:
    """
    Determina si un error merece reintento automático.
    Útil en decoradores de reintento genéricos.
    """
    if isinstance(exc, (ConnectionError, HttpError, CloudflareBlocked)):
        # Solo reintentar 5xx y 429; 4xx (salvo 429) no
        if isinstance(exc, HttpError) and exc.status and 400 <= exc.status < 500 and exc.status != 429:
            return False
        return True
    if isinstance(exc, (TimeoutError, OSError, IOError)):
        return True
    return False


def classify_http_status(status: int) -> str:
    """Clasifica un código HTTP para logging/métricas."""
    if 200 <= status < 300:
        return "success"
    if status == 429:
        return "rate_limited"
    if 400 <= status < 500:
        return "client_error"
    if 500 <= status < 600:
        return "server_error"
    return "unknown"