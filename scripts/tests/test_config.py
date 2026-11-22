# -*- coding: utf-8 -*-
"""Tests básicos para la configuración y capas centrales."""
import sys
from pathlib import Path

# Añadir el directorio padre al path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


def test_config_loads():
    """Verifica que config.py carga sin errores y expone las claves esperadas."""
    import config
    assert hasattr(config, "DB_CONFIG")
    assert hasattr(config, "DEST_DIR")
    assert hasattr(config, "SITES")
    assert hasattr(config, "MODE_VALUES")
    assert hasattr(config, "ESTADO_FILTROS")
    assert hasattr(config, "COLORS")
    assert hasattr(config, "SITE_ACCENTS")
    # Valores por defecto
    assert config.DB_CONFIG["host"] == "127.0.0.1"
    assert config.DB_CONFIG["user"] == "root"
    assert config.JPEG_QUALITY == 82


def test_exceptions_hierarchy():
    """Verifica la jerarquía de excepciones."""
    from exceptions import (
        ComicManagerError, DatabaseError, ConnectionError, QueryError,
        ScraperError, CloudflareBlocked, HttpError, ParseError,
        DownloadError, FileTooSmall, OptimizationError,
        PawchiveError, PawchiveApiError, PawchiveNoImages,
        ConfigError,  # noqa: F401
        is_retryable_error, classify_http_status,
    )

    # Jerarquía básica
    assert issubclass(DatabaseError, ComicManagerError)
    assert issubclass(ConnectionError, DatabaseError)
    assert issubclass(QueryError, DatabaseError)

    assert issubclass(ScraperError, ComicManagerError)
    assert issubclass(CloudflareBlocked, ScraperError)
    assert issubclass(HttpError, ScraperError)
    assert issubclass(ParseError, ScraperError)

    assert issubclass(DownloadError, ComicManagerError)
    assert issubclass(FileTooSmall, DownloadError)

    assert issubclass(OptimizationError, ComicManagerError)

    assert issubclass(PawchiveError, ComicManagerError)
    assert issubclass(PawchiveApiError, PawchiveError)
    assert issubclass(PawchiveNoImages, PawchiveError)

    # is_retryable_error
    assert is_retryable_error(ConnectionError("test"))
    assert is_retryable_error(HttpError("url", status=500))
    assert is_retryable_error(HttpError("url", status=429))
    assert not is_retryable_error(HttpError("url", status=404))
    assert is_retryable_error(TimeoutError())
    assert not is_retryable_error(ValueError("bad value"))

    # classify_http_status
    assert classify_http_status(200) == "success"
    assert classify_http_status(429) == "rate_limited"
    assert classify_http_status(404) == "client_error"
    assert classify_http_status(500) == "server_error"


def test_db_module_imports():
    """Verifica que db.py se importa y expone las funciones clave."""
    import db
    assert hasattr(db, "init_pool")
    assert hasattr(db, "get_pool")
    assert hasattr(db, "connection")
    assert hasattr(db, "transaction")
    assert hasattr(db, "execute")
    assert hasattr(db, "fetch_one")
    assert hasattr(db, "fetch_all")
    assert hasattr(db, "fetch_iter")
    assert hasattr(db, "ComicRepository")
    assert hasattr(db, "PageRepository")
    assert hasattr(db, "TagRepository")


def test_sites_config():
    """Verifica la configuración de sitios."""
    from config import SITES
    assert "vcp" in SITES
    assert "vmp" in SITES
    assert "pch" in SITES
    assert SITES["vcp"]["nombre"] == "vercomicsporno"
    assert SITES["vmp"]["nombre"] == "vermangasporno"
    assert SITES["pch"]["nombre"] == "pawchive"


def test_mode_values():
    """Verifica los valores de modo de scraping."""
    from config import MODE_VALUES
    assert MODE_VALUES["Auto (cookies de Brave)"] == "auto"
    assert MODE_VALUES["HTTP rápido (sin navegador)"] == "http"
    assert MODE_VALUES["Solo navegador"] == "browser"


def test_estado_filtros():
    """Verifica los filtros de estado."""
    from config import ESTADO_FILTROS
    assert ESTADO_FILTROS == ("Todos", "pendiente", "descargado", "duplicado")


def test_invalid_chars_regex():
    """Verifica el regex de caracteres inválidos."""
    from config import INVALID_CHARS
    import re
    pattern = re.compile(INVALID_CHARS)
    # Debe detectar caracteres inválidos de Windows
    assert pattern.search('test<file>') is not None
    assert pattern.search('test:file') is not None
    assert pattern.search('test"file') is not None
    assert pattern.search('test|file') is not None
    assert pattern.search('test?file') is not None
    assert pattern.search('test*file') is not None
    # Caracteres válidos no deben match
    assert pattern.search('test file') is None
    assert pattern.search('test-file') is None
    assert pattern.search('test_file') is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])