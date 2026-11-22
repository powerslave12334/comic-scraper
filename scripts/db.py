# -*- coding: utf-8 -*-
"""
Capa de acceso a datos (Database + Repository).
- Connection pool thread-safe (mysql-connector-python pooling)
- Repositorios tipados para Comics, Pages, Tags
- Transacciones explícitas y helpers de batch
"""
from __future__ import annotations
import contextlib
import logging
import threading
from typing import Generator, Iterator, Sequence

import mysql.connector
import mysql.connector.pooling
from mysql.connector import MySQLConnection
from mysql.connector.pooling import MySQLConnectionPool

from config import DB_CONFIG
from exceptions import ConnectionError, QueryError

log = logging.getLogger(__name__)

# Pool global (inicializado en init_pool)
_pool: MySQLConnectionPool | None = None
_pool_lock = threading.Lock()


def init_pool(pool_size: int = 10, **override) -> MySQLConnectionPool:
    """
    Inicializa el pool de conexiones.
    Debe llamarse una vez al arrancar la app (gui.py, scraper.py main, etc.).
    """
    global _pool
    with _pool_lock:
        if _pool is not None:
            return _pool
        cfg = {**DB_CONFIG, **override}
        cfg["pool_name"] = "comic_pool"
        cfg["pool_size"] = pool_size
        cfg["pool_reset_session"] = True
        try:
            _pool = mysql.connector.pooling.MySQLConnectionPool(**cfg)
            log.info("Pool MySQL inicializado (size=%d)", pool_size)
        except mysql.connector.Error as e:
            raise ConnectionError(f"No se pudo crear pool MySQL: {e}") from e
        return _pool


def get_pool() -> MySQLConnectionPool:
    """Devuelve el pool (lo inicializa con defaults si hace falta)."""
    global _pool
    if _pool is None:
        return init_pool()
    return _pool


@contextlib.contextmanager
def connection() -> Generator[MySQLConnection, None, None]:
    """
    Context manager para obtener una conexión del pool.
    Uso:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(...)
            conn.commit()
    El commit/rollback es responsabilidad del llamador.
    """
    pool = get_pool()
    conn = pool.get_connection()
    try:
        yield conn
    except mysql.connector.Error as e:
        # No hacemos rollback automático: el caller decide
        raise QueryError(f"Error de BD: {e}") from e
    finally:
        # Devuelve la conexión al pool (no cierra realmente)
        try:
            conn.close()
        except Exception:
            pass


@contextlib.contextmanager
def transaction() -> Generator[MySQLConnection, None, None]:
    """
    Context manager para transacción atómica.
    Hace commit si no hay excepción, rollback si la hay.
    Uso:
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(...)
            # commit automático al salir
    """
    with connection() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise


def execute(sql: str, params: Sequence | None = None, *, many: bool = False) -> int:
    """
    Ejecuta INSERT/UPDATE/DELETE y devuelve rowcount.
    Si many=True, usa executemany (params debe ser lista de tuplas).
    """
    with transaction() as conn:
        with conn.cursor() as cur:
            if many:
                cur.executemany(sql, params or [])
            else:
                cur.execute(sql, params or ())
            return cur.rowcount


def fetch_one(sql: str, params: Sequence | None = None) -> tuple | None:
    """Ejecuta SELECT y devuelve una fila (o None)."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchone()


def fetch_all(sql: str, params: Sequence | None = None) -> list[tuple]:
    """Ejecuta SELECT y devuelve todas las filas."""
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            return cur.fetchall()


def fetch_iter(sql: str, params: Sequence | None = None, batch: int = 1000) -> Iterator[tuple]:
    """
    Itera resultados en batches (server-side cursor).
    Útil para consultas grandes sin cargar todo en memoria.
    """
    with connection() as conn:
        with conn.cursor(buffered=False) as cur:
            cur.execute(sql, params or ())
            while True:
                rows = cur.fetchmany(batch)
                if not rows:
                    break
                for row in rows:
                    yield row


# -------------- REPOSITORIOS --------------

class ComicRepository:
    """Operaciones sobre la tabla `comics`."""

    @staticmethod
    def upsert(
        titulo: str,
        url_comic: str,
        sitio: str,
        estado: str = "pendiente",
        n_paginas_web: int | None = None,
        **extra
    ) -> tuple[str, int | None]:
        """
        Inserta o actualiza un cómic.
        Devuelve (status, comic_id) donde status ∈ {"nuevo", "existe", "duplicado"}.
        """
        # Verificar duplicado en OTRO sitio (mismo título normalizado)
        norm = " ".join(titulo.split()).lower()
        other = fetch_one(
            "SELECT id FROM comics WHERE sitio <> %s AND "
            "LOWER(TRIM(REPLACE(REPLACE(titulo, '\r', ''), '\n', ''))) = %s",
            (sitio, norm)
        )
        if other:
            # Duplicado cross-site
            execute(
                "INSERT IGNORE INTO comics (titulo, url_comic, sitio, estado) "
                "VALUES (%s, %s, %s, 'duplicado')",
                (titulo, url_comic, sitio)
            )
            return "duplicado", None

        # Insert normal (IGNORE por unique key url_comic)
        affected = execute(
            "INSERT IGNORE INTO comics (titulo, url_comic, sitio, estado, n_paginas_web) "
            "VALUES (%s, %s, %s, %s, %s)",
            (titulo, url_comic, sitio, estado, n_paginas_web)
        )
        # Obtener ID (sea nuevo o existente)
        row = fetch_one("SELECT id FROM comics WHERE url_comic = %s", (url_comic,))
        comic_id = row[0] if row else None
        # affected == 0 -> la fila ya existía (INSERT IGNORE no hizo nada)
        status = "nuevo" if (comic_id and affected > 0) else "existe"
        return status, comic_id

    @staticmethod
    def get_pending(sitio: str, include_no_metadata: bool = True) -> list[tuple]:
        """Cómics pendientes de imágenes y/o metadatos."""
        if include_no_metadata:
            sql = ("SELECT id, titulo, url_comic FROM comics "
                   "WHERE sitio = %s AND estado <> 'duplicado' "
                   "AND (estado = 'pendiente' OR n_paginas_web IS NULL)")
        else:
            sql = ("SELECT id, titulo, url_comic FROM comics "
                   "WHERE sitio = %s AND estado = 'pendiente'")
        return fetch_all(sql, (sitio,))

    @staticmethod
    def get_by_site(sitio: str) -> list[tuple]:
        """Todos los cómics de un sitio (para UI)."""
        return fetch_all(
            "SELECT c.id, c.titulo, c.estado, c.sitio, c.n_paginas_web, "
            "COALESCE(p.n, 0), c.url_comic "
            "FROM comics c "
            "LEFT JOIN (SELECT comic_id, COUNT(*) AS n FROM paginas GROUP BY comic_id) p "
            "ON p.comic_id = c.id "
            "WHERE c.sitio = %s ORDER BY c.id DESC",
            (sitio,)
        )

    @staticmethod
    def update_status(comic_id: int, estado: str, fecha_descarga: str | None = None) -> None:
        """Actualiza estado y opcionalmente fecha_descarga."""
        if fecha_descarga:
            execute(
                "UPDATE comics SET estado=%s, fecha_descarga=%s WHERE id=%s",
                (estado, fecha_descarga, comic_id)
            )
        else:
            execute("UPDATE comics SET estado=%s WHERE id=%s", (estado, comic_id))

    @staticmethod
    def update_metadata(comic_id: int, n_paginas_web: int | None, tags: list[tuple[str, str | None]]) -> None:
        """Actualiza n_paginas_web y tags (transacción atómica)."""
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE comics SET n_paginas_web=%s WHERE id=%s",
                    (0 if n_paginas_web is None else n_paginas_web, comic_id)
                )
                for nombre, slug in tags:
                    cur.execute("SELECT id FROM etiquetas WHERE nombre=%s", (nombre,))
                    row = cur.fetchone()
                    if row:
                        etiqueta_id = row[0]
                    else:
                        cur.execute("INSERT INTO etiquetas (nombre, slug) VALUES (%s, %s)", (nombre, slug))
                        etiqueta_id = cur.lastrowid
                    cur.execute(
                        "INSERT IGNORE INTO comic_etiquetas (comic_id, etiqueta_id) VALUES (%s, %s)",
                        (comic_id, etiqueta_id)
                    )

    @staticmethod
    def count_by_status(sitio: str) -> tuple[int, int, int, int]:
        """Devuelve (total, descargados, pendientes, duplicados)."""
        row = fetch_one(
            "SELECT COUNT(*), SUM(estado='descargado'), SUM(estado='pendiente'), SUM(estado='duplicado') "
            "FROM comics WHERE sitio = %s",
            (sitio,)
        )
        return (row[0] or 0, row[1] or 0, row[2] or 0, row[3] or 0) if row else (0, 0, 0, 0)

    @staticmethod
    def stats_metadata(sitio: str) -> list[tuple[str, int, int]]:
        """(sitio, total, con_metadatos) para barras de progreso."""
        return fetch_all(
            "SELECT sitio, COUNT(*), SUM(n_paginas_web IS NOT NULL) "
            "FROM comics WHERE sitio = %s GROUP BY sitio",
            (sitio,)
        )

    @staticmethod
    def downloads_last_14_days(sitio: str) -> list[tuple[str, int]]:
        return fetch_all(
            "SELECT fecha_descarga, COUNT(*) FROM comics "
            "WHERE sitio = %s AND fecha_descarga >= CURDATE() - INTERVAL 13 DAY "
            "GROUP BY fecha_descarga ORDER BY fecha_descarga",
            (sitio,)
        )

    @staticmethod
    def clear_site(sitio: str) -> None:
        """Borra todos los cómics de un sitio (respeta FK cascada)."""
        with transaction() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE ce FROM comic_etiquetas ce JOIN comics c ON c.id = ce.comic_id WHERE c.sitio = %s", (sitio,))
                cur.execute("DELETE p FROM paginas p JOIN comics c ON c.id = p.comic_id WHERE c.sitio = %s", (sitio,))
                cur.execute("DELETE FROM comics WHERE sitio = %s", (sitio,))


class PageRepository:
    """Operaciones sobre la tabla `paginas`."""

    @staticmethod
    def insert_batch(comic_id: int, pages: list[tuple[int, str]]) -> int:
        """
        Inserta múltiples páginas de golpe.
        pages = [(numero, url_imagen), ...]
        Devuelve filas insertadas (ignorando duplicados comic_id+numero).
        """
        if not pages:
            return 0
        sql = "INSERT IGNORE INTO paginas (comic_id, numero, url_imagen) VALUES (%s, %s, %s)"
        data = [(comic_id, num, url) for num, url in pages]
        return execute(sql, data, many=True)

    @staticmethod
    def insert_batch_with_type(comic_id: int, pages: list[tuple[int, str, str]]) -> int:
        """
        Inserta páginas con tipo (imagen/video/archivo).
        pages = [(numero, url_imagen, tipo), ...]
        """
        if not pages:
            return 0
        sql = "INSERT IGNORE INTO paginas (comic_id, numero, url_imagen, tipo) VALUES (%s, %s, %s, %s)"
        data = [(comic_id, num, url, tipo) for num, url, tipo in pages]
        return execute(sql, data, many=True)

    @staticmethod
    def get_by_comic(comic_id: int) -> list[tuple]:
        return fetch_all(
            "SELECT id, comic_id, numero, url_imagen, tipo FROM paginas "
            "WHERE comic_id = %s ORDER BY numero",
            (comic_id,)
        )

    @staticmethod
    def count_by_site(sitio: str) -> int:
        row = fetch_one(
            "SELECT COUNT(*) FROM paginas p JOIN comics c ON c.id = p.comic_id WHERE c.sitio = %s",
            (sitio,)
        )
        return row[0] if row else 0

    @staticmethod
    def expected_files(sitio: str | None = None) -> dict[int, set[str]]:
        """
        Devuelve {comic_id: {filename, ...}} para verificación de disco.
        Si sitio=None, todos los sitios.
        """
        from download import esperado_de_fila
        sql = "SELECT comic_id, numero, url_imagen FROM paginas"
        params = ()
        if sitio:
            sql = ("SELECT p.comic_id, p.numero, p.url_imagen FROM paginas p "
                   "JOIN comics c ON c.id = p.comic_id WHERE c.sitio = %s")
            params = (sitio,)
        rows = fetch_all(sql, params)
        out: dict[int, set[str]] = {}
        for comic_id, numero, url in rows:
            out.setdefault(comic_id, set()).add(esperado_de_fila(numero, url))
        return out


class TagRepository:
    """Operaciones sobre etiquetas."""

    @staticmethod
    def get_or_create(nombre: str, slug: str | None = None) -> int:
        row = fetch_one("SELECT id FROM etiquetas WHERE nombre=%s", (nombre,))
        if row:
            return row[0]
        execute("INSERT INTO etiquetas (nombre, slug) VALUES (%s, %s)", (nombre, slug))
        return fetch_one("SELECT LAST_INSERT_ID()")[0]

    @staticmethod
    def link_comic(comic_id: int, etiqueta_id: int) -> None:
        execute("INSERT IGNORE INTO comic_etiquetas (comic_id, etiqueta_id) VALUES (%s, %s)", (comic_id, etiqueta_id))

    @staticmethod
    def count_distinct(sitio: str) -> int:
        row = fetch_one(
            "SELECT COUNT(DISTINCT ce.etiqueta_id) FROM comic_etiquetas ce "
            "JOIN comics c ON c.id = ce.comic_id WHERE c.sitio = %s",
            (sitio,)
        )
        return row[0] if row else 0

    @staticmethod
    def search_by_name(query: str) -> list[tuple]:
        return fetch_all(
            "SELECT e.nombre, COUNT(*) FROM etiquetas e "
            "JOIN comic_etiquetas ce ON ce.etiqueta_id = e.id "
            "JOIN comics c ON c.id = ce.comic_id "
            "WHERE e.nombre LIKE %s GROUP BY e.nombre ORDER BY 2 DESC LIMIT 50",
            (f"%{query}%",)
        )


# Helper para saber si el último INSERT IGNORE afectó filas.
# OJO: no se puede consultar el rowcount de otra conexión (el pool usa una
# conexión por operación), así que ComicRepository.upsert usa directamente
# el valor devuelto por execute().
def rowcount_affected() -> int:
    """Deprecated: devuelve siempre 0.

    Se conserva por compatibilidad con código antiguo. Usar el valor de
    retorno de `execute()` (misma operación) en su lugar."""
    return 0


# -------------- INICIALIZACIÓN AUTOMÁTICA --------------
# El pool se crea on-demand en get_pool().
# Para forzar inicialización temprana (ej. en tests), llamar init_pool().