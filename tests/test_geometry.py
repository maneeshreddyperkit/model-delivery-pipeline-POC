"""Sanity checks for the geometry core.

Run with:  .venv\\Scripts\\python.exe -m tests.test_geometry
These are the invariants the optimise and QA stages depend on, so they
are worth asserting rather than assuming.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import numpy as np

from modelops import geometry as g


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}{(' - ' + detail) if detail else ''}")
    if not condition:
        raise AssertionError(name)


def test_primitives() -> None:
    print("primitives")
    b = g.box(100, 200, 300)
    check("box has 12 triangles", b.triangle_count == 12)
    check("box volume correct",
          abs(b.signed_volume() - 100 * 200 * 300) < 1e-6,
          f"{b.signed_volume():,.0f}")
    check("box extents correct", np.allclose(b.extents(), [100, 200, 300]))

    c = g.cylinder(50, 200, sections=64)
    expected = np.pi * 50 ** 2 * 200
    check("cylinder volume within 1% of analytic",
          abs(c.signed_volume() - expected) / expected < 0.01,
          f"{c.signed_volume():,.0f} vs {expected:,.0f}")

    s = g.uv_sphere(75, segments=48, rings=32)
    expected = 4.0 / 3.0 * np.pi * 75 ** 3
    check("sphere volume within 1% of analytic",
          abs(s.signed_volume() - expected) / expected < 0.01,
          f"{s.signed_volume():,.0f} vs {expected:,.0f}")

    # Inscribed tessellations always under-report volume, and the error is
    # a pure function of segment count, so the tolerance is tied to the
    # tessellation rather than picked by feel.
    t = g.torus(300, 40, 64, 32)
    expected = 2 * np.pi ** 2 * 300 * 40 ** 2
    check("torus volume within 1% of analytic",
          abs(t.signed_volume() - expected) / expected < 0.01,
          f"{t.signed_volume():,.0f} vs {expected:,.0f}")

    coarse = g.torus(300, 40, 32, 16)
    check("coarser torus is inscribed (smaller) but same shape",
          coarse.signed_volume() < t.signed_volume() < expected,
          f"{coarse.signed_volume():,.0f} < {t.signed_volume():,.0f} < {expected:,.0f}")

    quarter = g.torus(300, 40, 32, 16, sweep_degrees=90)
    check("90-degree elbow is a quarter of the full sweep",
          abs(quarter.signed_volume() / coarse.signed_volume() - 0.25) < 0.02,
          f"ratio={quarter.signed_volume() / coarse.signed_volume():.3f}")

    beam = g.i_beam(400, 200, 12, 20, 6000)
    check("i-beam produces geometry", beam.triangle_count > 0)
    check("i-beam length along X", abs(beam.extents()[0] - 6000) < 1e-6)


def test_transforms() -> None:
    print("transforms")
    b = g.box(10, 10, 10)
    moved = b.transformed(g.translation(100, 0, 0))
    check("translation moves centroid",
          abs(moved.vertices[:, 0].mean() - 100) < 1e-9)
    rotated = b.transformed(g.rotation("z", 90))
    check("rotation preserves volume",
          abs(rotated.signed_volume() - b.signed_volume()) < 1e-6)


def test_weld() -> None:
    print("weld")
    # Two 100mm boxes butted face to face at x=50: 16 corners, but the
    # 4 on the shared face are coincident, so 12 survive welding.
    a = g.box(100, 100, 100)
    bb = g.box(100, 100, 100).transformed(g.translation(100, 0, 0))
    merged = g.concat([a, bb])
    check("concat sums triangles", merged.triangle_count == 24)
    check("concat sums vertices", merged.vertex_count == 16)

    welded, removed = g.weld_vertices(merged, 1e-5)
    check("weld merges the 4 shared corners", removed == 4, f"removed={removed}")
    check("welded mesh has 12 corners", welded.vertex_count == 12,
          f"{welded.vertex_count}")
    check("weld preserves triangle count for touching solids",
          welded.triangle_count == 24, f"{welded.triangle_count}")

    # A part exported twice at the same location: welding collapses the
    # vertices but deliberately keeps both triangle sets, because welding
    # must never change what is drawn. Removing the redundant shell is a
    # separate, explicit operation.
    dup = g.concat([a, a.copy()])
    welded2, _ = g.weld_vertices(dup, 1e-5)
    check("coincident boxes weld to 8 vertices",
          welded2.vertex_count == 8, f"{welded2.vertex_count}")
    check("welding keeps both triangle shells",
          welded2.triangle_count == 24, f"{welded2.triangle_count}")

    deduped, dropped = g.drop_duplicate_faces(welded2)
    check("duplicate-face removal drops the redundant shell",
          dropped == 12 and deduped.triangle_count == 12,
          f"dropped={dropped}, remaining={deduped.triangle_count}")
    check("de-duplicated volume matches a single box",
          abs(deduped.signed_volume() - a.signed_volume()) < 1e-6,
          f"{deduped.signed_volume():,.0f} vs {a.signed_volume():,.0f}")

    _, none_dropped = g.drop_duplicate_faces(welded)
    check("de-duplication leaves genuinely distinct faces alone",
          none_dropped == 0, f"dropped={none_dropped}")

    # An unwelded soup (every triangle its own 3 vertices) is the realistic case.
    soup_v = merged.vertices[merged.faces].reshape(-1, 3)
    soup_f = np.arange(soup_v.shape[0]).reshape(-1, 3)
    soup = g.Mesh(soup_v, soup_f)
    check("soup has 3 vertices per triangle", soup.vertex_count == 24 * 3)
    welded3, removed3 = g.weld_vertices(soup, 1e-5)
    check("soup of 72 vertices welds back to 12 corners",
          welded3.vertex_count == 12, f"{welded3.vertex_count}")
    check("soup weld preserves volume",
          abs(welded3.signed_volume() - merged.signed_volume()) < 1e-6)


def test_decimate() -> None:
    print("decimate")
    s = g.uv_sphere(100, segments=64, rings=48)
    before = s.triangle_count
    d = g.decimate(s, 0.25, min_faces=100)
    check("decimation reduces triangles", d.triangle_count < before,
          f"{before} -> {d.triangle_count}")
    check("decimation lands near target ratio",
          abs(d.triangle_count / before - 0.25) < 0.12,
          f"ratio={d.triangle_count / before:.2f}")
    drift = abs(d.signed_volume() - s.signed_volume()) / s.signed_volume()
    check("decimation keeps volume within 5%", drift < 0.05, f"drift={drift:.3%}")
    check("small meshes are left alone",
          g.decimate(g.box(1, 1, 1), 0.1, min_faces=200).triangle_count == 12)


def test_obj_roundtrip(tmp: Path) -> None:
    print("obj round-trip")
    groups = {
        "geom_valve": g.cylinder(40, 120, sections=16),
        "geom_beam": g.i_beam(300, 150, 10, 16, 4000),
        "geom_elbow": g.torus(200, 50, 16, 10, sweep_degrees=90),
    }
    path = tmp / "roundtrip.obj"
    g.write_obj(path, groups)
    parsed = g.parse_obj(path.read_text(encoding="utf-8"))

    check("all groups survive", set(parsed) == set(groups), str(sorted(parsed)))
    for name, original in groups.items():
        got = parsed[name]
        check(f"{name} triangle count preserved",
              got.triangle_count == original.triangle_count,
              f"{got.triangle_count} vs {original.triangle_count}")
        check(f"{name} bounds preserved",
              np.allclose(got.bounds(), original.bounds(), atol=1e-3))


def test_obj_rejects_garbage() -> None:
    print("obj error handling")
    for bad, label in [
        ("v 0 0 0\nf 1 2 99\n", "face index out of range"),
        ("v 0 0\n", "short vertex"),
        ("v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2\n", "short face"),
    ]:
        try:
            g.parse_obj(bad)
        except ValueError:
            check(f"rejects {label}", True)
        else:
            check(f"rejects {label}", False)


if __name__ == "__main__":
    tmp = Path(__file__).resolve().parents[1] / "data" / "_test_tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    test_primitives()
    test_transforms()
    test_weld()
    test_decimate()
    test_obj_roundtrip(tmp)
    test_obj_rejects_garbage()
    print("\nAll geometry checks passed.")
