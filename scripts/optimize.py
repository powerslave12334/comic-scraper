# -*- coding: utf-8 -*-
"""
Optimización de imágenes en disco: re-codifica JPEG y optimiza PNG
conservando el MISMO nombre y extensión (download.py y verify_dl.py
no se enteran).

- JPEG: quita el EXIF, conserva el perfil ICC (si no, cambian los colores),
  re-codifica a q=82 (sin pérdida visual), optimize + progresivo.
- PNG: re-codifica con optimize=True (pérdida cero).
- Solo reemplaza si el resultado es MÁS PEQUEÑO (nunca agranda).
- Nunca re-procesa un archivo ya optimizado: manifiesto
  `optimize_manifest.json` (ruta -> tamaño/mtime), reanudable.

Uso:
    python optimize.py                       # destino de gui_config.json
    python optimize.py --dest "J:\\comics"
    python optimize.py --dry-run             # solo informe, no escribe nada
    python optimize.py --quality 85 --limit 500 --workers 6
"""
import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from PIL import Image, ImageOps

SCRIPT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = SCRIPT_DIR / "gui_config.json"
MANIFEST_NAME = "optimize_manifest.json"
IMG_EXTS = {".jpg", ".jpeg", ".png"}


def destino_por_defecto() -> str:
    """Destino guardado por la GUI (gui_config.json)."""
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if cfg.get("dest"):
            return cfg["dest"]
    except Exception:
        pass
    return "C:/Comics"


def fmt_bytes(b: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.0f} {unit}" if unit == "B" else f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} PB"


def load_manifest(dest: Path) -> dict:
    try:
        data = json.loads((dest / MANIFEST_NAME).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_manifest(dest: Path, manifest: dict) -> None:
    tmp = dest / (MANIFEST_NAME + ".tmp")
    try:
        tmp.write_text(json.dumps(manifest, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, dest / MANIFEST_NAME)
    except Exception:
        pass


def _encode(img: Image.Image, fmt: str, quality: int, icc) -> bytes:
    """Codifica en memoria con los parámetros de optimización."""
    import io
    buf = io.BytesIO()
    if fmt == "JPEG":
        img.save(buf, "JPEG", quality=quality, optimize=True,
                 progressive=True, icc_profile=icc)
    else:  # PNG (pérdida cero)
        img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


def optimize_file(path: Path, dest: Path, quality: int, min_size: int,
                  manifest: dict, lock: threading.Lock,
                  dry_run: bool) -> tuple[str, int, int]:
    """Optimiza un archivo. Devuelve (estado, antes, después).

    estados: ok (reemplazado/más pequeño), keep (no valía la pena),
    small (< min_size), format (no JPEG/PNG), error, done (ya optimizado).
    """
    try:
        st = path.stat()
    except OSError:
        return "error", 0, 0

    try:
        rel = str(path.relative_to(dest))
    except ValueError:
        rel = str(path)

    rec = f"{st.st_size}|{int(st.st_mtime)}"
    with lock:
        if manifest.get(rel) == rec:
            return "done", st.st_size, st.st_size

    if st.st_size < min_size:
        return "small", st.st_size, st.st_size
    if path.suffix.lower() not in IMG_EXTS:
        return "format", st.st_size, st.st_size

    try:
        with Image.open(path) as img:
            fmt = img.format
            if fmt not in ("JPEG", "PNG"):
                return "format", st.st_size, st.st_size
            icc = img.info.get("icc_profile")
            # EXIF Orientation: aplicar a los píxeles ANTES de quitar el EXIF
            # (si no, las imágenes con orientación saldrían de lado)
            img = ImageOps.exif_transpose(img) or img
            if fmt == "JPEG" and img.mode not in ("RGB", "L"):
                img = img.convert("RGB")
            data = _encode(img, fmt, quality, icc)
    except Exception:
        return "error", st.st_size, st.st_size

    if len(data) >= st.st_size:
        # No ahorra: conservar el original (y no reintentar en el futuro)
        with lock:
            manifest[rel] = rec
        return "keep", st.st_size, st.st_size

    if dry_run:
        return "ok", st.st_size, len(data)

    tmp = path.parent / (path.name + ".opttmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
        new_st = path.stat()
        with lock:
            manifest[rel] = f"{new_st.st_size}|{int(new_st.st_mtime)}"
        return "ok", st.st_size, len(data)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return "error", st.st_size, st.st_size


def optimize_paths(paths, dest: Path, quality: int = 82,
                   min_size: int = 200 * 1024, workers: int = 4,
                   dry_run: bool = False, progress_every: int = 100) -> dict:
    """Optimiza una lista de rutas. Compartido por CLI y download.py."""
    dest = Path(dest)
    manifest = {} if dry_run else load_manifest(dest)
    lock = threading.Lock()
    stats = {"done": 0, "ok": 0, "keep": 0, "small": 0, "format": 0,
             "error": 0, "before": 0, "after": 0, "saved": 0}
    counter = {"n": 0}
    total = len(paths)
    lock_print = threading.Lock()

    def work(p: Path):
        estado, antes, despues = optimize_file(
            p, dest, quality, min_size, manifest, lock, dry_run)
        with lock:
            stats[estado] += 1
            if estado == "ok":
                stats["before"] += antes
                stats["after"] += despues
                stats["saved"] += antes - despues
            counter["n"] += 1
            n = counter["n"]
        if progress_every and n % progress_every == 0:
            with lock_print:
                print(f"--- progreso: {n}/{total} ---", flush=True)
        if not dry_run and n % 2000 == 0:
            save_manifest(dest, manifest)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        list(ex.map(work, paths))

    if not dry_run:
        save_manifest(dest, manifest)
    return stats


def collect_files(dest: Path, limit: int | None) -> list[Path]:
    files = []
    for root, _dirs, names in os.walk(str(dest)):
        for n in names:
            if Path(n).suffix.lower() in IMG_EXTS:
                files.append(Path(root) / n)
                if limit and len(files) >= limit:
                    return files
    return files


def report(stats: dict, dry_run: bool) -> None:
    total_encode = stats["before"]
    print("\n===== OPTIMIZACIÓN =====")
    if dry_run:
        print("(dry-run: no se escribió nada)")
    print(f"Ya optimizados (antes): {stats['done']}")
    print(f"Re-codificados         : {stats['ok']}  "
          f"({fmt_bytes(stats['before'])} -> {fmt_bytes(stats['after'])})")
    print(f"Sin cambio (no valía)  : {stats['keep']}")
    print(f"Pequeños (< umbral)    : {stats['small']}")
    print(f"Otros formatos         : {stats['format']}")
    print(f"Errores                : {stats['error']}")
    if total_encode:
        print(f"Ahorro total           : {fmt_bytes(stats['saved'])} "
              f"({100 * stats['saved'] / total_encode:.0f}% de lo procesado)")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Optimiza imágenes en disco (mismo nombre, q82 JPEG)")
    parser.add_argument("--dest", default=None,
                        help="Carpeta destino (por defecto, gui_config.json)")
    parser.add_argument("--quality", type=int, default=82,
                        help="Calidad JPEG (por defecto 82)")
    parser.add_argument("--min-size", type=int, default=200 * 1024,
                        help="Ignora archivos menores que N bytes (def. 200KB)")
    parser.add_argument("--workers", type=int, default=4,
                        help="Hilos de optimización")
    parser.add_argument("--limit", type=int, default=None,
                        help="Máximo de archivos (para pruebas)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Solo informe: no escribe ni modifica nada")
    args = parser.parse_args()

    dest = Path(args.dest or destino_por_defecto()).expanduser()
    if not dest.is_dir():
        print(f"[X] No existe la carpeta: {dest}")
        return 1
    print(f'Carpeta destino: "{dest}"')
    t0 = time.time()
    files = collect_files(dest, args.limit)
    print(f"Archivos de imagen encontrados: {len(files)} "
          f"(calidad q{args.quality})")
    if not files:
        print("Nada que optimizar.")
        return 0

    stats = optimize_paths(files, dest, quality=args.quality,
                           min_size=args.min_size, workers=args.workers,
                           dry_run=args.dry_run)
    report(stats, args.dry_run)
    print(f"Tiempo: {time.time() - t0:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
