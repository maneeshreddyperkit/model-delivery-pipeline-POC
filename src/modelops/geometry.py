"""Geometry primitives and mesh operations.

Written directly against NumPy rather than leaning on a library's
creation helpers, so the numbers reported by the optimise stage
(triangle counts, welded vertices, bounding boxes, volume drift) come
from operations this codebase actually controls and can explain.

Convention: right-handed Z-up, millimetres, triangles wound CCW.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np


# =====================================================================
# Mesh container
# =====================================================================

@dataclass
class Mesh:
    vertices: np.ndarray  # (N, 3) float64
    faces: np.ndarray     # (M, 3) int32

    def __post_init__(self) -> None:
        self.vertices = np.asarray(self.vertices, dtype=np.float64).reshape(-1, 3)
        self.faces = np.asarray(self.faces, dtype=np.int64).reshape(-1, 3)

    # -- basic properties --------------------------------------------------
    @property
    def triangle_count(self) -> int:
        return int(self.faces.shape[0])

    @property
    def vertex_count(self) -> int:
        return int(self.vertices.shape[0])

    def bounds(self) -> np.ndarray:
        """(2, 3) array of [min_xyz, max_xyz]."""
        if self.vertex_count == 0:
            return np.zeros((2, 3))
        return np.vstack([self.vertices.min(axis=0), self.vertices.max(axis=0)])

    def extents(self) -> np.ndarray:
        b = self.bounds()
        return b[1] - b[0]

    def diagonal(self) -> float:
        return float(np.linalg.norm(self.extents()))

    def signed_volume(self) -> float:
        """Divergence-theorem volume over the triangle soup.

        Used as a cheap 'did optimisation destroy the shape' signal. For a
        closed manifold it is the true volume; for open shells it is still
        a stable scalar that should barely move under vertex welding and
        should move only slightly under decimation.
        """
        if self.faces.shape[0] == 0:
            return 0.0
        tri = self.vertices[self.faces]              # (M, 3, 3)
        a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
        return float(np.abs(np.einsum("ij,ij->i", a, np.cross(b, c)).sum()) / 6.0)

    def area(self) -> float:
        if self.faces.shape[0] == 0:
            return 0.0
        tri = self.vertices[self.faces]
        cross = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
        return float(0.5 * np.linalg.norm(cross, axis=1).sum())

    # -- transforms --------------------------------------------------------
    def transformed(self, matrix: np.ndarray) -> "Mesh":
        m = np.asarray(matrix, dtype=np.float64).reshape(4, 4)
        homo = np.hstack([self.vertices, np.ones((self.vertex_count, 1))])
        moved = (homo @ m.T)[:, :3]
        return Mesh(moved, self.faces.copy())

    def copy(self) -> "Mesh":
        return Mesh(self.vertices.copy(), self.faces.copy())


def concat(meshes: list[Mesh]) -> Mesh:
    """Merge meshes into one, offsetting face indices."""
    real = [m for m in meshes if m.vertex_count and m.triangle_count]
    if not real:
        return Mesh(np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64))
    verts, faces, offset = [], [], 0
    for m in real:
        verts.append(m.vertices)
        faces.append(m.faces + offset)
        offset += m.vertex_count
    return Mesh(np.vstack(verts), np.vstack(faces))


# =====================================================================
# Mesh operations used by the optimise stage
# =====================================================================

def weld_vertices(mesh: Mesh, tolerance: float = 1e-5) -> tuple[Mesh, int]:
    """Merge coincident vertices; return (mesh, vertices_removed).

    Exports from plant/CAD tools routinely emit every triangle with its
    own three vertices. Welding is the single cheapest real win on
    delivery payload size and costs nothing visually.
    """
    if mesh.vertex_count == 0:
        return mesh.copy(), 0

    decimals = max(0, int(round(-math.log10(tolerance)))) if tolerance > 0 else 9
    keys = np.round(mesh.vertices, decimals)
    _, first_occurrence, inverse = np.unique(
        keys, axis=0, return_index=True, return_inverse=True
    )

    # Keep the original (unrounded) coordinate of the first vertex in each
    # cluster as the representative, then remap faces through `inverse`,
    # which already maps every old vertex to its unique-row position.
    new_vertices = mesh.vertices[first_occurrence]
    new_faces = inverse.reshape(-1)[mesh.faces]

    removed = mesh.vertex_count - new_vertices.shape[0]
    welded = Mesh(new_vertices, new_faces)
    return drop_degenerate_faces(welded), removed


def drop_degenerate_faces(mesh: Mesh) -> Mesh:
    """Remove triangles that collapsed to a line or point after welding."""
    if mesh.triangle_count == 0:
        return mesh
    f = mesh.faces
    keep = (f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 0] != f[:, 2])
    if keep.all():
        return mesh
    return Mesh(mesh.vertices, f[keep])


def drop_duplicate_faces(mesh: Mesh) -> tuple[Mesh, int]:
    """Remove triangles that reference the same vertex set twice.

    Federated plant models regularly contain the same part twice, either
    because two disciplines both modelled it or because an export ran
    over the same source range twice. After welding, those show up as
    literally identical triangles: invisible on screen, but paid for in
    payload size and in every downstream triangle count.

    Winding is ignored, so a shell and its inside-out twin also collapse.
    This runs after welding, never before - it relies on shared indices.
    """
    if mesh.triangle_count == 0:
        return mesh.copy(), 0
    keys = np.sort(mesh.faces, axis=1)
    _, first_occurrence = np.unique(keys, axis=0, return_index=True)
    if first_occurrence.size == mesh.triangle_count:
        return mesh.copy(), 0
    keep = np.sort(first_occurrence)  # preserve original face order
    dropped = mesh.triangle_count - keep.size
    return Mesh(mesh.vertices.copy(), mesh.faces[keep]), int(dropped)


def decimate(mesh: Mesh, target_ratio: float, *, min_faces: int = 200) -> Mesh:
    """Quadric edge-collapse decimation to a fraction of the triangle count.

    Small parts are left alone: decimating a 24-triangle valve body saves
    nothing and makes it look broken.
    """
    if target_ratio >= 1.0 or mesh.triangle_count <= min_faces:
        return mesh.copy()

    try:
        import fast_simplification
    except ImportError:
        return mesh.copy()

    reduction = max(0.0, min(0.95, 1.0 - target_ratio))
    try:
        verts, faces = fast_simplification.simplify(
            mesh.vertices.astype(np.float32),
            mesh.faces.astype(np.int32),
            reduction,
        )
    except Exception:
        # Degenerate input can defeat the simplifier; keeping the original
        # geometry is always safe, and the QA stage records the miss.
        return mesh.copy()

    if faces is None or len(faces) == 0:
        return mesh.copy()
    return Mesh(np.asarray(verts, dtype=np.float64), np.asarray(faces, dtype=np.int64))


def quantize(mesh: Mesh, decimals: int = 3) -> Mesh:
    """Snap coordinates to sub-millimetre precision.

    Plant models are metre-scale objects described in millimetres; storing
    17 significant digits of float noise costs bytes and buys nothing.
    """
    return Mesh(np.round(mesh.vertices, decimals), mesh.faces.copy())


# =====================================================================
# Transform helpers
# =====================================================================

def identity() -> np.ndarray:
    return np.eye(4)


def translation(x: float, y: float, z: float) -> np.ndarray:
    m = np.eye(4)
    m[:3, 3] = (x, y, z)
    return m


def rotation(axis: str, degrees: float) -> np.ndarray:
    t = math.radians(degrees)
    c, s = math.cos(t), math.sin(t)
    m = np.eye(4)
    if axis == "x":
        m[:3, :3] = [[1, 0, 0], [0, c, -s], [0, s, c]]
    elif axis == "y":
        m[:3, :3] = [[c, 0, s], [0, 1, 0], [-s, 0, c]]
    elif axis == "z":
        m[:3, :3] = [[c, -s, 0], [s, c, 0], [0, 0, 1]]
    else:
        raise ValueError(f"Unknown axis {axis!r}")
    return m


def compose(*matrices: np.ndarray) -> np.ndarray:
    out = np.eye(4)
    for m in matrices:
        out = out @ m
    return out


# =====================================================================
# Primitives (local space, centred at origin unless noted)
# =====================================================================

def box(dx: float, dy: float, dz: float) -> Mesh:
    hx, hy, hz = dx / 2.0, dy / 2.0, dz / 2.0
    v = np.array([
        [-hx, -hy, -hz], [hx, -hy, -hz], [hx, hy, -hz], [-hx, hy, -hz],
        [-hx, -hy,  hz], [hx, -hy,  hz], [hx, hy,  hz], [-hx, hy,  hz],
    ])
    f = np.array([
        [0, 2, 1], [0, 3, 2],   # bottom
        [4, 5, 6], [4, 6, 7],   # top
        [0, 1, 5], [0, 5, 4],   # -y
        [1, 2, 6], [1, 6, 5],   # +x
        [2, 3, 7], [2, 7, 6],   # +y
        [3, 0, 4], [3, 4, 7],   # -x
    ])
    return Mesh(v, f)


def cylinder(radius: float, height: float, sections: int = 24, *, capped: bool = True) -> Mesh:
    theta = np.linspace(0.0, 2.0 * np.pi, sections, endpoint=False)
    cs, sn = np.cos(theta), np.sin(theta)
    hz = height / 2.0

    lower = np.column_stack([radius * cs, radius * sn, np.full(sections, -hz)])
    upper = np.column_stack([radius * cs, radius * sn, np.full(sections, hz)])
    verts = [lower, upper]
    faces: list[list[int]] = []

    for i in range(sections):
        j = (i + 1) % sections
        a, b = i, j                       # lower ring
        c, d = sections + i, sections + j  # upper ring
        faces.append([a, b, d])
        faces.append([a, d, c])

    if capped:
        base_c = 2 * sections
        top_c = base_c + 1
        verts.append(np.array([[0.0, 0.0, -hz], [0.0, 0.0, hz]]))
        for i in range(sections):
            j = (i + 1) % sections
            faces.append([base_c, j, i])
            faces.append([top_c, sections + i, sections + j])

    return Mesh(np.vstack(verts), np.array(faces))


def cone(radius: float, height: float, sections: int = 24) -> Mesh:
    theta = np.linspace(0.0, 2.0 * np.pi, sections, endpoint=False)
    ring = np.column_stack([radius * np.cos(theta), radius * np.sin(theta),
                            np.full(sections, -height / 2.0)])
    apex = np.array([[0.0, 0.0, height / 2.0]])
    centre = np.array([[0.0, 0.0, -height / 2.0]])
    verts = np.vstack([ring, apex, centre])
    apex_i, centre_i = sections, sections + 1
    faces = []
    for i in range(sections):
        j = (i + 1) % sections
        faces.append([i, j, apex_i])
        faces.append([centre_i, j, i])
    return Mesh(verts, np.array(faces))


def uv_sphere(radius: float, segments: int = 20, rings: int = 12) -> Mesh:
    verts = [[0.0, 0.0, radius]]
    for r in range(1, rings):
        phi = math.pi * r / rings
        z = radius * math.cos(phi)
        rad = radius * math.sin(phi)
        for s in range(segments):
            t = 2.0 * math.pi * s / segments
            verts.append([rad * math.cos(t), rad * math.sin(t), z])
    verts.append([0.0, 0.0, -radius])
    bottom = len(verts) - 1

    faces = []
    for s in range(segments):
        faces.append([0, 1 + s, 1 + (s + 1) % segments])
    for r in range(rings - 2):
        base = 1 + r * segments
        nxt = base + segments
        for s in range(segments):
            s2 = (s + 1) % segments
            faces.append([base + s, nxt + s, nxt + s2])
            faces.append([base + s, nxt + s2, base + s2])
    last = 1 + (rings - 2) * segments
    for s in range(segments):
        faces.append([bottom, last + (s + 1) % segments, last + s])
    return Mesh(np.array(verts), np.array(faces))


def torus(major_radius: float, minor_radius: float,
          major_segments: int = 24, minor_segments: int = 12,
          sweep_degrees: float = 360.0) -> Mesh:
    """Full torus, or a swept segment - used for pipe elbows."""
    closed = abs(sweep_degrees - 360.0) < 1e-9
    n_major = major_segments if closed else major_segments + 1
    sweep = math.radians(sweep_degrees)

    verts = []
    for i in range(n_major):
        u = sweep * (i / major_segments if not closed else i / major_segments)
        cu, su = math.cos(u), math.sin(u)
        for j in range(minor_segments):
            v = 2.0 * math.pi * j / minor_segments
            r = major_radius + minor_radius * math.cos(v)
            verts.append([r * cu, r * su, minor_radius * math.sin(v)])

    faces = []
    i_limit = n_major if closed else n_major - 1
    for i in range(i_limit):
        i2 = (i + 1) % n_major
        for j in range(minor_segments):
            j2 = (j + 1) % minor_segments
            a = i * minor_segments + j
            b = i * minor_segments + j2
            c = i2 * minor_segments + j
            d = i2 * minor_segments + j2
            faces.append([a, c, d])
            faces.append([a, d, b])
    return Mesh(np.array(verts), np.array(faces))


def i_beam(depth: float, flange_width: float, web_thickness: float,
           flange_thickness: float, length: float) -> Mesh:
    """Extruded structural I-section along +X, centred at origin."""
    hd, hw = depth / 2.0, flange_width / 2.0
    hweb, ft = web_thickness / 2.0, flange_thickness

    profile = np.array([
        [-hw, -hd], [hw, -hd], [hw, -hd + ft], [hweb, -hd + ft],
        [hweb, hd - ft], [hw, hd - ft], [hw, hd], [-hw, hd],
        [-hw, hd - ft], [-hweb, hd - ft], [-hweb, -hd + ft], [-hw, -hd + ft],
    ])
    n = len(profile)
    hl = length / 2.0
    verts = np.vstack([
        np.column_stack([np.full(n, -hl), profile[:, 0], profile[:, 1]]),
        np.column_stack([np.full(n,  hl), profile[:, 0], profile[:, 1]]),
    ])

    faces = []
    for i in range(n):
        j = (i + 1) % n
        faces.append([i, j, n + j])
        faces.append([i, n + j, n + i])

    # Cap both ends by fanning the (convex-enough) profile from its centroid.
    c0, c1 = len(verts), len(verts) + 1
    verts = np.vstack([verts, [[-hl, 0.0, 0.0], [hl, 0.0, 0.0]]])
    for i in range(n):
        j = (i + 1) % n
        faces.append([c0, j, i])
        faces.append([c1, n + i, n + j])
    return Mesh(verts, np.array(faces))


def channel(width: float, height: float, thickness: float, length: float) -> Mesh:
    """U-section cable tray / channel extruded along +X."""
    hw, hh, t = width / 2.0, height / 2.0, thickness
    profile = np.array([
        [-hw, -hh], [hw, -hh], [hw, hh], [hw - t, hh],
        [hw - t, -hh + t], [-hw + t, -hh + t], [-hw + t, hh], [-hw, hh],
    ])
    n = len(profile)
    hl = length / 2.0
    verts = np.vstack([
        np.column_stack([np.full(n, -hl), profile[:, 0], profile[:, 1]]),
        np.column_stack([np.full(n,  hl), profile[:, 0], profile[:, 1]]),
    ])
    faces = []
    for i in range(n):
        j = (i + 1) % n
        faces.append([i, j, n + j])
        faces.append([i, n + j, n + i])
    return Mesh(verts, np.array(faces))


# =====================================================================
# OBJ serialisation
#
# The synthetic source format stores unique geometry definitions as OBJ
# groups; component instances reference a group plus a 4x4 transform.
# That mirrors how real plant exports carry repeated hardware and is what
# makes instance de-duplication a meaningful optimisation rather than a
# trick.
# =====================================================================

def write_obj(path, groups: dict[str, Mesh]) -> None:
    lines: list[str] = ["# Model Delivery Pipeline - synthetic plant geometry",
                        "# units: mm, axis: Z-up"]
    offset = 1  # OBJ vertex indices are 1-based and file-global
    for name, mesh in groups.items():
        lines.append(f"g {name}")
        for v in mesh.vertices:
            lines.append(f"v {v[0]:.4f} {v[1]:.4f} {v[2]:.4f}")
        for f in mesh.faces:
            lines.append(f"f {f[0] + offset} {f[1] + offset} {f[2] + offset}")
        offset += mesh.vertex_count
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")


def parse_obj(text: str) -> dict[str, Mesh]:
    """Parse grouped OBJ text into {group_name: Mesh}.

    Hand-rolled rather than delegated so that malformed geometry raises a
    classified pipeline error instead of a library-specific exception.
    """
    vertices: list[tuple[float, float, float]] = []
    groups: dict[str, list[tuple[int, int, int]]] = {}
    current = "default"
    groups[current] = []

    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        head, _, rest = line.partition(" ")
        if head == "v":
            parts = rest.split()
            if len(parts) < 3:
                raise ValueError(f"Malformed vertex on line {lineno}: {raw!r}")
            vertices.append((float(parts[0]), float(parts[1]), float(parts[2])))
        elif head == "g" or head == "o":
            current = rest.strip() or "default"
            groups.setdefault(current, [])
        elif head == "f":
            parts = rest.split()
            if len(parts) < 3:
                raise ValueError(f"Malformed face on line {lineno}: {raw!r}")
            idx = [int(p.split("/")[0]) for p in parts]
            idx = [(i - 1) if i > 0 else (len(vertices) + i) for i in idx]
            # Fan-triangulate any n-gon.
            for k in range(1, len(idx) - 1):
                groups[current].append((idx[0], idx[k], idx[k + 1]))

    all_v = np.array(vertices, dtype=np.float64) if vertices else np.zeros((0, 3))
    out: dict[str, Mesh] = {}
    for name, faces in groups.items():
        if not faces:
            continue
        f = np.array(faces, dtype=np.int64)
        if f.size and (f.min() < 0 or f.max() >= len(all_v)):
            raise ValueError(f"Group {name!r} references vertices outside the file")
        used, remapped = np.unique(f, return_inverse=True)
        out[name] = Mesh(all_v[used], remapped.reshape(-1, 3))
    return out
