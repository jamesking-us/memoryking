"""Verify packaged phone assets and transfer bounds without claiming device installation."""

import gzip
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    from memoryking.adapters.viewer_http import ASSETS

    directory = ROOT / "src/memoryking/adapters/viewer_web"
    expected = {name for name, _ in ASSETS.values()}
    assert {path.name for path in directory.iterdir()} == expected
    records = {
        name: {
            "bytes": (directory / name).stat().st_size,
            "gzip_estimate": len(gzip.compress((directory / name).read_bytes(), mtime=0)),
        }
        for name in sorted(expected)
    }
    manifest = json.loads((directory / "manifest.webmanifest").read_text())
    assert manifest["id"] == manifest["scope"] == manifest["start_url"] == "/viewer/"
    assert manifest["display"] == "standalone"
    from PIL import Image

    for size in (192, 512):
        with Image.open(directory / f"icon-{size}.png") as image:
            assert image.size == (size, size) and image.format == "PNG"
    # Conservatively include both install icons, including the larger icon that
    # is not on the initial browser critical path. The server does not gzip.
    transferred = sum(item["bytes"] for item in records.values())
    assert transferred <= 750 * 1024, transferred
    assert 'import("./graph.js")' in (directory / "presentation.js").read_text()
    for path in directory.glob("*.js"):
        source = path.read_text()
        assert "@memoryking/contracts" not in source
        assert "innerHTML" not in source and "document.write" not in source
        assert "serviceWorker.register" not in source
    result = {
        "passed": True,
        "all_assets_raw_bytes": transferred,
        "budget_bytes": 750 * 1024,
        "scope": "All packaged assets, including optional graph and both icons; uncompressed entity bytes. gzip_estimate is not an observed Content-Encoding or phone-install result.",
        "assets": records,
    }
    output = ROOT / "tmp/checks/mobile-assets.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(f"Mobile assets passed: {len(records)} fixed assets, {transferred} uncompressed bytes")


if __name__ == "__main__":
    main()
