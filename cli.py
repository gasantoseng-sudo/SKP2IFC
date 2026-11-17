"""Linha de comando: python app.py --cli entrada.skp [mais.skp | pasta] -o saida"""

from __future__ import annotations

import argparse
import os

import skp2ifc_core as core


def main_optimize(argv) -> int:
    import ifc_optimize as opt
    ap = argparse.ArgumentParser(prog="skp2ifc optimize", description="Otimiza .ifc existentes (IFC2X3/IFC4).")
    ap.add_argument("inputs", nargs="+", help="arquivos .ifc ou pastas")
    ap.add_argument("-o", "--out", help="pasta de saída (padrão: ao lado, com _otimizado)")
    ap.add_argument("--max-tri", type=int, default=30000,
                    help="máx. de triângulos por representação (0 = só limpeza sem perda)")
    ap.add_argument("--keep", type=float, default=100.0,
                    help="%% de triângulos a manter em cada malha (100 = desligado; ex.: 30)")
    ap.add_argument("--overwrite", action="store_true")
    a = ap.parse_args(argv)
    files = []
    for p in a.inputs:
        files += opt.find_ifc_files(p) if os.path.isdir(p) else [p]
    if not files:
        print("Nenhum .ifc encontrado.")
        return 2
    res = opt.optimize_batch(files, a.out, opt.OptOptions(max_tri_per_rep=a.max_tri, keep_ratio=a.keep / 100.0, overwrite=a.overwrite),
                             log=print)
    return 0 if all(not r.error for r in res) else 1


def main(argv=None) -> int:
    if argv and argv[0] in ("--version", "-V"):
        import version
        print(f"{version.APP_NAME} {version.__version__}")
        return 0
    if argv and argv[0] == "optimize":
        return main_optimize(argv[1:])
    ap = argparse.ArgumentParser(prog="skp2ifc", description="Converte .skp em .ifc (sem SketchUp).")
    ap.add_argument("inputs", nargs="+", help="arquivos .skp ou pastas")
    ap.add_argument("-o", "--out", help="pasta de saída (padrão: ao lado do original)")
    ap.add_argument("-u", "--unit", choices=list(core.UNIT_SCALE), default="m")
    ap.add_argument("-s", "--schema", choices=core.SCHEMAS, default="IFC2X3")
    ap.add_argument("--offset", nargs=3, type=float, default=(0, 0, 0), metavar=("X", "Y", "Z"),
                    help="deslocamento na unidade escolhida")
    ap.add_argument("--to-origin", action="store_true", help="leva o canto mínimo à origem")
    ap.add_argument("--exclude", default=",".join(core.DEFAULT_EXCLUDE_KEYWORDS),
                    help="palavras-chave de tags a excluir (vírgula); '' = nada")
    ap.add_argument("--include-hidden", action="store_true", help="não ignora tags ocultas")
    ap.add_argument("--overwrite", action="store_true")
    ap.add_argument("--max-tri", type=int, default=30000,
                    help="máx. de triângulos por elemento; acima disso simplifica (0 = sem limite)")
    ap.add_argument("--list-tags", action="store_true", help="só lista as tags e sai")
    a = ap.parse_args(argv)

    files = []
    for p in a.inputs:
        files += core.find_skp_files(p) if os.path.isdir(p) else [p]
    if not files:
        print("Nenhum .skp encontrado.")
        return 2

    if a.list_tags:
        for p in files:
            print(p)
            for t in sorted(core.scan_tags(p).values(), key=lambda x: x.name.lower()):
                print(f"  {t.name:40s} {t.triangles:>9d} tri{'  [oculta]' if t.hidden else ''}")
        return 0

    opts = core.Options(
        unit=a.unit, schema=a.schema, offset=tuple(a.offset), origin_to_min=a.to_origin,
        exclude_keywords=tuple(k for k in a.exclude.split(",") if k.strip()),
        skip_hidden_tags=not a.include_hidden, overwrite=a.overwrite,
        max_tri_per_element=a.max_tri)
    results = core.convert_batch(files, a.out, opts, log=print)
    return 0 if all(not r.error for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
