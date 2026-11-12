"""Conversion routes (synthetic files only)."""
from __future__ import annotations

import time

import ezdxf
import pytest
from ezdxf import recover
from fastapi.testclient import TestClient

from app import convert_api, jobs
from app import main as appmain
from tests.test_convert import make_dxf, make_pdf


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "DATA_DIR", tmp_path / "jobs")
    (tmp_path / "jobs").mkdir()
    return TestClient(appmain.app)


def post(client, url, path, name=None, **form):
    with open(path, "rb") as fh:
        return client.post(url, files={"file": (name or path.name, fh, "application/octet-stream")}, data=form)


def test_pdf_to_dxf_round_trip(client, tmp_path):
    pdf = make_pdf(tmp_path / "a.pdf", pages=2)
    r = post(client, "/api/convert/pdf-to-dxf", pdf, page="1", scale="1:100", tolerance="0.2")
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["report"]["entity_total"] > 0 and j["report"]["scaled"] is True
    assert "output" not in j["report"]                     # no server path leaks
    assert j["summary"] and "DXF" in j["note"] and j["filename"] == "a.dxf"
    d = client.get(j["dxf_url"])
    assert d.status_code == 200 and "attachment" in d.headers["content-disposition"]
    out = tmp_path / "o.dxf"
    out.write_bytes(d.content)
    doc, auditor = recover.readfile(str(out))
    assert not auditor.errors
    # and back to PDF
    r2 = post(client, "/api/convert/to-pdf", out, paper="A4", orientation="landscape", mono="true")
    assert r2.status_code == 200, r2.text
    pdf2 = client.get(r2.json()["pdf_url"])
    assert pdf2.content.startswith(b"%PDF-") and r2.json()["report"]["pages"][0]["paper"].startswith("A4 landscape")


def test_calibration_and_page_range(client, tmp_path):
    pdf = make_pdf(tmp_path / "a.pdf", pages=2)
    ok = post(client, "/api/convert/pdf-to-dxf", pdf, calib="0,0,100,0,5000")
    assert ok.status_code == 200 and ok.json()["report"]["scaled"] is True
    assert post(client, "/api/convert/pdf-to-dxf", pdf, page="9").status_code == 422
    assert post(client, "/api/convert/pdf-to-dxf", pdf, calib="1,2,3").status_code == 422
    assert post(client, "/api/convert/pdf-to-dxf", pdf, scale="big").status_code == 422
    assert post(client, "/api/convert/pdf-to-dxf", pdf, tolerance="0").status_code == 422


def test_dxf_options_validated(client, tmp_path):
    dxf = make_dxf(tmp_path / "a.dxf")
    assert post(client, "/api/convert/to-pdf", dxf, paper="A9").status_code == 422
    assert post(client, "/api/convert/to-pdf", dxf, layouts="x").status_code == 422
    r = post(client, "/api/convert/to-pdf", dxf, paper="A0", orientation="portrait", hide_layers="TXT")
    assert r.status_code == 200 and r.json()["report"]["pages"][0]["paper"] == "A0 portrait"


def test_rejects_bad_uploads(client, tmp_path):
    pdf = make_pdf(tmp_path / "a.pdf")
    dxf = make_dxf(tmp_path / "a.dxf")
    assert post(client, "/api/convert/pdf-to-dxf", dxf).status_code == 400          # wrong extension
    assert post(client, "/api/convert/to-pdf", pdf).status_code == 400
    fake = tmp_path / "x.pdf"
    fake.write_bytes(b"not a pdf at all")
    r = post(client, "/api/convert/pdf-to-dxf", fake)
    assert r.status_code == 400 and "valid PDF" in r.json()["detail"]
    fake2 = tmp_path / "x.dwg"
    fake2.write_bytes(b"MZ\x90\x00 random")
    assert post(client, "/api/convert/to-pdf", fake2).status_code == 400
    empty = tmp_path / "e.dxf"
    empty.write_bytes(b"")
    assert post(client, "/api/convert/to-pdf", empty).status_code == 400
    assert not list((tmp_path / "jobs" / "convert").iterdir())                      # failed uploads leave nothing


def test_size_limit(client, tmp_path, monkeypatch):
    monkeypatch.setattr(convert_api, "MAX_PDF_MB", 0)
    r = post(client, "/api/convert/pdf-to-dxf", make_pdf(tmp_path / "a.pdf"))
    assert r.status_code == 413


def test_scanned_pdf_message_and_dwg_output_refused(client, tmp_path):
    from tests.test_pdf_extract import write_pdf
    img = bytes((i * 7) % 256 for i in range(64 * 64))
    scan = write_pdf(tmp_path / "s.pdf", "q 842 0 0 595 0 0 cm /Im0 Do Q\n", image=img, img_wh=(64, 64))
    r = post(client, "/api/convert/pdf-to-dxf", scan)
    assert r.status_code == 422 and "scanned" in r.json()["detail"]
    r = post(client, "/api/convert/pdf-to-dxf", make_pdf(tmp_path / "a.pdf"), format="dwg")
    assert r.status_code == 422 and "Save As DWG" in r.json()["detail"]


def test_download_ids_and_cleanup(client, tmp_path):
    assert client.get("/api/convert/../x/out.dxf").status_code in (404, 405)
    assert client.get(f"/api/convert/{'0' * 32}/out.dxf").status_code == 404
    assert client.get(f"/api/convert/{'0' * 32}/out.exe").status_code == 404
    pdf = make_pdf(tmp_path / "a.pdf")
    j = post(client, "/api/convert/pdf-to-dxf", pdf).json()
    d = tmp_path / "jobs" / "convert" / j["id"]
    assert not (d / "input.pdf").exists()                                            # upload removed at once
    convert_api.sweep(time.time() + convert_api.KEEP_S + 5)
    assert not d.exists() and client.get(j["dxf_url"]).status_code == 404
    assert client.get("/api/jobs").json() == []                                      # not listed as a job
