"""PyMuPDF backend (AGPL-3.0 or Artifex commercial licence).

This is the ONLY module that imports pymupdf. It converts PyMuPDF objects into the plain
types of app/pdf_backends/base.py, in displayed-page coordinates (rotation applied, y down).
"""
from __future__ import annotations

from typing import Iterable

import pymupdf

from .base import BackendInfo, PdfAnnot, PdfPageInfo, PdfPath, TextSpan

INFO = BackendInfo("pymupdf", "AGPL-3.0 / commercial (Artifex)",
                   ["paths", "text", "ocg-layers", "images", "annotations", "render", "render-layers"])


def _pt(m, p) -> tuple[float, float]:
    q = pymupdf.Point(p) * m
    return (float(q.x), float(q.y))


class _Page:
    def __init__(self, doc: "_Doc", index: int):
        self._doc = doc
        self._p = doc._d[index]
        self._m = self._p.rotation_matrix
        r = self._p.rect
        self.info = PdfPageInfo(index + 1, float(r.width), float(r.height), int(self._p.rotation))

    def paths(self) -> list[PdfPath]:
        m = self._m
        out: list[PdfPath] = []
        for k, d in enumerate(self._p.get_drawings()):
            segs = []
            for it in d["items"]:
                op = it[0]
                if op == "l":
                    segs.append(("l", [_pt(m, it[1]), _pt(m, it[2])]))
                elif op == "c":
                    segs.append(("c", [_pt(m, it[1]), _pt(m, it[2]), _pt(m, it[3]), _pt(m, it[4])]))
                elif op == "re":
                    r = it[1]
                    segs.append(("re", [_pt(m, r.tl), _pt(m, r.tr), _pt(m, r.br), _pt(m, r.bl)]))
                elif op == "qu":
                    q = it[1]
                    segs.append(("re", [_pt(m, q.ul), _pt(m, q.ur), _pt(m, q.lr), _pt(m, q.ll)]))
            t = d.get("type") or ""
            dashes = (d.get("dashes") or "").strip()
            out.append(PdfPath(
                segs=segs, layer=d.get("layer") or "",
                stroke=tuple(d["color"]) if "s" in t and d.get("color") is not None else None,
                fill=tuple(d["fill"]) if "f" in t and d.get("fill") is not None else None,
                width=float(d.get("width") or 0.0), closed=bool(d.get("closePath")),
                dashed=bool(dashes) and not dashes.startswith("[]"), seq=int(d.get("seqno", k))))
        return out

    def text_spans(self) -> list[TextSpan]:
        m = self._m
        out: list[TextSpan] = []
        for k, s in enumerate(self._p.get_texttrace()):
            chars = s.get("chars") or ()
            if not chars:
                continue
            text = "".join(chr(c[0]) if c[0] >= 0 else "�" for c in chars)
            x0, y0, x1, y1 = s["bbox"]
            a, b = _pt(m, (x0, y0)), _pt(m, (x1, y1))
            bbox = (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
            dx, dy = s.get("dir") or (1.0, 0.0)
            v = pymupdf.Point(dx, dy) * pymupdf.Matrix(m.a, m.b, m.c, m.d, 0, 0)
            out.append(TextSpan(text=text, bbox=bbox, origin=_pt(m, chars[0][2]), dir=(float(v.x), float(v.y)),
                                size=float(s.get("size") or 0.0), font=s.get("font") or "",
                                layer=s.get("layer") or "", seq=int(s.get("seqno", k))))
        return out

    def images(self) -> list[tuple[float, float, float, float]]:
        out = []
        for im in self._p.get_image_info():
            r = pymupdf.Rect(im["bbox"]) * self._m
            out.append((float(r.x0), float(r.y0), float(r.x1), float(r.y1)))
        return out

    def annotations(self) -> list[PdfAnnot]:
        out: list[PdfAnnot] = []
        for an in self._p.annots() or ():
            try:
                info = an.info or {}
                r = pymupdf.Rect(an.rect) * self._m
                out.append(PdfAnnot(kind=str(an.type[1]) if an.type else "", rect=(float(r.x0), float(r.y0), float(r.x1), float(r.y1)),
                                    text=str(info.get("content") or ""), title=str(info.get("title") or ""),
                                    subject=str(info.get("subject") or "")))
            except Exception:
                continue
        return out

    def ocg_layers(self) -> list[str]:
        return self._doc.ocg_layers()

    def render_png(self, clip=None, dpi: float = 72, layers: Iterable[str] | None = None) -> bytes:
        return self._pix(clip, dpi, layers, gray=False).tobytes("png")

    def render_gray(self, clip=None, dpi: float = 72, layers: Iterable[str] | None = None) -> tuple[int, int, bytes]:
        pix = self._pix(clip, dpi, layers, gray=True)
        return pix.width, pix.height, bytes(pix.samples)

    def _pix(self, clip, dpi, layers, gray: bool):
        d = self._doc._d
        saved = None
        if layers is not None:
            # the UI layer configuration is what MuPDF's renderer honours
            want = set(layers)
            saved = [(u["number"], u["on"]) for u in d.layer_ui_configs()]
            for u in d.layer_ui_configs():
                d.set_layer_ui_config(u["number"], 0 if u["text"] in want else 2)
        try:
            z = dpi / 72.0
            page = d[self.info.number - 1]          # reload so the layer state applies
            # clip is in displayed coordinates; PyMuPDF's clip is too (it applies the rotation)
            return page.get_pixmap(matrix=pymupdf.Matrix(z, z), clip=pymupdf.Rect(*clip) if clip else None,
                                   alpha=False, colorspace=pymupdf.csGRAY if gray else pymupdf.csRGB)
        finally:
            if saved is not None:
                for num, on in saved:
                    d.set_layer_ui_config(num, 0 if on else 2)


class _Doc:
    def __init__(self, path: str):
        self._d = pymupdf.open(path)
        if self._d.needs_pass:
            raise ValueError("encrypted PDF (password required)")
        self.page_count = self._d.page_count
        self._layers: list[str] | None = None

    def page(self, index: int) -> _Page:
        return _Page(self, index)

    def ocg_layers(self) -> list[str]:
        if self._layers is None:
            seen: list[str] = []
            for v in self._d.get_ocgs().values():
                if v["name"] not in seen:
                    seen.append(v["name"])
            self._layers = seen
        return self._layers

    def close(self) -> None:
        self._d.close()


def open_pdf(path: str) -> _Doc:
    return _Doc(str(path))
