# Mejoras Propuestas para el Gestor de Cómics

## Base de Datos (MySQL)
1. **Índices compuestos**: Añadir índices en `(sitio, estado)`, `(fecha_descarga)`, `(sitio, n_paginas_web)` para acelerar consultas frecuentes
2. **Tabla de referencia `sitios`**: Normalizar `comics.sitio` con FK a tabla `sitios(id, codigo, nombre, base_url, catalogo_url)` en lugar de VARCHAR
3. **Particionado**: Considerar particionar `paginas` por `comic_id` si la tabla crece mucho (>10M filas)
4. **Connection pooling**: Usar `mysql.connector.pooling` en lugar de abrir/cerrar conexiones constantemente

## Scraper (`scraper.py`)
5. **Retry con backoff exponencial** en modo HTTP: actualmente solo `sleep(0.4-1.0)`, añadir reintentos con `2^attempt * base_delay` ante 429/5xx
6. **Rate limiting por dominio**: `asyncio.Semaphore` + token bucket para no saturar el servidor
7. **Checkpointing de progreso**: Guardar `last_page_processed` en BD/archivo para reanudar tras interrupción
8. **Logging estructurado (JSON)**: Opción `--log-json` para parsers automáticos (ELK, Loki, etc.)
9. **Validación de selectores**: Verificar que los selectores CSS siguen funcionando al inicio (health check)
10. **User-Agent rotation**: Rotar UA de una lista realista para evitar fingerprinting

## Descargador (`download.py`)
11. **Límite de ancho de banda**: `--max-speed 5M` para no saturar la red
12. **Límite concurrente por dominio**: `Semaphore` por host (ej. max 2 conexiones simultáneas a `vercomicsporno.com`)
13. **Resume HTTP Range**: Soporte para reanudar archivos parciales (`Range: bytes=X-`)
14. **Verificación de integridad**: `--checksum` opcional (MD5/SHA256 de archivos conocidos)
15. **Limpieza de archivos huérfanos**: Opción `--clean-orphans` para borrar archivos en disco sin entrada en BD

## GUI (`gui.py`, `dashboard.py`)
16. **Atajos de teclado**: `Ctrl+R` (refrescar), `Ctrl+F` (buscar), `Ctrl+Enter` (abrir carpeta), `Delete` (vaciar sitio)
17. **Personalización de columnas**: Menú para mostrar/ocultar columnas en la tabla (ID, Pág. web, Sitio, etc.)
18. **Exportar lista filtrada**: Botón "Exportar CSV/JSON" con la selección actual
19. **Operaciones en lote UI**: Selección múltiple → "Descargar seleccionados", "Marcar como...", "Eliminar de BD"
20. **Persistencia de trabajo**: Guardar estado del job en `jobs_history.json` para reanudar tras cierre accidental
21. **Mensajes de error amigables**: Traducir errores técnicos (ej. "CfBlocked" → "Cloudflare bloqueó la petición; cierra Brave o usa --mode browser")
22. **Tema persistente**: El toggle light/dark ya existe, pero verificar que se aplica a TODOS los widgets (incl. Treeview ttk)
23. **Indicador de salud del sistema**: En dashboard, mostrar latencia de BD, espacio disco, última sync exitosa

## Pawchive (`pawchive_api.py`, `scraper.py` scrape_pawchive_*)
24. **Sync incremental**: Guardar `last_post_id` o `last_sync_timestamp` por creador para solo traer posts nuevos
25. **Soporte para vídeos**: Opción `--include-videos` para descargar mp4/webm (actualmente se omiten)
26. **Filtros avanzados en Explorar**: Por fecha, por nº de imágenes, por favoritos
27. **Previsualización en miniatura**: Cache local de thumbnails ya funciona, pero añadir zoom al hacer hover

## Brave Cloudflare (`brave_cf.py`)
28. **Soporte multi-navegador**: Edge, Chrome, Firefox (browser_cookie3 ya soporta varios)
29. **Detección automática de perfil**: Buscar perfil "Default" o el más reciente si hay varios
30. **Cache de cookies por dominio separado**: Ya existe `domain` param, pero mejorar aislamiento

## Optimizador (`optimize.py`)
30. **Salida WebP**: `--format webp` para ~25% más ahorro vs JPEG q82 (con fallback JPEG)
31. **Barra de progreso con ETA**: `tqdm` o similar en CLI, integrable en GUI
32. **Detección de imágenes ya optimizadas**: Heurística por tamaño/calidad para saltar `--dry-run` innecesarios

## General / Arquitectura
33. **Configuración centralizada**: `config.yaml` o `config.toml` con todos los parámetros (DB, paths, timeouts, selectors)
34. **Variables de entorno para secretos**: `DB_PASSWORD`, `BRAVE_PATH`, etc. en `.env` (no hardcoded)
35. **Type hints completos**: Añadir `py.typed` y pasar `mypy --strict`
36. **Linting y formateo**: `ruff` + `black` en pre-commit hooks
37. **Tests unitarios**: `pytest` para funciones puras (`norm_title`, `clean_img_src`, `parse_metadata`, `esperado_de_fila`, `safe_name`)
38. **Tests de integración**: Mock HTTP (`respx`) para scraper/download sin red real
39. **CI/CD**: GitHub Actions con lint, type-check, tests, build exe (PyInstaller/Nuitka)
40. **Documentación técnica**: `ARCHITECTURE.md` con diagrama de flujo de datos, esquema BD, decisiones de diseño

## Seguridad
41. **Credenciales fuera del código**: `DB` dict leído de `.env` o `config.yaml` (nunca en repo)
42. **Validación de entrada**: Sanitizar `titulo`, `url_comic` antes de INSERT (prevenir inyección aunque use prepared statements)
43. **Permisos de archivo**: `optimize_manifest.json` y `pawchive_creators.json` con permisos restrictivos (solo usuario)

## Rendimiento
44. **Batch INSERT**: En `scrape_catalog_http`, acumular 100-500 filas y hacer `executemany` en vez de uno a uno
45. **Prepared statements cache**: Reutilizar cursores preparados para inserts repetidos
46. **Async HTTP**: Migrar `brave_cf.fetch` a `curl_cffi.AsyncSession` + `asyncio` para scraper HTTP concurrente
47. **Índice de búsqueda full-text**: `ALTER TABLE comics ADD FULLTEXT(titulo)` + `MATCH() AGAINST()` para búsqueda instantánea

## Mantenibilidad
48. **Modularización**: Separar `scraper.py` en `scraper_http.py`, `scraper_browser.py`, `scraper_pawchive.py`, `models.py`, `db.py`
49. **Dataclasses/Pydantic**: Modelos tipados para `Comic`, `Page`, `Tag`, `Creator`, `Post` en lugar de tuplas/dicts
50. **Dependency injection**: Pasar `db`, `session`, `config` como parámetros en lugar de globales