"""Núcleo do visualizador de origem: carrega uma amostra de pontos (SKP ou IFC)
e desenha planta 2D e vista 3D simples em imagem (Pillow + numpy).

Não depende de tkinter, então pode ser testado sem janela.
Convenção interna: coordenadas em METROS, eixo Z para cima.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

import skp2ifc_core as core

MAX_POINTS = 300_000          # pontos desenhados no total (amostra)
MAX_PER_ELEMENT = 600         # amostra máxima por elemento/malha

PALETTE = [
    (31, 119, 180), (255, 127, 14), (44, 160, 44), (214, 39, 40), (148, 103, 189),
    (140, 86, 75), (227, 119, 194), (127, 127, 127), (188, 189, 34), (23, 190, 207),
    (174, 199, 232), (255, 187, 120), (152, 223, 138), (255, 152, 150), (197, 176, 213),
]


@dataclass
class Model:
    path: str = ""
    kind: str = ""                                  # "skp" | "ifc"
    groups: Dict[str, np.ndarray] = field(default_factory=dict)   # nome -> (N,3) metros, Z-up
    counts: Dict[str, int] = field(default_factory=dict)          # nome -> nº de elementos/malhas
    bbox_min: Tuple[float, float, float] = (0.0, 0.0, 0.0)        # bbox REAL (todos os vértices)
    bbox_max: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    note: str = ""


def group_colors(names) -> Dict[str, Tuple[int, int, int]]:
    return {n: PALETTE[i % len(PALETTE)] for i, n in enumerate(sorted(names, key=str.lower))}


def _sample(v: np.ndarray, k: int, rng) -> np.ndarray:
    if len(v) <= k:
        return v
    return v[rng.choice(len(v), k, replace=False)]


def _surface_sample(v: np.ndarray, faces: Optional[np.ndarray], k: int, rng) -> np.ndarray:
    """Amostra k pontos sobre a SUPERFÍCIE das malhas (ponderado pela área).
    Sem faces válidas, cai para os próprios vértices."""
    if faces is None or len(faces) == 0:
        return _sample(v, k, rng)
    a, b, c = v[faces[:, 0]], v[faces[:, 1]], v[faces[:, 2]]
    area = np.linalg.norm(np.cross(b - a, c - a), axis=1)
    tot = float(area.sum())
    if tot <= 0:
        return _sample(v, k, rng)
    tri = rng.choice(len(faces), size=k, p=area / tot)
    r1 = np.sqrt(rng.random(k))[:, None]
    r2 = rng.random(k)[:, None]
    return a[tri] * (1 - r1) + b[tri] * (r1 * (1 - r2)) + c[tri] * (r1 * r2)


def _finish(model: Model, raw: Dict[str, List[np.ndarray]], mn, mx, counts) -> Model:
    rng = np.random.default_rng(1)
    total = sum(len(a) for lst in raw.values() for a in lst)
    keep = min(1.0, MAX_POINTS / max(1, total))
    for name, lst in raw.items():
        pts = np.concatenate(lst) if lst else np.zeros((0, 3))
        if keep < 1.0:
            pts = _sample(pts, max(1, int(len(pts) * keep)), rng)
        model.groups[name] = pts.astype(np.float32)
    model.counts = dict(counts)
    model.bbox_min = tuple(float(x) for x in mn)
    model.bbox_max = tuple(float(x) for x in mx)
    return model


# ------------------------------------------------------------------ SKP
def load_skp(path: str, status=core._noop) -> Model:
    scene = core.load_scene(path, status)
    status("Preparando a visualização", 0.9)
    rng = np.random.default_rng(1)
    raw: Dict[str, List[np.ndarray]] = {}
    counts: Dict[str, int] = {}
    mn = np.full(3, np.inf)
    mx = np.full(3, -np.inf)
    for prim in scene.glb_primitives:
        meta = scene.mesh_index.get(prim.geom_name)
        tag = (meta.layer if meta else "") or "Layer0"
        pos = np.asarray(prim.positions, dtype=np.float64).reshape(-1, 3)
        if len(pos) == 0:
            continue
        v = np.empty_like(pos)               # glTF Y-up -> Z-up (mesmo critério do conversor)
        v[:, 0] = pos[:, 0]
        v[:, 1] = -pos[:, 2]
        v[:, 2] = pos[:, 1]
        mn = np.minimum(mn, v.min(axis=0))
        mx = np.maximum(mx, v.max(axis=0))
        idx = np.asarray(prim.indices, dtype=np.int64).reshape(-1, 3)
        k = int(min(MAX_PER_ELEMENT, max(20, len(idx) * 4)))
        raw.setdefault(tag, []).append(_surface_sample(v, idx, k, rng))
        counts[tag] = counts.get(tag, 0) + 1
    if not raw:
        raise ValueError("O arquivo não tem geometria visível.")
    m = Model(path=path, kind="skp")
    return _finish(m, raw, mn, mx, counts)


# ------------------------------------------------------------------ IFC
def _ifc_group_name(prod, ue) -> str:
    name = prod.is_a()
    if name == "IfcBuildingElementProxy":
        try:
            name = (ue.get_psets(prod).get("SKP_Info") or {}).get("Tag") or name
        except Exception:
            pass
    return name


def load_ifc(path: str, status=core._noop) -> Model:
    """Caminho rápido: lê direto as listas de pontos das malhas triangulares e aplica a
    posição do elemento. O que não for malha triangular (extrusões, mapeamentos...) passa
    pelo motor de geometria do ifcopenshell, só para esses elementos."""
    import multiprocessing

    import ifcopenshell
    import ifcopenshell.geom
    import ifcopenshell.util.element as ue
    import ifcopenshell.util.placement as up
    import ifcopenshell.util.unit as uu

    status("Abrindo o IFC", None)
    f = ifcopenshell.open(path)
    scale = uu.calculate_unit_scale(f) or 1.0
    rng = np.random.default_rng(1)
    raw: Dict[str, List[np.ndarray]] = {}
    counts: Dict[str, int] = {}
    mn = np.full(3, np.inf)
    mx = np.full(3, -np.inf)
    slow = []

    def add(name, pts_world, faces):
        nonlocal mn, mx
        if len(pts_world) == 0:
            return
        mn = np.minimum(mn, pts_world.min(axis=0))
        mx = np.maximum(mx, pts_world.max(axis=0))
        k = int(min(MAX_PER_ELEMENT, max(20, (len(faces) if faces is not None else 0) * 4)))
        raw.setdefault(name, []).append(_surface_sample(pts_world, faces, k, rng))

    prods = [p for p in f.by_type("IfcProduct") if getattr(p, "Representation", None)]
    total = max(1, len(prods))
    status("Lendo a geometria do IFC", 0.02)
    for n, prod in enumerate(prods):
        if n % 200 == 0:
            status(f"Lendo elementos do IFC: {n} de {total}", 0.02 + 0.6 * n / total)
        items = []
        for rep in prod.Representation.Representations or []:
            items.extend(rep.Items or [])
        if not items or not all(i.is_a("IfcTriangulatedFaceSet") and not i.PnIndex for i in items):
            slow.append(prod)
            continue
        try:
            M = up.get_local_placement(prod.ObjectPlacement) if prod.ObjectPlacement else np.eye(4)
        except Exception:
            slow.append(prod)
            continue
        name = _ifc_group_name(prod, ue)
        seen = set()
        for it in items:
            cl = it.Coordinates
            if cl.id() in seen and it.id() in seen:
                continue
            seen.add(it.id())
            P = np.asarray(cl.CoordList, dtype=np.float64).reshape(-1, 3)
            T = np.asarray(it.CoordIndex, dtype=np.int64).reshape(-1, 3) - 1
            W = (P @ M[:3, :3].T + M[:3, 3]) * scale
            add(name, W, T)
        counts[name] = counts.get(name, 0) + 1

    if slow:
        status(f"Lendo {len(slow)} elementos com geometria complexa (mais lento)", 0.65)
        settings = ifcopenshell.geom.settings()
        settings.set("use-world-coords", True)
        it = ifcopenshell.geom.iterator(settings, f, max(1, multiprocessing.cpu_count()), include=slow)
        k = 0
        if it.initialize():
            while True:
                shape = it.get()
                try:
                    prod = f.by_id(shape.id)
                    v = np.asarray(shape.geometry.verts, dtype=np.float64).reshape(-1, 3)
                    fc = np.asarray(shape.geometry.faces, dtype=np.int64).reshape(-1, 3)
                    name = _ifc_group_name(prod, ue)
                    add(name, v, fc)
                    counts[name] = counts.get(name, 0) + 1
                except Exception:
                    pass
                k += 1
                if k % 25 == 0:
                    try:
                        frac = 0.65 + 0.33 * min(1.0, it.progress() / 100.0)
                    except Exception:
                        frac = None
                    status(f"Geometria complexa: {k} de {len(slow)} elementos", frac)
                if not it.next():
                    break
    if not raw:
        raise ValueError("Não encontrei geometria 3D nesse IFC.")
    m = Model(path=path, kind="ifc")
    return _finish(m, raw, mn, mx, counts)


def load_model(path: str, status=core._noop) -> Model:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".skp":
        return load_skp(path, status)
    if ext == ".ifc":
        return load_ifc(path, status)
    raise ValueError("Escolha um arquivo .skp ou .ifc.")


# ------------------------------------------------------------ transformação
def shifted_bbox(model: Model, shift) -> Tuple[np.ndarray, np.ndarray]:
    s = np.asarray(shift, dtype=np.float64)
    return np.asarray(model.bbox_min) + s, np.asarray(model.bbox_max) + s


def compute_shift(model: Model, offset_m, to_min: bool) -> np.ndarray:
    """Mesma regra do conversor: p' = p + offset  (ou  p - min + offset)."""
    sh = np.asarray(offset_m, dtype=np.float64).copy()
    if to_min:
        sh = sh - np.asarray(model.bbox_min)
    return sh


# --------------------------------------------------------------- desenho
@dataclass
class View3D:
    yaw: float = -45.0       # graus em torno de Z
    pitch: float = 35.0      # graus de inclinação (0 = lado, 90 = planta)
    zoom: float = 1.0
    pan: Tuple[float, float] = (0.0, 0.0)   # em pixels


def _fmt(v: float, unit: str, k: float) -> str:
    x = v * k
    return f"{x:,.0f}".replace(",", ".") if unit != "m" else f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def render(model: Model, shift, visible: Dict[str, bool], colors, mode: str,
           size: Tuple[int, int], view: Optional[View3D] = None,
           unit: str = "m", include_origin: bool = True):
    """Devolve uma imagem PIL. mode = 'plan' ou '3d'."""
    from PIL import Image, ImageDraw

    W, H = size
    shift = np.asarray(shift, dtype=np.float64)
    k = {"m": 1.0, "cm": 100.0, "mm": 1000.0}[unit]
    view = view or View3D()
    bg = (250, 250, 250)

    mn, mx = shifted_bbox(model, shift)
    corners = np.array([[x, y, z] for x in (mn[0], mx[0]) for y in (mn[1], mx[1]) for z in (mn[2], mx[2])])
    fit = np.vstack([corners, [[0.0, 0.0, 0.0]]]) if include_origin else corners
    ext = float(np.max(fit.max(axis=0) - fit.min(axis=0)))
    ext = ext if ext > 0 else 1.0
    center = (fit.max(axis=0) + fit.min(axis=0)) / 2.0

    if mode == "plan":
        yaw = pitch = 0.0
    else:
        yaw, pitch = math.radians(view.yaw), math.radians(view.pitch)

    # rotação: primeiro em Z (yaw), depois inclinação (pitch) -> x' direita, y' para cima, d profundidade
    cy, sy, cp, sp = math.cos(yaw), math.sin(yaw), math.cos(pitch), math.sin(pitch)

    def project(P: np.ndarray):
        Q = P - center
        if mode == "plan":
            xs, ys, dep = Q[:, 0], Q[:, 1], Q[:, 2]
        else:
            x1 = Q[:, 0] * cy - Q[:, 1] * sy
            y1 = Q[:, 0] * sy + Q[:, 1] * cy
            xs = x1
            ys = y1 * sp + Q[:, 2] * cp
            dep = y1 * cp - Q[:, 2] * sp          # maior = mais perto de quem olha? (usado só p/ ordenar)
        return xs, ys, dep

    margin = 60
    scale = (min(W, H) - 2 * margin) / ext * view.zoom
    ox, oy = W / 2 + view.pan[0], H / 2 + view.pan[1]

    def to_px(xs, ys):
        return ox + xs * scale, oy - ys * scale

    img = np.empty((H, W, 3), dtype=np.uint8)
    img[:] = bg

    # pontos
    xs_all, ys_all, dp_all, col_all, z_all = [], [], [], [], []
    for name, pts in model.groups.items():
        if not visible.get(name, True) or len(pts) == 0:
            continue
        P = pts.astype(np.float64) + shift
        xs, ys, dep = project(P)
        xs_all.append(xs); ys_all.append(ys); dp_all.append(dep); z_all.append(P[:, 2])
        col_all.append(np.tile(np.array(colors.get(name, (80, 80, 80)), dtype=np.float32), (len(P), 1)))
    if xs_all:
        xs = np.concatenate(xs_all); ys = np.concatenate(ys_all)
        dep = np.concatenate(dp_all); zz = np.concatenate(z_all)
        col = np.concatenate(col_all)
        px, py = to_px(xs, ys)
        px = np.round(px).astype(np.int64)
        py = np.round(py).astype(np.int64)
        # sombreamento leve pela altura (planta) / profundidade (3D)
        key = zz if mode == "plan" else dep
        rng_ = float(key.max() - key.min()) or 1.0
        shade = 0.65 + 0.35 * (key - key.min()) / rng_
        col = (col * shade[:, None]).clip(0, 255).astype(np.uint8)
        order = np.argsort(key)                     # desenha de trás para frente
        px, py, col = px[order], py[order], col[order]
        for dx in (0, 1):
            for dy in (0, 1):
                xx, yy = px + dx, py + dy
                ok = (xx >= 0) & (xx < W) & (yy >= 0) & (yy < H)
                img[yy[ok], xx[ok]] = col[ok]

    pil = Image.fromarray(img)
    d = ImageDraw.Draw(pil)

    def line3(a, b, fill, width=1):
        pa = project(np.array([a], dtype=np.float64))
        pb = project(np.array([b], dtype=np.float64))
        xa, ya = to_px(pa[0][0], pa[1][0])
        xb, yb = to_px(pb[0][0], pb[1][0])
        d.line([(xa, ya), (xb, yb)], fill=fill, width=width)

    # caixa envolvente
    bx = (60, 60, 60)
    edges = [((0, 0, 0), (1, 0, 0)), ((0, 1, 0), (1, 1, 0)), ((0, 0, 1), (1, 0, 1)), ((0, 1, 1), (1, 1, 1)),
             ((0, 0, 0), (0, 1, 0)), ((1, 0, 0), (1, 1, 0)), ((0, 0, 1), (0, 1, 1)), ((1, 0, 1), (1, 1, 1)),
             ((0, 0, 0), (0, 0, 1)), ((1, 0, 0), (1, 0, 1)), ((0, 1, 0), (0, 1, 1)), ((1, 1, 0), (1, 1, 1))]
    for a, b in edges:
        if mode == "plan" and (a[2] or b[2]):
            continue
        pa = [mn[i] if a[i] == 0 else mx[i] for i in range(3)]
        pb = [mn[i] if b[i] == 0 else mx[i] for i in range(3)]
        line3(pa, pb, bx)

    # eixos na origem (0,0,0 do IFC resultante)
    L = ext * 0.12
    line3((0, 0, 0), (L, 0, 0), (220, 30, 30), 3)
    line3((0, 0, 0), (0, L, 0), (30, 170, 30), 3)
    if mode != "plan":
        line3((0, 0, 0), (0, 0, L), (30, 30, 230), 3)
    p0 = project(np.zeros((1, 3)))
    x0, y0 = to_px(p0[0][0], p0[1][0])
    r = 7
    d.ellipse([x0 - r, y0 - r, x0 + r, y0 + r], outline=(0, 0, 0), width=2, fill=(255, 235, 0))
    d.text((x0 + 10, y0 - 24), "ORIGEM (0,0,0)", fill=(0, 0, 0))
    for lbl, vec, c in (("X", (L, 0, 0), (220, 30, 30)), ("Y", (0, L, 0), (30, 170, 30)), ("Z", (0, 0, L), (30, 30, 230))):
        if lbl == "Z" and mode == "plan":
            continue
        pp = project(np.array([vec], dtype=np.float64))
        lx, ly = to_px(pp[0][0], pp[1][0])
        d.text((lx + 4, ly - 12), lbl, fill=c)

    # rótulos do canto mínimo/máximo
    for P, tag in ((mn, "min"), (mx, "max")):
        pp = project(np.array([P], dtype=np.float64))
        lx, ly = to_px(pp[0][0], pp[1][0])
        d.text((lx + 4, ly + 14 if tag == "min" else ly - 16),
               f"{tag}: {_fmt(P[0], unit, k)} / {_fmt(P[1], unit, k)} / {_fmt(P[2], unit, k)} {unit}",
               fill=(40, 40, 40))
    d.text((8, 6), "Planta (vista de cima, Y para cima)" if mode == "plan"
           else "Vista 3D (arraste = girar, roda = zoom, botao direito = mover)", fill=(0, 0, 0))
    return pil


def describe(model: Model, shift, unit: str = "m") -> str:
    k = {"m": 1.0, "cm": 100.0, "mm": 1000.0}[unit]
    mn, mx = shifted_bbox(model, shift)
    sz = mx - mn
    dist = float(np.linalg.norm(((mn + mx) / 2.0)[:2]))
    return (f"Canto mínimo: X {_fmt(mn[0], unit, k)}  Y {_fmt(mn[1], unit, k)}  Z {_fmt(mn[2], unit, k)} {unit}\n"
            f"Canto máximo: X {_fmt(mx[0], unit, k)}  Y {_fmt(mx[1], unit, k)}  Z {_fmt(mx[2], unit, k)} {unit}\n"
            f"Tamanho: {_fmt(sz[0], unit, k)} × {_fmt(sz[1], unit, k)} × {_fmt(sz[2], unit, k)} {unit}\n"
            f"Distância do centro à origem (em planta): {_fmt(dist, unit, k)} {unit}")
