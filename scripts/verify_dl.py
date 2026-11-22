# -*- coding: utf-8 -*-
"""Verificación real: comics en BD vs archivos en disco (Python, sin comodines).

    python verify_dl.py                     # solo informe (destino de gui_config.json)
    python verify_dl.py --dest "J:/comics"  # informe con otro destino
    python verify_dl.py --sync-estado       # informe + corrige comics.estado

Criterio de 'descargado': la carpeta del cómic tiene TODOS los archivos
esperados de sus páginas (mismo criterio que usa download.py al terminar).
Si falta alguno -> 'pendiente'. Los 'duplicado' nunca se tocan.
"""
import argparse
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import mysql.connector

import download
from config import SCRIPT_DIR, DB_CONFIG
from db import PageRepository

def destino_por_defecto() -> str:
    """Destino guardado por la GUI (gui_config.json) o J:/comics."""
    try:
        cfg = json.loads((SCRIPT_DIR / "gui_config.json").read_text(encoding="utf-8"))
        if cfg.get("dest"):
            return cfg["dest"]
    except Exception:
        pass
    return "J:/comics"


def main() -> int:
    parser = argparse.ArgumentParser(description="BD vs disco (informe/estado)")
    parser.add_argument("--dest", default=None,
                        help="Carpeta destino (por defecto, gui_config.json)")
    parser.add_argument("--sync-estado", action="store_true",
                        help="Corrige comics.estado según lo que hay en disco")
    args = parser.parse_args()

    download.OUT_DIR = Path(args.dest or destino_por_defecto()).expanduser()
    print(f'Carpeta destino: "{download.OUT_DIR}"')

    conn = mysql.connector.connect(**DB_CONFIG)
    cur = conn.cursor()
    cur.execute("""
        SELECT c.id, c.titulo, c.sitio, c.estado
        FROM comics c
    """)
    comics = cur.fetchall()

    # Usar repositorio para obtener archivos esperados
    esperado = PageRepository.expected_files()

    sin_carpeta = []
    vacias = []
    con_faltantes = []
    completos = 0
    total_esperado = 0
    total_existente = 0
    updates: list[tuple[str, int]] = []      # (estado_nuevo, comic_id)
    sin_paginas_desc = 0
    duplicados = 0

    for comic_id, titulo, sitio, estado in comics:
        if estado == "duplicado":
            duplicados += 1
            continue
        esperados = esperado.get(comic_id)
        if not esperados:
            # Sin imágenes en la BD: no puede estar en disco.
            if estado == "descargado":
                updates.append(("pendiente", comic_id))
                sin_paginas_desc += 1
            continue

        total_esperado += len(esperados)
        existentes = download.list_files(download.carpeta_comic(sitio, titulo))
        if existentes is None:
            sin_carpeta.append((comic_id, titulo))
            nuevo = "pendiente"
        else:
            faltan = esperados - existentes
            total_existente += len(esperados & existentes)
            if not existentes:
                vacias.append((comic_id, titulo))
                nuevo = "pendiente"
            elif faltan:
                con_faltantes.append((comic_id, titulo, sorted(faltan)[:5],
                                      len(faltan)))
                nuevo = "pendiente"
            else:
                completos += 1
                nuevo = "descargado"
        if nuevo != estado:
            updates.append((nuevo, comic_id))

    print(f"Comics con imagenes en BD : {sum(1 for c in comics if esperado.get(c[0]))}")
    print(f"  completos (100%)        : {completos}")
    print(f"  con faltantes parciales  : {len(con_faltantes)}")
    print(f"  carpeta totalmente vacia : {len(vacias)}")
    print(f"  sin carpeta              : {len(sin_carpeta)}")
    print(f"Archivos esperados        : {total_esperado}")
    print(f"Archivos existentes       : {total_existente}")
    print(f"Faltantes                 : {total_esperado - total_existente}")
    print(f"Duplicados (ignorados)    : {duplicados}")
    print()
    for cid, tit, faltan, n in con_faltantes[:10]:
        print(f"  [parcial] id={cid} {tit[:50]} -> faltan {n}: {faltan}")
    for cid, tit in vacias[:10]:
        print(f"  [vacia]   id={cid} {tit[:60]}")
    for cid, tit in sin_carpeta[:10]:
        print(f"  [nocarp]  id={cid} {tit[:60]}")

    if args.sync_estado:
        if updates:
            cur.executemany(
                "UPDATE comics SET estado=%s WHERE id=%s "
                "AND estado <> 'duplicado'", updates)
            conn.commit()
            n_desc = sum(1 for e, _ in updates if e == "descargado")
            print(f"\n[SYNC] comics.estado corregido en {len(updates)} cómics: "
                  f"{n_desc} -> descargado, {len(updates) - n_desc} -> pendiente"
                  + (f" (y {sin_paginas_desc} 'descargado' sin imágenes "
                     f"-> pendiente)" if sin_paginas_desc else ""))
        else:
            print("\n[SYNC] comics.estado ya coincide con el disco. "
                  "Nada que corregir.")

    cur.close()
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())