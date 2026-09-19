#!/usr/bin/env python3
"""
Rebuild data/publications.json and data/scholar.json.

Sources, in order of trust:

  1. data/publications.manual.json   hand-maintained. Always wins.
  2. Google Scholar (via scholarly)  the paper list and per-paper citation counts.
  3. OpenAlex, then Crossref         authors, venue, volume/pages, DOI.

Scholar has no public API and blocks automated traffic, so step 2 fails often.
When it does, the script falls back to whatever is already in publications.json
and leaves scholar.json untouched, so the site keeps the last good numbers
instead of going blank. Nothing is written unless at least one paper survives.

Usage
    python3 scripts/update_publications.py
    python3 scripts/update_publications.py --no-scholar     # metadata refresh only
    python3 scripts/update_publications.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
PUBS_FILE = DATA / "publications.json"
MANUAL_FILE = DATA / "publications.manual.json"
SCHOLAR_FILE = DATA / "scholar.json"

SCHOLAR_ID = os.environ.get("GOOGLE_SCHOLAR_ID", "8DvgcegAAAAJ")
# OpenAlex asks for a contact address; it buys you the faster, politer pool.
MAILTO = os.environ.get("OPENALEX_MAILTO", "z.zhenxuan24@imperial.ac.uk")
SELF = ["zhenxuan zhang", "z zhang", "zhang zhenxuan"]

UA = f"zhenxuan-homepage/1.0 (mailto:{MAILTO})"
TIMEOUT = 25


# ---------------------------------------------------------------- utilities

def log(msg: str) -> None:
    print(msg, flush=True)


def norm(title: str) -> str:
    """Lower-cased, punctuation-free title. The join key everywhere."""
    t = unicodedata.normalize("NFKD", title or "").lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return " ".join(t.split())


def slug(title: str) -> str:
    return "-".join(norm(title).split()[:6]) or "untitled"


def abbrev(full_name: str) -> str:
    """'Zhenxuan Zhang' -> 'Z. Zhang'."""
    parts = (full_name or "").split()
    if len(parts) < 2:
        return full_name or ""
    return " ".join(p[0] + "." for p in parts[:-1]) + " " + parts[-1]


def is_self(full_name: str) -> bool:
    n = norm(full_name)
    return n in SELF or (n.endswith("zhang") and n.startswith("zhenxuan"))


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


# ---------------------------------------------------------------- Scholar

def fetch_scholar(scholar_id: str):
    """Return (metrics_dict, [{'title','year','citations'}]) or (None, [])."""
    try:
        from scholarly import scholarly
    except ImportError:
        log("scholarly is not installed, skipping Google Scholar")
        return None, []

    try:
        log(f"Google Scholar: fetching profile {scholar_id}")
        author = scholarly.search_author_id(scholar_id)
        author = scholarly.fill(author, sections=["basics", "indices", "counts", "publications"])
    except Exception as exc:                                   # captcha, block, timeout
        log(f"Google Scholar failed ({type(exc).__name__}: {exc})")
        return None, []

    metrics = {
        "scholar_id": scholar_id,
        "ok": True,
        "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "citations": author.get("citedby"),
        "citations_5y": author.get("citedby5y"),
        "h_index": author.get("hindex"),
        "i10_index": author.get("i10index"),
        "cites_per_year": {str(k): v for k, v in (author.get("cites_per_year") or {}).items()},
        "message": "",
    }

    papers = []
    for pub in author.get("publications", []):
        bib = pub.get("bib", {}) or {}
        title = bib.get("title")
        if not title:
            continue
        year = bib.get("pub_year")
        papers.append({
            "title": title,
            "year": int(year) if str(year).isdigit() else None,
            "citations": pub.get("num_citations") or 0,
        })

    log(f"Google Scholar: {len(papers)} papers, {metrics['citations']} citations")
    return metrics, papers


# ---------------------------------------------------------------- OpenAlex

def openalex_by_title(title: str, session: requests.Session):
    try:
        r = session.get(
            "https://api.openalex.org/works",
            params={"search": title, "per-page": 5, "mailto": MAILTO},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        results = r.json().get("results", [])
    except Exception as exc:
        log(f"  OpenAlex lookup failed: {exc}")
        return None

    want = norm(title)
    for w in results:
        got = norm(w.get("display_name") or "")
        if got == want or (got and (got.startswith(want[:60]) or want.startswith(got[:60]))):
            return w
    return None


def openalex_by_author(author_id: str, session: requests.Session):
    """Optional discovery path. Set OPENALEX_AUTHOR_ID to switch it on."""
    works, cursor = [], "*"
    while cursor:
        try:
            r = session.get(
                "https://api.openalex.org/works",
                params={"filter": f"author.id:{author_id}", "per-page": 100,
                        "cursor": cursor, "mailto": MAILTO},
                timeout=TIMEOUT,
            )
            r.raise_for_status()
            page = r.json()
        except Exception as exc:
            log(f"OpenAlex author query failed: {exc}")
            break
        works.extend(page.get("results", []))
        cursor = page.get("meta", {}).get("next_cursor")
        time.sleep(0.2)
    log(f"OpenAlex: {len(works)} works for author {author_id}")
    return works


def from_openalex(w: dict) -> dict:
    """Map one OpenAlex work onto our publication shape."""
    authors = []
    for a in w.get("authorships", []) or []:
        name = (a.get("author") or {}).get("display_name") or ""
        short = abbrev(name)
        authors.append(f"**{short}**" if is_self(name) else short)
    if len(authors) > 14:
        authors = authors[:13] + ["et al."]

    loc = w.get("primary_location") or {}
    src = (loc.get("source") or {}).get("display_name") or ""
    if "arxiv" in src.lower():
        src = "arXiv"

    bib = w.get("biblio") or {}
    detail = ""
    if bib.get("volume"):
        detail = str(bib["volume"])
        if bib.get("issue"):
            detail += f"({bib['issue']})"
        if bib.get("first_page"):
            detail += f":{bib['first_page']}"
            if bib.get("last_page"):
                detail += f"–{bib['last_page']}"

    doi = (w.get("doi") or "").replace("https://doi.org/", "")
    wtype = w.get("type") or ""
    is_preprint = wtype == "preprint" or src == "arXiv"

    # an open-access PDF link lets fetch_thumbnails.py pull a figure later
    oa = w.get("best_oa_location") or {}
    oa_pdf = oa.get("pdf_url") or ""

    return {
        "title": w.get("display_name") or "",
        "authors": ", ".join(authors),
        "venue": src,
        "detail": detail,
        "year": w.get("publication_year"),
        "status": "preprint" if is_preprint else "published",
        "type": "preprint" if is_preprint else ("journal" if wtype == "article" else "conference"),
        "doi": doi,
        "url": w.get("doi") or (loc.get("landing_page_url") or ""),
        "oa_pdf": oa_pdf,
    }


# ---------------------------------------------------------------- Crossref

def crossref_by_title(title: str, session: requests.Session):
    try:
        r = session.get(
            "https://api.crossref.org/works",
            params={"query.bibliographic": title, "rows": 3, "mailto": MAILTO},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        items = r.json().get("message", {}).get("items", [])
    except Exception as exc:
        log(f"  Crossref lookup failed: {exc}")
        return None

    want = norm(title)
    for it in items:
        got = norm((it.get("title") or [""])[0])
        if got == want:
            authors = []
            for a in it.get("author", []) or []:
                name = f"{a.get('given','')} {a.get('family','')}".strip()
                short = abbrev(name)
                authors.append(f"**{short}**" if is_self(name) else short)
            year = None
            for key in ("published-print", "published-online", "issued"):
                parts = (it.get(key) or {}).get("date-parts") or [[]]
                if parts[0]:
                    year = parts[0][0]
                    break
            return {
                "title": (it.get("title") or [""])[0],
                "authors": ", ".join(authors),
                "venue": (it.get("container-title") or [""])[0],
                "detail": it.get("volume", ""),
                "year": year,
                "status": "published",
                "type": "journal" if it.get("type") == "journal-article" else "conference",
                "doi": it.get("DOI", ""),
                "url": it.get("URL", ""),
            }
    return None


# ---------------------------------------------------------------- merge

def build(args) -> tuple[list, dict | None]:
    manual = read_json(MANUAL_FILE, {})
    overrides = manual.get("overrides", {}) or {}
    excluded = {norm(t) for t in (manual.get("exclude") or [])}
    previous = {norm(p["title"]): p for p in read_json(PUBS_FILE, {}).get("items", [])}

    metrics, scholar_papers = (None, [])
    if not args.no_scholar:
        metrics, scholar_papers = fetch_scholar(SCHOLAR_ID)

    session = requests.Session()
    session.headers["User-Agent"] = UA

    discovered: dict[str, dict] = {}

    author_id = os.environ.get("OPENALEX_AUTHOR_ID", "").strip()
    if author_id:
        for w in openalex_by_author(author_id, session):
            item = from_openalex(w)
            if item["title"]:
                discovered[norm(item["title"])] = item

    for sp in scholar_papers:
        key = norm(sp["title"])
        if key in excluded:
            continue

        old = previous.get(key, {})
        item = discovered.get(key)

        # a paper we already resolved once needs no second lookup
        already_complete = bool(old.get("venue")) and bool(old.get("authors"))
        if item is None and not already_complete:
            log(f"  looking up: {sp['title'][:64]}")
            w = openalex_by_title(sp["title"], session)
            item = from_openalex(w) if w else crossref_by_title(sp["title"], session)
            time.sleep(0.3)

        if item is None:                       # nothing new; Scholar's bare facts only
            item = {"title": sp["title"], "year": sp["year"],
                    "status": "published", "type": "conference"}

        # previous run is the base; only non-empty new values are allowed to replace it
        merged = dict(old)
        merged.update({k: v for k, v in item.items() if v not in (None, "", [])})
        merged["citations"] = sp["citations"]
        merged.setdefault("year", sp["year"])
        discovered[key] = merged

    # keep anything a previous run found that this run did not see
    for key, old in previous.items():
        if key in excluded:
            continue
        if key not in discovered:
            discovered[key] = old

    items = []
    for key, item in discovered.items():
        if key in excluded:
            continue
        merged = dict(item)
        merged.setdefault("badges", [])
        merged.setdefault("thumb", "")
        merged.setdefault("note", "")
        merged["id"] = merged.get("id") or slug(merged["title"])
        merged.update(overrides.get(key, {}))          # hand edits win
        if not merged.get("year"):
            merged["year"] = datetime.now().year
        items.append(merged)

    # hand-written entries (under review, in press) are appended verbatim
    for entry in manual.get("entries", []) or []:
        if norm(entry.get("title", "")) in excluded:
            continue
        entry = dict(entry)
        entry["id"] = entry.get("id") or slug(entry["title"])
        items = [i for i in items if norm(i["title"]) != norm(entry["title"])]
        items.append(entry)

    items.sort(key=lambda p: (-(p.get("year") or 0), not p.get("highlight"), p["title"]))
    return items, metrics


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-scholar", action="store_true", help="skip Google Scholar entirely")
    ap.add_argument("--dry-run", action="store_true", help="print, do not write")
    args = ap.parse_args()

    items, metrics = build(args)

    if not items:
        log("No publications survived the merge. Leaving existing files alone.")
        return 1

    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "scholar+openalex" if metrics else "openalex",
        "note": "Rebuilt by scripts/update_publications.py. Hand edits belong in publications.manual.json.",
        "items": items,
    }

    counts = {}
    for i in items:
        counts[i.get("status", "?")] = counts.get(i.get("status", "?"), 0) + 1
    log(f"{len(items)} papers: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))

    if args.dry_run:
        log(json.dumps(payload, ensure_ascii=False, indent=2)[:3000])
        return 0

    PUBS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log(f"wrote {PUBS_FILE.relative_to(ROOT)}")

    if metrics:
        old = read_json(SCHOLAR_FILE, {})
        metrics["_readme"] = old.get("_readme", "")
        SCHOLAR_FILE.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log(f"wrote {SCHOLAR_FILE.relative_to(ROOT)}")
    else:
        log("No Scholar metrics this run. scholar.json left as it was.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
