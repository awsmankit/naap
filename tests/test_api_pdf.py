"""API tests for PDF (structural) jobs: upload checks, scanned PDFs, sheets, calibration,
sheet renders, the /mb contract for both kinds, and the UI mounts."""
from __future__ import annotations

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import jobs
from app import main as appmain
from tests.test_pdf_extract import vector_sheet, write_pdf

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "DATA_DIR", tmp_path / "jobs")
    (tmp_path / "jobs").mkdir()
    return TestClient(appmain.app)


def _upload(client, path: Path, name: str | None = None):
    with path.open("rb") as fh:
        return client.post("/api/jobs", files={"file": (name or path.name, fh, "application/octet-stream")})


def _wait(client, job_id: str, timeout=120) -> dict:
    t = time.time()
    while time.time() - t < timeout:
        st = client.get(f"/api/jobs/{job_id}").json()
        if st["status"] in ("ready", "error"):
            return st
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def test_rejects_wrong_type_and_fake_pdf(client, tmp_path):
    bad = tmp_path / "x.txt"
    bad.write_text("hello")
    r = _upload(client, bad)
    assert r.status_code == 400 and ".pdf" in r.json()["detail"]
    fake = tmp_path / "fake.pdf"
    fake.write_bytes(b"MZ\x90\x00 not a pdf at all")
    r = _upload(client, fake)
    assert r.status_code == 400 and "PDF" in r.json()["detail"]
    assert not any(jobs.DATA_DIR.iterdir())  # rejected uploads leave nothing behind


def test_scanned_pdf_reports_clear_error(client, tmp_path):
    img = bytes((i * 7) % 256 for i in range(64 * 64))
    p = write_pdf(tmp_path / "scan.pdf", "q 842 0 0 595 0 0 cm /Im0 Do Q\n", image=img, img_wh=(64, 64))
    r = _upload(client, p)
    assert r.status_code == 200
    st = _wait(client, r.json()["id"])
    assert st["status"] == "error"
    assert st["error"].startswith("scanned PDF not supported yet")
    assert st.get("error_kind") == "scanned"


def test_small_vector_pdf_job(client, tmp_path):
    p = vector_sheet(tmp_path)
    jid = _upload(client, p, "my sheet.pdf").json()["id"]
    st = _wait(client, jid)
    assert st["status"] == "ready", st
    assert st["kind"] == "pdf" and st["stats"]["units"] == "pt" and st["stats"]["sheets"] == 1
    assert (jobs.DATA_DIR / jid / "input.pdf").exists()

    cfg = client.get(f"/api/jobs/{jid}/config").json()
    assert cfg["engine"] == "struct" and cfg["calibration"] == {}
    assert cfg["regions"][0]["kind"] == "sheet" and cfg["regions"][0]["sheet"] == 1

    sheets = client.get(f"/api/jobs/{jid}/sheets").json()
    assert sheets[0]["title_block"]["drawing_no"] == "ABC-STR-07"
    assert sheets[0]["counts"]["entities"] > 0

    mb = client.get(f"/api/jobs/{jid}/mb").json()
    assert mb["kind"] == "structural"
    for k in ("rows", "abstract", "rooms", "openings", "dimensions", "flags", "meta", "steel", "schedules", "calibration"):
        assert k in mb

    png = client.get(f"/api/jobs/{jid}/sheet/1.png?dpi=20")
    assert png.status_code == 200 and png.content[:4] == b"\x89PNG"
    assert client.get(f"/api/jobs/{jid}/sheet/1.png?dpi=20").content == png.content  # cached
    crop = client.get(f"/api/jobs/{jid}/sheet/1.png?dpi=72&clip=0,0,100,50")
    assert crop.status_code == 200 and crop.content[:4] == b"\x89PNG"
    assert client.get(f"/api/jobs/{jid}/sheet/1.png?dpi=5000").status_code == 400
    assert client.get(f"/api/jobs/{jid}/sheet/1.png?clip=1;2").status_code == 400
    assert client.get(f"/api/jobs/{jid}/sheet/9.png").status_code == 404

    # calibration: validation, then a user override that survives a config save
    assert client.put(f"/api/jobs/{jid}/calibration", json={"7": {"scale": 100}}).status_code == 400
    assert client.put(f"/api/jobs/{jid}/calibration", json={"1": {"p1": [0, 0], "p2": [1e9, 0], "mm": 5}}).status_code == 400
    assert client.put(f"/api/jobs/{jid}/calibration", json={"1": {"p1": [10, 10], "p2": [110, 10], "mm": -1}}).status_code == 400
    r = client.put(f"/api/jobs/{jid}/calibration", json={"1": {"p1": [10, 10], "p2": [110, 10], "mm": 4000}})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["1"]["mm"] == 4000
    cfg = client.get(f"/api/jobs/{jid}/config").json()
    del cfg["calibration"]
    client.put(f"/api/jobs/{jid}/config", json=cfg)
    assert client.get(f"/api/jobs/{jid}/config").json()["calibration"]["1"]["mm"] == 4000
    cal = client.get(f"/api/jobs/{jid}/calibration").json()
    assert cal["user"]["1"]["p2"] == [110.0, 10.0]
    assert client.put(f"/api/jobs/{jid}/calibration", json={"1": None}).json()["user"] == {}

    b = client.post(f"/api/jobs/{jid}/build", json=client.get(f"/api/jobs/{jid}/config").json())
    assert b.status_code == 200
    x = client.get(f"/api/jobs/{jid}/mb.xlsx")
    assert x.status_code == 200 and x.content[:2] == b"PK"

    ents = client.get(f"/api/jobs/{jid}/entities", params={"handles": "p1:1"}).json()
    assert "P1:1" in ents


def test_dxf_job_has_no_sheet_routes(client, tmp_path):
    import ezdxf

    doc = ezdxf.new()
    doc.modelspace().add_line((0, 0), (1000, 0))
    p = tmp_path / "a.dxf"
    doc.saveas(p)
    jid = _upload(client, p).json()["id"]
    assert _wait(client, jid)["status"] == "ready"
    assert client.get(f"/api/jobs/{jid}/sheets").status_code == 404
    mb = client.get(f"/api/jobs/{jid}/mb").json()
    assert mb["kind"] == "architectural"


def test_ui_mounts(client):
    assert "js/main.js" in client.get("/").text
    assert client.get("/legacy/").status_code == 404
    assert client.get("/dev_server.py").status_code == 404
    assert client.get("/js/main.js").status_code == 200


