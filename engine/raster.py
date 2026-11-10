"""Small raster toolkit for plan measurement (numpy + scipy.ndimage).

A Grid covers a bbox in drawing units with square cells. Cell (j, i) has its
centre at (x0 + (i + .5) * cell, y0 + (j + .5) * cell); arrays are indexed [row, col].
Areas are cell counts x cell**2, which is unbiased for polygons (centre sampling).
"""
from __future__ import annotations

import math

import numpy as np
from scipy import ndimage


class Grid:
    def __init__(self, bbox, cell: float, pad: float = 0.0):
        x0, y0, x1, y1 = bbox
        self.cell = float(cell)
        self.x0 = x0 - pad
        self.y0 = y0 - pad
        self.nx = max(int(math.ceil((x1 + pad - self.x0) / cell)), 1)
        self.ny = max(int(math.ceil((y1 + pad - self.y0) / cell)), 1)

    # ------------------------------------------------------------ coords
    @property
    def shape(self):
        return (self.ny, self.nx)

    def zeros(self, dtype=bool):
        return np.zeros(self.shape, dtype=dtype)

    def col(self, x: float) -> int:
        return int(math.floor((x - self.x0) / self.cell))

    def row(self, y: float) -> int:
        return int(math.floor((y - self.y0) / self.cell))

    def inside(self, j: int, i: int) -> bool:
        return 0 <= j < self.ny and 0 <= i < self.nx

    def xc(self, i):
        return self.x0 + (np.asarray(i) + 0.5) * self.cell

    def yc(self, j):
        return self.y0 + (np.asarray(j) + 0.5) * self.cell

    def bbox_of(self, mask) -> list[float] | None:
        rows = np.flatnonzero(mask.any(axis=1))
        if not len(rows):
            return None
        cols = np.flatnonzero(mask.any(axis=0))
        c = self.cell
        return [round(self.x0 + cols[0] * c, 1), round(self.y0 + rows[0] * c, 1),
                round(self.x0 + (cols[-1] + 1) * c, 1), round(self.y0 + (rows[-1] + 1) * c, 1)]

    def slices(self, bbox):
        """Row/col slices covering a drawing bbox (clipped)."""
        i0, i1 = max(self.col(bbox[0]), 0), min(self.col(bbox[2]) + 1, self.nx)
        j0, j1 = max(self.row(bbox[1]), 0), min(self.row(bbox[3]) + 1, self.ny)
        return slice(j0, max(j1, j0)), slice(i0, max(i1, i0))

    def rect_slices(self, bbox):
        """Exactly the cells fill_rect(bbox) sets, as (row, col) slices (possibly empty)."""
        i0 = max(int(math.ceil((bbox[0] - self.x0) / self.cell - 0.5)), 0)
        i1 = min(int(math.ceil((bbox[2] - self.x0) / self.cell - 0.5)), self.nx)
        j0 = max(int(math.ceil((bbox[1] - self.y0) / self.cell - 0.5)), 0)
        j1 = min(int(math.ceil((bbox[3] - self.y0) / self.cell - 0.5)), self.ny)
        return slice(j0, max(j1, j0)), slice(i0, max(i1, i0))

    # ------------------------------------------------------------ drawing
    def fill_loops(self, loops: list[list[float]], out=None, value=True) -> np.ndarray:
        """Even-odd fill of one or more closed loops (flat [x, y, ...] lists)."""
        out = self.zeros() if out is None else out
        rows_x: dict[int, list[float]] = {}
        for lp in loops:
            xs = np.asarray(lp[0::2], dtype=float)
            ys = np.asarray(lp[1::2], dtype=float)
            if len(xs) < 3:
                continue
            x2, y2 = np.roll(xs, -1), np.roll(ys, -1)
            for ax, ay, bx, by in zip(xs, ys, x2, y2):
                if ay == by:
                    continue
                lo, hi = min(ay, by), max(ay, by)
                j0 = max(int(math.ceil((lo - self.y0) / self.cell - 0.5)), 0)
                j1 = min(int(math.ceil((hi - self.y0) / self.cell - 0.5)), self.ny)
                if j1 <= j0:
                    continue
                yc = self.y0 + (np.arange(j0, j1) + 0.5) * self.cell
                xc = ax + (yc - ay) * (bx - ax) / (by - ay)
                for j, x in zip(range(j0, j1), xc):
                    rows_x.setdefault(j, []).append(float(x))
        for j, xs in rows_x.items():
            xs.sort()
            for k in range(0, len(xs) - 1, 2):
                i0 = max(int(math.ceil((xs[k] - self.x0) / self.cell - 0.5)), 0)
                i1 = min(int(math.ceil((xs[k + 1] - self.x0) / self.cell - 0.5)), self.nx)
                if i1 > i0:
                    out[j, i0:i1] = value
        return out

    def draw_segment(self, arr: np.ndarray, x1, y1, x2, y2, value=True) -> None:
        n = int(max(abs(x2 - x1), abs(y2 - y1)) / (self.cell * 0.5)) + 2
        xs = np.linspace(x1, x2, n)
        ys = np.linspace(y1, y2, n)
        i = np.floor((xs - self.x0) / self.cell).astype(int)
        j = np.floor((ys - self.y0) / self.cell).astype(int)
        ok = (i >= 0) & (i < self.nx) & (j >= 0) & (j < self.ny)
        arr[j[ok], i[ok]] = value

    def draw_polyline(self, arr, flat: list[float], closed=False, value=True) -> None:
        pts = list(zip(flat[0::2], flat[1::2]))
        if closed and len(pts) > 2:
            pts.append(pts[0])
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            self.draw_segment(arr, ax, ay, bx, by, value)

    def fill_rect(self, arr, bbox, value=True) -> None:
        """Cells whose centre lies inside bbox."""
        i0 = max(int(math.ceil((bbox[0] - self.x0) / self.cell - 0.5)), 0)
        i1 = min(int(math.ceil((bbox[2] - self.x0) / self.cell - 0.5)), self.nx)
        j0 = max(int(math.ceil((bbox[1] - self.y0) / self.cell - 0.5)), 0)
        j1 = min(int(math.ceil((bbox[3] - self.y0) / self.cell - 0.5)), self.ny)
        if i1 > i0 and j1 > j0:
            arr[j0:j1, i0:i1] = value


def label4(mask: np.ndarray):
    return ndimage.label(mask)


def touches_border(lab: np.ndarray) -> set[int]:
    edge = np.concatenate([lab[0], lab[-1], lab[:, 0], lab[:, -1]])
    return set(np.unique(edge[edge > 0]).tolist())


def run_bounds(mask: np.ndarray, axis: int):
    """For each True cell: (start, end_exclusive) index of its run along axis."""
    m = mask if axis == 1 else mask.T
    ny, nx = m.shape
    idx = np.broadcast_to(np.arange(nx), m.shape)
    # start: last False index before cell (+1)
    s = np.where(~m, idx, -1)
    start = np.maximum.accumulate(s, axis=1) + 1
    e = np.where(~m, idx, nx)
    end = np.minimum.accumulate(e[:, ::-1], axis=1)[:, ::-1]
    if axis == 0:
        return start.T, end.T
    return start, end


def run_length(mask: np.ndarray, axis: int) -> np.ndarray:
    s, e = run_bounds(mask, axis)
    return np.where(mask, e - s, 0)


def unique_rows(arr: np.ndarray, return_inverse: bool = False, return_counts: bool = False):
    """np.unique(arr, axis=0, ...) for small non-negative-ish int columns, via one int64 key per row
    (same lexicographic order and results, much faster)."""
    arr = np.asarray(arr)
    if arr.ndim != 2 or not len(arr) or arr.dtype.kind not in "iub":
        return np.unique(arr, axis=0, return_inverse=return_inverse, return_counts=return_counts)
    a64 = arr.astype(np.int64)
    lo = a64.min(axis=0)
    span = a64.max(axis=0) - lo + 1
    if float(np.prod(span.astype(float))) >= 2 ** 62:
        return np.unique(arr, axis=0, return_inverse=return_inverse, return_counts=return_counts)
    key = np.zeros(len(a64), dtype=np.int64)
    for c in range(a64.shape[1]):
        key = key * span[c] + (a64[:, c] - lo[c])
    res = np.unique(key, return_index=True, return_inverse=return_inverse, return_counts=return_counts)
    uk, first = res[0], res[1]
    out = [arr[first]]
    if return_inverse:
        out.append(res[2])
    if return_counts:
        out.append(res[-1])
    return out[0] if len(out) == 1 else tuple(out)
