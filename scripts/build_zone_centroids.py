"""Build data/reference/zone_centroids.csv from TLC's taxi_zones.zip shapefile.

Run once (and again only if TLC changes the zones); the output is DVC-tracked.

    uv run python scripts/build_zone_centroids.py

Centroids are area-weighted polygon centroids in the shapefile's own CRS
(NAD83 / New York Long Island, US survey feet), computed with the shoelace
formula over every ring so multi-part zones and holes are handled. Staying in
State Plane means planar distances between centroids are accurate to well
under 1% across the city, with no reprojection dependency.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import shapefile  # pyshp

from tripduration.ingest import _with_retries, download

ZIP_URL = "https://d37ci6vzurychx.cloudfront.net/misc/taxi_zones.zip"
OUT = Path("data/reference/zone_centroids.csv")
REPORT = Path("reports/ingest/zone_centroids.json")
FT_PER_KM = 3280.839895

log = logging.getLogger("build_zone_centroids")


def ring_area_and_moment(pts: list[tuple[float, float]]) -> tuple[float, float, float]:
    """Signed area and first moments (Cx*A, Cy*A) of one closed ring."""
    a = cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1], strict=True):
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    return a / 2.0, cx / 6.0, cy / 6.0


def polygon_centroid(shape: shapefile.Shape) -> tuple[float, float, float]:
    """Area-weighted centroid (x, y) and |area| over all rings of a polygon."""
    parts = list(shape.parts) + [len(shape.points)]
    area = mx = my = 0.0
    for start, end in zip(parts[:-1], parts[1:], strict=True):
        ring = [(float(x), float(y)) for x, y in shape.points[start:end]]
        a, cx, cy = ring_area_and_moment(ring)
        area += a
        mx += cx
        my += cy
    if area == 0:
        raise ValueError("degenerate polygon")
    return mx / area, my / area, abs(area)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".zip")
    md5, size = _with_retries(lambda: download(ZIP_URL, tmp), what=f"GET {ZIP_URL}")
    try:
        with zipfile.ZipFile(tmp) as zf:
            names = {
                Path(n).suffix: n
                for n in zf.namelist()
                if n.endswith((".shp", ".shx", ".dbf"))
            }
            sf = shapefile.Reader(
                shp=io.BytesIO(zf.read(names[".shp"])),
                shx=io.BytesIO(zf.read(names[".shx"])),
                dbf=io.BytesIO(zf.read(names[".dbf"])),
            )
            rows = []
            for rec, shp in zip(sf.records(), sf.shapes(), strict=True):
                x, y, area = polygon_centroid(shp)
                rows.append(
                    {
                        "LocationID": int(rec["LocationID"]),
                        "borough": rec["borough"],
                        "zone": rec["zone"],
                        "x_ft": round(x, 1),
                        "y_ft": round(y, 1),
                        "area_sqft": round(area, 0),
                    }
                )
    finally:
        tmp.unlink(missing_ok=True)

    rows.sort(key=lambda r: r["LocationID"])
    ids = [r["LocationID"] for r in rows]
    if ids != list(range(1, 264)):
        raise SystemExit(f"expected LocationID 1..263 exactly once, got {len(ids)} ids")
    with OUT.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(
        json.dumps(
            {
                "source_url": ZIP_URL,
                "source_md5": md5,
                "bytes": size,
                "zones": len(rows),
                "crs": "NAD83 / New York Long Island (ftUS), EPSG:2263",
                "output": str(OUT),
                "output_md5": hashlib.md5(OUT.read_bytes()).hexdigest(),
                "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    log.info("wrote %s (%d zones) from %s md5=%s", OUT, len(rows), ZIP_URL, md5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
