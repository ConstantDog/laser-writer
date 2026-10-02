"""Copy licenses from the exact build environment and fetch matching LGPL sources."""

from pathlib import Path
import hashlib
import importlib.metadata as metadata
import json
import shutil
import ssl
import sys
import tkinter as tk
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
LICENSES = ROOT / "third_party_licenses"
SOURCES = ROOT / "third_party_sources"
PACKAGES = [
    "gdstk",
    "matplotlib",
    "numpy",
    "shapely",
    "pyserial",
    "Pillow",
    "contourpy",
    "cycler",
    "fonttools",
    "kiwisolver",
    "packaging",
    "pyparsing",
    "python-dateutil",
    "six",
    "PyInstaller",
]


def fetch(url, output, digest=None):
    data = urllib.request.urlopen(url, timeout=60).read()
    if digest and hashlib.sha256(data).hexdigest() != digest:
        raise ValueError(f"Source checksum mismatch: {output.name}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    return {"file": output.name, "url": url, "sha256": hashlib.sha256(data).hexdigest()}


def main():
    LICENSES.mkdir(exist_ok=True)
    SOURCES.mkdir(exist_ok=True)
    inventory = []
    for name in PACKAGES:
        dist = metadata.distribution(name)
        copied = []
        for file in dist.files or []:
            if (
                Path(str(file))
                .name.lower()
                .startswith(("license", "copying", "notice"))
            ):
                src = Path(dist.locate_file(file))
                if not src.is_file() or src.suffix in {".py", ".pyc"}:
                    continue
                target = LICENSES / name / str(file).replace("..", "_")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, target)
                copied.append(target.relative_to(ROOT).as_posix())
        if name == "pyserial" and not copied:
            target = LICENSES / name / "LICENSE.txt"
            fetch(
                "https://raw.githubusercontent.com/pyserial/pyserial/v3.5/LICENSE.txt",
                target,
            )
            copied.append(target.relative_to(ROOT).as_posix())
        if not copied:
            raise RuntimeError(f"Missing license: {name}")
        inventory.append({"package": name, "version": dist.version, "files": copied})
    shutil.copyfile(
        Path(sys.base_prefix) / "LICENSE.txt", LICENSES / "Python-LICENSE.txt"
    )
    openssl_version = ssl.OPENSSL_VERSION.split()[1]
    fetch(
        f"https://raw.githubusercontent.com/openssl/openssl/openssl-{openssl_version}/LICENSE.txt",
        LICENSES / "OpenSSL-LICENSE.txt",
    )
    fetch(
        "https://raw.githubusercontent.com/zlib-ng/zlib-ng/2.2.5/LICENSE.md",
        LICENSES / "zlib-ng-LICENSE.md",
    )
    root = tk.Tk()
    root.withdraw()
    try:
        for name, variable in [("Tcl", "tcl_library"), ("Tk", "tk_library")]:
            library = root.tk.getvar(variable)
            filename = f"{library}/license.terms"
            handle = root.tk.call("open", filename, "r")
            try:
                text = root.tk.call("read", handle)
            finally:
                root.tk.call("close", handle)
            (LICENSES / f"{name}-license.terms").write_text(text, encoding="utf-8")
    finally:
        root.destroy()
    import shapely

    geos_version = shapely.geos_version_string
    sources = [
        fetch(
            f"https://download.osgeo.org/geos/geos-{geos_version}.tar.bz2",
            SOURCES / f"geos-{geos_version}.tar.bz2",
        )
    ]
    version = metadata.version("shapely")
    info = json.load(
        urllib.request.urlopen(
            f"https://pypi.org/pypi/shapely/{version}/json", timeout=30
        )
    )
    source = next(entry for entry in info["urls"] if entry["packagetype"] == "sdist")
    sources.append(
        fetch(source["url"], SOURCES / source["filename"], source["digests"]["sha256"])
    )
    (ROOT / "DEPENDENCIES.json").write_text(
        json.dumps(
            {
                "python": sys.version.split()[0],
                "openssl": ssl.OPENSSL_VERSION,
                "geos": geos_version,
                "packages": inventory,
                "sources": sources,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(
        f"Collected {len(inventory)} package license sets, Python/Tcl/Tk notices and matching GEOS/Shapely sources."
    )


if __name__ == "__main__":
    main()
