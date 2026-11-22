# Base de datos de cómics — vercomicsporno.com + vermangasporno.com

Pipeline: **Scraper (HTTP con huella TLS de Chrome o Playwright) → MySQL (XAMPP) → Descarga masiva de imágenes**

Sitios compatibles (mismo tema WordPress "pp", selectores idénticos):

| Código | Sitio | Catálogo | Carpeta de descarga |
|---|---|---|---|
| `vcp` | vercomicsporno.com | `/sex-comics` (≈466 págs.) | `<destino>/<título>` (raíz) |
| `vmp` | vermangasporno.com | `/manga-hentai-xxx/` (≈381 págs.) | `<destino>/vermangasporno/<título>` |

## Requisitos

- XAMPP corriendo con **MySQL** activo
- **Brave** instalado (para leer sus cookies automáticamente)
- Python 3.10+ con dependencias:

```cmd
cd "J:\proyect x\scripts"
pip install -r requirements.txt
playwright install chromium   # solo necesario para el modo navegador (respaldo)
```

## 1. Crear la base de datos

Abrir `http://localhost/phpmyadmin` → Importar → `scripts/db_setup.sql`

O desde la consola:

```cmd
C:\xampp\mysql\bin\mysql.exe -u root < db_setup.sql
```

## 2. Extraer el catálogo y las imágenes (scraper)

```cmd
python scraper.py --mode http --limit 1   # prueba rápida: 1 página, sin navegador
python scraper.py                         # modo auto: HTTP primero, navegador de respaldo
python scraper.py --mode browser          # fuerza el navegador (Playwright + Chrome)
```

| Opción | Efecto |
|---|---|
| `--mode auto` | **Por defecto.** Intenta HTTP con cookies de Brave; si Cloudflare bloquea, cae al navegador |
| `--mode http` | Solo HTTP directo (`curl_cffi`), **no abre navegador** (el más rápido) |
| `--mode browser` | Solo Playwright con Chrome real (respaldo) |
| `--site vcp\|vmp\|pch\|all` | **Por defecto `vcp`.** Sitio a sincronizar: vercomicsporno, vermangasporno, pawchive (API) o `all` = vcp+vmp uno tras otro |
| `--limit N` | Solo procesa N páginas del catálogo (para pruebas) |
| `--headless` | Oculta el navegador en modo browser; por defecto es **visible** porque Cloudflare lo exige |

```cmd
python scraper.py --site vmp --limit 1   # prueba: catálogo de vermangasporno
python scraper.py --site all             # sincroniza los dos sitios seguidos
```

### Motor HTTP (sin navegador)

- **No hace falta cerrar Brave**: el motor usa una huella TLS idéntica a la de
  Chrome/Brave (`curl_cffi`), que es lo que Cloudflare comprueba. Las cookies
  `cf_clearance` de Brave son **opcionales** y solo se intentan leer como
  respaldo si alguna petición llegara a bloquearse (y en ese caso solo si Brave
  está cerrado; si está abierto, se usa el modo navegador).
- `brave_cf.py` copia la BD `Network\Cookies` de Brave y descifra las cookies
  con DPAPI/AES de Windows — **no requiere permisos de administrador**. Caché
  de cookies en `cf_cache.json` (válida hasta 6 h).
- El User-Agent es el exacto de tu Brave (derivado de su versión).

Prueba el motor por separado (con Brave abierto o cerrado):

```cmd
python brave_cf.py    # imprime la página del catálogo sin abrir navegador
```

Qué extrae:

- **Catálogo** (`/sex-comics/page/1..466`): selector `article.pp-card` → `a.pp-card-link` + `h3.pp-card-title` → tabla `comics`
- **Páginas de cada cómic**: selector `div.pp-comic-content img` (todas las imágenes cargan en una sola vista) → tabla `paginas`
- **Metadatos de cada cómic** (de la página del cómic):
  - `div.pp-pagecount` → columna `comics.n_paginas_web` (nº de páginas que
    indica la web; `0` = la web no muestra el dato, `NULL` = aún sin consultar)
  - `div.pp-taglist a.pp-tag` → tablas `etiquetas` + `comic_etiquetas`
    (relación n:m; p. ej. `#Artist: DarkerGrey`, `#BBW`, …)

| Opción extra | Efecto |
|---|---|
| `--skip-catalog` | No revisa el catálogo; solo procesa cómics pendientes o sin metadatos (backfill). Ej.: `python scraper.py --mode http --skip-catalog` |

### Duplicados entre sitios

El contenido de ambas webs se solapa (hay cómics publicados en los dos sitios).
Al sincronizar, si el **título** de una tarjeta ya existe en el *otro* sitio
(comparación sin distinguir mayúsculas ni espacios repetidos), el cómic se
guarda con `estado='duplicado'`:

- Aparece en la BD/GUI con el aviso **⚠️ Duplicado**.
- **No se le extraen imágenes** → `download.py` no lo baja (no se gastan GB
  en repetir el mismo cómic).
- Para traerlo igualmente: `UPDATE comics SET estado='pendiente' WHERE id=…`.

### Estado de los cómics (`comics.estado`)

| Valor | Significado |
|---|---|
| `pendiente` | Aún **no está completo en disco** (con o sin URLs extraídas en la BD) |
| `descargado` | **Todas** las imágenes esperadas del cómic existen en su carpeta |
| `duplicado` | Mismo título en el otro sitio; sin imágenes (ver arriba) |

- El scraper **nunca** marca `descargado`: al extraer las URLs el cómic
  sigue `pendiente`.
- `download.py`, al terminar, comprueba en disco cada cómic procesado y
  pone `descargado` solo si está al 100 % (si falta alguna imagen, queda
  `pendiente` y la próxima ejecución reintenta solo lo que falta).
- `verify_dl.py --sync-estado` corrige la columna según el disco (útil si
  borras carpetas a mano o cambias de carpeta destino); en la GUI está el
  botón **🎞 Verificar disco**.

Consultar los tags desde MySQL:

```sql
-- Cómics con un tag concreto
SELECT c.titulo FROM comics c
JOIN comic_etiquetas ce ON ce.comic_id = c.id
JOIN etiquetas e ON e.id = ce.etiqueta_id
WHERE e.nombre = 'Full Color';

-- Nº de uso de cada tag
SELECT e.nombre, COUNT(*) FROM etiquetas e
JOIN comic_etiquetas ce ON ce.etiqueta_id = e.id
GROUP BY e.nombre ORDER BY 2 DESC;
```

## 3. Descargar las imágenes

```cmd
python download.py                          # te preguntará la carpeta destino
python download.py --dest "D:\MiColeccion"  # destino directo, sin preguntar
python download.py --dest "E:\comics" --workers 8
python download.py --site vmp --dest "J:\comics"   # solo los de vermangasporno
```

- **Elegir dónde descargar**:
  - `--dest "RUTA"` / `-d "RUTA"` → indica la carpeta.
  - Sin `--dest` → pregunta por pantalla (`Enter` = `C:\Comics`).
  - `--no-prompt` → no pregunta (usa `--dest` o el valor por defecto); útil para
    tareas programadas.
- **Elegir sitio**: `--site vcp|vmp|pch|all` (**por defecto `all`** = todos los que
  haya registrados).
- La carpeta se crea automáticamente si no existe.
- Estructura:
  - `vercomicsporno` → `<destino>\<título>\001.jpg` (raíz, **sin mover** la
    colección existente)
  - `vermangasporno` → `<destino>\vermangasporno\<título>\001.jpg`
  - `pawchive` → `<destino>\pawchive\<título>\001.jpg`
- **Reanudable**: los archivos ya existentes se omiten (si cambias de carpeta,
  todo se descarga de nuevo en la nueva).
- **Estado al terminar**: comprueba el disco y marca `estado='descargado'`
  solo a los cómics cuya carpeta tiene todas sus imágenes; el resto queda
  `pendiente` (se ve en la tabla de la GUI).
- **Motor**: `curl_cffi` con huella TLS de Chrome (mismo motor que el scraper,
  sin navegador). Si Cloudflare devuelve 403, renueva las cookies y reintenta.

## Optimizar imágenes (q82, sin pérdida visual)

Las webs sirven JPEG de calidad alta (q90+); re-codificarlos a **q82**
aporta un **~30% menos de peso** sin diferencia visible al leer (medido en
muestra real: −31%). El nombre y la extensión **no cambian**, así que la
reanudación de `download.py` y la verificación de `verify_dl.py` siguen
funcionando igual.

- **JPEG**: quita el EXIF (aplicando antes la orientación, por si acaso),
  conserva el perfil ICC (si no, cambian los colores), `optimize` +
  progresivo. **PNG**: re-codificado sin pérdida.
- Solo reemplaza si el resultado es más pequeño (nunca agranda).
- `optimize_manifest.json` en la carpeta destino: registra lo ya
  optimizado para no repetirlo (evita pérdida generacional) y permite
  reanudar lotes grandes.

```cmd
python optimize.py                     # destino de gui_config.json
python optimize.py --dest "J:\comics"
python optimize.py --dry-run           # solo informe, no escribe nada
python optimize.py --quality 85 --limit 500 --workers 6
```

- **Automático en descargas**: `download.py` optimiza lo nuevo al
  terminar (q82); `--no-optimize` lo desactiva, `--quality N` cambia la
  calidad.
- **Botón 🗜 Optimizar imágenes** (Utilidades del dashboard): lote sobre
  la colección existente, con log e historial como cualquier trabajo.

## Pawchive (tercer sitio, vía API)

[Pawchive](https://pawchive.pw) archiva posts de **Patreon / Fanbox /
Discord**. Se integra como tercer sitio del circuito (misma tabla, misma
descarga), pero usando su **API JSON** en lugar de HTML:

```cmd
python scraper.py --site pch           # catálogo + páginas de tus creadores
python scraper.py --site pch --limit 5 # prueba: 5 posts por creador
python scraper.py --site pch --skip-catalog  # solo páginas/metadatos pendientes
python download.py --site pch --dest "J:\comics"   # descargar solo pawchive
```

- **Creadores**: edítalos con el botón **🐾 Creadores Pawchive** del
  dashboard (pega una URL por línea, p. ej.
  `https://pawchive.pw/patreon/user/12345`), o **búscalos y márcalos en la
  pestaña 🐾 Explorar** (➕ Añadir / ➖ Quitar). Se guardan en
  `pawchive_creators.json`; solo se sincronizan esos creadores (no todo el
  archivo).
- **API** (`pawchive_api.py`): JSON público sin Cloudflare, con el mismo
  motor HTTP del proyecto (`curl_cffi`). Pausa de 0,3 s entre peticiones y
  reintentos. El listado pagina de 50 en 50 y **no** da la URL completa:
  la resuelve el detalle de cada post (`{server}/data{path}`, nodo
  `n*.pawchive.pw` asignado por archivo).
- **Solo imágenes**: los posts cuyo contenido es un vídeo (o solo
  preview) se omiten y se contabilizan en el log. Los mp4 adjuntos quedan
  fuera.
- **Títulos**: `Creador — Título` (si dos posts repiten título, se añade
  ` #id` para que ninguna carpeta se mezcle). La URL única del post hace
  la re-sincronización idempotente.
- **Metadatos**: `n_paginas_web` y los tags del post se guardan en la fase
  de páginas (detalle real); los tags pueden venir vacíos (`None`) y
  cuando existen llegan como un SET tipo `{a,b,c}` o `{"A",NSFW}` que
  `pawchive_api.post_tags()` trocea respetando las comillas.
- **Carpetas**: `<destino>\pawchive\<título>\001.jpg`… — el resto del
  circuito (optimización q82, 🎞 Verificar disco, filtros de la tabla)
  funciona sin cambios.

## Interfaz gráfica (lanzador + una ventana por sitio)

```cmd
python gui.py     # o gui.bat
```

**Un lanzador + una ventana por sitio** (acento azul `#3b82f6`):

- **🚀 Lanzador** (`Launcher`): botones 📕 VerComicsPorno / 📗 VerMangasPorno /
  🐾 Pawchive con el recuento de cómics de cada sitio (hilo de fondo),
  estado del trabajo global, botón ⏻ Cerrar todo. **Arranca sin abrir
  ninguna ventana de sitio**: las ventanas se abren al pulsar su botón (y
  cada una recuerda la geometría con la que se cerró la última vez,
  `geo_vcp` / `geo_vmp` / `geo_pch`).
- **Ventana por sitio** (`SiteWindow`, `CTkToplevel`): cada ventana es de UN
  sitio — **sin selector ni filtro de sitio** (columna Sitio oculta), con
  sus dos pestañas (Pawchive añade una tercera, 🐾 Explorar):

| Pestaña | Módulo | Contenido |
|---|---|---|
| **📊 Dashboard** | `dashboard.py` (frame embebible) | 6 **tarjetas** de estadísticas **solo de ese sitio** con chip de icono teñido por color, **gráfico de descargas de los últimos 14 días** (barras por día, resalta hoy, estado vacío 📭), barra de **metadatos del sitio**, **estado del sistema** (MySQL, disco libre, tamaño de la colección con escaneo en segundo plano + 🔄 Recalcular), controles agrupados (**Scraping** / **Descarga** / **Utilidades**) con la etiqueta de sitio fija (en Pawchive el motor es **API JSON, sin navegador** y aparece 🐾 Creadores), **historial de trabajos** (últimos 20 en `jobs_history.json`, compartido entre ventanas) y **registro de actividad** con colores por tipo de línea y botones 🧹 Limpiar / 📋 Copiar. El contenido superior hace scroll si la ventana es baja |
| **📚 Colección** | `gui.py` | Tabla **de ese sitio**, buscador, filtro **Estado**, contador y barra de selección |
| **🐾 Explorar** (solo Pawchive) | `gui.py` | Artistas + buscador de posts de la API de Pawchive (ver abajo) |

### 🐾 Explorar (solo la ventana de Pawchive)

Pestaña con **dos paneles**; todo va por la API JSON de `pawchive_api.py`
(sin navegador, sin descargas):

- **🎨 Artistas (izquierda)**
  - **Índice de creadores**: `GET /api/v1/creators` devuelve **todos**
    (~99.300) y **no admite filtro de servidor**, así que se descarga **una
    sola vez** y se guarda en **`pawchive_index_cache.json`** (≈4,3 MB);
    después todo el filtrado es local. La etiqueta muestra el recuento y la
    fecha; el botón 🔄 vuelve a descargarlo (el arranque usa la caché si
    existe, y la carga se hace **en un hilo aparte**: la ventana no se
    congela). Se pintan como mucho **1.500 artistas** (`PCH_IDX_MAX`),
    ordenados por popularidad (`favorited` ↓).
  - **Grid de tarjetas (4 columnas)**: cada artista es una tarjeta de
    180×220 px con su **avatar** y un **overlay negro** dentro con el
    **nombre** (en negrita) y la línea **`servicio · id`**. El grid está
    **virtualizado**: del índice completo solo se crean las tarjetas que
    caben en el viewport (24 de 99.348 con el índice completo), al hacer
    scroll se recrean las que entran en pantalla y el `scrollregion` mantiene
    la altura real del índice. La **selección sobrevive al reciclado** (se
    guarda por índice, no por widget).
  - **Filtros**: búsqueda por **nombre o id** (debounce de 300 ms),
    desplegable de **servicio** (Todos / Patreon / Fanbox / Discord) y
    casilla **★ Solo mis creadores**.
  - **Selección múltiple** con recuento + **➕ Añadir a mis creadores** /
    **➖ Quitar**: escriben `pawchive_creators.json` (la lista que usa
    `scraper.py --site pch` para sincronizar) y actualizan el botón
    🐾 Creadores del Dashboard.
  - **Clic en un artista** → lo selecciona y carga **sus posts** en el panel
    derecho (ver *modo creador* abajo); **doble clic** abre su perfil en el
    navegador. Con **Ctrl/Clic** se añade a la selección.
- **🔍 Buscador de posts (derecha)**
  - `GET /api/v1/posts?q=…&service=…&o=…` con **búsqueda real** (si la
    consulta está vacía lista los posts recientes), filtro de servicio y
    **paginación de 50 en 50** (◀ Anterior / Siguiente ▶, la última página
    deshabilita Siguiente).
  - **Modo creador**: al hacer clic en un artista el cabecero pasa a
    **🎨 Posts de {nombre}** con un botón **← Volver**, el desplegable de
    servicio se deshabilita (lo fija el creador) y la entrada busca
    **dentro de ese creador** con `GET /api/v1/{service}/user/{id}?q=…&o=…`
    (la `q` la resuelve el servidor, con la misma paginación de 50 en 50).
    ← Volver restaura la búsqueda global anterior. Mientras carga, botón
    🔍 deshabilitado y aviso *«⏳ Cargando posts de X…»*.
  - Tabla **Título / Creador / Fecha / Servicio / Img** con selección
    múltiple **acumulada entre páginas** (fuente de verdad: lo marcado no
    se pierde al cambiar de página ni de búsqueda, y el contador muestra el
    total; al volver a una página se re-marcan sus posts), **⬜ Todo** /
    **✖ Limpiar** (esta vacía también la acumulación), **doble clic** para
    abrir el post en el navegador y contador de selección.
  - **➕ Importar seleccionados a la BD**: tras confirmar, un hilo de fondo
    inserta **toda la acumulación** con título **`Creador — Título`**
    (sufijo ` #id_post` si el título choca), la URL única, sus **páginas**
    y sus **tags** en una transacción; el registro de la pestaña muestra el
    detalle y la etiqueta el resumen (✅ nuevos · ↩️ existentes · ⚠️
    duplicados · 🚫 sin imágenes · ❌ errores). **Solo guarda metadatos**
    (`estado='pendiente'`): las imágenes se descargan después desde
    **📚 Colección** con el botón de descarga normal. Los posts solo-vídeo
    se cuentan como *sin imágenes*.

### Arranque rápido (sin congelones)

La ventana de sitio se abre **por fases**, para que el usuario la vea al
instante y la interfaz no se quede quieta:

| Fase | Cuándo | Qué hace |
|---|---|---|
| 1 | `open_site()` | Tabla de pestañas, panel Explorar y barra inferior (el Dashboard muestra un ⏳ *Cargando…*) |
| 2 | al pintarse la ventana (`<Map>`) | Dashboard, construido **por pasos** (tarjetas → gráfico → controles → info → registro), cediendo el control a Tk entre paso y paso |
| 3 | al terminar la fase 2 | Pestaña Colección y primera carga de filas de la BD |

- `update_idletasks()` se **acota** durante la construcción: customtkinter
  lo llama en cada `_draw` de optionmenu/scrollbar y cada repaso drenaba
  **toda** la maquetación pendiente (~1 s de congelón por ventana).
- Se corta la **recursión** `CTkScrollbar._draw` → `canvas.update_idletasks()`
  → `<Configure>` → otro `_draw` (era la causa del congelón final de ~2,5 s).
- El **índice de artistas** (4,1 MB) se lee y deserializa **en un hilo
  aparte**; la GUI solo consume la lista ya en memoria.
- El **grid de artistas** está virtualizado: solo se crean las tarjetas
  visibles (24 de 99.348), así que no hay 99.348 widgets que crear.

Medido en este equipo con el **mainloop real** (la misma forma en que se
mide la respuesta de la app; con un `update()` en bucle los números salen
inflados porque Tk drena todos los eventos pendientes de golpe):

| Sitio | `open_site()` | Ventana visible | Congelón máximo |
|---|---|---|---|
| VerComicsPorno | 90-440 ms | ~300 ms | ~345 ms |
| VerMangasPorno | 60-430 ms | ~250 ms | ~370 ms |
| Pawchive | 240-450 ms | ~300 ms | ~190 ms |

> Antes de estos cambios la misma apertura tardaba **3,4 s** con la ventana
> congelada y el "congelón" final reachaba **2,5 s** (recursión de
> `CTkScrollbar._draw`).

### Trabajos globales (solo UNO a la vez)

- Toda la aplicación comparte **un único candado de trabajo**
  (`ACTIVE_JOB`): solo puede haber **un scraper/descarga/verificación/
  optimización en marcha** en cualquier momento.
- La ventana que lo lanza es la **dueña**: ve el registro, el progreso y su
  botón ⏹ Parar activo. Las demás ventanas muestran todos sus botones de
  trabajo **deshabilitados** con el aviso *«⏳ Trabajo en otra ventana»* y,
  al terminar, recargan solas la tabla y el historial.
- Al cerrar una ventana con un trabajo propio se pregunta y, si se acepta,
  se detiene el proceso y se libera el candado para el resto.

### Rendimiento de la tabla (20.000+ filas)

- **Carga por bloques**: solo se pintan 500 filas a la vez (`CHUNK`); al
  llegar al 98% del scroll se carga el siguiente bloque automáticamente.
  Añadir filas no bloquea la interfaz.
- **Consulta optimizada**: un único `LEFT JOIN` con recuento de páginas
  (antes: subconsulta correlacionada = un escaneo por cómic).
- **Debounce de 250 ms** en buscador y filtros: se rellena al dejar de
  teclear, no con cada tecla.
- **Orden por columna**: clic en la cabecera alterna ascendente/descendente
  (▲/▼); los números nulos (sin metadata) siempre al final.
- Filas rayadas, columnas ID/Pág./Pág. web centradas, scrollbar oscura y
  selección múltiple resaltada en azul.

### Acciones sobre filas

- **Barra de selección** (sobre la tabla): muestra el nº de filas marcadas
  con botones 📂 Abrir carpeta / 📋 Copiar título / 🔗 Copiar URL.
- **Menú contextual** (clic derecho): 📂 Abrir carpeta, 📋 Copiar título,
  🔗 Copiar URL, 🌐 Abrir en el navegador.
- **Doble clic** en una fila abre la carpeta del cómic.

### Estados vacíos

Panel centrado con acción según el caso: **BD de este sitio vacía** →
🔄 Sincronizar catálogo; **sin resultados** (filtros/búsqueda) → 🧹 Limpiar
filtros; **sin MySQL** → 🔄 Reintentar (se conservan en pantalla las filas ya
cargadas si las hubiera).

### Barra inferior (fuera de las pestañas, siempre visible)

Punto de estado ● (gris = inactivo, ámbar = trabajo en otra ventana, azul =
trabajo en marcha en esta ventana, verde/rojo al terminar) + mensaje, barra
de progreso y botón ⏹ Parar (solo activo en la ventana dueña del trabajo).

### Otros botones del dashboard

- **📚 Completar metadatos** → `scraper.py --site <sitio> --mode http --skip-catalog` (rellena nº de páginas web y tags sin revisar el catálogo).
- **🎞 Verificar disco** → `verify_dl.py --sync-estado` (recalcula `estado` según lo que hay en disco: descargado = carpeta completa).
- **🗜 Optimizar imágenes** → `optimize.py` (re-codifica a q82, ~30% menos de peso, sin pérdida visible).
- La configuración (carpeta destino, hilos, motor, filtro de estado, caché
  de escaneo, ventanas abiertas `open_windows` y geometría por sitio
  `geo_vcp`/`geo_vmp`/`geo_pch`) se guarda en `gui_config.json`.
- **🗑 Vaciar solo este sitio** borra únicamente los cómics de esa ventana
  (nunca toca los demás sitios) y pide confirmación; está bloqueado si hay
  un trabajo en marcha.

## Notas

- **Cloudflare** — dos mecanismos de defensa:
  1. *Comprobación automática*: se supera con la **huella TLS de Chrome**
     (`curl_cffi`) — no requiere navegador ni cookies; Brave puede estar abierto.
  2. *Reto interactivo (Turnstile)*: solo aparece en raras ocasiones. Respaldo
     con navegador **visible** + **Chrome real** (`channel="chrome"`) y **perfil
     persistente** (`.browser-profile`); si aparece el checkbox "Verifique que
     es un ser humano", el script hace clic automáticamente.
- Si el modo HTTP se bloqueara: ejecuta `python brave_cf.py` para ver el error
  exacto, o pasa a `--mode browser`. Las cookies de Brave se leen solo como
  respaldo y únicamente con Brave cerrado.
- La consola de Windows debe soportar UTF-8 (el script lo configura sola).
- Los archivos `debug_page.html` / `debug_page.png` se generan solo si una
  página falla; sirven para diagnosticar (puedes borrarlos).

## Archivos

| Archivo | Función |
|---|---|
| `db_setup.sql` | Crea BD `mis_comics` con tablas `comics` (con `sitio`), `paginas`, `etiquetas` y `comic_etiquetas` |
| `scraper.py` | Extrae títulos, URLs, imágenes y metadatos (`--mode http/browser/auto`, `--site vcp/vmp/pch/all`; `pch` = Pawchive por API) |
| `pawchive_api.py` | Cliente de la API de Pawchive: creadores (`pawchive_creators.json`), endpoints `/creators` y `/posts` (búsqueda con `q`, filtro `service`, paginación `o`), listado por creador, detalle con URL completa de imágenes y tags |
| `brave_cf.py` | Motor HTTP: huella TLS de Chrome (`curl_cffi`) + cookies de Brave opcionales |
| `download.py` | Descarga masiva de imágenes organizada por cómic (y por sitio, `--site vcp/vmp/pch/all`); optimiza lo nuevo (q82) salvo `--no-optimize` |
| `optimize.py` | Re-codifica JPEG/PNG en el mismo archivo (q82 sin pérdida visual, sin EXIF, con ICC); `--dry-run` para estimar el ahorro |
| `gui.py` / `gui.bat` | Lanzador (abre/recuerda las ventanas de sitio) + ventana por sitio con pestañas 📊 Dashboard + 📚 Colección (sitio fijo, tabla por bloques, orden por cabecera, menú contextual, estados vacíos, barra de selección) y, en Pawchive, **🐾 Explorar** (artistas seleccionables → `pawchive_creators.json`, clic en un artista → **sus posts** con buscador interno y paginación, selección **acumulada entre páginas** → importación de metadatos a la BD, índice cacheado en `pawchive_index_cache.json`), candado de **un único trabajo global** con broadcast al resto de ventanas y barra de progreso |
| `dashboard.py` | Pestaña 📊 Dashboard (frame embebible, una por sitio): paleta azul, tarjetas con chips, gráfico de 14 días, metadatos del sitio, estado del sistema, controles agrupados, historial y registro con colores |
| `verify_dl.py` | Verifica BD vs disco: informa de faltantes y, con `--sync-estado`, corrige `comics.estado` (destino de `gui_config.json` o `--dest`) |
| `requirements.txt` | Dependencias Python |
| `scrape.bat` / `download.bat` | Atajos para ejecutar desde Windows |
