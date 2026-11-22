# Ideas de Mejoras — Base de Datos de Cómics

## ✅ Implementado en esta iteración

### Arranque fluido de la GUI (ventana por fases)

- `open_site()` devuelve en **~90-250 ms** y la ventana se ve en **~200-300 ms**
  (antes: ~3,4 s con la ventana congelada durante toda la construcción).
- **Fase 1** (síncrona): pestañas + Explorar + barra inferior, con un
  placeholder *⏳ Cargando…* en el Dashboard.
- **Fase 2** (al pintarse la ventana, evento `<Map>`): Dashboard construido
  **por pasos**, cediendo el control a Tk entre paso y paso.
- **Fase 3**: pestaña Colección + primera carga de filas.
- `update_idletasks()` **acotado** durante la construcción (customtkinter lo
  llama en cada `_draw` de optionmenu/scrollbar y drenaba toda la
  maquetación pendiente).
- **Guardia de reentrancia**: `CTkScrollbar._draw` → `canvas.update_idletasks()`
  → `<Configure>` → otro `_draw` (recursión que congelaba 2,5 s al final).
- Congelón máximo medido (mainloop real): **180-370 ms** (antes 910 ms y 2,5 s).

### Grid de artistas paginado (pestaña 🐾 Explorar)

- Grid de **3×3 = 9 tarjetas por página** (170×210): avatar + banda con el
  **nombre** y **`servicio · id`**.
- Barra de paginación: **‹ Anterior**, **Página X de Y**, **Siguiente ›** y
  salto directo a una página (1.500 artistas = 167 páginas).
- Solo se crean 9 tarjetas a la vez (antes: grid virtualizado con scroll
  infinito y `scrollregion` forzado al alto total del índice).
- La **selección sobrevive al cambio de página** (se guarda por índice) y al
  filtrar se vuelve a la página 1.
- El panel de artistas y el de posts se reparten con **grid** (no con
  `pack`): el Treeview de posts ya no se come el ancho y las **3 columnas**
  de tarjetas caben enteras (antes la 3ª quedaba cortada).

### Posts como tarjetas paginadas (pestaña 🐾 Explorar)

- El buscador de posts deja de ser un **Treeview** (tabla de 50 filas) y
  pasa a un **grid de 3×3 = 9 tarjetas por página**, igual que los
  artistas: portada, **título**, **creador · fecha** y
  **servicio · 🖼 n 🎬 n 📁 n**.
- Barra de paginación: **‹ Anterior**, **Página X de Y**, **Siguiente ›** y
  salto directo. La API sigue entregando bloques de **50** posts (`o=50`),
  pero en pantalla solo se pintan 9: al llegar al final de un bloque se
  pide el siguiente a la API y la numeración de página continúa (pág. 7 de
  12, …). El salto directo también cambia de bloque si hace falta.
- **Selección por clic** (marcar/desmarcar) con la acumulación entre
  páginas intacta; **Todo** marca los de la página actual, **Limpiar**
  vacía todo. **Doble clic** abre el post en el navegador.
- **Portadas recortadas al tamaño real de la tarjeta**: la imagen se guarda
  cuadrada en la caché y se recorta (`ImageOps.fit`) al ancho/alto de la
  etiqueta, así **llena la tarjeta sin deformarse**; si no hay portada se
  ve la inicial del título.
- El **preview** al pasar el ratón (detalle del post + lista de archivos) se
  pide **en un hilo**: antes se hacía en el hilo principal y congelaba la
  ventana mientras arrive la respuesta de la API.
- La caché de portadas tiene **versión propia** (`p2:`) para no mezclar los
  PNG antiguos (con fondo gris) con los nuevos (recorte cuadrado).

### Miniaturas: por qué no se veían (y arreglo)

| Causa | Detalle |
|---|---|
| `tk.PhotoImage` creado **fuera del hilo principal** | Tk no admite crear objetos desde otro hilo: la llamada falla y el `except Exception: pass` se la tragaba → **ninguna** miniatura se pintaba nunca |
| `after()` llamado desde el hilo de descarga | GTK/Tk no es seguro entre hilos: ahora la imagen viaja por la **cola de mensajes** (`_pch_q`) y el `PhotoImage` se crea en el hilo principal |
| El overlay tapaba el avatar | El overlay opaco cubría toda la tarjeta: ahora es una banda inferior y el avatar se ve |
| Sin caché en disco | Ahora el PNG se guarda en `scripts/pawchive_thumbs/` y la segunda vez se pinta **sin red** |
| `PhotoImage` plano en un `CTkLabel` | Aviso de customtkinter en pantallas HiDPI: las tarjetas usan ahora `CTkImage` (escala bien) |
| Errores silenciosos | Si una miniatura falla se muestra la inicial del artista y el error queda en el registro |

### Bugs corregidos

| Bug | Síntoma | Causa |
|---|---|---|
| Botón **Descargar invisible** | Las secciones "Descarga" y "Utilidades" del Dashboard quedaban aplastadas a 1×1 | `left.pack_propagate(False)` sin altura fija: el packer repartía el espacio de la ventana |
| Importación de posts Pawchive rota | `AttributeError: module 'scraper' has no attribute 'cargar_titulos_otros'` | La función usaba la API anterior a `db.py` (`ComicRepository`/`PageRepository`) |
| `upsert()` siempre devolvía *"existe"* | Las páginas y tags no se guardaban al importar | `db.rowcount_affected()` devolvía siempre 0 |
| Clic en una tarjeta de artista sin efecto | `AttributeError: '_on_artist_card_click'` | El método se usaba en los `bind` pero nunca se definió |
| Caché de cookies de Cloudflare muerta | `load_cache()` siempre devolvía `None` (el `NameError` lo tragaba el `except`) | La constante pasó a llamarse `CF_CACHE_MAX_AGE` en `config.py` pero el uso se quedó con el nombre viejo |
| `db_setup.sql` no se podía importar | `CREATE INDEX IF NOT EXISTS` no existe en MySQL (es sintaxis de MariaDB) | Los índices opcionales ahora van comentados con la nota |

### Verificación (sin descargas)

- `scripts/tests/` (pytest): 7/7 OK.
- `smoke_gui.py`: **91/91** comprobaciones de la GUI (lanzador, ventanas,
  filtros, candado, pestaña Explorar, modo creador, importación con API falsa).
- `test_creator_gui.py` / `test_import_real.py`: importación real de un post
  de Pawchive (solo metadatos) → comprueba páginas y tags en la BD y **limpia
  todo** al terminar.
- `test_ui.py`, `test_index_gui.py`, `test_all_files.py`, `test_tags.py`,
  `test_e2e.py`, `test_sites.py`, `test_dash_layout.py`, `test_dash_scroll.py`,
  `test_pch_open.py`, `test_pch_speed.py`: OK.
- `test_paginacion.py` (nuevo): 19/19 — 9 tarjetas por página, 3 páginas,
  botones y salto directo, la selección sobrevive al cambio de página y la
  **miniatura real se pinta** (red + caché en disco).
- `test_posts_cards.py` (nuevo): 32/32 — 9 tarjetas de posts por página
  (3×3), paginación local dentro del bloque de 50, salto directo (incluido
  el cambio de bloque), selección por clic que **acumula entre páginas**,
  **Todo**, portada real pintada (red + caché en disco), preview flotante
  al pasar el ratón y borde de acento en la tarjeta seleccionada.
- `test_gap.py` mide el congelón **con el mainloop real** (con un `update()`
  en bucle el número sale inflado ~10×: artefacto de la medición).
- Estado final de la BD tras la batería: 11.157 vcp + 9.185 vmp,
  **0 pawchive**, 606.748 páginas, 7.324 etiquetas.

---

## Resumen del Proyecto
Pipeline completo: **Scraper (HTTP + TLS fingerprint) → MySQL → Descarga masiva → Optimización q82 → Verificación**
- Sitios: vercomicsporno.com (vcp), vermangasporno.com (vmp), Pawchive API (pch)
- GUI: Lanzador + 3 ventanas por sitio (Dashboard + Colección + Explorar Pawchive)
- Estado: `pendiente` / `descargado` / `duplicado` (cross-site)
- Motor HTTP: `curl_cffi` con huella TLS de Chrome + cookies Brave opcionales

---

## 🎯 Mejoras de Alto Impacto (Implementar Primero)

### 1. Logging Estructurado y Persistente
**Problema:** Logs solo en consola/GUI; se pierden al cerrar. Difícil depurar fallos en producción.
**Solución:**
- Añadir `logging` con `RotatingFileHandler` en `scraper.py`, `download.py`, `optimize.py`
- Niveles: DEBUG (detalle HTTP), INFO (progreso), WARNING (reintentos), ERROR (fallos)
- Archivo único `scraper.log` rotativo (5 MB, 3 backups)
- Incluir `site`, `comic_id`, `url` en cada log estructurado (JSON lines)

### 2. Pool de Conexiones Realmente Usado
**Problema:** `scraper.py` y `download.py` crean conexiones directas `mysql.connector.connect()` ignorando el pool de `db.py`.
**Solución:**
- Cambiar `scraper.py` y `download.py` para usar `db.connection()` y `db.transaction()`
- Eliminar conexiones `mysql.connector.connect()` manuales
- Pool size configurable (default 10, ajustable por `DB_POOL_SIZE` env)

### 3. Métricas y Health Checks
**Problema:** No hay visibilidad de rendimiento (latencia HTTP, throughput descarga, tiempo BD).
**Solución:**
- Decorador `@timed` en funciones críticas (`fetch`, `download`, `optimize_file`, queries BD)
- Exponer `/metrics` endpoint simple (Prometheus text) o log periódico con stats
- Health check: `python -m health` verifica MySQL, disco, Brave cookies, conectividad a sitios

### 4. Rate Limiting Adaptativo por Sitio
**Problema:** `time.sleep(random.uniform(0.4, 1.0))` fijo; no responde a 429/5xx.
**Solución:**
- Token bucket por dominio (`vercomicsporno.com`, `vermangasporno.com`, `pawchive.pw`)
- Backoff exponencial automático en 429/5xx (ya en download, faltaría en scraper HTTP)
- Respetar `Retry-After` header si viene

### 5. Tests Unitarios Críticos
**Problema:** Cero tests. Cambios en selectores/parsing rompen silenciosamente.
**Solución (pytest):**
- `test_scraper.py`: mocks HTML → `parse_metadata`, `detect_last_page`, `clean_img_src`
- `test_pawchive_api.py`: JSON fixtures → `post_tags`, `all_files`, `count_by_type_list`
- `test_download.py`: `esperado_de_fila`, `carpeta_comic`, `safe_name`
- `test_optimize.py`: imágenes sintéticas → `_encode`, `optimize_file` (dry-run)
- `test_db.py`: repo methods con BD en memoria (sqlite) o testcontainers MySQL
- CI: GitHub Actions (Windows + Ubuntu) ejecutando tests en cada push

---

## 🔧 Mejoras de Código y Arquitectura

### 6. Tipado Estricto (mypy strict)
**Estado actual:** Type hints parciales, `Any` abundante, `ignore` implícitos.
**Acción:**
- `pyproject.toml` → `[tool.mypy]` strict=true, `disallow_untyped_defs = true`
- Añadir stubs: `mysql-connector-python` (typeshed), `curl_cffi`, `customtkinter`
- Corregir `rowcount_affected()` falso en `db.py` (línea 395-400)

### 7. Configuración Unificada (Pydantic Settings)
**Problema:** `config.py` mezcla defaults, JSON, env vars manualmente.
**Solución:**
- `pydantic-settings.BaseSettings` con `env_file=".env"`, `env_nested_delimiter="__"`
- Validación de tipos, valores por defecto, docs en campos
- `Settings` singleton inyectado en lugar de imports globales

### 8. Eliminar Variables Globales Mutables
**Problema:** `scraper.py` usa `SITE_KEY`, `SITE`, `BASE_URL`, `CATALOG_URL` globales mutados por `set_site()`.
**Solución:**
- `SiteConfig` dataclass/frozen passado explícitamente a funciones
- `Scraper(site: SiteConfig)` clase con métodos de instancia

### 9. Separación CLI / Lógica de Negocio
**Problema:** `scraper.py`, `download.py`, `optimize.py` mezclan `argparse` + lógica.
**Solución:**
- `scraper_core.py` / `download_core.py` / `optimize_core.py`: funciones puras, sin CLI
- `cli/scraper.py`, `cli/download.py`, `cli/optimize.py`: solo parsing args + llamada a core
- Permite importar y usar desde tests, otros scripts, API futura

### 10. Manejo de Errores Unificado
**Problema:** `try/except Exception` genéricos, re-raise inconsistente, sin context.
**Solución:**
- `Result[T, E]` pattern (o `returns` lib) para operaciones fallibles
- `except (CloudflareBlocked, HttpError) as e:` específico, no `except Exception:`
- `logger.exception()` con traceback en lugar de `print(f"[X] {e}")`

---

## 🖥️ Mejoras de GUI y UX

### 11. Barra de Progreso Real (determinada) en Scraping
**Problema:** Scraper usa `indeterminate`; usuario no sabe cuánto falta.
**Solución:**
- Pre-contar cómics pendientes antes de iniciar (query `COUNT(*)`)
- Emitir `progreso: X/Y` cada N cómics (ya existe en download)
- Dashboard muestra barra + ETA estimado

### 12. Filtro de Tags en Tabla Colección
**Problema:** Solo filtro por estado y búsqueda texto. Tags no filtrables.
**Solución:**
- Checkbox "🏷 Filtrar por tags" → abre diálogo multi-select con tags del sitio
- Query: `WHERE c.id IN (SELECT comic_id FROM comic_etiquetas WHERE etiqueta_id IN (...))`
- Indicador visual: chip con nº tags activos al lado del buscador

### 13. Exportar / Importar Colección (CSV/JSON)
**Problema:** No hay forma de sacar datos para backup o migración.
**Solución:**
- Botón "📤 Exportar" → CSV/JSON con: id, titulo, estado, sitio, paginas, url, tags, fecha_descarga
- Botón "📥 Importar" → upsert por `url_comic` (idempotente), útil para migrar entre PCs

### 14. Modo "Solo Verificación" en Dashboard
**Problema:** Verificar disco lanza subproceso; no hay feedback granular.
**Solución:**
- Botón "🔍 Verificar (rápido)" → cuenta archivos en hilo fondo, actualiza badges en tabla
- Columna "% disco" en tabla (existentes/esperados) con barra de progreso por fila
- Tooltip en fila: lista archivos faltantes

### 15. Atajos de Teclado y Accesibilidad
**Problema:** Todo por ratón; power users lentos.
**Atajos propuestos:**
- `Ctrl+R` = Refrescar tabla
- `Ctrl+F` = Foco buscador
- `Enter` en fila = Abrir carpeta
- `Ctrl+C` en selección = Copiar títulos
- `Del` = Marcar como duplicado (soft delete)
- `Space` = Toggle selección fila
- `F1` = Ayuda contextual (tooltips expandidos)

### 16. Tema Personalizable (User Themes)
**Problema:** Solo light/dark hardcodeado; colores fijos en `config.py`.
**Solución:**
- `themes/` carpeta con `.json` (nombre, paleta, acentos por sitio)
- Selector en Launcher → guarda en `gui_config.json`
- Generador de tema desde color base (algoritmo armónico)

---

## ⚡ Mejoras de Rendimiento

### 17. Descarga Paralela por Cómic (no solo global)
**Actual:** `ThreadPoolExecutor` global reparte páginas de distintos cómics.
**Mejora:** Agrupar por `comic_id` → descargar páginas de un cómic en secuencia (mejor cache DNS/TCP), cómics en paralelo. Reduce latencia tail.

### 18. Índice de Artistas Pawchive Incremental
**Problema:** `/creators` devuelve 99k items; se descarga completo cada vez.
**Solución:**
- `If-Modified-Since` / `ETag` en request (si API soporta)
- Delta: guardar `favorited` timestamp; solo actualizar creadores con `favorited > last_sync`
- Background sync periódico (configurable) en lugar de manual

### 19. Batch INSERT en Scraper (ya parcial, mejorar)
**Actual:** `PageRepository.insert_batch` usa `INSERT IGNORE` por lote.
**Mejora:**
- `executemany` con chunks de 500-1000 (evita paquetes MySQL grandes)
- `LOAD DATA LOCAL INFILE` para catálogo inicial (CSV temporal → 10x más rápido)

### 20. Caché de Miniaturas Pawchive Compartida
**Problema:** Cada ventana descarga sus propias miniaturas; duplicadas en disco.
**Solución:**
- `pawchive_thumbs/` global (ya existe) → clave `sha1(service:id:post_id)`
- `fetch_thumb` verifica archivo local antes de HTTP
- Limpieza automática: borrar thumbnails > 30 días sin usar

---

## 🛡️ Seguridad y Robustez

### 21. Validación de Rutas (Path Traversal)
**Riesgo:** `carpeta_comic()` usa `safe_name()` pero `titulo` viene de BD (controlado por scraper).
**Acción:** 
- `safe_name` ya filtra `<>:"/\|?*` y truncamiento; OK.
- Añadir test: `assert ".." not in safe_name("../../etc/passwd")`

### 22. Sanitización de Entrada Usuario (GUI)
**Riesgo:** `edit_pawchive_creators` pega URLs; `parse_creator_url` usa regex.
**Acción:** Validar que `service ∈ {patreon,fanbox,discord}`, `id` numérico, longitud máx.

### 23. Credenciales MySQL en Keyring (no plaintext)
**Problema:** `gui_config.json` y env vars pueden tener password en claro.
**Solución:**
- `keyring` backend (Windows Credential Manager, macOS Keychain, Linux Secret Service)
- `DB_PASSWORD` opcional en env; si ausente → `keyring.get_password("comic_manager", "mysql")`
- GUI: botón "🔐 Guardar contraseña en keyring"

### 24. Timeouts Configurables por Operación
**Problema:** `HTTP_TIMEOUT=40` global; descarga imágenes 45s, API Pawchive 30s.
**Solución:** Timeouts por tipo: `scrape_timeout`, `download_timeout`, `api_timeout`, `thumb_timeout` en config.

---

## 📦 Empaquetado y Distribución

### 25. Ejecutable Standalone (PyInstaller / Nuitka)
**Objetivo:** Usuario final sin Python instalado.
**Pasos:**
- `pyproject.toml` `[tool.pyinstaller]` o `nuitka` config
- Incluir: `brave_cf` (DLLs Windows), `playwright` (solo si modo browser), `customtkinter` assets
- One-file o one-dir; firmar certificado (evitar SmartScreen)
- Auto-updater: `pyupdater` o GitHub Releases + verificación hash

### 26. Instalador Windows (Inno Setup / NSIS)
- Crea shortcuts, registra desinstalador, pide XAMPP si no detectado
- Opción "Ejecutar al inicio" (tray icon + sync programado)

### 27. Documentación Usuario (MkDocs + GitHub Pages)
- `docs/` con: instalación, configuración XAMPP, Brave cookies, GUI tour, CLI reference, troubleshooting
- Generado desde docstrings + markdown narrativo
- Badge "docs passing" en README

---

## 🧪 Testing y CI/CD

### 28. Pipeline CI (GitHub Actions)
```yaml
# .github/workflows/ci.yml
jobs:
  test:
    runs-on: windows-latest  # + ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
      - run: pip install -r requirements.txt -r requirements-dev.txt
      - run: mypy --strict .
      - run: ruff check .
      - run: pytest -v --cov=.
      - run: python -m pytest test_gui.py --headed  # optional
  build:
    needs: test
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install nuitka
      - run: python -m nuitka --standalone --onefile cli/main.py
      - uses: actions/upload-artifact@v4
```

### 29. Tests de Integración (Testcontainers)
- MySQL real en container temporal
- `scraper.py --mode http --limit 1` contra sitio real (marcar `@pytest.mark.integration`)
- `download.py` contra servidor HTTP local (pytest-httpserver)

### 30. Fuzzing Selectores HTML
- `hypothesis` + HTML generado → `parse_metadata`, `detect_last_page` no crashean
- Detecta regresiones por cambios de layout web

---

## 💡 Ideas Experimentales (Futuro)

### 31. API REST Local (FastAPI)
- `GET /api/comics?site=vcp&estado=pendiente&limit=50`
- `POST /api/scrape` → job asíncrono + WebSocket progreso
- Permite: frontend web, app móvil, integración con otros tools

### 32. Detección de Cambios en Web (Selectors Watchdog)
- Hash de selectores críticos (`article.pp-card`, `div.pp-comic-content img`)
- Ejecución diaria: `python scraper.py --site vcp --limit 1 --mode http`
- Si falla selector → alerta (log, email, notificación Windows)

### 33. Deduplicación Perceptual (pHash)
- `imagehash` en descarga: comparar pHash nuevos vs existentes
- Detecta: mismo cómic distinto título, recortes, watermarks
- Opcional (costoso CPU); activar con `--dedup-perceptual`

### 34. Soporte Multi-usuario (SQLite local + sync)
- Cada usuario su `gui_config.json`, `pawchive_creators.json`, `jobs_history.json`
- BD compartida (MySQL) con `usuario_id` en tablas
- Lock por usuario en GUI (ya existe `ACTIVE_JOB` global)

### 35. Plugin System (Hooks)
- `hooks/pre_scrape.py`, `post_download.py`, `pre_optimize.py`
- Usuario pone scripts Python en `hooks/` → se ejecutan automáticamente
- Casos: notificar Telegram, subir a cloud, renombrar por convención propia

---

## 📋 Checklist de Priorización (MoSCoW)

| Prioridad | Item | Esfuerzo | Impacto |
|-----------|------|----------|---------|
| **Must** | Logging estructurado + archivo | Bajo | Alto |
| **Must** | Pool BD real en scraper/download | Bajo | Alto |
| **Must** | Tests unitarios core (scraper, pawchive, download) | Medio | Alto |
| **Must** | mypy strict + CI | Medio | Alto |
| **Should** | Rate limiting adaptativo | Medio | Alto |
| **Should** | Separar CLI / core | Medio | Medio |
| **Should** | Filtro tags en GUI | Medio | Alto |
| **Should** | Exportar/Importar colección | Bajo | Medio |
| **Could** | Tema personalizable | Medio | Bajo |
| **Could** | Ejecutable standalone | Alto | Medio |
| **Could** | API REST local | Alto | Medio |
| **Won't** | Multi-usuario (v1) | Muy alto | Bajo |
| **Won't** | Deduplicación perceptual (v1) | Alto | Bajo |

---

## 🚀 Próximos Pasos Sugeridos

1. **Semana 1:** Logging + Pool BD + mypy strict + CI básico
2. **Semana 2:** Tests unitarios (scraper, pawchive, download, optimize, db)
3. **Semana 3:** Rate limiting adaptativo + Separar CLI/core
4. **Semana 4:** Filtro tags GUI + Exportar/Importar + Barra progreso real
5. **Continuo:** Issues GitHub para cada item; PRs pequeños y revisados

---

## Notas de Implementación

- **No romper compatibilidad:** BD schema igual, `gui_config.json` igual, estructura carpetas igual
- **Feature flags:** Nuevas features detrás de config (`enable_tags_filter = true`)
- **Migración gradual:** `scraper_core.py` nuevo convive con `scraper.py` legacy; switch por config
- **Documentar decisiones:** `ADR/` (Architecture Decision Records) para cambios mayores