"""Minimal, instancing-aware glTF 2.0 / GLB writer.

Why hand-rolled
---------------
glTF is the format web plant viewers consume, and the single most
valuable property of a plant model is that it is mostly repeats: one
valve definition used 800 times, one beam section used 3,000 times.
Delivered correctly that is *one* mesh plus 3,000 node transforms.

Off-the-shelf exporters tested for this POC emitted one mesh entry per
instance. The vertex buffers were shared, but the JSON carried thousands
of near-identical mesh definitions, which is where the payload went. The
writer below emits one mesh per unique (geometry, material) pair and
references it from many nodes, which is what instancing is supposed to
mean.

Output is a single self-contained .glb: 12-byte header, JSON chunk,
binary chunk. No external references, nothing fetched at view time.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass

import numpy as np

from .geometry import Mesh

GLB_MAGIC = 0x46546C67          # 'glTF'
CHUNK_JSON = 0x4E4F534A         # 'JSON'
CHUNK_BIN = 0x004E4942          # 'BIN\0'

COMPONENT_UNSIGNED_SHORT = 5123
COMPONENT_UNSIGNED_INT = 5125
COMPONENT_FLOAT = 5126
TARGET_ARRAY_BUFFER = 34962
TARGET_ELEMENT_ARRAY_BUFFER = 34963

# Z-up (plant/engineering convention) to Y-up (glTF convention).
Z_UP_TO_Y_UP = np.array([
    [1.0, 0.0, 0.0, 0.0],
    [0.0, 0.0, 1.0, 0.0],
    [0.0, -1.0, 0.0, 0.0],
    [0.0, 0.0, 0.0, 1.0],
])


@dataclass(frozen=True)
class Instance:
    """One placed occurrence of a shared geometry definition."""
    name: str
    geometry_key: str
    matrix: tuple  # 16 floats, row-major
    material: str


# Discipline/category palette. Colours are part of the deliverable: an
# operator scanning a model reads category by colour before they read tags.
MATERIAL_PALETTE: dict[str, tuple[float, float, float]] = {
    "Pipe":          (0.62, 0.66, 0.70),
    "Elbow":         (0.55, 0.60, 0.65),
    "Reducer":       (0.55, 0.60, 0.65),
    "Valve":         (0.85, 0.33, 0.20),
    "Flange":        (0.72, 0.52, 0.22),
    "PipeSupport":   (0.40, 0.42, 0.45),
    "Column":        (0.30, 0.45, 0.68),
    "Beam":          (0.36, 0.53, 0.76),
    "Brace":         (0.45, 0.60, 0.82),
    "BasePlate":     (0.28, 0.34, 0.42),
    "Grating":       (0.50, 0.55, 0.58),
    "Vessel":        (0.72, 0.72, 0.76),
    "VesselHead":    (0.70, 0.70, 0.74),
    "Pump":          (0.20, 0.62, 0.50),
    "Motor":         (0.16, 0.50, 0.42),
    "Skid":          (0.35, 0.38, 0.40),
    "HeatExchanger": (0.60, 0.66, 0.72),
    "Nozzle":        (0.80, 0.60, 0.25),
    "Saddle":        (0.38, 0.40, 0.44),
    "CableTray":     (0.90, 0.72, 0.22),
    "TraySupport":   (0.55, 0.45, 0.20),
    "JunctionBox":   (0.78, 0.55, 0.18),
    "Conduit":       (0.70, 0.62, 0.35),
    "_default":      (0.65, 0.65, 0.68),
}


def material_for(category: str | None) -> str:
    return category if category in MATERIAL_PALETTE else "_default"


# =====================================================================
# Buffer assembly
# =====================================================================

class _BufferBuilder:
    def __init__(self) -> None:
        self._chunks: list[bytes] = []
        self._offset = 0
        self.views: list[dict] = []

    def add_view(self, data: bytes, target: int | None) -> int:
        # glTF requires bufferView offsets aligned to the component size;
        # 4-byte alignment satisfies every type used here.
        pad = (-self._offset) % 4
        if pad:
            self._chunks.append(b"\x00" * pad)
            self._offset += pad
        view = {"buffer": 0, "byteOffset": self._offset, "byteLength": len(data)}
        if target is not None:
            view["target"] = target
        self.views.append(view)
        self._chunks.append(data)
        self._offset += len(data)
        return len(self.views) - 1

    def build(self) -> bytes:
        return b"".join(self._chunks)


def _quaternion_from_matrix(r: np.ndarray) -> np.ndarray:
    """Rotation matrix to (x, y, z, w), using the numerically stable branch."""
    trace = r[0, 0] + r[1, 1] + r[2, 2]
    if trace > 0.0:
        s = 0.5 / np.sqrt(trace + 1.0)
        return np.array([(r[2, 1] - r[1, 2]) * s, (r[0, 2] - r[2, 0]) * s,
                         (r[1, 0] - r[0, 1]) * s, 0.25 / s])
    if r[0, 0] > r[1, 1] and r[0, 0] > r[2, 2]:
        s = 2.0 * np.sqrt(1.0 + r[0, 0] - r[1, 1] - r[2, 2])
        return np.array([0.25 * s, (r[0, 1] + r[1, 0]) / s,
                         (r[0, 2] + r[2, 0]) / s, (r[2, 1] - r[1, 2]) / s])
    if r[1, 1] > r[2, 2]:
        s = 2.0 * np.sqrt(1.0 + r[1, 1] - r[0, 0] - r[2, 2])
        return np.array([(r[0, 1] + r[1, 0]) / s, 0.25 * s,
                         (r[1, 2] + r[2, 1]) / s, (r[0, 2] - r[2, 0]) / s])
    s = 2.0 * np.sqrt(1.0 + r[2, 2] - r[0, 0] - r[1, 1])
    return np.array([(r[0, 2] + r[2, 0]) / s, (r[1, 2] + r[2, 1]) / s,
                     0.25 * s, (r[1, 0] - r[0, 1]) / s])


def _node_transform(matrix: np.ndarray) -> dict:
    """Emit a node transform as translation/rotation/scale rather than a matrix.

    A 4x4 matrix costs 16 full-precision numbers in JSON for every node,
    and a plant model is mostly nodes. Component placements are rigid
    transforms, so the same information fits in 3 numbers plus a
    quaternion, and identity parts can be dropped entirely. On a model
    with thousands of instances this is the single largest contributor to
    delivered payload size.

    Values are rounded to sub-micron precision at millimetre scale, which
    is far below anything meaningful in a plant model and keeps the JSON
    numbers short.
    """
    out: dict = {}
    translation = matrix[:3, 3]
    basis = matrix[:3, :3]
    scale = np.linalg.norm(basis, axis=0)
    scale = np.where(scale < 1e-12, 1.0, scale)

    if np.any(np.abs(translation) > 5e-4):
        out["translation"] = [round(float(v), 3) for v in translation]

    rotation = basis / scale
    if np.linalg.det(rotation) < 0:      # reflection: fall back to a matrix
        return {"matrix": [round(float(v), 6) for v in matrix.flatten(order="F")]}

    quat = _quaternion_from_matrix(rotation)
    norm = np.linalg.norm(quat)
    if norm > 1e-12:
        quat = quat / norm
    if abs(quat[3] - 1.0) > 1e-7 or np.any(np.abs(quat[:3]) > 1e-7):
        out["rotation"] = [round(float(v), 6) for v in quat]

    if np.any(np.abs(scale - 1.0) > 1e-6):
        out["scale"] = [round(float(v), 6) for v in scale]

    return out


def _flat_shaded(mesh: Mesh) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Expand to one vertex per triangle corner with true face normals.

    Industrial geometry is full of hard edges. Smooth-shading a welded
    valve body makes it look melted, so the delivered form is expanded
    per face. This is affordable precisely because it is applied to the
    small set of *unique* definitions, not to every instance.
    """
    tri = mesh.vertices[mesh.faces]                       # (M, 3, 3)
    edge1 = tri[:, 1] - tri[:, 0]
    edge2 = tri[:, 2] - tri[:, 0]
    normals = np.cross(edge1, edge2)
    lengths = np.linalg.norm(normals, axis=1, keepdims=True)
    normals = np.divide(normals, lengths, out=np.zeros_like(normals), where=lengths > 1e-12)

    positions = tri.reshape(-1, 3).astype(np.float32)
    vertex_normals = np.repeat(normals, 3, axis=0).astype(np.float32)
    indices = np.arange(positions.shape[0], dtype=np.uint32)
    return positions, vertex_normals, indices


# =====================================================================
# Writer
# =====================================================================

def write_glb(path, geometries: dict[str, Mesh], instances: list[Instance], *,
              generator: str = "modelops", extras: dict | None = None) -> dict:
    """Write an instanced GLB. Returns payload statistics."""
    used: dict[tuple[str, str], list[Instance]] = {}
    for inst in instances:
        if inst.geometry_key in geometries:
            used.setdefault((inst.geometry_key, inst.material), []).append(inst)

    buf = _BufferBuilder()
    accessors: list[dict] = []
    meshes: list[dict] = []
    materials: list[dict] = []
    material_index: dict[str, int] = {}
    mesh_index: dict[tuple[str, str], int] = {}

    unique_triangles = 0
    unique_vertices = 0

    for (geom_key, material_key), _ in used.items():
        mesh = geometries[geom_key]
        if mesh.triangle_count == 0:
            continue
        positions, normals, indices = _flat_shaded(mesh)
        unique_triangles += mesh.triangle_count
        unique_vertices += positions.shape[0]

        # Narrower index type when the mesh is small enough, which is the
        # normal case once geometry is de-duplicated.
        if positions.shape[0] <= 65535:
            index_bytes = indices.astype(np.uint16).tobytes()
            index_type = COMPONENT_UNSIGNED_SHORT
        else:
            index_bytes = indices.astype(np.uint32).tobytes()
            index_type = COMPONENT_UNSIGNED_INT

        idx_view = buf.add_view(index_bytes, TARGET_ELEMENT_ARRAY_BUFFER)
        pos_view = buf.add_view(positions.tobytes(), TARGET_ARRAY_BUFFER)
        nrm_view = buf.add_view(normals.tobytes(), TARGET_ARRAY_BUFFER)

        accessors.append({"bufferView": idx_view, "componentType": index_type,
                          "count": int(indices.size), "type": "SCALAR"})
        idx_acc = len(accessors) - 1

        accessors.append({"bufferView": pos_view, "componentType": COMPONENT_FLOAT,
                          "count": int(positions.shape[0]), "type": "VEC3",
                          "min": [float(v) for v in positions.min(axis=0)],
                          "max": [float(v) for v in positions.max(axis=0)]})
        pos_acc = len(accessors) - 1

        accessors.append({"bufferView": nrm_view, "componentType": COMPONENT_FLOAT,
                          "count": int(normals.shape[0]), "type": "VEC3"})
        nrm_acc = len(accessors) - 1

        if material_key not in material_index:
            r, g, b = MATERIAL_PALETTE.get(material_key, MATERIAL_PALETTE["_default"])
            materials.append({
                "name": material_key,
                "pbrMetallicRoughness": {
                    "baseColorFactor": [r, g, b, 1.0],
                    "metallicFactor": 0.15,
                    "roughnessFactor": 0.75,
                },
                "doubleSided": True,
            })
            material_index[material_key] = len(materials) - 1

        meshes.append({
            "name": f"{geom_key}::{material_key}",
            "primitives": [{
                "attributes": {"POSITION": pos_acc, "NORMAL": nrm_acc},
                "indices": idx_acc,
                "material": material_index[material_key],
                "mode": 4,
            }],
        })
        mesh_index[(geom_key, material_key)] = len(meshes) - 1

    # ---- nodes: one per instance, all pointing at shared meshes --------
    nodes: list[dict] = [{"name": "PLANT_ROOT",
                          "matrix": [float(v) for v in Z_UP_TO_Y_UP.flatten(order="F")],
                          "children": []}]
    for (geom_key, material_key), group in used.items():
        mi = mesh_index.get((geom_key, material_key))
        if mi is None:
            continue
        for inst in group:
            m = np.asarray(inst.matrix, dtype=np.float64).reshape(4, 4)
            node = {"name": inst.name, "mesh": mi}
            node.update(_node_transform(m))
            nodes.append(node)
            nodes[0]["children"].append(len(nodes) - 1)

    binary = buf.build()
    gltf = {
        "asset": {"version": "2.0", "generator": generator},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": nodes,
        "meshes": meshes,
        "materials": materials,
        "accessors": accessors,
        "bufferViews": buf.views,
        "buffers": [{"byteLength": len(binary)}],
    }
    if extras:
        gltf["extras"] = extras

    json_bytes = json.dumps(gltf, separators=(",", ":")).encode("utf-8")
    json_bytes += b" " * ((-len(json_bytes)) % 4)          # pad with spaces
    binary += b"\x00" * ((-len(binary)) % 4)               # pad with zeros

    total = 12 + 8 + len(json_bytes) + 8 + len(binary)
    with open(path, "wb") as fh:
        fh.write(struct.pack("<III", GLB_MAGIC, 2, total))
        fh.write(struct.pack("<II", len(json_bytes), CHUNK_JSON))
        fh.write(json_bytes)
        fh.write(struct.pack("<II", len(binary), CHUNK_BIN))
        fh.write(binary)

    return {
        "bytes": total,
        "json_bytes": len(json_bytes),
        "binary_bytes": len(binary),
        "mesh_definitions": len(meshes),
        "instance_nodes": len(nodes) - 1,
        "materials": len(materials),
        "unique_triangles": unique_triangles,
        "unique_vertices": unique_vertices,
        "instanced_triangles": sum(
            geometries[k].triangle_count for (k, _), grp in used.items() for _ in grp
        ),
    }


def read_glb_json(path) -> dict:
    """Parse back the JSON chunk. Used by the publish stage's self-check."""
    with open(path, "rb") as fh:
        magic, version, _total = struct.unpack("<III", fh.read(12))
        if magic != GLB_MAGIC:
            raise ValueError("Not a GLB file")
        if version != 2:
            raise ValueError(f"Unsupported GLB version {version}")
        length, kind = struct.unpack("<II", fh.read(8))
        if kind != CHUNK_JSON:
            raise ValueError("First GLB chunk is not JSON")
        return json.loads(fh.read(length))
