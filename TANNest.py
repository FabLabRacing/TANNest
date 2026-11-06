#!/usr/bin/env python3
"""
DXF Sheet Nester
================
Nests DXF parts onto stock sheets (rectangular / bounding-box nesting) and
automatically splits parts that are too long for any sheet into weldable
segments, keeping seams away from internal features and notches.

Requires:  pip install ezdxf shapely
Run:       python tannest.py

How it works (short version):
  1. Each DXF is read and its geometry is chained into closed loops.
     The biggest loop is the part outline. Loops inside it are classified:
        - a loop with other loops inside it  -> COMMON_LINE (a drop part that
          shares its cut line with the opening, e.g. a cover plate)
        - a loop with nothing inside it      -> HOLES
     If a DXF contains several separate outlines, each one becomes its own part.
  2. Parts that don't fit any enabled sheet get split along their long axis.
     Several seam layouts are generated (fill-the-biggest-sheet from the left,
     from the right, evenly balanced, ...), all respecting keep-out zones.
  3. Every combination of seam layouts is packed with a MaxRects packer using
     several orderings/heuristics. Each sheet is then shrunk to the smallest
     enabled sheet its contents still fit on.
  4. The result with the least total sheet area wins (ties: fewer seams,
     then fewer sheets).

Limits worth knowing:
  - Nesting uses bounding rectangles (0 / 90 degree rotation). It does not
    true-shape nest or place small parts inside openings.
  - Splitting is one direction only (seams perpendicular to the long axis).
  - Split segment outlines are written as polylines (arcs on the cut outline
    are flattened to the tolerance setting). Un-split parts and all internal
    features are written as their original lines / arcs / circles.


"""

import itertools
import json
import math
import os
import queue
import random
import threading
import time
import traceback
from dataclasses import dataclass

import ezdxf
from ezdxf import path as ezpath
from ezdxf.math import Matrix44, Vec3
from shapely.geometry import Polygon, box
from shapely.geometry.polygon import orient

EPS = 1e-6
APP_NAME = "TANNest"
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".tannest.json")

DEFAULT_SHEETS = [(48, 96), (60, 96), (72, 96),
                  (48, 120), (60, 120), (72, 120),
                  (48, 144), (60, 144), (72, 144)]

# Output layers: name -> ACI color
LAYERS = {"OUTSIDE": 7, "COMMON_LINE": 1, "HOLES": 3,
          "SHEET_REF_NOCUT": 8, "SEAM_REF": 6}

UNIT_NAMES = {0: "unitless", 1: "in", 2: "ft", 4: "mm", 5: "cm", 6: "m"}


# ============================================================================
#  Settings
# ============================================================================
@dataclass
class Settings:
    border: float = 0.375        # min distance part -> sheet edge
    spacing: float = 1.0         # min distance part -> part
    seam_clear: float = 2.0      # min distance seam -> internal feature / notch
    min_seg: float = 6.0         # shortest allowed split segment
    seam_step: float = 0.0625    # seams land on multiples of this (0 = exact)
    flatten_tol: float = 0.005   # chord tolerance when arcs become polylines
    join_tol: float = 0.005      # endpoint gap allowed when chaining lines/arcs
    effort: int = 400            # number of pack attempts
    avoid_notches: bool = True   # keep seams out of notches / concave areas
    # ---- cost model (layouts are scored in dollars)
    thickness: float = 0.25          # plate thickness, inches
    density: float = 0.2836          # lb / cubic inch (steel)
    material_cost_lb: float = 0.0    # $ / lb of plate
    weld_cost_ft: float = 0.0        # $ / ft of finished seam (fit-up, prep, all passes)
    remnant_credit: float = 0.0      # % of material value credited back for usable remnants
    min_remnant: float = 12.0        # inches: narrowest strip that counts as a usable remnant


# ============================================================================
#  DXF reading
# ============================================================================
@dataclass
class Loop:
    ents: list          # [(entity, reversed_flag), ...] in chain order
    pts: list           # flattened (x, y) points, original coordinates
    poly: Polygon       # filled polygon of this loop
    depth: int = 0
    parent: int = -1
    has_children: bool = False


@dataclass
class Part:
    name: str
    file: str
    outer: Loop
    inner: list                  # [(Loop, layer_name)]
    units: int = 1
    qty: int = 1
    profile_only: bool = False
    allow_rotate: bool = True
    allow_split: bool = True
    doc: object = None           # keeps the source document alive

    @property
    def size(self):
        x0, y0, x1, y1 = self.outer.poly.bounds
        return x1 - x0, y1 - y0


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _iter_geometry(entities, skipped, depth=0):
    """Yield drawable entities, exploding block references."""
    for e in entities:
        t = e.dxftype()
        if t == "INSERT" and depth < 8:
            try:
                yield from _iter_geometry(list(e.virtual_entities()), skipped, depth + 1)
            except Exception:
                skipped["INSERT(failed)"] = skipped.get("INSERT(failed)", 0) + 1
        elif t in ("LINE", "ARC", "CIRCLE", "LWPOLYLINE", "POLYLINE", "SPLINE", "ELLIPSE"):
            yield e
        else:
            skipped[t] = skipped.get(t, 0) + 1


def _flatten(e, tol):
    p = ezpath.make_path(e)
    return [(v.x, v.y) for v in p.flattening(tol)]


def load_dxf(path, settings):
    """Read a DXF and return (list_of_Parts, info_text)."""
    doc = ezdxf.readfile(path)
    units = doc.header.get("$INSUNITS", 0)
    skipped = {}
    closed, opens = [], []
    jt, tol = settings.join_tol, settings.flatten_tol

    for e in _iter_geometry(doc.modelspace(), skipped):
        try:
            pts = _flatten(e, tol)
        except Exception:
            skipped[e.dxftype() + "(unreadable)"] = skipped.get(e.dxftype() + "(unreadable)", 0) + 1
            continue
        if len(pts) < 2:
            continue
        if e.dxftype() == "CIRCLE" or _dist(pts[0], pts[-1]) <= jt:
            closed.append(([(e, False)], pts))
        else:
            opens.append((e, pts))

    # --- chain open entities end-to-end into closed loops
    used = [False] * len(opens)
    open_chains = 0
    for i in range(len(opens)):
        if used[i]:
            continue
        used[i] = True
        chain = [(opens[i][0], False)]
        pts = list(opens[i][1])
        start = pts[0]
        while _dist(pts[-1], start) > jt:
            cur = pts[-1]
            hit = None
            for j in range(len(opens)):
                if used[j]:
                    continue
                pj = opens[j][1]
                if _dist(pj[0], cur) <= jt:
                    hit = (j, False)
                    break
                if _dist(pj[-1], cur) <= jt:
                    hit = (j, True)
                    break
            if hit is None:
                break
            j, rev = hit
            used[j] = True
            pj = opens[j][1][::-1] if rev else opens[j][1]
            chain.append((opens[j][0], rev))
            pts.extend(pj[1:])
        if _dist(pts[-1], start) <= jt:
            closed.append((chain, pts))
        else:
            open_chains += 1

    # --- polygons
    loops = []
    for ents, pts in closed:
        if len(pts) < 3:
            continue
        poly = Polygon(pts)
        if not poly.is_valid:
            poly = poly.buffer(0)
            if poly.geom_type == "MultiPolygon":
                poly = max(poly.geoms, key=lambda g: g.area)
        if poly.is_empty or poly.area < 1e-6:
            continue
        loops.append(Loop(ents=ents, pts=pts, poly=poly))

    # --- remove duplicate loops (same contour drawn twice -> double cut)
    dups = 0
    uniq = []
    for lp in loops:
        b = lp.poly.bounds
        is_dup = False
        for u in uniq:
            ub = u.poly.bounds
            if (abs(u.poly.area - lp.poly.area) <= 1e-3 * max(1.0, u.poly.area)
                    and all(abs(p - q) <= 1e-3 for p, q in zip(b, ub))):
                is_dup = True
                break
        if is_dup:
            dups += 1
        else:
            uniq.append(lp)
    loops = uniq

    # --- nesting depth: parent = smallest loop that contains this one
    for i, lp in enumerate(loops):
        rp = lp.poly.representative_point()
        best = None
        for j, other in enumerate(loops):
            if i == j or other.poly.area <= lp.poly.area:
                continue
            if other.poly.contains(rp):
                if best is None or other.poly.area < loops[best].poly.area:
                    best = j
        lp.parent = -1 if best is None else best
    for lp in loops:
        d, p = 0, lp.parent
        while p != -1:
            d += 1
            p = loops[p].parent
        lp.depth = d
        if lp.parent != -1:
            loops[lp.parent].has_children = True

    # --- one Part per top-level loop
    base = os.path.splitext(os.path.basename(path))[0]
    tops = [i for i, lp in enumerate(loops) if lp.depth == 0]
    parts = []
    for k, ti in enumerate(sorted(tops, key=lambda i: -loops[i].poly.area)):
        inner = []
        for i, lp in enumerate(loops):
            p = lp.parent
            while p != -1 and p != ti:
                p = loops[p].parent
            if p == ti:
                inner.append((lp, "COMMON_LINE" if lp.has_children else "HOLES"))
        name = base if len(tops) == 1 else f"{base}#{k + 1}"
        parts.append(Part(name=name, file=path, outer=loops[ti], inner=inner,
                          units=units, doc=doc))

    info = [f"{os.path.basename(path)}: units={UNIT_NAMES.get(units, units)}, "
            f"{len(loops)} closed contour(s), {len(parts)} part(s)"]
    if dups:
        info.append(f"  removed {dups} duplicate contour(s) (would have been cut twice)")
    if open_chains:
        info.append(f"  WARNING: {open_chains} open contour(s) ignored - check the drawing")
    if skipped:
        info.append("  ignored: " + ", ".join(f"{k} x{v}" for k, v in skipped.items()))
    for p in parts:
        w, h = p.size
        nc = sum(1 for _, l in p.inner if l == "COMMON_LINE")
        nh = sum(1 for _, l in p.inner if l == "HOLES")
        info.append(f"  {p.name}: {w:.3f} x {h:.3f}, {nh} hole(s), {nc} shared-cutline part(s)")
    return parts, "\n".join(info)


# ============================================================================
#  Geometry prep: normalize each part (long axis along X, bbox at 0,0)
# ============================================================================
def xform_pts(M, pts):
    return [(v.x, v.y) for v in M.transform_vertices(Vec3(x, y, 0) for x, y in pts)]


@dataclass
class Prep:
    part: Part
    M0: Matrix44            # original -> normalized
    outer: Polygon          # normalized outline
    inner: list             # [(Loop, layer, normalized Polygon)]
    W: float
    H: float
    rotated0: bool


def prepare(part):
    x0, y0, x1, y1 = part.outer.poly.bounds
    rot = part.allow_rotate and (y1 - y0) > (x1 - x0) + EPS
    if rot:
        # rotate 90 deg CCW: (x, y) -> (-y, x); bbox becomes [-y1,-y0] x [x0,x1]
        M0 = Matrix44.chain(Matrix44.z_rotate(math.pi / 2), Matrix44.translate(y1, -x0, 0))
    else:
        M0 = Matrix44.translate(-x0, -y0, 0)
    outer = Polygon(xform_pts(M0, part.outer.pts))
    if not outer.is_valid:
        outer = outer.buffer(0)
    inner = []
    if not part.profile_only:
        for lp, layer in part.inner:
            inner.append((lp, layer, Polygon(xform_pts(M0, lp.pts)).buffer(0)))
    bx0, by0, bx1, by1 = outer.bounds
    return Prep(part, M0, outer, inner, bx1 - bx0, by1 - by0, rot)


# ============================================================================
#  Seam planning
# ============================================================================
def merge_intervals(iv):
    iv = sorted((s, e) for s, e in iv if e > s)
    out = []
    for s, e in iv:
        if out and s <= out[-1][1] + EPS:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def keepouts(prep, st, use_notches=True):
    iv = []
    for _, _, poly in prep.inner:
        a, _, b, _ = poly.bounds
        iv.append((a - st.seam_clear, b + st.seam_clear))
    if use_notches and st.avoid_notches:
        # grow the outline a hair first so float noise along straight edges
        # doesn't turn into long sliver "notches"
        grow = max(1e-3, 2 * st.join_tol)
        diff = prep.outer.convex_hull.difference(prep.outer.buffer(grow, join_style=2))
        geoms = getattr(diff, "geoms", [diff])
        for g in geoms:
            if not g.is_empty and g.area > 1e-3:
                a, _, b, _ = g.bounds
                iv.append((a - grow - st.seam_clear, b + grow + st.seam_clear))
    return merge_intervals(iv)


def _floor(x, step):
    return math.floor(x / step + 1e-9) * step if step > 0 else x


def _ceil(x, step):
    return math.ceil(x / step - 1e-9) * step if step > 0 else x


def _inside(x, iv):
    return any(s + EPS < x < e - EPS for s, e in iv)


def _latest_allowed(lo, hi, iv, step):
    x = _floor(hi, step)
    for s, e in reversed(iv):
        if s + EPS < x < e - EPS:
            x = _floor(s, step)
    return x if x >= lo - EPS and not _inside(x, iv) else None


def _greedy(L, m, iv, st):
    seams, pos = [], 0.0
    while L - pos > m + EPS:
        x = _latest_allowed(pos + st.min_seg, pos + m, iv, st.seam_step)
        if x is None or x <= pos + EPS:
            return None
        seams.append(x)
        pos = x
    if L - pos < st.min_seg - EPS:
        return None
    return seams


def _valid(seams, L, m_max, iv, st):
    edges = [0.0] + list(seams) + [L]
    for a, b in zip(edges, edges[1:]):
        if b - a > m_max + EPS or b - a < st.min_seg - EPS:
            return False
    return not any(_inside(s, iv) for s in seams)


def seam_candidates(prep, sheets, st, max_cands=24):
    """Return (list of seam lists in normalized X, note). [[]] means no split needed."""
    L, H = prep.W, prep.H
    rot = prep.part.allow_rotate
    fits = False
    maxlens = set()
    for W_s, L_s in sheets:
        uW, uL = W_s - 2 * st.border, L_s - 2 * st.border
        if H <= uW + EPS:
            maxlens.add(round(uL, 6))
            if L <= uL + EPS:
                fits = True
        if rot and H <= uL + EPS:
            maxlens.add(round(uW, 6))
            if L <= uW + EPS:
                fits = True
    if fits:
        return [[]], None
    if not maxlens:
        return None, f"{prep.part.name}: {H:.3f} is too wide for every enabled sheet"
    if not prep.part.allow_split:
        return None, f"{prep.part.name}: too long for every sheet and splitting is off"
    m_max = max(maxlens)

    note = None
    iv = keepouts(prep, st, True)
    cands = _gen_cands(L, maxlens, m_max, iv, st)
    if not cands and st.avoid_notches:
        iv = keepouts(prep, st, False)
        cands = _gen_cands(L, maxlens, m_max, iv, st)
        if cands:
            note = f"{prep.part.name}: no seam layout avoids the notches, seams may cross one"
    if not cands:
        return None, (f"{prep.part.name}: could not place seams clear of internal "
                      f"features (try a smaller seam clearance or bigger sheets)")

    # rank by a quick material estimate: each piece alone on its smallest sheet
    def est(seams):
        edges = [0.0] + list(seams) + [L]
        tot = 0.0
        for a, b in zip(edges, edges[1:]):
            ln = b - a
            best = None
            for W_s, L_s in sheets:
                uW, uL = W_s - 2 * st.border, L_s - 2 * st.border
                ok = (ln <= uL + EPS and H <= uW + EPS) or (rot and ln <= uW + EPS and H <= uL + EPS)
                if ok and (best is None or W_s * L_s < best):
                    best = W_s * L_s
            tot += best if best else 1e12
        return tot
    uniform = getattr(_gen_cands, "uniform", [])
    cm = CostModel(st, prep.part.units)

    def est_key(c):
        a = est(c)
        dollars = a * cm.usd_per_area + sum(seam_lengths(prep, c)) * cm.usd_per_len
        return (round(dollars, 2), a, len(c), c)
    cands.sort(key=est_key)
    # keep the best estimates plus all simple fill / balanced layouts (the
    # estimate can't see sheet sharing, so don't let it throw those away)
    keep = cands[:max_cands]
    for c in sorted(uniform, key=est_key):
        if c not in keep:
            keep.append(c)
    return keep, note


# ---- interval helpers for exact seam placement with mixed sheet lengths
def _iv_shift(R, a, b):
    return merge_intervals([(s + a, e + b) for s, e in R])


def _iv_and(R, A):
    out = []
    for s, e in R:
        for s2, e2 in A:
            lo, hi = max(s, s2), min(e, e2)
            if hi >= lo - EPS:
                out.append((lo, max(lo, hi)))
    return merge_intervals_closed(out)


def merge_intervals_closed(iv):
    iv = sorted(iv)
    out = []
    for s, e in iv:
        if out and s <= out[-1][1] + EPS:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _allowed(L, iv):
    out, pos = [], 0.0
    for s, e in iv:
        if s > pos:
            out.append((pos, min(s, L)))
        pos = max(pos, e)
    if pos < L:
        out.append((pos, L))
    return out


def _pick(R, target, step):
    """Point in interval set R nearest target, on the rounding step if possible."""
    best = None
    for s, e in R:
        lo, hi = (_ceil(s, step), _floor(e, step)) if step > 0 else (s, e)
        if hi < lo - EPS:
            continue
        x = min(max(target, lo), hi)
        if step > 0:
            x = min(max(round(x / step) * step, lo), hi)
        if best is None or abs(x - target) < abs(best - target):
            best = x
    return best


def _fit_caps(L, caps, allowed, st):
    """Seams for pieces with length limits caps[0..n-1] in order, or None."""
    n = len(caps)
    R = [[(0.0, 0.0)]]
    for k in range(n - 1):
        nxt = _iv_and(_iv_shift(R[-1], st.min_seg, caps[k]), allowed)
        if not nxt:
            return None
        R.append(nxt)
    last = _iv_and(_iv_shift(R[-1], st.min_seg, caps[-1]), [(L, L)])
    if not last:
        return None
    # backward feasibility
    B = [None] * n
    B[n - 1] = [(L, L)]
    for k in range(n - 1, 0, -1):
        B[k - 1] = _iv_and(_iv_shift(B[k], -caps[k], -st.min_seg), R[k])
        if not B[k - 1]:
            return None
    # forward pick, spreading slack in proportion to each piece's limit
    seams, prev, tot = [], 0.0, sum(caps)
    for k in range(n - 1):
        target = L * sum(caps[:k + 1]) / tot
        F = _iv_and(B[k], [(prev + st.min_seg, prev + caps[k])])
        x = _pick(F, target, st.seam_step)
        if x is None:
            x = _pick(F, target, 0)
            if x is None:
                return None
        seams.append(x)
        prev = x
    return seams


def _gen_cands(L, maxlens, m_max, iv, st):
    out = set()
    mirror = [(L - e, L - s) for s, e in reversed(iv)]
    for m in sorted(maxlens, reverse=True):
        if m < st.min_seg:
            continue
        g = _greedy(L, m, iv, st)
        if g and _valid(g, L, m_max, iv, st):
            out.add(tuple(round(s, 6) for s in g))
        g = _greedy(L, m, mirror, st)
        if g:
            g = sorted(L - s for s in g)
            if _valid(g, L, m_max, iv, st):
                out.add(tuple(round(s, 6) for s in g))
        n0 = math.ceil(L / m - 1e-9)
        for n in (n0, n0 + 1):
            if n < 2:
                continue
            seams = []
            for k in range(1, n):
                t = k * L / n
                x = round(t / st.seam_step) * st.seam_step if st.seam_step > 0 else t
                for s, e in iv:
                    if s + EPS < x < e - EPS:
                        x = _floor(s, st.seam_step) if (x - s) < (e - x) else _ceil(e, st.seam_step)
                seams.append(x)
            if seams == sorted(seams) and _valid(seams, L, m_max, iv, st):
                out.add(tuple(round(s, 6) for s in seams))
    uniform = set(out)
    # mixed sheet lengths: every multiset of piece limits for the fewest pieces
    # (and one more), in every distinct order
    allowed = _allowed(L, iv)
    caps_all = sorted(maxlens, reverse=True)
    n_min = math.ceil(L / m_max - 1e-9)
    tried = 0
    for n in (n_min, n_min + 1):
        for ms in itertools.combinations_with_replacement(caps_all, n):
            if sum(ms) < L - EPS:
                continue
            for order in set(itertools.permutations(ms)):
                tried += 1
                if tried > 4000:
                    break
                sm = _fit_caps(L, list(order), allowed, st)
                if sm and _valid(sm, L, m_max, iv, st):
                    out.add(tuple(round(x, 6) for x in sm))
    _gen_cands.uniform = [list(c) for c in uniform]
    return [list(c) for c in out]


# ============================================================================
#  Items (what actually gets packed)
# ============================================================================
@dataclass
class Item:
    name: str
    prep: Prep
    poly: Polygon        # outline in part-normalized coords
    inner: list          # [(Loop, layer)]
    minx: float
    miny: float
    w: float
    h: float
    split: bool
    seg: tuple = None    # (a, b) normalized x-range when split


def build_items(prep, seams, instance_tag=""):
    if not seams:
        return [Item(prep.part.name + instance_tag, prep, prep.outer,
                     [(lp, ly) for lp, ly, _ in prep.inner],
                     0.0, 0.0, prep.W, prep.H, False)]
    items = []
    edges = [0.0] + list(seams) + [prep.W]
    n = 0
    for a, b in zip(edges, edges[1:]):
        clip = prep.outer.intersection(box(a, -1, b, prep.H + 1))
        geoms = [g for g in getattr(clip, "geoms", [clip]) if g.geom_type == "Polygon" and g.area > 1e-6]
        for g in geoms:
            n += 1
            g = orient(g)
            x0, y0, x1, y1 = g.bounds
            inner = []
            for lp, ly, ip in prep.inner:
                ix0, _, ix1, _ = ip.bounds
                if ix0 >= a - EPS and ix1 <= b + EPS and g.buffer(EPS).contains(ip.representative_point()):
                    inner.append((lp, ly))
            items.append(Item(f"{prep.part.name}{instance_tag}-S{n}", prep, g, inner,
                              x0, y0, x1 - x0, y1 - y0, True, (a, b)))
    return items


# ============================================================================
#  MaxRects packer
# ============================================================================
class MaxRects:
    def __init__(self, W, H):
        self.W, self.H = W, H
        self.free = [(0.0, 0.0, W, H)]

    def find(self, w, h, rot, heur):
        best, best_score = None, None
        opts = [(w, h, False)] + ([(h, w, True)] if rot and abs(w - h) > EPS else [])
        for fx, fy, fw, fh in self.free:
            for ww, hh, r in opts:
                if ww <= fw + EPS and hh <= fh + EPS:
                    dx, dy = fw - ww, fh - hh
                    if heur == "BL":
                        sc = (round(fy, 6), round(fx, 6))
                    elif heur == "BSSF":
                        sc = (min(dx, dy), max(dx, dy))
                    else:  # BAF
                        sc = (fw * fh - ww * hh, min(dx, dy))
                    if best_score is None or sc < best_score:
                        best_score, best = sc, (fx, fy, ww, hh, r)
        return best

    def place(self, x, y, w, h):
        new = []
        for fx, fy, fw, fh in self.free:
            if x >= fx + fw - EPS or x + w <= fx + EPS or y >= fy + fh - EPS or y + h <= fy + EPS:
                new.append((fx, fy, fw, fh))
                continue
            if x > fx + EPS:
                new.append((fx, fy, x - fx, fh))
            if x + w < fx + fw - EPS:
                new.append((x + w, fy, fx + fw - x - w, fh))
            if y > fy + EPS:
                new.append((fx, fy, fw, y - fy))
            if y + h < fy + fh - EPS:
                new.append((fx, y + h, fw, fy + fh - y - h))
        pruned = []
        for i, a in enumerate(new):
            contained = False
            for j, b in enumerate(new):
                if i != j and a[0] >= b[0] - EPS and a[1] >= b[1] - EPS and \
                        a[0] + a[2] <= b[0] + b[2] + EPS and a[1] + a[3] <= b[1] + b[3] + EPS:
                    if a != b or i > j:
                        contained = True
                        break
            if not contained:
                pruned.append(a)
        self.free = pruned


class Bin:
    def __init__(self, sheet, st):
        self.sheet = sheet                      # (W, L): L along X
        W, L = sheet
        g = st.spacing
        self.mr = MaxRects(L - 2 * st.border + g, W - 2 * st.border + g)
        self.placed = []                         # (item, x, y, rotated)
        self.st = st

    def try_add(self, it, heur):
        g = self.st.spacing
        r = self.mr.find(it.w + g, it.h + g, it.prep.part.allow_rotate, heur)
        if r is None:
            return False
        x, y, ww, hh, rot = r
        self.mr.place(x, y, ww, hh)
        self.placed.append((it, x, y, rot))
        return True


def _fits_sheet(it, sheet, st):
    W, L = sheet
    uW, uL = W - 2 * st.border, L - 2 * st.border
    if it.w <= uL + EPS and it.h <= uW + EPS:
        return True
    return it.prep.part.allow_rotate and it.h <= uL + EPS and it.w <= uW + EPS


def _pack_into(items, sheet, st):
    for heur in ("BL", "BSSF", "BAF"):
        b = Bin(sheet, st)
        if all(b.try_add(it, heur) for it in sorted(items, key=lambda i: -i.w * i.h)):
            return b
    return None


def pack(items, sheets, st, heur):
    by_area_desc = sorted(sheets, key=lambda s: -s[0] * s[1])
    bins = []
    for it in items:
        if any(b.try_add(it, heur) for b in bins):
            continue
        for s in by_area_desc:
            if _fits_sheet(it, s, st):
                b = Bin(s, st)
                if b.try_add(it, heur):
                    bins.append(b)
                    break
        else:
            return None
    # shrink each sheet to the smallest one that still holds its parts
    by_area_asc = sorted(sheets, key=lambda s: s[0] * s[1])
    out = []
    for b in bins:
        cur = b
        for s in by_area_asc:
            if s[0] * s[1] >= cur.sheet[0] * cur.sheet[1] - EPS:
                break
            if all(_fits_sheet(p[0], s, st) for p in b.placed):
                nb = _pack_into([p[0] for p in b.placed], s, st)
                if nb:
                    cur = nb
                    break
        out.append(cur)
    return out


# ============================================================================
#  Cost model
# ============================================================================
MATERIALS = [("Steel", 0.2836), ("Stainless 304", 0.289), ("Aluminum 6061", 0.0975)]
UNIT_TO_IN = {0: 1.0, 1: 1.0, 2: 12.0, 4: 1 / 25.4, 5: 1 / 2.54, 6: 1000 / 25.4}


class CostModel:
    """Converts drawing geometry to pounds and dollars.

    Sheet material is charged for the whole sheet. Weld is charged per seam by
    the seam's actual length across the part. Remnants (full-width strip past
    the last part, and the strip above the parts) are credited back at
    remnant_credit % when their narrow side is at least min_remnant.
    """

    def __init__(self, st, units):
        self.st = st
        self.f = UNIT_TO_IN.get(units, 1.0)                     # drawing unit -> inch
        self.lb_per_area = st.thickness * st.density * self.f ** 2
        self.usd_per_area = self.lb_per_area * st.material_cost_lb
        self.usd_per_len = st.weld_cost_ft / 12.0 * self.f
        self.priced = self.usd_per_area > 0 or self.usd_per_len > 0

    def remnants(self, b):
        """[(length, width), ...] usable remnant strips on a bin, drawing units."""
        st = self.st
        W, L = b.sheet
        right = top = 0.0
        for it, x, y, r in b.placed:
            right = max(right, st.border + x + (it.h if r else it.w))
            top = max(top, st.border + y + (it.w if r else it.h))
        out = []
        end_len = L - right - st.spacing
        if end_len * self.f >= st.min_remnant - EPS:
            out.append((end_len, W))
        top_w = W - top - st.spacing
        if top_w * self.f >= st.min_remnant - EPS and right > 0:
            out.append((right, top_w))
        return out

    def evaluate(self, bins, seam_len):
        area = sum(b.sheet[0] * b.sheet[1] for b in bins)
        rem_area = sum(l * w for b in bins for l, w in self.remnants(b))
        material = area * self.usd_per_area
        credit = rem_area * self.usd_per_area * self.st.remnant_credit / 100.0
        weld = seam_len * self.usd_per_len
        return {"area": area, "rem_area": rem_area, "material": material, "credit": credit,
                "weld": weld, "total": material - credit + weld,
                "sheet_lb": area * self.lb_per_area, "rem_lb": rem_area * self.lb_per_area,
                "weld_ft": seam_len * self.f / 12.0}


def seam_lengths(prep, seams):
    """Length of each seam line across the part outline (drawing units)."""
    from shapely.geometry import LineString
    return [LineString([(s, -1), (s, prep.H + 1)]).intersection(prep.outer).length for s in seams]


def net_part_area(prep):
    holes = sum(p.area for _, ly, p in prep.inner if ly == "HOLES")
    return prep.outer.area - holes


# ============================================================================
#  Optimizer
# ============================================================================
@dataclass
class Result:
    bins: list
    preps: list
    seams: dict              # part name -> list of normalized seams
    notes: list
    sheet_area: float
    part_area: float
    n_seams: int
    attempts: int
    seconds: float
    cost: dict = None            # CostModel.evaluate() of the chosen layout
    seam_len: dict = None        # part name -> [seam lengths]
    tradeoff: list = None        # best layout found at each seam count
    net_area: float = 0.0
    model: object = None


def optimize(parts, sheets, st, progress=None, cancel=None):
    t0 = time.time()
    sheets = [(min(a, b), max(a, b)) for a, b in sheets]
    notes = []
    preps, cand_lists = [], []
    for p in parts:
        if p.qty < 1:
            continue
        pr = prepare(p)
        cands, note = seam_candidates(pr, sheets, st)
        if cands is None:
            raise ValueError(note)
        if note:
            notes.append(note)
        preps.append(pr)
        cand_lists.append(cands)
    if not preps:
        raise ValueError("No parts to nest.")

    # pre-build items for every (part, seam layout)
    built = []
    for pr, cands in zip(preps, cand_lists):
        per = []
        for c in cands:
            its = []
            for q in range(pr.part.qty):
                tag = f"_{q + 1}" if pr.part.qty > 1 else ""
                its.extend(build_items(pr, c, tag))
            per.append(its)
        built.append(per)

    cm = CostModel(st, preps[0].part.units)
    cand_seam_len = [[sum(seam_lengths(pr, c)) * pr.part.qty for c in cands]
                     for pr, cands in zip(preps, cand_lists)]
    by_seams = {}

    rng = random.Random(1)
    orders = [lambda i: -i.w * i.h, lambda i: -i.w, lambda i: -i.h, lambda i: -max(i.w, i.h)]
    heurs = ["BL", "BSSF", "BAF"]
    cache = {}
    state = {"best": None, "key": None, "attempts": 0}
    budget = max(200, st.effort * 4)

    def n_seams_of(combo):
        return sum(len(cand_lists[k][idx]) * preps[k].part.qty for k, idx in enumerate(combo))

    def run(combo, tries, randomize=False):
        items = [it for k, idx in enumerate(combo) for it in built[k][idx]]
        ns = n_seams_of(combo)
        sl = sum(cand_seam_len[k][idx] for k, idx in enumerate(combo))
        best_k = None
        for t in range(tries):
            if cancel is not None and cancel.is_set():
                break
            if not randomize and t < len(orders) * len(heurs):
                seq = sorted(items, key=orders[t % len(orders)])
                heur = heurs[(t // len(orders)) % len(heurs)]
            else:
                seq = items[:]
                rng.shuffle(seq)
                seq.sort(key=lambda i: -(i.w * i.h) * rng.uniform(0.7, 1.3))
                heur = rng.choice(heurs)
            bins = pack(seq, sheets, st, heur)
            state["attempts"] += 1
            if bins is None:
                continue
            ev = cm.evaluate(bins, sl)
            area = ev["area"]
            # dollars first; with no prices entered this falls through to sheet area
            key = (round(ev["total"], 2), round(area, 4), ns, len(bins))
            if best_k is None or key < best_k:
                best_k = key
            if state["key"] is None or key < state["key"]:
                state["key"], state["best"] = key, (bins, combo, ns, area)
            if ns not in by_seams or key < by_seams[ns][0]:
                by_seams[ns] = (key, len(bins), ev)
            if progress and state["attempts"] % 20 == 0:
                progress(min(1.0, state["attempts"] / budget), state["key"])
        prev = cache.get(combo)
        if best_k is not None and (prev is None or best_k < prev):
            cache[combo] = best_k
        return best_k

    # 1) coordinate descent over seam layouts (one part at a time)
    quick = 6
    starts = [tuple(0 for _ in cand_lists),
              tuple(min(range(len(c)), key=lambda i: (len(c[i]), i)) for c in cand_lists)]
    for start in starts:
        cur = start
        run(cur, quick)
        for _round in range(3):
            changed = False
            for k in range(len(cand_lists)):
                for idx in range(len(cand_lists[k])):
                    if state["attempts"] > budget * 0.6:
                        break
                    c2 = cur[:k] + (idx,) + cur[k + 1:]
                    if c2 not in cache:
                        run(c2, quick)
                    if c2 in cache and (cur not in cache or cache[c2] < cache[cur]):
                        cur, changed = c2, True
            if not changed:
                break
    # 2) spend the rest of the effort on the most promising combinations
    top = sorted(cache, key=lambda c: cache[c])[:8]
    left = max(0, budget - state["attempts"])
    for c in top:
        if cancel is not None and cancel.is_set():
            break
        run(c, max(12, left // max(1, len(top))), randomize=True)

    best, attempts = state["best"], state["attempts"]
    if best is None:
        raise ValueError("No layout found: some part does not fit any enabled sheet.")

    bins, combo, n_seams, area = best
    bins.sort(key=lambda b: (-b.sheet[1], -b.sheet[0], b.placed[0][0].name))
    for b in bins:
        b.placed.sort(key=lambda p: (p[1], p[2]))
    seams = {preps[k].part.name: cand_lists[k][idx] for k, idx in enumerate(combo) if cand_lists[k][idx]}
    part_area = sum(pr.outer.area * pr.part.qty for pr in preps)
    net = sum(net_part_area(pr) * pr.part.qty for pr in preps)
    slens = {preps[k].part.name: seam_lengths(preps[k], cand_lists[k][idx])
             for k, idx in enumerate(combo) if cand_lists[k][idx]}
    total_sl = sum(sum(v) * preps[k].part.qty for k, v in
                   ((k, slens.get(preps[k].part.name, [])) for k in range(len(preps))))
    cost = cm.evaluate(bins, total_sl)
    tradeoff = [(ns, nb, ev) for ns, (key, nb, ev) in sorted(by_seams.items())]
    # only keep seam counts that are an improvement on something with fewer seams
    pareto, best_area = [], None
    for ns, nb, ev in tradeoff:
        if best_area is None or ev["area"] < best_area - EPS:
            pareto.append((ns, nb, ev))
            best_area = ev["area"]
    return Result(bins, preps, seams, notes, area, part_area, n_seams, attempts, time.time() - t0,
                  cost=cost, seam_len=slens, tradeoff=pareto, net_area=net, model=cm)


# ============================================================================
#  Placement geometry + DXF output
# ============================================================================
def placement_matrix(item, x, y, rot, st):
    """Part-normalized coords -> sheet coords."""
    S = Matrix44.translate(-item.minx, -item.miny, 0)
    if rot:
        R = Matrix44.chain(Matrix44.z_rotate(math.pi / 2), Matrix44.translate(item.h, 0, 0))
    else:
        R = Matrix44()
    P = Matrix44.translate(st.border + x, st.border + y, 0)
    return Matrix44.chain(S, R, P)


def placed_geometry(bin_, st):
    """[(layer, [(x,y),...]), ...] for drawing previews."""
    out = []
    for it, x, y, rot in bin_.placed:
        T = placement_matrix(it, x, y, rot, st)
        if it.split:
            out.append(("OUTSIDE", xform_pts(T, list(it.poly.exterior.coords))))
        else:
            M = Matrix44.chain(it.prep.M0, T)
            out.append(("OUTSIDE", xform_pts(M, it.prep.part.outer.pts)))
        M = Matrix44.chain(it.prep.M0, T)
        for lp, layer in it.inner:
            out.append((layer, xform_pts(M, lp.pts)))
    return out


def _add_transformed(msp, e, M, layer, rev, tol):
    """Copy a source entity into msp, transformed, keeping true arcs where possible."""
    t = e.dxftype()
    c = e.copy()
    c.transform(M)
    a = {"layer": layer}
    ext = c.dxf.get("extrusion", Vec3(0, 0, 1)) if c.dxf.hasattr("extrusion") else Vec3(0, 0, 1)
    flat_ok = Vec3(ext).isclose(Vec3(0, 0, 1), abs_tol=1e-9)
    if t == "LINE":
        msp.add_line(c.dxf.start, c.dxf.end, dxfattribs=a)
    elif t == "CIRCLE" and flat_ok:
        msp.add_circle(c.dxf.center, c.dxf.radius, dxfattribs=a)
    elif t == "ARC" and flat_ok:
        msp.add_arc(c.dxf.center, c.dxf.radius, c.dxf.start_angle, c.dxf.end_angle, dxfattribs=a)
    elif t == "LWPOLYLINE" and flat_ok:
        msp.add_lwpolyline(list(c.get_points("xyseb")), format="xyseb", close=c.closed, dxfattribs=a)
    else:
        pts = _flatten(c, tol)
        closed = _dist(pts[0], pts[-1]) < 1e-9
        msp.add_lwpolyline(pts[:-1] if closed else pts, close=closed, dxfattribs=a)


def _new_doc(units):
    doc = ezdxf.new("R2010")
    doc.header["$INSUNITS"] = units
    for name, col in LAYERS.items():
        doc.layers.add(name, color=col)
    return doc


def _safe(s):
    return "".join(ch if ch.isalnum() or ch in "-_#+" else "_" for ch in s)


def sheet_filename(i, b):
    if len(b.placed) <= 3:
        names = "+".join(p[0].name for p in b.placed)
    else:
        names = f"{len(b.placed)}parts"
    W, L = b.sheet
    return f"Sheet{i:02d}_{W:g}x{L:g}_{_safe(names)}.dxf"


def write_outputs(res, st, folder):
    os.makedirs(folder, exist_ok=True)
    units = res.preps[0].part.units
    written = []
    for i, b in enumerate(res.bins, 1):
        doc = _new_doc(units)
        msp = doc.modelspace()
        W, L = b.sheet
        msp.add_lwpolyline([(0, 0), (L, 0), (L, W), (0, W)], close=True,
                           dxfattribs={"layer": "SHEET_REF_NOCUT"})
        for it, x, y, rot in b.placed:
            T = placement_matrix(it, x, y, rot, st)
            M = Matrix44.chain(it.prep.M0, T)
            if it.split:
                pts = xform_pts(T, list(it.poly.exterior.coords)[:-1])
                msp.add_lwpolyline(pts, close=True, dxfattribs={"layer": "OUTSIDE"})
            else:
                for e, rev in it.prep.part.outer.ents:
                    _add_transformed(msp, e, M, "OUTSIDE", rev, st.flatten_tol)
            for lp, layer in it.inner:
                for e, rev in lp.ents:
                    _add_transformed(msp, e, M, layer, rev, st.flatten_tol)
        fn = os.path.join(folder, sheet_filename(i, b))
        doc.saveas(fn)
        written.append(fn)

    # seam maps (original coordinates) for each split part
    for pr in res.preps:
        seams = res.seams.get(pr.part.name)
        if not seams:
            continue
        doc = _new_doc(pr.part.units)
        msp = doc.modelspace()
        ident = Matrix44()
        for e, rev in pr.part.outer.ents:
            _add_transformed(msp, e, ident, "OUTSIDE", rev, st.flatten_tol)
        for lp, layer, _ in pr.inner:
            for e, rev in lp.ents:
                _add_transformed(msp, e, ident, layer, rev, st.flatten_tol)
        inv = Matrix44(pr.M0)
        inv.inverse()
        for s in seams:
            p0, p1 = xform_pts(inv, [(s, -3), (s, pr.H + 3)])
            msp.add_line(p0, p1, dxfattribs={"layer": "SEAM_REF"})
            msp.add_text(seam_label(pr, s), height=max(0.5, pr.H / 40),
                         dxfattribs={"layer": "SEAM_REF"}).set_placement(p1)
        fn = os.path.join(folder, f"SeamMap_{_safe(pr.part.name)}.dxf")
        doc.saveas(fn)
        written.append(fn)

    fn = os.path.join(folder, "Nest_Report.txt")
    with open(fn, "w") as f:
        f.write(report_text(res, st))
    written.append(fn)
    return written


def seam_label(pr, s):
    inv = Matrix44(pr.M0)
    inv.inverse()
    (x0, y0), (x1, y1) = xform_pts(inv, [(s, 0), (s, pr.H)])
    if abs(x0 - x1) < 1e-6:
        return f"SEAM X={x0:.4f}"
    return f"SEAM Y={y0:.4f}"


def _usd(x):
    return f"${x:,.2f}"


def report_text(res, st):
    u = UNIT_NAMES.get(res.preps[0].part.units, "")
    cm, c = res.model, res.cost
    L = [f"{APP_NAME} report  ({time.strftime('%m/%d/%Y %I:%M %p')})",
         "DRAFT - verify every sheet in CAM before cutting.", "",
         f"Edge border {st.border}  |  part spacing {st.spacing}  |  seam clearance {st.seam_clear}"
         f"  |  min segment {st.min_seg}  (drawing units: {u})",
         f"Plate {st.thickness} in @ {st.density} lb/in^3  |  material {_usd(st.material_cost_lb)}/lb"
         f"  |  weld {_usd(st.weld_cost_ft)}/ft  |  remnant credit {st.remnant_credit:g}%"
         f" (strips >= {st.min_remnant:g} in)", ""]
    if not cm.priced:
        L.append("NOTE: no prices entered - layouts were scored by total sheet area.")
    counts = {}
    for b in res.bins:
        counts[b.sheet] = counts.get(b.sheet, 0) + 1
    L.append(f"Sheets used: {len(res.bins)}   (" +
             ", ".join(f"{n} x {W:g}x{Lg:g}" for (W, Lg), n in sorted(counts.items())) + ")")
    L.append(f"Seams: {res.n_seams}   ({res.attempts} layouts tried in {res.seconds:.1f} s)")
    L.append("")
    part_lb = res.net_area * cm.lb_per_area
    L.append("COST SUMMARY")
    L.append(f"  Plate bought      {c['sheet_lb']:>10,.1f} lb   {_usd(c['material']):>12}")
    L.append(f"  Parts (net)       {part_lb:>10,.1f} lb   "
             f"({100 * res.net_area / max(c['area'], EPS):.1f}% of plate)")
    if c["rem_area"] > 0:
        L.append(f"  Usable remnants   {c['rem_lb']:>10,.1f} lb   -{_usd(c['credit']):>11}"
                 f"   (credited at {st.remnant_credit:g}%)")
    scrap = c["sheet_lb"] - part_lb - c["rem_lb"]
    L.append(f"  Scrap / skeleton  {scrap:>10,.1f} lb")
    L.append(f"  Welding           {c['weld_ft']:>10,.2f} ft   {_usd(c['weld']):>12}")
    L.append(f"  TOTAL                            {_usd(c['total']):>12}")
    units = {pr.part.units for pr in res.preps}
    if len(units) > 1:
        L.append("WARNING: parts use different DXF units: " + ", ".join(UNIT_NAMES.get(x, str(x)) for x in units))
    for n in res.notes:
        L.append("NOTE: " + n)

    if res.tradeoff and len(res.tradeoff) > 1:
        L.append("")
        L.append("TRADE-OFF: best layout found at each seam count (fewer seams vs less plate)")
        L.append(f"  {'seams':>5} {'sheets':>6} {'plate sq in':>12} {'material':>11} {'weld':>10} "
                 f"{'credit':>9} {'total':>11}")
        for ns, nb, ev in res.tradeoff:
            mark = "  <- chosen" if ns == res.n_seams and abs(ev["area"] - c["area"]) < EPS else ""
            L.append(f"  {ns:>5} {nb:>6} {ev['area']:>12,.0f} {_usd(ev['material']):>11} "
                     f"{_usd(ev['weld']):>10} {_usd(ev['credit']):>9} {_usd(ev['total']):>11}{mark}")
        if len(res.tradeoff) >= 2:
            (n1, _, e1), (n2, _, e2) = res.tradeoff[0], res.tradeoff[1]
            d_area = e1["area"] - e2["area"]
            d_ft = (e2["weld_ft"] - e1["weld_ft"])
            if d_ft > EPS and d_area > EPS:
                lb = d_area * cm.lb_per_area
                L.append(f"  Break-even: going from {n1} to {n2} seams saves {lb:,.1f} lb of plate for "
                         f"{d_ft:,.2f} ft more weld -> worth it only if a foot of seam costs less "
                         f"than {lb / d_ft:,.2f} lb of plate")
                if st.material_cost_lb > 0:
                    L.append(f"              (at {_usd(st.material_cost_lb)}/lb that is "
                             f"{_usd(lb / d_ft * st.material_cost_lb)}/ft of weld)")
    L.append("")
    for i, b in enumerate(res.bins, 1):
        W, Ls = b.sheet
        maxx = max(st.border + x + (it.h if r else it.w) for it, x, y, r in b.placed)
        sheet_usd = W * Ls * cm.usd_per_area
        L.append(f"{sheet_filename(i, b)}")
        L.append(f"  sheet {W:g} x {Ls:g}  ({W * Ls * cm.lb_per_area:,.1f} lb, {_usd(sheet_usd)}),"
                 f" remnant past last part: {Ls - maxx:.3f}")
        for it, x, y, r in b.placed:
            L.append(f"    {it.name:<28} {it.w:9.3f} x {it.h:8.3f}  at ({st.border + x:.3f}, "
                     f"{st.border + y:.3f}){'  rotated 90' if r else ''}")
    if res.seams:
        L.append("")
        L.append("Seams (original drawing coordinates):")
        for pr in res.preps:
            s = res.seams.get(pr.part.name)
            if s:
                edges = [0.0] + s + [pr.W]
                lens = ", ".join(f"{b - a:.3f}" for a, b in zip(edges, edges[1:]))
                sl = res.seam_len[pr.part.name]
                L.append(f"  {pr.part.name}: " + ", ".join(seam_label(pr, x) for x in s))
                L.append(f"      segment lengths: {lens}")
                L.append("      weld length per seam: " + ", ".join(f"{x:.3f}" for x in sl) +
                         (f"   (x{pr.part.qty} parts)" if pr.part.qty > 1 else ""))
    return "\n".join(L) + "\n"


# ============================================================================
#  GUI
# ============================================================================
def run_gui(test_hook=None):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    class PartDialog(tk.Toplevel):
        def __init__(self, master, part):
            super().__init__(master)
            self.title("Part options")
            self.transient(master)
            self.resizable(False, False)
            self.part, self.ok = part, False
            self.v_name = tk.StringVar(value=part.name)
            self.v_qty = tk.IntVar(value=part.qty)
            self.v_prof = tk.BooleanVar(value=part.profile_only)
            self.v_rot = tk.BooleanVar(value=part.allow_rotate)
            self.v_split = tk.BooleanVar(value=part.allow_split)
            f = ttk.Frame(self, padding=10)
            f.pack(fill="both")
            ttk.Label(f, text="Name").grid(row=0, column=0, sticky="w")
            ttk.Entry(f, textvariable=self.v_name, width=30).grid(row=0, column=1, pady=2)
            ttk.Label(f, text="Quantity").grid(row=1, column=0, sticky="w")
            ttk.Spinbox(f, from_=0, to=999, textvariable=self.v_qty, width=6).grid(row=1, column=1, sticky="w")
            ttk.Checkbutton(f, text="Outside profile only (drop internal features)",
                            variable=self.v_prof).grid(row=2, column=0, columnspan=2, sticky="w")
            ttk.Checkbutton(f, text="Allow 90 deg rotation", variable=self.v_rot).grid(
                row=3, column=0, columnspan=2, sticky="w")
            ttk.Checkbutton(f, text="Allow splitting into segments", variable=self.v_split).grid(
                row=4, column=0, columnspan=2, sticky="w")
            bf = ttk.Frame(f)
            bf.grid(row=5, column=0, columnspan=2, pady=(8, 0))
            ttk.Button(bf, text="OK", command=self._ok).pack(side="left", padx=4)
            ttk.Button(bf, text="Cancel", command=self.destroy).pack(side="left", padx=4)
            self.grab_set()

        def _ok(self):
            try:
                q = int(self.v_qty.get())
            except Exception:
                messagebox.showerror(APP_NAME, "Quantity must be a whole number", parent=self)
                return
            p = self.part
            p.name = self.v_name.get().strip() or p.name
            p.qty = max(0, q)
            p.profile_only = self.v_prof.get()
            p.allow_rotate = self.v_rot.get()
            p.allow_split = self.v_split.get()
            self.ok = True
            self.destroy()

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title(APP_NAME)
            self.geometry("1280x820")
            self.minsize(1000, 650)
            self.parts = []
            self.sheets = []        # [[W, L, enabled]]
            self.result = None
            self.q = queue.Queue()
            self.cancel = threading.Event()
            self.st = Settings()
            self._load_config()
            self._build()
            self._refresh_sheets()
            self.protocol("WM_DELETE_WINDOW", self._close)

        # ---------- config ----------
        def _load_config(self):
            self.sheets = [[w, l, True] for w, l in DEFAULT_SHEETS]
            try:
                with open(CONFIG_PATH) as f:
                    cfg = json.load(f)
                self.sheets = [[float(a), float(b), bool(c)] for a, b, c in cfg.get("sheets", [])] or self.sheets
                for k, v in cfg.get("settings", {}).items():
                    if hasattr(self.st, k):
                        setattr(self.st, k, type(getattr(self.st, k))(v))
            except Exception:
                pass

        def _save_config(self):
            try:
                self._read_settings(quiet=True)
                with open(CONFIG_PATH, "w") as f:
                    json.dump({"sheets": self.sheets, "settings": self.st.__dict__}, f, indent=1)
            except Exception:
                pass

        def _close(self):
            self.cancel.set()
            self._save_config()
            self.destroy()

        # ---------- layout ----------
        def _build(self):
            pw = ttk.PanedWindow(self, orient="horizontal")
            pw.pack(fill="both", expand=True, padx=6, pady=6)
            left = ttk.Frame(pw, padding=4)
            right = ttk.Frame(pw, padding=4)
            pw.add(left, weight=0)
            pw.add(right, weight=1)

            # sheets
            sf = ttk.LabelFrame(left, text="Stock sheets (W x L)", padding=4)
            sf.pack(fill="both", expand=True)
            self.tv_sheets = ttk.Treeview(sf, columns=("on", "w", "l"), show="headings", height=9)
            for c, t, wd in (("on", "Use", 45), ("w", "Width", 70), ("l", "Length", 70)):
                self.tv_sheets.heading(c, text=t)
                self.tv_sheets.column(c, width=wd, anchor="center")
            self.tv_sheets.pack(fill="both", expand=True)
            self.tv_sheets.bind("<Double-1>", lambda e: self._toggle_sheet())
            r = ttk.Frame(sf)
            r.pack(fill="x", pady=3)
            self.e_w = ttk.Entry(r, width=6)
            self.e_l = ttk.Entry(r, width=6)
            self.e_w.pack(side="left")
            ttk.Label(r, text="x").pack(side="left")
            self.e_l.pack(side="left")
            ttk.Button(r, text="Add", command=self._add_sheet, width=5).pack(side="left", padx=2)
            r2 = ttk.Frame(sf)
            r2.pack(fill="x")
            ttk.Button(r2, text="On/Off", command=self._toggle_sheet).pack(side="left")
            ttk.Button(r2, text="Remove", command=self._del_sheet).pack(side="left", padx=2)
            ttk.Button(r2, text="Defaults", command=self._default_sheets).pack(side="left")

            # settings
            gf = ttk.LabelFrame(left, text="Settings", padding=4)
            gf.pack(fill="x", pady=(6, 0))
            self.vars = {}
            rows = [("border", "Edge border"), ("spacing", "Part spacing"),
                    ("seam_clear", "Seam clearance"), ("min_seg", "Min segment length"),
                    ("seam_step", "Seam rounding step"), ("flatten_tol", "Arc flatten tol."),
                    ("join_tol", "Endpoint join tol."), ("effort", "Search effort")]
            for i, (k, lbl) in enumerate(rows):
                ttk.Label(gf, text=lbl).grid(row=i, column=0, sticky="w")
                v = tk.StringVar(value=str(getattr(self.st, k)))
                ttk.Entry(gf, textvariable=v, width=9).grid(row=i, column=1, sticky="e", pady=1)
                self.vars[k] = v
            self.v_notch = tk.BooleanVar(value=self.st.avoid_notches)
            ttk.Checkbutton(gf, text="Keep seams out of notches", variable=self.v_notch).grid(
                row=len(rows), column=0, columnspan=2, sticky="w")

            cf_ = ttk.LabelFrame(left, text="Cost (scores layouts in $)", padding=4)
            cf_.pack(fill="x", pady=(6, 0))
            ttk.Label(cf_, text="Material").grid(row=0, column=0, sticky="w")
            self.v_mat = tk.StringVar()
            mats = [f"{n} ({d})" for n, d in MATERIALS] + ["Custom"]
            cb = ttk.Combobox(cf_, textvariable=self.v_mat, values=mats, width=17, state="readonly")
            cb.grid(row=0, column=1, sticky="e", pady=1)
            crow = [("thickness", "Thickness (in)"), ("density", "Density (lb/in^3)"),
                    ("material_cost_lb", "Material $/lb"), ("weld_cost_ft", "Weld $/ft"),
                    ("remnant_credit", "Remnant credit %"), ("min_remnant", "Min remnant (in)")]
            for i, (k, lbl) in enumerate(crow, 1):
                ttk.Label(cf_, text=lbl).grid(row=i, column=0, sticky="w")
                v = tk.StringVar(value=str(getattr(self.st, k)))
                ttk.Entry(cf_, textvariable=v, width=9).grid(row=i, column=1, sticky="e", pady=1)
                self.vars[k] = v

            def _mat_pick(_e=None):
                for n, d in MATERIALS:
                    if self.v_mat.get().startswith(n):
                        self.vars["density"].set(str(d))
            cb.bind("<<ComboboxSelected>>", _mat_pick)
            cur = next((f"{n} ({d})" for n, d in MATERIALS if abs(d - self.st.density) < 1e-6), "Custom")
            self.v_mat.set(cur)

            # parts
            pf = ttk.LabelFrame(right, text="Parts", padding=4)
            pf.pack(fill="x")
            cols = ("name", "qty", "size", "feat", "rot", "split", "file")
            self.tv_parts = ttk.Treeview(pf, columns=cols, show="headings", height=6)
            for c, t, wd in (("name", "Name", 170), ("qty", "Qty", 45), ("size", "Size", 150),
                             ("feat", "Features", 120), ("rot", "Rotate", 55), ("split", "Split", 50),
                             ("file", "File", 300)):
                self.tv_parts.heading(c, text=t)
                self.tv_parts.column(c, width=wd, anchor="w" if c in ("name", "file") else "center")
            self.tv_parts.pack(fill="x")
            self.tv_parts.bind("<Double-1>", lambda e: self._edit_part())
            br = ttk.Frame(pf)
            br.pack(fill="x", pady=3)
            ttk.Button(br, text="Add DXF...", command=self._add_dxf).pack(side="left")
            ttk.Button(br, text="Edit...", command=self._edit_part).pack(side="left", padx=2)
            ttk.Button(br, text="Duplicate", command=self._dup_part).pack(side="left")
            ttk.Button(br, text="Remove", command=self._del_part).pack(side="left", padx=2)
            self.b_run = ttk.Button(br, text="Optimize", command=self._run)
            self.b_run.pack(side="right")
            self.b_export = ttk.Button(br, text="Export DXFs...", command=self._export, state="disabled")
            self.b_export.pack(side="right", padx=4)

            pr = ttk.Frame(right)
            pr.pack(fill="x", pady=2)
            self.pbar = ttk.Progressbar(pr, mode="determinate", maximum=1.0, length=180)
            self.pbar.pack(side="left")
            self.v_status = tk.StringVar(value="Add DXF parts, then Optimize.")
            ttk.Label(pr, textvariable=self.v_status, anchor="w").pack(side="left", fill="x", expand=True, padx=6)

            nb = ttk.Notebook(right)
            nb.pack(fill="both", expand=True)
            cf = ttk.Frame(nb)
            nb.add(cf, text="Preview")
            self.canvas = tk.Canvas(cf, background="white", highlightthickness=0)
            sb = ttk.Scrollbar(cf, orient="vertical", command=self.canvas.yview)
            self.canvas.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            self.canvas.pack(side="left", fill="both", expand=True)
            self.canvas.bind("<Configure>", lambda e: self._draw())
            self.canvas.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))
            self.canvas.bind_all("<Button-4>", lambda e: self.canvas.yview_scroll(-3, "units"))
            self.canvas.bind_all("<Button-5>", lambda e: self.canvas.yview_scroll(3, "units"))
            tf = ttk.Frame(nb)
            nb.add(tf, text="Report / Log")
            self.txt = tk.Text(tf, wrap="none", font=("Courier", 10))
            ts = ttk.Scrollbar(tf, orient="vertical", command=self.txt.yview)
            self.txt.configure(yscrollcommand=ts.set)
            ts.pack(side="right", fill="y")
            self.txt.pack(fill="both", expand=True)
            self.nb = nb

        # ---------- sheets ----------
        def _refresh_sheets(self):
            self.tv_sheets.delete(*self.tv_sheets.get_children())
            self.sheets.sort(key=lambda s: (s[1], s[0]))
            for i, (w, l, on) in enumerate(self.sheets):
                self.tv_sheets.insert("", "end", iid=str(i), values=("Yes" if on else "-", f"{w:g}", f"{l:g}"))

        def _add_sheet(self):
            try:
                w, l = float(self.e_w.get()), float(self.e_l.get())
                assert w > 0 and l > 0
            except Exception:
                messagebox.showerror(APP_NAME, "Enter sheet width and length as numbers")
                return
            w, l = min(w, l), max(w, l)
            if not any(abs(s[0] - w) < EPS and abs(s[1] - l) < EPS for s in self.sheets):
                self.sheets.append([w, l, True])
            self._refresh_sheets()

        def _sel_sheets(self):
            return [int(i) for i in self.tv_sheets.selection()]

        def _toggle_sheet(self):
            for i in self._sel_sheets():
                self.sheets[i][2] = not self.sheets[i][2]
            sel = self.tv_sheets.selection()
            self._refresh_sheets()
            self.tv_sheets.selection_set(sel)

        def _del_sheet(self):
            for i in sorted(self._sel_sheets(), reverse=True):
                del self.sheets[i]
            self._refresh_sheets()

        def _default_sheets(self):
            self.sheets = [[w, l, True] for w, l in DEFAULT_SHEETS]
            self._refresh_sheets()

        # ---------- parts ----------
        def _refresh_parts(self):
            self.tv_parts.delete(*self.tv_parts.get_children())
            for i, p in enumerate(self.parts):
                w, h = p.size
                nf = len(p.inner)
                feat = "profile only" if p.profile_only else (f"{nf} feature(s)" if nf else "none")
                self.tv_parts.insert("", "end", iid=str(i), values=(
                    p.name, p.qty, f"{w:.3f} x {h:.3f}", feat,
                    "Yes" if p.allow_rotate else "No", "Yes" if p.allow_split else "No",
                    os.path.basename(p.file)))

        def _log(self, s):
            self.txt.insert("end", s + "\n")
            self.txt.see("end")

        def _add_dxf(self):
            self._read_settings(quiet=True)
            files = filedialog.askopenfilenames(title="Add DXF parts",
                                                filetypes=[("DXF files", "*.dxf"), ("All files", "*.*")])
            for fn in files:
                try:
                    parts, info = load_dxf(fn, self.st)
                    if not parts:
                        raise ValueError("no closed contours found")
                    self.parts.extend(parts)
                    self._log(info)
                except Exception as ex:
                    messagebox.showerror(APP_NAME, f"Could not read {os.path.basename(fn)}:\n{ex}")
            self._refresh_parts()

        def _sel_part(self):
            s = self.tv_parts.selection()
            return int(s[0]) if s else None

        def _edit_part(self):
            i = self._sel_part()
            if i is None:
                return
            d = PartDialog(self, self.parts[i])
            self.wait_window(d)
            self._refresh_parts()

        def _dup_part(self):
            i = self._sel_part()
            if i is None:
                return
            p = self.parts[i]
            q = Part(name=p.name + " (copy)", file=p.file, outer=p.outer, inner=p.inner,
                     units=p.units, qty=p.qty, profile_only=p.profile_only,
                     allow_rotate=p.allow_rotate, allow_split=p.allow_split, doc=p.doc)
            self.parts.insert(i + 1, q)
            self._refresh_parts()

        def _del_part(self):
            i = self._sel_part()
            if i is not None:
                del self.parts[i]
                self._refresh_parts()

        # ---------- run ----------
        def _read_settings(self, quiet=False):
            try:
                for k, v in self.vars.items():
                    cur = getattr(self.st, k)
                    setattr(self.st, k, type(cur)(float(v.get())) if isinstance(cur, int) else float(v.get()))
                self.st.avoid_notches = self.v_notch.get()
                if self.st.border < 0 or self.st.spacing < 0 or self.st.min_seg <= 0:
                    raise ValueError("border/spacing must be >= 0 and min segment > 0")
                return True
            except Exception as ex:
                if not quiet:
                    messagebox.showerror(APP_NAME, f"Bad setting: {ex}")
                return False

        def _run(self):
            if not self._read_settings():
                return
            names = [p.name for p in self.parts if p.qty > 0]
            if len(names) != len(set(names)):
                messagebox.showerror(APP_NAME, "Part names must be unique (use Edit to rename).")
                return
            sheets = [(w, l) for w, l, on in self.sheets if on]
            if not sheets or not names:
                messagebox.showerror(APP_NAME, "Need at least one enabled sheet and one part.")
                return
            self.cancel.clear()
            self.b_run.configure(state="disabled")
            self.b_export.configure(state="disabled")
            self.v_status.set("Optimizing...")
            self.pbar["value"] = 0
            st = Settings(**self.st.__dict__)
            parts = list(self.parts)

            def work():
                try:
                    res = optimize(parts, sheets, st,
                                   progress=lambda f, k: self.q.put(("prog", f, k)), cancel=self.cancel)
                    self.q.put(("done", res, st))
                except Exception as ex:
                    self.q.put(("err", f"{ex}", traceback.format_exc()))
            threading.Thread(target=work, daemon=True).start()
            self.after(100, self._poll)

        def _poll(self):
            try:
                while True:
                    msg = self.q.get_nowait()
                    if msg[0] == "prog":
                        self.pbar["value"] = msg[1]
                        tot, a, s, n = msg[2]
                        money = f"${tot:,.2f}, " if tot else ""
                        self.v_status.set(f"Best so far: {money}{n} sheets, {a:,.0f} sq, {s} seams")
                    elif msg[0] == "done":
                        self._finished(msg[1], msg[2])
                        return
                    elif msg[0] == "err":
                        self.b_run.configure(state="normal")
                        self.v_status.set("Failed.")
                        self._log(msg[2])
                        messagebox.showerror(APP_NAME, msg[1])
                        return
            except queue.Empty:
                pass
            self.after(100, self._poll)

        def _finished(self, res, st):
            self.result, self.res_st = res, st
            self.pbar["value"] = 1.0
            self.b_run.configure(state="normal")
            self.b_export.configure(state="normal")
            c = res.cost
            if res.model.priced:
                self.v_status.set(f"Total {_usd(c['total'])}  (plate {_usd(c['material'])}, weld "
                                  f"{_usd(c['weld'])}, credit {_usd(c['credit'])})  |  "
                                  f"{len(res.bins)} sheets, {res.n_seams} seams")
            else:
                self.v_status.set(f"{len(res.bins)} sheets, {c['area']:,.0f} sq, {res.n_seams} seams"
                                  f"  (enter prices to score in $)")
            self.txt.delete("1.0", "end")
            self.txt.insert("end", report_text(res, st))
            self._draw()

        def _export(self):
            if not self.result:
                return
            d = filedialog.askdirectory(title="Choose output folder")
            if not d:
                return
            try:
                files = write_outputs(self.result, self.res_st, d)
                messagebox.showinfo(APP_NAME, f"Wrote {len(files)} files to\n{d}")
            except Exception as ex:
                self._log(traceback.format_exc())
                messagebox.showerror(APP_NAME, f"Export failed: {ex}")

        # ---------- preview ----------
        def _draw(self):
            c = self.canvas
            c.delete("all")
            if not self.result:
                c.create_text(20, 20, anchor="nw", fill="gray40",
                              text="Preview appears here after Optimize.")
                return
            res, st = self.result, self.res_st
            cw = max(300, c.winfo_width())
            maxL = max(b.sheet[1] for b in res.bins)
            maxW = max(b.sheet[0] for b in res.bins)
            pad, gap = 16, 24
            # fit two sheets per row when the window is wide enough
            cols = 2 if cw > 900 else 1
            s = min((cw - pad * 2 - gap * (cols - 1)) / (maxL * cols), 230 / maxW)
            cell_w, cell_h = maxL * s + gap, maxW * s + 40
            colors = {"OUTSIDE": "black", "COMMON_LINE": "red", "HOLES": "green4"}
            y = 0
            for i, b in enumerate(res.bins, 1):
                W, L = b.sheet
                col, row = (i - 1) % cols, (i - 1) // cols
                x0 = pad + col * cell_w
                y0 = 8 + row * cell_h
                c.create_text(x0, y0, anchor="nw", font=("TkDefaultFont", 9, "bold"),
                              text=f"Sheet {i}:  {W:g} x {L:g}")
                c.create_text(x0, y0 + 14, anchor="nw", font=("TkDefaultFont", 8), fill="gray25",
                              text=", ".join(p[0].name for p in b.placed))
                top = y0 + 30 + W * s
                c.create_rectangle(x0, y0 + 30, x0 + L * s, top, outline="gray60", dash=(4, 3))
                for layer, pts in placed_geometry(b, st):
                    flat = []
                    for px, py in pts:
                        flat += [x0 + px * s, top - py * s]
                    if len(flat) >= 4:
                        c.create_line(*flat, fill=colors.get(layer, "blue"))
                y = max(y, top + 12)
            c.configure(scrollregion=(0, 0, cw, y))

    app = App()
    if test_hook:
        app.after(300, lambda: test_hook(app))
    app.mainloop()


if __name__ == "__main__":
    run_gui()
