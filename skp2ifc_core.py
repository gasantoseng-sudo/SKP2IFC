"""Núcleo do conversor SKP -> IFC (sem interface gráfica).

Lê arquivos .skp com o OpenSKP (não precisa do SketchUp instalado) e escreve
IFC com o ifcopenshell. Tudo vira IfcBuildingElementProxy, que é o suficiente
para usar o IFC como modelo de referência de arquitetura.

Convenções:
  * O SketchUp é Z-up; o OpenSKP entrega a cena em metros, Y-up (padrão glTF).
    Aqui convertemos de volta para Z-up: (x, y, z)_gltf -> (x, -z, y)_ifc.
  * Cada instância (grupo/componente) vira um elemento, com a tag (layer) e o
    caminho de origem gravados no Pset "SKP_Info".
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

import numpy as np

UNIT_SCALE = {"m": 1.0, "cm": 100.0, "mm": 1000.0}
UNIT_PREFIX = {"m": None, "cm": "CENTI", "mm": "MILLI"}
SCHEMAS = ("IFC2X3", "IFC4")

# Palavras que costumam marcar itens que só pesam em um modelo de referência.
DEFAULT_EXCLUDE_KEYWORDS = (
    "mobili", "furniture", "veget", "arvore", "árvore", "tree",
    "pessoa", "people", "cota", "dimension",
)

LogFn = Callable[[str], None]
ProgressFn = Callable[[float], None]
StatusFn = Callable[[str, Optional[float]], None]  # (etapa, fração 0..1 ou None)


class ConversionCancelled(Exception):
    pass


@dataclass
class Options:
    unit: str = "m"                      # "m", "cm" ou "mm"
    schema: str = "IFC2X3"               # "IFC2X3" ou "IFC4"
    offset: Tuple[float, float, float] = (0.0, 0.0, 0.0)  # na unidade escolhida
    origin_to_min: bool = False          # leva o canto mínimo do modelo à origem
    exclude_tags: Set[str] = field(default_factory=set)         # exclusão explícita
    force_include_tags: Set[str] = field(default_factory=set)   # vence as palavras-chave
    exclude_keywords: Tuple[str, ...] = ()                      # exclui tags que contenham
    skip_hidden_tags: bool = True        # respeita tags desligadas no SketchUp
    overwrite: bool = False              # False = nunca sobrescreve (numera)
    max_tri_per_element: int = 30000     # 0 = sem limite; acima disso a malha é simplificada


@dataclass
class TagInfo:
    name: str
    hidden: bool = False
    triangles: int = 0


@dataclass
class Result:
    source: str
    output: Optional[str] = None
    elements: int = 0
    triangles: int = 0
    skipped_triangles: int = 0
    decimated: int = 0                   # elementos simplificados
    bbox_min: Optional[Tuple[float, float, float]] = None  # unidade escolhida
    bbox_max: Optional[Tuple[float, float, float]] = None
    seconds: float = 0.0
    error: Optional[str] = None


def _noop(*_a, **_k):
    return None


# --------------------------------------------------------------------------
# Leitura
# --------------------------------------------------------------------------

_REC_RE = re.compile(r"Processed (\d+)/(\d+) top-level records")
_DEF_RE = re.compile(r"Processed (\d+) component definitions")
_INST_RE = re.compile(r"Processed (\d+) placed instances")
_CACHE: Dict[str, object] = {}  # no máximo 1 cena na memória: {"key": ..., "scene": ...}


class _ProgressCapture(logging.Handler):
    """Traduz as mensagens de log do OpenSKP em (etapa, fração)."""

    def __init__(self, status: StatusFn) -> None:
        super().__init__(logging.DEBUG)
        self.status = status

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
            m = _REC_RE.search(msg)
            if m:
                a, b = int(m.group(1)), int(m.group(2))
                self.status("Lendo o arquivo", 0.55 * a / max(1, b))
                return
            m = _DEF_RE.search(msg)
            if m:
                self.status(f"Lendo componentes ({int(m.group(1))} lidos)", None)
                return
            m = _INST_RE.search(msg)
            if m:
                self.status(f"Montando a cena ({int(m.group(1))} instâncias)", None)
        except Exception:
            pass


def clear_cache() -> None:
    _CACHE.clear()


# --------------------------------------------------------------------------
# Correções do leitor do openskp (1.5.0) para .skp antigos:
# (1) SketchUp 2018-2020: cada tag (layer) aponta para um objeto CCustomLineStyle ("Solid Basic", ...).
# (2) SketchUp 2013 e similares: o registro CImage não tem o GUID final.
# Sobre (1):
# O estilo vem gravado logo depois do registro da tag; sem este ajuste a leitura
# para com "no reader for class CCustomLineStyle". Não afeta arquivos sem esse objeto.
_PATCHED = False


def _patch_openskp() -> None:
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True
    try:
        import struct
        from openskp import legacy
    except Exception:
        return
    readers = getattr(legacy, "_READERS", None)
    if not isinstance(readers, dict) or "CCustomLineStyle" in readers or "CLayer" not in readers:
        return
    orig_layer = readers["CLayer"]

    def read_line_style(ar, r):
        legacy._preamble(ar, r)
        name = r.utf16()
        ver = r.utf16()
        vals = [r.f64() for _ in range(3)]
        return {"k": "linestyle", "name": name, "ver": ver, "vals": vals}

    def is_style_ref(ar, r) -> bool:
        if r.pos + 2 > len(ar.data):
            return False
        tag = struct.unpack_from("<H", ar.data, r.pos)[0]
        if tag == 0xFFFF:
            return ar.data[r.pos + 6:r.pos + 22] == b"CCustomLineStyle"
        if tag & 0x8000:
            ent = ar.slots.get(tag & 0x7FFF)
            return bool(ent and ent[0] == "class" and ent[1] == "CCustomLineStyle")
        if tag and tag != 0x7FFF:
            ent = ar.slots.get(tag)
            return bool(ent and ent[0] == "obj" and ent[1] == "CCustomLineStyle")
        return False

    def read_layer(ar, r):
        v = orig_layer(ar, r)
        if is_style_ref(ar, r):
            ar.read_object(r)
        return v

    readers["CCustomLineStyle"] = read_line_style
    readers["CLayer"] = read_layer

    # CImage em arquivos antigos (SketchUp 2013 e similares, sem "persistent id"): o registro
    # termina na string do caminho, SEM o GUID de 16 bytes que o leitor espera nos arquivos 2017+.
    orig_image = readers.get("CImage")
    if orig_image is not None:
        def read_image(ar, r):
            if getattr(ar, "has_pid", True):
                return orig_image(ar, r)
            legacy._preamble(ar, r)
            db = legacy._drawbase(ar, r)
            ds, _dn, _ = ar.read_object(r)       # definição que contém a imagem
            xform = r.f64s(12)
            r.f64()                               # constante 1.0
            r.utf16()                             # caminho de origem
            return {"k": "image", "db": db, "def": ds, "xform": xform, "guid": ""}

        readers["CImage"] = read_image


def load_scene(path: str, status: StatusFn = _noop, use_cache: bool = True):
    """Abre o .skp e devolve a cena baked (metros, Y-up).

    Guarda a última cena lida: "Analisar tags" seguido de "Converter" lê o
    arquivo uma vez só (a conversão limpa o cache ao terminar).
    """
    from openskp import SkpFile

    _patch_openskp()
    st = os.stat(path)
    key = f"{os.path.abspath(path)}|{st.st_size}|{st.st_mtime_ns}"
    if use_cache and _CACHE.get("key") == key:
        status("Usando a leitura já feita", None)
        return _CACHE["scene"]

    status("Lendo o arquivo", None)
    handler = _ProgressCapture(status)
    lg = logging.getLogger("openskp")
    old_level = lg.level
    lg.setLevel(logging.DEBUG)
    lg.addHandler(handler)
    try:
        scene = SkpFile.open(path).build_scene()
    except Exception as e:
        msg = str(e)
        if "no reader for class" in msg or type(e).__name__ == "SkpParseError":
            cls = re.search(r"no reader for class (\w+)", msg)
            what = f" (elemento não suportado: {cls.group(1)})" if cls else ""
            raise RuntimeError(
                "Não consegui ler este .skp" + what + ". O formato antigo do SketchUp (2020 ou anterior) "
                "tem variações que o leitor ainda não conhece.\n"
                "    Solução rápida: abra o arquivo no SketchUp 2021 ou mais novo e use Arquivo > Salvar como "
                "(o formato novo é lido sem esse problema).\n"
                "    Se não der, envie o arquivo para o desenvolvedor do app para incluir o suporte.\n"
                f"    Detalhe técnico: {msg[:200]}") from e
        raise
    finally:
        lg.removeHandler(handler)
        lg.setLevel(old_level)
    _CACHE.clear()
    _CACHE.update({"key": key, "scene": scene})
    return scene


def scan_tags(path: str, scene=None, status: StatusFn = _noop) -> Dict[str, TagInfo]:
    """Lista as tags (layers) do arquivo, com contagem de triângulos."""
    scene = scene or load_scene(path, status)
    tags: Dict[str, TagInfo] = {}
    for prim in scene.glb_primitives:
        meta = scene.mesh_index.get(prim.geom_name)
        name = (meta.layer if meta else "") or "Layer0"
        info = tags.setdefault(name, TagInfo(name, bool(scene.layer_hidden.get(name, False))))
        info.triangles += len(prim.indices) // 3
    for name, hidden in scene.layer_hidden.items():
        tags.setdefault(name, TagInfo(name, bool(hidden)))
    return tags


def default_excluded(tag_names: Iterable[str],
                     keywords: Iterable[str] = DEFAULT_EXCLUDE_KEYWORDS) -> Set[str]:
    kws = [k.strip().lower() for k in keywords if k.strip()]
    out = set()
    for t in tag_names:
        low = t.lower()
        if any(k in low for k in kws):
            out.add(t)
    return out


def tag_is_excluded(tag: str, opts: "Options") -> bool:
    if tag in opts.exclude_tags:
        return True
    if tag in opts.force_include_tags:
        return False
    low = tag.lower()
    return any(k and k in low for k in (x.strip().lower() for x in opts.exclude_keywords))


# --------------------------------------------------------------------------
# Escrita
# --------------------------------------------------------------------------

def _unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 2
    while os.path.exists(f"{base}_{n}{ext}"):
        n += 1
    return f"{base}_{n}{ext}"


def _clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name or "").strip()
    return name or "Sem nome"


# casas decimais por unidade: 0,01 mm de precisão (bem abaixo do que importa na obra)
COORD_DECIMALS = {"m": 5, "cm": 4, "mm": 3}


def _weld(v: np.ndarray, idx: np.ndarray, decimals: int):
    """Une vértices coincidentes. Devolve (vértices únicos, triângulos remapeados)."""
    vr = np.round(v, decimals)
    uniq, inv = np.unique(vr, axis=0, return_inverse=True)
    inv = inv.reshape(-1)
    t = inv[idx]
    ok = (t[:, 0] != t[:, 1]) & (t[:, 1] != t[:, 2]) & (t[:, 0] != t[:, 2])
    return uniq, t[ok]


def _simplify(v: np.ndarray, t: np.ndarray, target_tris: int):
    """Simplificação quadrática da malha. Falha silenciosa (devolve a original)."""
    try:
        import fast_simplification
    except Exception:
        return v, t, False
    if len(t) <= max(target_tris, 50):
        return v, t, False
    red = 1.0 - target_tris / len(t)
    red = float(min(max(red, 0.0), 0.98))
    try:
        v2, t2 = fast_simplification.simplify(
            np.ascontiguousarray(v, dtype=np.float64),
            np.ascontiguousarray(t, dtype=np.int32), target_reduction=red)
        if len(t2) == 0:
            return v, t, False
        return np.asarray(v2, dtype=np.float64), np.asarray(t2, dtype=np.int64), True
    except Exception:
        return v, t, False


def _build_ifc(scene, opts: Options, project_name: str, log: LogFn,
               cancel: Optional[threading.Event],
               status: StatusFn = _noop) -> Tuple[object, Result]:
    import ifcopenshell
    import ifcopenshell.api
    # nas versões novas os submódulos da API precisam ser importados à mão
    import ifcopenshell.api.aggregate
    import ifcopenshell.api.context
    import ifcopenshell.api.geometry
    import ifcopenshell.api.owner
    import ifcopenshell.api.owner.settings
    import ifcopenshell.api.project
    import ifcopenshell.api.pset
    import ifcopenshell.api.root
    import ifcopenshell.api.spatial
    import ifcopenshell.api.style
    import ifcopenshell.api.unit

    api = ifcopenshell.api
    scale = UNIT_SCALE[opts.unit]
    res = Result(source="")

    status("Aplicando filtros e convertendo coordenadas", 0.6)
    # 1) seleciona primitivas e converte para IFC (Z-up) na unidade escolhida
    selected = []  # (prim, meta, verts_ifc ndarray (n,3), tris ndarray (m,3))
    dropped: Dict[str, Tuple[str, int]] = {}  # tag -> (motivo, triângulos)
    for prim in scene.glb_primitives:
        meta = scene.mesh_index.get(prim.geom_name)
        tag = (meta.layer if meta else "") or "Layer0"
        reason = None
        if tag_is_excluded(tag, opts):
            reason = "excluída pelo filtro"
        elif opts.skip_hidden_tags and scene.layer_hidden.get(tag, False):
            reason = "oculta no SketchUp"
        if reason:
            old = dropped.get(tag, (reason, 0))
            dropped[tag] = (reason, old[1] + len(prim.indices) // 3)
            continue
        pos = np.asarray(prim.positions, dtype=np.float64).reshape(-1, 3)
        idx = np.asarray(prim.indices, dtype=np.int64).reshape(-1, 3)
        if len(pos) == 0 or len(idx) == 0:
            continue
        v = np.empty_like(pos)
        v[:, 0] = pos[:, 0]
        v[:, 1] = -pos[:, 2]
        v[:, 2] = pos[:, 1]
        v *= scale
        # descarta triângulos degenerados (índices repetidos)
        ok = (idx[:, 0] != idx[:, 1]) & (idx[:, 1] != idx[:, 2]) & (idx[:, 0] != idx[:, 2])
        res.skipped_triangles += int((~ok).sum())
        idx = idx[ok]
        if len(idx) == 0:
            continue
        selected.append((prim, meta, tag, v, idx))

    if dropped:
        log("  Tags NÃO exportadas:")
        for tag, (why, tri) in sorted(dropped.items(), key=lambda kv: kv[0].lower()):
            log(f"    - {tag}  ({why}, {tri} triângulos)")
    if not selected:
        raise ValueError("Nenhuma geometria restou depois dos filtros de tags.")

    allv = np.concatenate([s[3] for s in selected])
    mn = allv.min(axis=0)
    shift = np.array(opts.offset, dtype=np.float64)
    if opts.origin_to_min:
        shift = shift - mn
    if np.any(shift != 0):
        for s in selected:
            s[3][:] += shift
        allv = allv + shift
    res.bbox_min = tuple(float(x) for x in allv.min(axis=0))
    res.bbox_max = tuple(float(x) for x in allv.max(axis=0))

    # 2) estrutura do arquivo IFC
    f = ifcopenshell.api.project.create_file(version=opts.schema)
    if opts.schema == "IFC2X3":
        # o IFC2X3 exige OwnerHistory (usuário + aplicação) em todo elemento
        person = api.owner.add_person(f, identification="SKP2IFC",
                                      family_name="SKP2IFC", given_name="Conversor")
        org = api.owner.add_organisation(f, identification="SKP2IFC", name="SKP2IFC")
        user = api.owner.add_person_and_organisation(f, person=person, organisation=org)
        application = api.owner.add_application(
            f, application_full_name="SKP2IFC", application_identifier="SKP2IFC")
        api.owner.settings.get_user = lambda _ifc: user
        api.owner.settings.get_application = lambda _ifc: application
    project = api.root.create_entity(f, ifc_class="IfcProject", name=project_name)
    length = api.unit.add_si_unit(f, unit_type="LENGTHUNIT", prefix=UNIT_PREFIX[opts.unit])
    api.unit.assign_unit(f, units=[
        length,
        api.unit.add_si_unit(f, unit_type="AREAUNIT"),
        api.unit.add_si_unit(f, unit_type="VOLUMEUNIT"),
        api.unit.add_si_unit(f, unit_type="PLANEANGLEUNIT"),
    ])
    ctx = api.context.add_context(f, context_type="Model")
    body = api.context.add_context(
        f, context_type="Model", context_identifier="Body",
        target_view="MODEL_VIEW", parent=ctx)

    site = api.root.create_entity(f, ifc_class="IfcSite", name="Terreno")
    building = api.root.create_entity(f, ifc_class="IfcBuilding", name="Edificio")
    storey = api.root.create_entity(f, ifc_class="IfcBuildingStorey", name="Modelo SKP")
    api.aggregate.assign_object(f, products=[site], relating_object=project)
    api.aggregate.assign_object(f, products=[building], relating_object=site)
    api.aggregate.assign_object(f, products=[storey], relating_object=building)
    for p in (site, building, storey):
        api.geometry.edit_object_placement(f, product=p)

    # 3) estilos (cor/transparência) por material do SketchUp
    style_cache: Dict[int, object] = {}

    def style_for(mat_idx: int):
        if mat_idx in style_cache:
            return style_cache[mat_idx]
        try:
            mat = scene.gltf_materials[mat_idx]
            r, g, b, a = mat["pbrMetallicRoughness"]["baseColorFactor"]
        except Exception:
            r = g = b = 0.53
            a = 1.0
        style = api.style.add_style(f, name=f"SKP_Material_{mat_idx}")
        api.style.add_surface_style(
            f, style=style, ifc_class="IfcSurfaceStyleRendering",
            attributes={
                "SurfaceColour": {"Name": None, "Red": float(r), "Green": float(g), "Blue": float(b)},
                "Transparency": float(max(0.0, min(1.0, 1.0 - a))),
                "ReflectanceMethod": "NOTDEFINED",
            })
        style_cache[mat_idx] = style
        return style

    # 4) agrupa primitivas por instância (caminho + tag) -> um elemento cada
    groups: Dict[Tuple[str, str], list] = {}
    for s in selected:
        _prim, meta, tag, _v, _i = s
        key = ((meta.path if meta else "") or "ROOT", tag)
        groups.setdefault(key, []).append(s)

    is_ifc4 = opts.schema == "IFC4"
    total = len(groups)
    t_last = 0.0
    for n, ((path, tag), items_src) in enumerate(groups.items()):
        if cancel is not None and cancel.is_set():
            raise ConversionCancelled()
        rep_items = []
        item_styles = []
        meta0_ = items_src[0][1]
        el_name = _clean_name(meta0_.name if meta0_ and meta0_.name and meta0_.name != "ROOT"
                              else (path.split(" / ")[-1] if path != "ROOT" else "Modelo"))
        nd = COORD_DECIMALS[opts.unit]
        parts = []  # (vértices, triângulos, índice de material)
        before = sum(len(s_[4]) for s_ in items_src)
        too_big = opts.max_tri_per_element > 0 and before > opts.max_tri_per_element
        for (prim, meta, _tag, v, idx) in items_src:
            vv, tt = _weld(v, idx, nd)
            if too_big and len(tt):
                share = len(tt) / before
                vv, tt, _ok = _simplify(vv, tt, max(1, int(opts.max_tri_per_element * share)))
            if len(tt):
                parts.append((vv, tt, prim.material_index))
        if not parts:
            continue
        # um só conjunto de pontos por elemento, compartilhado entre os itens
        offs = np.cumsum([0] + [len(p_[0]) for p_ in parts])
        allv = np.concatenate([p_[0] for p_ in parts])
        allt = [p_[1] + offs[k] for k, p_ in enumerate(parts)]
        used = np.unique(np.concatenate(allt).reshape(-1))
        remap = np.full(len(allv), -1, dtype=np.int64)
        remap[used] = np.arange(len(used))
        coords = np.round(allv[used], nd)
        if is_ifc4:
            pl = f.createIfcCartesianPointList3D([tuple(map(float, p_)) for p_ in coords])
        else:
            pts = [f.createIfcCartesianPoint(tuple(map(float, p_))) for p_ in coords]
        tri_count = 0
        for k, tt in enumerate(allt):
            ii = remap[tt]
            if is_ifc4:
                item = f.createIfcTriangulatedFaceSet(
                    pl, None, None, [tuple(int(q) + 1 for q in t_) for t_ in ii], None)
            else:
                faces = []
                for t_ in ii:
                    loop = f.createIfcPolyLoop([pts[int(t_[0])], pts[int(t_[1])], pts[int(t_[2])]])
                    faces.append(f.createIfcFace([f.createIfcFaceOuterBound(loop, True)]))
                item = f.createIfcFaceBasedSurfaceModel([f.createIfcConnectedFaceSet(faces)])
            rep_items.append(item)
            item_styles.append(style_for(parts[k][2]))
            tri_count += len(ii)
        if too_big and tri_count < before:
            res.decimated += 1
            log(f"  Simplificado: {el_name}: {before} -> {tri_count} triângulos")

        rep = f.createIfcShapeRepresentation(
            body, "Body", "Tessellation" if is_ifc4 else "SurfaceModel", rep_items)
        for item, style in zip(rep_items, item_styles):
            api.style.assign_item_style(f, item=item, style=style)

        meta0 = items_src[0][1]
        name = _clean_name(meta0.name if meta0 and meta0.name and meta0.name != "ROOT"
                           else (path.split(" / ")[-1] if path != "ROOT" else "Modelo"))
        element = api.root.create_entity(f, ifc_class="IfcBuildingElementProxy", name=name)
        api.geometry.edit_object_placement(f, product=element)
        element.Representation = f.createIfcProductDefinitionShape(None, None, [rep])
        api.spatial.assign_container(f, products=[element], relating_structure=storey)
        pset = api.pset.add_pset(f, product=element, name="SKP_Info")
        api.pset.edit_pset(f, pset=pset, properties={
            "Tag": tag,
            "Caminho": path.replace(" / ", " > "),
            "Definicao": (meta0.definition_name if meta0 else "") or "",
        })
        res.triangles += tri_count

        now = time.monotonic()
        if now - t_last > 0.5 or n == total - 1:
            t_last = now
            log(f"  elementos escritos: {n + 1}/{total}")
            status(f"Gerando IFC: elemento {n + 1} de {total}", 0.65 + 0.30 * (n + 1) / total)

    res.elements = total
    return f, res


# --------------------------------------------------------------------------
# API pública
# --------------------------------------------------------------------------

def convert_file(src: str, dst: Optional[str], opts: Options,
                 log: LogFn = _noop,
                 cancel: Optional[threading.Event] = None,
                 status: StatusFn = _noop) -> Result:
    """Converte um .skp em .ifc. `dst` None = mesmo nome, ao lado do original."""
    t0 = time.monotonic()
    res = Result(source=src)
    try:
        if opts.unit not in UNIT_SCALE:
            raise ValueError(f"Unidade inválida: {opts.unit}")
        if opts.schema not in SCHEMAS:
            raise ValueError(f"Esquema IFC inválido: {opts.schema}")
        if dst is None:
            dst = os.path.splitext(src)[0] + ".ifc"
        if not opts.overwrite:
            dst = _unique_path(dst)
        os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)

        log(f"Lendo {os.path.basename(src)} ...")
        scene = load_scene(src, status)
        if not scene.glb_primitives:
            raise ValueError("O arquivo não tem faces (só linhas/guias?) ou não pôde ser interpretado.")
        log(f"  {len(scene.glb_primitives)} malhas encontradas. Gerando IFC ({opts.schema}) ...")
        try:
            f, built = _build_ifc(scene, opts, os.path.splitext(os.path.basename(src))[0],
                                  log, cancel, status)
        finally:
            clear_cache()
            try:
                import ifcopenshell.api.owner.settings as _os
                _os.factory_reset()
            except Exception:
                pass
        status("Gravando o arquivo IFC no disco (pode demorar em modelos grandes)", 0.97)
        f.write(dst)
        status("Concluído", 1.0)
        built.source = src
        built.output = dst
        built.seconds = time.monotonic() - t0
        return built
    except ConversionCancelled:
        res.error = "Cancelado."
    except Exception as e:  # erro por arquivo não derruba o lote
        res.error = f"{type(e).__name__}: {e}"
    res.seconds = time.monotonic() - t0
    return res


def find_skp_files(folder: str, recursive: bool = True) -> List[str]:
    out = []
    if recursive:
        for root, _d, files in os.walk(folder):
            out += [os.path.join(root, x) for x in files if x.lower().endswith(".skp")]
    else:
        out = [os.path.join(folder, x) for x in os.listdir(folder) if x.lower().endswith(".skp")]
    return sorted(out)


def convert_batch(sources: List[str], out_dir: Optional[str], opts: Options,
                  log: LogFn = _noop, progress: ProgressFn = _noop,
                  cancel: Optional[threading.Event] = None,
                  status: StatusFn = _noop) -> List[Result]:
    results = []
    for i, src in enumerate(sources):
        if cancel is not None and cancel.is_set():
            log("Lote cancelado.")
            break
        progress(i / max(1, len(sources)))
        dst = None
        if out_dir:
            dst = os.path.join(out_dir, os.path.splitext(os.path.basename(src))[0] + ".ifc")
        def st(text, frac, _i=i, _src=src):
            label = f"[{_i + 1}/{len(sources)}] {os.path.basename(_src)} — {text}"
            status(label, None if frac is None else (_i + frac) / len(sources))
        r = convert_file(src, dst, opts, log, cancel, st)
        if r.error:
            log(f"  ERRO: {r.error}")
        else:
            bmin, bmax = r.bbox_min, r.bbox_max
            log(f"  OK -> {r.output}\n     {r.elements} elementos, {r.triangles} triângulos, "
                f"{r.seconds:.1f}s\n     caixa ({opts.unit}): "
                f"min=({bmin[0]:.3f}, {bmin[1]:.3f}, {bmin[2]:.3f}) "
                f"max=({bmax[0]:.3f}, {bmax[1]:.3f}, {bmax[2]:.3f})")
        results.append(r)
    progress(1.0)
    return results
