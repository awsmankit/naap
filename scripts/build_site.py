"""Assemble the in-browser Naap (the GitHub Pages site) into site/.

    uv run python scripts/build_site.py                 # build site/ (downloads runtime files once)
    uv run python scripts/build_site.py --serve         # ... and serve it on http://127.0.0.1:8780
    uv run python scripts/build_site.py --no-vendor     # UI + Python only (no downloads; for tests)

The site is web/ with `mode: "local"` in config.js, our Python packages as py/naap.zip, the
public sample, and self-hosted runtime files under vendor/: Pyodide (CPython on WebAssembly),
the wheels the engine needs, and LibreDWG compiled to WebAssembly for DWG files. Every download
is pinned by SHA-256 and cached in .cache/site/. Nothing is loaded from a CDN at run time.
Licence texts for everything bundled go to licenses/.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PYODIDE_VERSION = "0.29.5"
PYODIDE_CDN = f"https://cdn.jsdelivr.net/pyodide/v{PYODIDE_VERSION}/full/"

# (url, sha256): the Pyodide runtime (npm), LibreDWG for the browser (npm), licence texts.
PYODIDE_NPM = (f"https://registry.npmjs.org/pyodide/-/pyodide-{PYODIDE_VERSION}.tgz",
               "6749b10ee515a1458ecea36d860c8ce2a48366e85b3f92d749ad1840e4aff181")
LIBREDWG_VERSION = "0.8.8"
LIBREDWG_NPM = (f"https://registry.npmjs.org/@mlightcad/libredwg-web/-/libredwg-web-{LIBREDWG_VERSION}.tgz",
                "6d568e4a1f09c259d968cc2fa9077b6d377a4744824a9dd265df6f4bed5ab194")
LIBREDWG_SOURCE = ("https://github.com/mlightcad/libredwg-web/tree/v0.8.8 "
                   "(commit 26f9929134c88d6058bd48d201efebe638e21d89)")
LICENCES = {
    "pyodide/LICENSE": (f"https://raw.githubusercontent.com/pyodide/pyodide/{PYODIDE_VERSION}/LICENSE",
                        "1f256ecad192880510e84ad60474eab7589218784b9a50bc7ceee34c2b91f1d5"),
    "cpython/LICENSE": ("https://raw.githubusercontent.com/python/cpython/v3.13.2/LICENSE",
                        "78b12c3a81360b357002334f0e70ea0e92eebf7a9b358805c03c48484945f3bb"),
    "libredwg/COPYING": ("https://raw.githubusercontent.com/mlightcad/libredwg-web/v0.8.8/COPYING",
                         "8ceb4b9ee5adedde47b31e975c1d90c73ad27b6b165a1dcd80c7c545eb65b903"),
}
# Pure-Python wheels that Pyodide does not ship (PyPI; versions and hashes as in uv.lock).
PYPI_WHEELS = {
    "ezdxf": ("https://files.pythonhosted.org/packages/fb/d7/9f420747a1edf562cabff228f8d1f7a16530ad021ea6f84c775ca15dec9b/ezdxf-1.4.5-py3-none-any.whl",
              "70a79ec7d40990f60453e3c87ffe9f668907ee061cf6f4c0a3794d032ec2b40e",
              ["numpy", "pyparsing", "typing-extensions", "fonttools"], ["ezdxf"]),
    "openpyxl": ("https://files.pythonhosted.org/packages/c0/da/977ded879c29cbd04de313843e76868e6e13408a94ed6b987245dc7c8506/openpyxl-3.1.5-py2.py3-none-any.whl",
                 "5282c12b107bffeef825f4617dc029afaf41d0ea60823bbb665ef3079dc79de2", ["et-xmlfile"], ["openpyxl"]),
    "et-xmlfile": ("https://files.pythonhosted.org/packages/c1/8b/5fe2cc11fee489817272089c4203e679c63b570a5aaeb18d852ae3cbba6a/et_xmlfile-2.0.0-py3-none-any.whl",
                   "7a91720bc756843502c3b7504c77b8fe44217c85c537d85037f0f536151b2caa", [], ["et_xmlfile"]),
}
# Packages from Pyodide's own repository (hashes come from its pinned pyodide-lock.json).
PYODIDE_PACKAGES = ["numpy", "scipy", "pyparsing", "typing-extensions", "fonttools", "pymupdf", "pillow"]
BASE_PACKAGES = ["numpy", "scipy", "pyparsing", "typing-extensions", "fonttools", "ezdxf", "openpyxl", "et-xmlfile"]
PDF_PACKAGES = ["pymupdf"]          # loaded the first time a PDF is opened
CONVERT_PACKAGES = ["pillow"]       # ezdxf's drawing add-on (DXF to PDF) imports it; loaded on the first conversion
# The engine uses scipy.ndimage and scipy.spatial only; these subpackages are never imported.
SCIPY_DROP = ["misc", "stats", "optimize", "interpolate", "io", "signal", "integrate", "cluster", "fft",
              "odr", "fftpack", "datasets"]
PYODIDE_CORE = ["pyodide.mjs", "pyodide.asm.js", "pyodide.asm.wasm", "python_stdlib.zip"]

WEB_SKIP = {"dev_server.py", "README.md", "build_mock.py"}
PY_PACKAGES = ["app", "engine", "engine_struct", "convert"]
PY_SKIP = {"app/main.py", "app/__main__.py"}    # the FastAPI server is not needed in the browser


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(url: str, digest: str, cache: Path) -> bytes:
    """Download once into the cache; always check the hash."""
    f = cache / f"{digest[:16]}-{url.rsplit('/', 1)[-1]}"
    if f.exists():
        data = f.read_bytes()
    else:
        print(f"  download {url}")
        with urllib.request.urlopen(url, timeout=120) as r:
            data = r.read()
    if sha256(data) != digest:
        raise SystemExit(f"checksum mismatch for {url}: got {sha256(data)}, want {digest}")
    if not f.exists():
        cache.mkdir(parents=True, exist_ok=True)
        f.write_bytes(data)
    return data


def tgz_member(data: bytes, name: str) -> bytes:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
        return t.extractfile(t.getmember(name)).read()


def trim_scipy(data: bytes) -> bytes:
    drop = tuple(f"scipy/{p}/" for p in SCIPY_DROP)
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as zin, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zout:
        for info in zin.infolist():
            if info.filename.startswith(drop):
                continue
            body = zin.read(info)
            if info.filename.endswith(".dist-info/RECORD"):
                body = b"".join(ln for ln in body.splitlines(keepends=True) if not ln.decode().startswith(drop))
            zout.writestr(info, body)
    return out.getvalue()


def licence_files(whl: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(whl)) as z:
        return {n: z.read(n) for n in z.namelist()
                if ".dist-info/" in n and any(k in n.rsplit("/", 1)[-1].upper() for k in ("LICEN", "COPYING"))}


def copy_web(out: Path) -> None:
    def ignore(d, names):
        return [n for n in names if n in WEB_SKIP or n == "__pycache__" or n.endswith((".py", ".pyc"))]
    shutil.copytree(ROOT / "web", out, ignore=ignore)
    cfg = out / "config.js"
    text = cfg.read_text()
    assert 'mode: "server"' in text, "web/config.js should default to server mode"
    cfg.write_text(text.replace('mode: "server"', 'mode: "local"'))
    (out / "samples").mkdir()
    shutil.copy2(ROOT / "samples" / "sample-house.dxf", out / "samples" / "sample-house.dxf")
    shutil.copy2(ROOT / "LICENSE", out / "LICENSE.txt")
    shutil.copy2(ROOT / "THIRD_PARTY_NOTICES.md", out / "THIRD_PARTY_NOTICES.md")
    (out / ".nojekyll").write_text("")


def python_zip(out: Path) -> Path:
    """Our packages (sources only) as one zip the worker unpacks into /naap."""
    z = out / "py" / "naap.zip"
    z.parent.mkdir(parents=True)
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for pkg in PY_PACKAGES:
            for f in sorted((ROOT / pkg).rglob("*.py")):
                rel = f.relative_to(ROOT).as_posix()
                if rel in PY_SKIP or "__pycache__" in rel:
                    continue
                info = zipfile.ZipInfo(rel, date_time=(2020, 1, 1, 0, 0, 0))  # reproducible zip
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, f.read_bytes())
    return z


def vendor(out: Path, cache: Path) -> dict:
    """Pyodide + wheels + LibreDWG into out/vendor; returns sizes for the manifest."""
    pdir = out / "vendor" / "pyodide"
    pdir.mkdir(parents=True)
    npm = fetch(*PYODIDE_NPM, cache)
    for name in PYODIDE_CORE:
        (pdir / name).write_bytes(tgz_member(npm, f"package/{name}"))
    lock = json.loads(tgz_member(npm, "package/pyodide-lock.json"))
    packages, lic = {}, {}
    for name in PYODIDE_PACKAGES:
        entry = dict(lock["packages"][name])
        data = fetch(PYODIDE_CDN + entry["file_name"], entry["sha256"], cache)
        if name == "scipy":
            data = trim_scipy(data)
            entry["sha256"] = sha256(data)
            entry["unvendored_tests"] = False
        (pdir / entry["file_name"]).write_bytes(data)
        packages[name] = entry
        lic[name] = licence_files(data)
    for name, (url, digest, depends, imports) in PYPI_WHEELS.items():
        data = fetch(url, digest, cache)
        fn = url.rsplit("/", 1)[-1]
        (pdir / fn).write_bytes(data)
        version = fn.split("-")[1]
        packages[name] = {"name": name, "version": version, "file_name": fn, "install_dir": "site", "sha256": digest,
                          "package_type": "package", "imports": imports, "depends": depends, "unvendored_tests": False}
        lic[name] = licence_files(data)
    for entry in packages.values():
        missing = [d for d in entry["depends"] if d not in packages]
        assert not missing, f"{entry['name']} needs {missing}, which the site does not ship"
    (pdir / "pyodide-lock.json").write_text(json.dumps({"info": lock["info"], "packages": packages}, indent=1))

    ldir = out / "vendor" / "libredwg"
    ldir.mkdir(parents=True)
    npm = fetch(*LIBREDWG_NPM, cache)
    for name in ("libredwg-web.js", "libredwg-web.wasm"):
        (ldir / name).write_bytes(tgz_member(npm, f"package/wasm/{name}"))

    lroot = out / "licenses"
    for rel, (url, digest) in LICENCES.items():
        (lroot / rel).parent.mkdir(parents=True, exist_ok=True)
        (lroot / rel).write_bytes(fetch(url, digest, cache))
    (lroot / "libredwg" / "SOURCE.txt").write_text(
        f"GNU LibreDWG compiled to WebAssembly, from @mlightcad/libredwg-web {LIBREDWG_VERSION} (GPL-3.0).\n"
        f"Corresponding source: {LIBREDWG_SOURCE}\nUpstream: https://www.gnu.org/software/libredwg/\n")
    for name, files in lic.items():
        for path, body in files.items():
            dest = lroot / "python" / name / path.rsplit("/", 1)[-1]
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(body)

    def size(names):
        return sum((pdir / packages[n]["file_name"]).stat().st_size for n in names)
    core = sum((pdir / n).stat().st_size for n in PYODIDE_CORE)
    return {"core": core, "base": size(BASE_PACKAGES), "pdf": size(PDF_PACKAGES), "convert": size(CONVERT_PACKAGES),
            "libredwg": sum(f.stat().st_size for f in ldir.iterdir()),
            "versions": {"pyodide": PYODIDE_VERSION, "libredwg-web": LIBREDWG_VERSION,
                         **{n: packages[n]["version"] for n in packages}}}


def build(out: Path, cache: Path, with_vendor: bool = True) -> dict:
    from app import service

    if out.exists():
        shutil.rmtree(out)
    copy_web(out)
    zpath = python_zip(out)
    sizes = vendor(out, cache) if with_vendor else {"core": 0, "base": 0, "pdf": 0, "convert": 0, "libredwg": 0, "versions": {}}
    manifest = {
        "pyodide": {"dir": "vendor/pyodide/", "version": PYODIDE_VERSION},
        "packages": BASE_PACKAGES,
        "pdf_packages": PDF_PACKAGES,
        "convert_packages": CONVERT_PACKAGES,
        "naap_zip": "py/naap.zip",
        "runtime_bytes": sizes["core"] + sizes["base"] + zpath.stat().st_size,
        "pdf_bytes": sizes["pdf"],
        "convert_bytes": sizes["convert"],
        "vendored": with_vendor,
        "versions": sizes["versions"],
        "meta": {**service.meta(), "runtime": "browser"},
    }
    (out / "worker" / "assets.json").write_text(json.dumps(manifest, indent=1))
    build_id = stamp_sw(out)
    total = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    return {"total": total, "first_load": manifest["runtime_bytes"], "build": build_id, **{k: v for k, v in sizes.items() if k != "versions"}}


# What the service worker (web/sw.js) caches on install: the app shell, our Python and the sample.
# The runtime under vendor/ is cached when the engine first loads it, never up front.
SW_PRECACHE = ["config.js", "manifest.webmanifest", "css/*.css", "js/*.js", "worker/*.js", "worker/assets.json",
               "fonts/*.woff2", "icons/*", "py/naap.zip", "samples/sample-house.dxf"]


def stamp_sw(out: Path) -> str:
    """Write the build's version and shell list into sw.js. Any change to the site gives a new
    sw.js, so browsers install it and drop the old caches; vendor/ has its own hash (its cache
    survives releases that do not change the runtime). Returns the build id."""
    sw = out / "sw.js"
    build, vend = hashlib.sha256(), hashlib.sha256()
    for f in sorted(p for p in out.rglob("*") if p.is_file() and p != sw):
        rel = f.relative_to(out).as_posix()
        line = f"{rel}\0{sha256(f.read_bytes())}\n".encode()
        build.update(line)
        if rel.startswith("vendor/"):
            vend.update(line)
    shell = ["./"] + sorted({f.relative_to(out).as_posix() for pat in SW_PRECACHE for f in out.glob(pat) if f.is_file()})
    text = sw.read_text()
    for key, val in (('"__NAAP_BUILD__"', json.dumps(build.hexdigest()[:16])),
                     ('"__NAAP_VENDOR__"', json.dumps(vend.hexdigest()[:16])),
                     ('["__NAAP_PRECACHE__"]', json.dumps(shell))):
        assert key in text, f"web/sw.js lost its {key} placeholder"
        text = text.replace(key, val)
    sw.write_text(text)
    return build.hexdigest()[:16]


def serve(out: Path, port: int) -> None:
    import functools
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    class H(SimpleHTTPRequestHandler):
        extensions_map = {**SimpleHTTPRequestHandler.extensions_map, ".js": "text/javascript", ".mjs": "text/javascript",
                          ".wasm": "application/wasm", ".json": "application/json", ".woff2": "font/woff2",
                          ".webmanifest": "application/manifest+json"}

    class S(ThreadingHTTPServer):
        request_queue_size = 64   # the worker fetches the runtime files in parallel

    print(f"serving {out} on http://127.0.0.1:{port}/")
    S(("127.0.0.1", port), functools.partial(H, directory=str(out))).serve_forever()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=ROOT / "site")
    ap.add_argument("--cache", type=Path, default=ROOT / ".cache" / "site")
    ap.add_argument("--no-vendor", action="store_true", help="skip the runtime downloads (the site will not run)")
    ap.add_argument("--serve", nargs="?", const=8780, type=int, metavar="PORT")
    a = ap.parse_args(argv)
    s = build(a.out.resolve(), a.cache.resolve(), with_vendor=not a.no_vendor)
    mb = lambda n: f"{n / 1e6:.1f} MB"  # noqa: E731
    print(f"site: {a.out}  total {mb(s['total'])}; first load (Pyodide + engine) {mb(s['first_load'])}; "
          f"+ PDF reader {mb(s['pdf'])}; + DWG converter {mb(s['libredwg'])}")
    if a.serve:
        serve(a.out.resolve(), a.serve)


if __name__ == "__main__":
    main()
