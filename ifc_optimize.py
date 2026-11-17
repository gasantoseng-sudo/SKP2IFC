"""Otimizador de IFC existente (IFC2X3 e IFC4), sem perder classes nem propriedades.

Só mexe nas malhas pesadas; todo o resto do arquivo (elementos, tipos, Psets,
materiais, estilos, hierarquia) fica como está.

O que faz:
  * IfcTriangulatedFaceSet (IFC4): une vértices duplicados e, se a representação
    passar do limite, simplifica a malha.
  * IfcPolygonalFaceSet (IFC4): só une vértices duplicados.
  * IfcFaceBasedSurfaceModel / IfcShellBasedSurfaceModel (IFC2X3/IFC4): une pontos
    duplicados; malhas abertas só de triângulos também podem ser simplificadas.
  * IfcFacetedBrep (IFC2X3/IFC4): só une pontos duplicados (preserva o sólido).
  * Extrusões, perfis, curvas etc. não são tocados.
"""

from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from skp2ifc_core import (ConversionCancelled, LogFn, StatusFn, _noop, _simplify,
                          _unique_path, _weld)


@dataclass
class OptOptions:
    max_tri_per_rep: int = 30000     # 0 = sem teto por representação
    keep_ratio: float = 1.0          # 1.0 = desligado; 0.3 = manter ~30% dos triângulos
    min_tri_floor: int = 300         # representações menores que isso não são simplificadas por keep_ratio
    precision_m: float = 1e-5        # 0,01 mm
    overwrite: bool = False
    suffix: str = "_otimizado"


@dataclass
class OptResult:
    source: str
    output: Optional[str] = None
    size_before: int = 0
    size_after: int = 0
    entities_before: int = 0
    entities_after: int = 0
    tri_before: int = 0
    tri_after: int = 0
    items_changed: int = 0
    simplified: List[Tuple[str, int, int]] = field(default_factory=list)  # (nome, antes, depois)
    welded_only_breps: int = 0
    seconds: float = 0.0
    error: Optional[str] = None


def _entity_count(f) -> int:
    try:
        return len(f.wrapped_data.entity_names())
    except Exception:
        return sum(1 for _ in f)


def _owner_name(f, rep) -> str:
    try:
        for inv in f.get_inverse(rep):
            if inv.is_a("IfcProductDefinitionShape"):
                for pr in (getattr(inv, "ShapeOfProduct", None) or []):
                    return pr.Name or pr.is_a()
            if inv.is_a("IfcRepresentationMap"):
                for t in f.get_inverse(inv):
                    if t.is_a("IfcTypeProduct"):
                        return f"{t.Name or t.is_a()} (tipo)"
                return f"Mapa #{inv.id()}"
    except Exception:
        pass
    return f"Representação #{rep.id()}"


def _classify(item) -> Optional[str]:
    if item.is_a("IfcTriangulatedFaceSet"):
        return "tri"
    if item.is_a("IfcPolygonalFaceSet"):
        return "poly"
    if item.is_a("IfcFaceBasedSurfaceModel") or item.is_a("IfcShellBasedSurfaceModel"):
        return "fbsm"
    if item.is_a("IfcFacetedBrep"):
        return "brep"
    return None


def _connected_sets(item) -> list:
    if item.is_a("IfcFaceBasedSurfaceModel"):
        return list(item.FbsmFaces or [])
    if item.is_a("IfcShellBasedSurfaceModel"):
        return list(item.SbsmBoundary or [])
    if item.is_a("IfcFacetedBrep"):
        return [item.Outer]
    return []


def _orphan(f, e) -> bool:
    try:
        return f.get_total_inverses(e) == 0
    except Exception:
        return False


# ------------------------------------------------------------------ pontos
class _PointWelder:
    """Unifica IfcCartesianPoint coincidentes usados em IfcPolyLoop."""

    def __init__(self, nd: int) -> None:
        self.nd = nd
        self.canon: Dict[tuple, object] = {}
        self.olds: Dict[int, object] = {}

    def weld_faces(self, faces) -> None:
        nd = self.nd
        for fc in faces:
            for b in fc.Bounds:
                loop = b.Bound
                if not loop.is_a("IfcPolyLoop"):
                    continue
                new, changed = [], False
                for p in loop.Polygon:
                    co = p.Coordinates
                    if len(co) != 3:
                        new.append(p)
                        continue
                    key = (round(co[0], nd), round(co[1], nd), round(co[2], nd))
                    c = self.canon.get(key)
                    if c is None:
                        self.canon[key] = p
                        if tuple(co) != key:
                            p.Coordinates = key
                        c = p
                    if c is not p:
                        changed = True
                        self.olds[p.id()] = p
                    new.append(c)
                if changed:
                    loop.Polygon = new

    def cleanup(self, f) -> None:
        for p in self.olds.values():
            try:
                if _orphan(f, p):
                    f.remove(p)
            except Exception:
                pass


def _triangle_arrays_from_cfs(cfs):
    """(V, T, faces) se o conjunto é só de triângulos simples; senão None."""
    faces = list(cfs.CfsFaces or [])
    if not faces:
        return None
    idx: Dict[int, int] = {}
    verts: List[tuple] = []
    tris = []
    for fc in faces:
        bs = fc.Bounds
        if len(bs) != 1:
            return None
        b = bs[0]
        if not b.is_a("IfcFaceOuterBound") or not b.Orientation or not b.Bound.is_a("IfcPolyLoop"):
            return None
        poly = b.Bound.Polygon
        if len(poly) != 3:
            return None
        row = []
        for p in poly:
            k = p.id()
            i = idx.get(k)
            if i is None:
                i = len(verts)
                idx[k] = i
                verts.append(tuple(p.Coordinates))
            row.append(i)
        tris.append(row)
    return np.asarray(verts, dtype=np.float64), np.asarray(tris, dtype=np.int64), faces


def _rebuild_cfs(f, cfs, V: np.ndarray, T: np.ndarray, old_faces, nd: int) -> None:
    pts = [f.createIfcCartesianPoint(tuple(float(x) for x in np.round(p, nd))) for p in V]
    new_faces = []
    for t in T:
        loop = f.createIfcPolyLoop([pts[int(t[0])], pts[int(t[1])], pts[int(t[2])]])
        new_faces.append(f.createIfcFace([f.createIfcFaceOuterBound(loop, True)]))
    cfs.CfsFaces = new_faces
    for fc in old_faces:
        bounds = list(fc.Bounds)
        f.remove(fc)
        for b in bounds:
            loop = b.Bound
            f.remove(b)
            if _orphan(f, loop):
                f.remove(loop)


# ---------------------------------------------------------- face sets IFC4
def _set_tri(f, item, V: np.ndarray, T: np.ndarray, nd: int) -> None:
    used = np.unique(T.reshape(-1))
    remap = np.full(len(V), -1, dtype=np.int64)
    remap[used] = np.arange(len(used))
    coords = np.round(V[used], nd)
    old = item.Coordinates
    item.Coordinates = f.createIfcCartesianPointList3D([tuple(float(x) for x in p) for p in coords])
    item.CoordIndex = [tuple(int(q) + 1 for q in row) for row in remap[T]]
    item.Normals = None  # as normais antigas deixam de valer; o visualizador recalcula
    if _orphan(f, old):
        f.remove(old)


def _weld_poly(f, item, nd: int) -> bool:
    if item.PnIndex:
        return False
    old = item.Coordinates
    P = np.asarray(old.CoordList, dtype=np.float64).reshape(-1, 3)
    uniq, inv = np.unique(np.round(P, nd), axis=0, return_inverse=True)
    inv = inv.reshape(-1)
    if len(uniq) == len(P):
        return False
    keep = []
    for fc in item.Faces:
        ci: List[int] = []
        for i in fc.CoordIndex:
            j = int(inv[i - 1]) + 1
            if not ci or ci[-1] != j:
                ci.append(j)
        if len(ci) > 1 and ci[0] == ci[-1]:
            ci.pop()
        if len(ci) < 3 or fc.is_a("IfcIndexedPolygonalFaceWithVoids"):
            if len(ci) >= 3:
                fc.CoordIndex = tuple(ci)
                fc.InnerCoordIndices = [tuple(int(inv[i - 1]) + 1 for i in inner)
                                        for inner in (fc.InnerCoordIndices or [])]
                keep.append(fc)
            else:
                f.remove(fc)
            continue
        fc.CoordIndex = tuple(ci)
        keep.append(fc)
    item.Faces = keep
    item.Coordinates = f.createIfcCartesianPointList3D(
        [tuple(float(x) for x in p) for p in uniq])
    if _orphan(f, old):
        f.remove(old)
    return True


# ------------------------------------------------------------------ principal
def optimize_ifc(src: str, dst: Optional[str], opts: OptOptions,
                 log: LogFn = _noop, status: StatusFn = _noop,
                 cancel=None) -> OptResult:
    t0 = time.monotonic()
    res = OptResult(source=src)
    try:
        import ifcopenshell
        import ifcopenshell.util.unit

        if dst is None:
            base, _ext = os.path.splitext(src)
            dst = base + opts.suffix + ".ifc"
        if os.path.abspath(dst) == os.path.abspath(src):
            raise ValueError("A saída não pode ser o próprio arquivo de origem.")
        if not opts.overwrite:
            dst = _unique_path(dst)
        os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)

        res.size_before = os.path.getsize(src)
        log(f"Abrindo {os.path.basename(src)} ({res.size_before / 1e6:.1f} MB) ...")
        status("Abrindo o IFC", None)
        f = ifcopenshell.open(src)
        schema = f.schema
        log(f"  Esquema: {schema}")
        res.entities_before = _entity_count(f)

        scale = ifcopenshell.util.unit.calculate_unit_scale(f) or 1.0
        nd = max(0, int(math.ceil(-math.log10(opts.precision_m / scale))))
        welder = _PointWelder(nd)

        reps = list(f.by_type("IfcShapeRepresentation"))
        seen: set = set()
        total = max(1, len(reps))
        t_last = 0.0

        for n, rep in enumerate(reps):
            if cancel is not None and cancel.is_set():
                raise ConversionCancelled()
            now = time.monotonic()
            if now - t_last > 0.5:
                t_last = now
                status(f"Otimizando malhas: representação {n + 1} de {total}", 0.05 + 0.85 * n / total)

            items = []
            for it in (rep.Items or []):
                if it.id() in seen:
                    continue
                kind = _classify(it)
                if kind:
                    seen.add(it.id())
                    items.append((kind, it))
            if not items:
                continue

            # ---- 1) leitura / união sem perda
            dec = []        # (ref, V, T, tri_antes)  -> candidatos a simplificação
            before_rep = 0
            changed_rep = False
            for kind, it in items:
                if kind == "tri":
                    if it.PnIndex:
                        continue
                    P = np.asarray(it.Coordinates.CoordList, dtype=np.float64).reshape(-1, 3)
                    T0 = np.asarray(it.CoordIndex, dtype=np.int64).reshape(-1, 3) - 1
                    before_rep += len(T0)
                    V, T = _weld(P, T0, nd)
                    dec.append((("tri", it), V, T, len(T0)))
                elif kind == "poly":
                    if _weld_poly(f, it, nd):
                        res.items_changed += 1
                    before_rep += len(it.Faces or [])
                else:  # fbsm / brep
                    for cfs in _connected_sets(it):
                        if cfs is None:
                            continue
                        faces = list(cfs.CfsFaces or [])
                        before_rep += len(faces)
                        welder.weld_faces(faces)
                        if kind == "fbsm" and not cfs.is_a("IfcClosedShell"):
                            arr = _triangle_arrays_from_cfs(cfs)
                            if arr is not None:
                                V, T, fcs = arr
                                dec.append((("cfs", cfs, fcs), V, T, len(T)))
                        elif kind == "brep":
                            res.welded_only_breps += 1
            res.tri_before += before_rep

            # ---- 2) limite de triângulos por representação
            total_dec = sum(len(d[2]) for d in dec)
            cap = opts.max_tri_per_rep if opts.max_tri_per_rep > 0 else 0
            if 0 < opts.keep_ratio < 1.0:
                c2 = max(opts.min_tri_floor, int(total_dec * opts.keep_ratio))
                cap = c2 if cap == 0 else min(cap, c2)
            simplify = cap > 0 and total_dec > cap
            after_dec = 0
            for ref, V, T, tri0 in dec:
                if simplify:
                    target = max(1, int(cap * len(T) / total_dec))
                    V2, T2, ok = _simplify(V, T, target)
                else:
                    V2, T2, ok = V, T, False
                if ref[0] == "tri":
                    it = ref[1]
                    if ok or len(V2) < len(it.Coordinates.CoordList) or len(T2) != tri0:
                        _set_tri(f, it, V2, T2, nd)
                        res.items_changed += 1
                        changed_rep = True
                    after_dec += len(T2)
                else:
                    cfs, fcs = ref[1], ref[2]
                    if ok:
                        _rebuild_cfs(f, cfs, V2, T2, fcs, nd)
                        res.items_changed += 1
                        changed_rep = True
                    after_dec += len(T2)
            if simplify and changed_rep:
                after_rep = before_rep - total_dec + after_dec
                res.simplified.append((_owner_name(f, rep), before_rep, after_rep))
                res.tri_after += after_rep
            else:
                res.tri_after += before_rep - total_dec + after_dec

        status("Limpando pontos duplicados", 0.92)
        welder.cleanup(f)
        res.entities_after = _entity_count(f)

        status("Gravando o IFC otimizado no disco", 0.95)
        f.write(dst)
        res.output = dst
        res.size_after = os.path.getsize(dst)
        status("Concluído", 1.0)
    except ConversionCancelled:
        res.error = "Cancelado."
    except Exception as e:
        res.error = f"{type(e).__name__}: {e}"
    res.seconds = time.monotonic() - t0
    return res


def find_ifc_files(folder: str, recursive: bool = True) -> List[str]:
    out: List[str] = []
    if recursive:
        for root, _d, files in os.walk(folder):
            out += [os.path.join(root, x) for x in files if x.lower().endswith(".ifc")]
    else:
        out = [os.path.join(folder, x) for x in os.listdir(folder) if x.lower().endswith(".ifc")]
    return sorted(out)


def optimize_batch(sources: List[str], out_dir: Optional[str], opts: OptOptions,
                   log: LogFn = _noop, status: StatusFn = _noop,
                   cancel=None) -> List[OptResult]:
    results: List[OptResult] = []
    for i, src in enumerate(sources):
        if cancel is not None and cancel.is_set():
            log("Lote cancelado.")
            break
        dst = None
        if out_dir:
            dst = os.path.join(out_dir, os.path.splitext(os.path.basename(src))[0]
                               + opts.suffix + ".ifc")

        def st(text, frac, _i=i, _src=src):
            label = f"[{_i + 1}/{len(sources)}] {os.path.basename(_src)} — {text}"
            status(label, None if frac is None else (_i + frac) / len(sources))

        r = optimize_ifc(src, dst, opts, log, st, cancel)
        if r.error:
            log(f"  ERRO: {r.error}")
        else:
            pct = (1 - r.size_after / r.size_before) * 100 if r.size_before else 0
            log(f"  OK -> {r.output}")
            log(f"     tamanho: {r.size_before / 1e6:.1f} MB -> {r.size_after / 1e6:.1f} MB "
                f"({pct:.0f}% menor)")
            log(f"     entidades: {r.entities_before} -> {r.entities_after}")
            log(f"     triângulos (malhas tratadas): {r.tri_before} -> {r.tri_after}")
            for name, a, b in sorted(r.simplified, key=lambda x: -x[1])[:10]:
                log(f"     simplificado: {name}: {a} -> {b}")
            if r.welded_only_breps:
                log(f"     {r.welded_only_breps} sólido(s) facetado(s) só tiveram pontos unidos "
                    f"(sólidos não são simplificados).")
            log(f"     tempo: {r.seconds:.1f}s")
        results.append(r)
    return results
