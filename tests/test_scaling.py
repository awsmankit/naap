"""Large drawings: the wall-gap check and the parapet corner join were quadratic in the number of wall
lines (a 33k-entity synthetic plan took 2 minutes in find_wall_gaps alone). The indexed versions must
give the same answers as the all-pairs loops they replace."""
from __future__ import annotations

import random
import time
from copy import deepcopy

from engine.pairs import Strip, join_corners


def _join_corners_all_pairs(strips):
    for s in strips:
        for o in strips:
            if o.horizontal == s.horizontal:
                continue
            reach = max(s.t, o.t)
            if not (o.lo - reach <= s.c <= o.hi + reach):
                continue
            if abs(o.c - s.lo) <= reach and o.c < s.lo:
                s.lo = o.c
            elif abs(o.c - s.hi) <= reach and o.c > s.hi:
                s.hi = o.c


def test_join_corners_matches_all_pairs():
    rnd = random.Random(7)
    for _ in range(30):
        strips = []
        for _ in range(rnd.randint(2, 60)):
            lo = rnd.uniform(0, 20000)
            strips.append(Strip(rnd.random() < 0.5, rnd.uniform(0, 20000), lo, lo + rnd.uniform(300, 8000), rnd.choice([115, 230, 300])))
        # corners and chains: snap some ends close to perpendicular centre lines
        for s in strips:
            perp = [o for o in strips if o.horizontal != s.horizontal]
            if perp and rnd.random() < 0.7:
                o = rnd.choice(perp)
                s.lo = o.c + rnd.uniform(0, 250)
                s.hi = max(s.hi, s.lo + 300)
        a, b = deepcopy(strips), deepcopy(strips)
        join_corners(a)
        _join_corners_all_pairs(b)
        assert [(s.lo, s.hi) for s in a] == [(s.lo, s.hi) for s in b]


def test_wall_gaps_scale_on_a_big_plan(tmp_path):
    """16 x 16 copies of the sample house's walls (about 15k entities) build in seconds."""
    import ezdxf

    from app.extract import extract
    from engine.cli import effective_config
    from engine.mb import build_mb
    from pathlib import Path
    src = ezdxf.readfile(Path(__file__).resolve().parent.parent / "samples" / "sample-house.dxf")
    doc = ezdxf.new()
    doc.header["$INSUNITS"] = 4
    for layer in src.layers:
        if layer.dxf.name not in doc.layers:
            doc.layers.add(layer.dxf.name, color=layer.dxf.color)
    msp = doc.modelspace()
    ents = [e for e in src.modelspace() if e.dxftype() in ("LINE", "LWPOLYLINE", "ARC") and e.dxf.layer.startswith("A-")
            and e.dxf.layer != "A-WALL PARAPET"]
    for i in range(16):
        for j in range(16):
            for e in ents:
                c = e.copy()
                c.translate(i * 14000, j * 10000, 0)
                msp.add_entity(c)
    msp.add_text("CAMPUS GROUND FLOOR PLAN", dxfattribs={"insert": (0, -2000), "height": 800, "layer": "A-TEXT"})
    doc.saveas(tmp_path / "big.dxf")
    geo = extract(tmp_path / "big.dxf")
    t = time.time()
    res = build_mb(geo, effective_config(geo, {}, "big"))
    assert time.time() - t < 20
    assert sum(f.category == "untagged-opening" for f in res.flags) >= 200   # the copies' wall gaps are still found
