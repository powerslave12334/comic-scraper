# Comic Scraper & Manager

**Pipeline completo:** Scraper (HTTP con huella TLS de Chrome / Playwright) → MySQL (XAMPP) → Descarga masiva de imágenes optimizadas (q82)

Gestiona cómics de **vercomicsporno.com**, **vermangasporno.com** y **pawchive.pw** (Patreon/Fanbox/Discord vía API JSON).

---

## 🚀 Instalación Rápida

### 1. Requisitos Previos
- **Windows** (probado en Windows 10/11)
- **XAMPP** con **MySQL** activo (`C:\xampp\mysql\bin\mysql.exe` en PATH)
- **Brave** instalado (para cookies `cf_clearance` opcionales)
- **Python 3.10+**

### 2. Clonar e Instalar Dependencias

```cmd
git clone https://github.com/powerslave12334/comic-scraper.git
cd comic-scraper\scripts

pip install -r requirements.txt
playwright install chromium   # solo necesario para modo navegador (respaldo)
```

### 3. Base de Datos
```cmd
# Opción A: phpMyAdmin → Importar → scripts\db_setup.sql
# Opción B: Línea de comandos
C:\xampp\mysql\bin\mysql.exe -u root < db_setup.sql
```
Crea la BD `mis_comics` con tablas: `comics`, `paginas`, `etiquetas`, `comic_etiquetas`.

---

## 📋 Uso Básico

### Interfaz Gráfica (Recomendado)
```cmd
cd scripts
python gui.py        # o gui.bat
```
- **Lanzador** → abre ventanas por sitio (📕 VCP / 📗 VMP / 🐾 Pawchive)
- Cada ventana tiene: **📊 Dashboard** + **📚 Colección** (+ **🐾 Explorar** en Pawchive)
- Un **único trabajo global** a la vez (scraper/descarga/optimización/verificación)
- Configuración persistente en `gui_config.json` (destino, hilos, motor, ventanas abiertas, geometrías)

### Scraper (Catálogo + Metadatos + URLs)
```cmd
# Modo HTTP (rápido, sin navegador) - por defecto
python scraper.py --site vcp           # vercomicsporno.com
python scraper.py --site vmp           # vermangasporno.com
python scraper.py --site pch           # pawchive.pw (API JSON)
python scraper.py --site all           # vcp + vmp + pch secuencial

# Opciones útiles
python scraper.py --mode http          # Solo HTTP (sin navegador)
python scraper.py --mode browser       # Fuerza Playwright + Chrome visible
python scraper.py --limit 5            # Solo 5 páginas (pruebas)
python scraper.py --skip-catalog       # Solo backfill: páginas/metadatos pendientes
```

### Descarga Masiva de Imágenes
```cmd
python download.py --dest "J:\MisComics" --workers 8
python download.py --site vcp --dest "J:\Comics"
python download.py --types imagen,video   # Solo imágenes y videos (pawchive)
python download.py --no-optimize          # Sin re-codificar a q82
```
- **Reanudable**: omite archivos ya descargados
- **Optimización automática q82** al terminar (--no-optimize para desactivar)
- Estructura: `<destino>/<sitio>/<titulo>/001.jpg`

### Optimización de Imágenes (q82 ~30% menos peso)
```cmd
python optimize.py --dest "J:\Comics" --quality 82 --workers 6
python optimize.py --dry-run   # Solo informe, no modifica
```

### Verificación BD ↔ Disco
```cmd
python verify_dl.py --sync-estado    # Corrige comics.estado según disco
python verify_dl.py --dest "J:\Comics"
```

---

## 🐾 Pawchive (Patreon / Fanbox / Discord)

Usa **API JSON** (sin navegador, sin Cloudflare):

```cmd
# 1. Configurar creadores (pestaña 🐾 Explorar en GUI o editar JSON)
#    Se guardan en pawchive_creators.json

# 2. Sincronizar
python scraper.py --site pch
python download.py --site pch --dest "J:\Comics"
```

**En la GUI (pestaña 🐾 Explorar):**
- **🎨 Artistas** → Índice cacheado (~99.300 creadores, 4.3 MB). Filtros: nombre, servicio (Patreon/Fanbox/Discord), ★ Solo mis creadores
- **🔍 Posts** → Búsqueda global o **modo creador** (clic en artista → sus posts, paginación 50, búsqueda interna `q=`)
- **Selección acumulada** entre páginas → **Importar metadatos a BD** (estado `pendiente`, luego descarga desde 📚 Colección)

---

## 🗄️ Base de Datos (`mis_comics`)

| Tabla | Descripción |
|-------|-------------|
| `comics` | `id`, `titulo`, `url_comic`, `url_visor`, `autor`, `genero`, `fecha_public`, `descripcion`, `estado` (pendiente/descargado/duplicado), `n_paginas_web`, `sitio` (vcp/vmp/pch), `fecha_descarga` |
| `paginas` | `id`, `comic_id`, `numero`, `url_imagen`, `tipo` (imagen/video/archivo) |
| `etiquetas` | `id`, `nombre`, `slug` |
| `comic_etiquetas` | `comic_id`, `etiqueta_id` (relación n:m) |

### Estados (`comics.estado`)
| Valor | Significado |
|-------|-------------|
| `pendiente` | No está completo en disco (con o sin URLs en BD) |
| `descargado` | **Todas** las imágenes están en disco |
| `duplicado` | Mismo título en otro sitio (no se descarga) |

---

## 🎨 Interfaz Gráfica - Detalles

### 🚀 Lanzador
- Botones por sitio con recuento de cómics (hilo de fondo)
- Estado del trabajo global + **⏻ Cerrar todo**
- Recuerda ventanas abiertas + geometría por sitio

### 📊 Dashboard (por sitio)
- 6 tarjetas de estadísticas + gráfico 14 días
- Metadatos del sitio + estado sistema (MySQL, disco, escaneo colección)
- Controles: Scraping / Descarga / Utilidades / 🗑 Vaciar sitio
- Historial (20 últimos) + Registro con colores

### 📚 Colección (por sitio)
- Tabla por bloques (500 filas) + orden por cabecera
- Buscador + filtro Estado + barra de selección (📂 Abrir / 📋 Copiar / 🔗 URL)
- Menú contextual + doble clic = abrir carpeta
- Estados vacíos contextuales

### 🐾 Explorar (solo Pawchive)
- **Artistas**: índice cacheado, filtros, ➕/➖ mis creadores → `pawchive_creators.json`
- **Posts**: búsqueda global + **modo creador** (clic artista → sus posts, búsqueda interna, paginación 50)
- **Selección acumulada** entre páginas → **Importar metadatos** → BD (`pendiente`)
- Preview flotante al hacer hover (archivos, contadores, botón abrir en web)

---

## ⚡ Trabajos Globales (Un Solo Candado)

- **Solo UN trabajo a la vez** en toda la app (scraper/download/optimize/verify)
- Ventana dueña: ve log, progreso, **⏹ Parar**
- Otras ventanas: botones deshabilitados + aviso «⏳ Trabajo en otra ventana»
- Al terminar: broadcast → tablas/historial se recargan solos

---

## 📦 Archivos Principales

| Archivo | Función |
|---------|---------|
| `db_setup.sql` | Esquema BD `mis_comics` |
| `scraper.py` | Catálogo + URLs + metadatos (`--mode http/browser/auto`, `--site vcp/vmp/pch/all`) |
| `pawchive_api.py` | Cliente API Pawchive (creadores, posts, búsqueda, detalle) |
| `brave_cf.py` | Motor HTTP: huella TLS Chrome (`curl_cffi`) + cookies Brave opcionales |
| `download.py` | Descarga masiva por cómic/sitio (`--types`, `--site`, `--workers`, q82 auto) |
| `optimize.py` | Re-codifica JPEG/PNG en sitio (q82, sin EXIF, ICC, `--dry-run`) |
| `gui.py` / `gui.bat` | Lanzador + ventana por sitio (Dashboard + Colección + Explorar) |
| `dashboard.py` | Dashboard embebible (tarjetas, gráfico, controles, historial, log) |
| `verify_dl.py` | BD ↔ Disco + `--sync-estado` |
| `requirements.txt` | Dependencias Python |

---

## 🔧 Solución de Problemas

| Problema | Solución |
|----------|----------|
| HTTP 403 / Cloudflare | 1. `python brave_cf.py` (verifica motor HTTP)<br>2. `--mode browser` (respaldo)<br>3. Cierra Brave y reintenta |
| HTTP 429 (Rate limit) | `download.py` usa **exponential backoff** (2s→4s→8s→16s→32s, máx 5 reintentos) |
| MySQL no conecta | XAMPP → MySQL activo? Puerto 3306? `DB` dict en scripts coincide? |
| Brave abierto y falla cookies | Cierra Brave; las cookies solo se leen con Brave cerrado |
| Preview no aparece | Hover 150ms sobre fila; se oculta al sacar ratón de la tabla |

---

## 📝 Licencia

Uso personal / educativo. Respeta los términos de los sitios origen.

---

## 📁 Estructura del Proyecto

```
comic-scraper/
├── scripts/
│   ├── gui.py                 # Lanzador + ventana por sitio (CTk)
│   ├── gui.bat                # Acceso directo Windows
│   ├── dashboard.py           # Dashboard embebible (frame)
│   ├── scraper.py             # Scraper principal (HTTP/Playwright)
│   ├── pawchive_api.py        # Cliente API Pawchive
│   ├── brave_cf.py            # Motor HTTP (curl_cffi + cookies Brave)
│   ├── download.py            # Descarga masiva (q82 auto)
│   ├── optimize.py            # Optimización imágenes (q82)
│   ├── verify_dl.py           # Verificación BD ↔ Disco
│   ├── scraper.py             # Backfill / catalog sync
│   ├── db_setup.sql           # Esquema BD MySQL
│   ├── requirements.txt       # Dependencias Python
│   ├── gui_config.json        # Config persistente (destino, hilos, etc.)
│   ├── pawchive_creators.json # Creadores Pawchive a sincronizar
│   ├── pawchive_index_cache.json # Índice artistas cacheado
│   ├── pawchive_thumbs/       # Cache miniaturas (auto)
│   ├── assets/                # Iconos SVG + patrones
│   ├── requirements.txt
│   ├── db_setup.sql
│   ├── gui.bat / scrape.bat / download.bat
│   └── README.md              # Este archivo (en scripts/)
└── README.md                  # ← Este archivo (raíz)
```

---

## 🙏 Créditos

- **curl_cffi** — Huella TLS de Chrome sin navegador
- **customtkinter** — UI moderna con tema oscuro
- **playwright** — Respaldo con Chrome real
- **Pillow** — Optimización de imágenes (q82, ICC, sin EXIF)

---

> **Nota:** Este proyecto es para uso personal/educativo. Respeta los términos de servicio de los sitios origen.