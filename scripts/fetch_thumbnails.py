#!/usr/bin/env python3
"""
Fill in missing paper thumbnails from open-access PDFs.

For every paper in data/publications.json that has no `thumb`, this finds an
open-access PDF, pulls the largest figure out of its first pages, crops it
square and writes img_journal/<id>.png.

It never touches a thumbnail that already exists. The hand-picked images in
data/publications.manual.json always win, and this only fills gaps.

Where the PDF comes from, in order:
  1. an arXiv ID, read from the paper's url or detail field
  2. an `oa_pdf` link, recorded by update_publications.py from OpenAlex
  3. Unpaywall, looked up by DOI

Papers behind a paywall with no preprint produce nothing, and the page falls
back to its generated k-space pattern. That is the expected outcome for a
minority of entries, not a failure.

Usage
    python3 scripts/fetch_thumbnails.py
    python3 scripts/fetch_thumbnails.py --only hipath --force
    python3 scripts/fetch_thumbnails.py --dry-run
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time
from pathlib import Path

import pymupdf
import requests

ROOT = Path(__file__).resolve().parent.parent
PUBS_FILE = ROOT / "data" / "publications.json"
IMG_DIR = ROOT / "img_journal"

MAILTO = os.environ.get("OPENALEX_MAILTO", "z.zhenxuan24@imperial.ac.uk")
UA = f"zhenxuan-homepage/1.0 (mailto:{MAILTO})"
TIMEOUT = 30
MAX_PDF_BYTES = 40 * 1024 * 1024
PAGES_TO_SCAN = 3
OUT_SIZE = 512
ARXIV_DELAY = 3.0          # arXiv asks for one request every few seconds


def log(m: str) -> None:
    print(m, flush=True)


# ------------------------------------------------------------- finding a PDF

def arxiv_id(item: dict) -> str | None:
    for field in (item.get("arxiv"), item.get("url"), item.get("detail"), item.get("venue")):
        if not field:
            continue
        m = re.search(r"(\d{4}\.\d{4,5})(v\d+)?", str(field))
        if m and ("arxiv" in str(field).lower() or item.get("status") == "preprint"):
            return m.group(1)
    return None


def unpaywall_pdf(doi: str, session: requests.Session) -> str | None:
    if not doi:
        return None
    try:
        r = session.get(f"https://api.unpaywall.org/v2/{doi}",
                        params={"email": MAILTO}, timeout=TIMEOUT)
        r.raise_for_status()
        loc = r.json().get("best_oa_location") or {}
        return loc.get("url_for_pdf") or None
    except Exception as exc:
        log(f"    Unpaywall: {exc}")
        return None


def pdf_url_for(item: dict, session: requests.Session) -> tuple[str | None, bool]:
    """Return (url, is_arxiv)."""
    aid = arxiv_id(item)
    if aid:
        return f"https://arxiv.org/pdf/{aid}", True
    if item.get("oa_pdf"):
        return item["oa_pdf"], False
    return unpaywall_pdf(item.get("doi", ""), session), False


def download(url: str, session: requests.Session) -> bytes | None:
    try:
        r = session.get(url, timeout=TIMEOUT, stream=True)
        r.raise_for_status()
        buf = io.BytesIO()
        for chunk in r.iter_content(65536):
            buf.write(chunk)
            if buf.tell() > MAX_PDF_BYTES:
                log("    PDF too large, skipping")
                return None
        data = buf.getvalue()
        if not data.startswith(b"%PDF"):
            log("    response is not a PDF (paywall or landing page)")
            return None
        return data
    except Exception as exc:
        log(f"    download failed: {exc}")
        return None


# ------------------------------------------------------------- figure picking

def embedded_figure(doc: pymupdf.Document) -> pymupdf.Pixmap | None:
    """Largest sensible raster image on the first pages."""
    best, best_area = None, 0
    for pno in range(min(PAGES_TO_SCAN, doc.page_count)):
        for info in doc[pno].get_images(full=True):
            xref = info[0]
            try:
                pix = pymupdf.Pixmap(doc, xref)
            except Exception:
                continue
            w, h = pix.width, pix.height
            area, ratio = w * h, w / h if h else 0
            if w < 240 or h < 160 or not (0.3 < ratio < 4.5):
                continue
            if area > best_area:
                best, best_area = pix, area
    return best


def rendered_figure(doc: pymupdf.Document) -> pymupdf.Pixmap:
    """
    No usable raster image, so the figure is vector art. Render page one and
    crop to whatever is drawn on it, padded out towards square. Falls back to
    the upper part of the page, where teaser figures usually sit.
    """
    page = doc[0]
    rect = page.rect
    boxes = [d["rect"] for d in page.get_drawings()] + \
            [pymupdf.Rect(b["bbox"]) for b in page.get_image_info()]
    boxes = [b for b in boxes if b.width > 30 and b.height > 30 and b.get_area() < rect.get_area() * 0.9]

    clip = None
    if boxes:
        union = boxes[0]
        for b in boxes[1:]:
            union |= b
        if union.get_area() > rect.get_area() * 0.03:
            pad = min(rect.width, rect.height) * 0.03
            union = pymupdf.Rect(union.x0 - pad, union.y0 - pad, union.x1 + pad, union.y1 + pad)

            # a very wide or very tall box crops badly once squared, so grow the short side
            w, h = union.width, union.height
            if w > h * 1.5:
                grow = (w / 1.5 - h) / 2
                union = pymupdf.Rect(union.x0, union.y0 - grow, union.x1, union.y1 + grow)
            elif h > w * 1.5:
                grow = (h / 1.5 - w) / 2
                union = pymupdf.Rect(union.x0 - grow, union.y0, union.x1 + grow, union.y1)

            clip = union & rect

    if clip is None or clip.is_empty or clip.get_area() < rect.get_area() * 0.02:
        clip = pymupdf.Rect(rect.x0, rect.y0, rect.x1, rect.y0 + rect.height * 0.55)

    return page.get_pixmap(matrix=pymupdf.Matrix(2.2, 2.2), clip=clip)


def square_png(pix: pymupdf.Pixmap, out: Path) -> None:
    """Scale so the short side is OUT_SIZE, then crop a square out of it."""
    if pix.alpha or pix.colorspace is None or pix.colorspace.n > 3:
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)

    pix.set_origin(0, 0)          # clipped renders carry a page offset; clip maths needs 0,0

    w, h = pix.width, pix.height
    scale = OUT_SIZE / min(w, h)
    sw, sh = max(OUT_SIZE, round(w * scale)), max(OUT_SIZE, round(h * scale))
    scaled = pymupdf.Pixmap(pix, sw, sh)

    x0 = (sw - OUT_SIZE) // 2                 # centre horizontally
    y0 = 0 if sh > sw else (sh - OUT_SIZE) // 2   # favour the top: captions sit low
    crop = pymupdf.Pixmap(scaled, sw, sh,
                          pymupdf.IRect(x0, y0, x0 + OUT_SIZE, y0 + OUT_SIZE))

    out.parent.mkdir(parents=True, exist_ok=True)
    crop.save(out)


# ------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="redo papers that already have a thumbnail")
    ap.add_argument("--only", help="process one paper id only")
    ap.add_argument("--dry-run", action="store_true", help="report what would be fetched")
    args = ap.parse_args()

    data = json.loads(PUBS_FILE.read_text(encoding="utf-8"))
    items = data.get("items", [])

    session = requests.Session()
    session.headers["User-Agent"] = UA

    made, skipped, failed = 0, 0, 0
    last_arxiv = 0.0

    for item in items:
        pid = item.get("id") or "untitled"
        if args.only and args.only != pid:
            continue

        existing = item.get("thumb")
        if existing and not args.force and (ROOT / existing).exists():
            skipped += 1
            continue

        log(f"  {pid}: {item.get('title','')[:58]}")
        url, is_arxiv = pdf_url_for(item, session)
        if not url:
            log("    no open-access PDF found")
            failed += 1
            continue
        if args.dry_run:
            log(f"    would fetch {url}")
            continue

        if is_arxiv:
            wait = ARXIV_DELAY - (time.time() - last_arxiv)
            if wait > 0:
                time.sleep(wait)
            last_arxiv = time.time()

        blob = download(url, session)
        if not blob:
            failed += 1
            continue

        try:
            doc = pymupdf.open(stream=blob, filetype="pdf")
            pix = embedded_figure(doc) or rendered_figure(doc)
            out = IMG_DIR / f"{pid}.png"
            square_png(pix, out)
            doc.close()
        except Exception as exc:
            log(f"    extraction failed: {type(exc).__name__}: {exc}")
            failed += 1
            continue

        item["thumb"] = f"img_journal/{pid}.png"
        log(f"    wrote {item['thumb']}")
        made += 1

    if made and not args.dry_run:
        PUBS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log(f"updated {PUBS_FILE.relative_to(ROOT)}")

    log(f"{made} new, {skipped} already had one, {failed} could not be fetched")
    return 0


if __name__ == "__main__":
    sys.exit(main())
