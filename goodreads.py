"""Goodreads library-export CSV -> clean, de-duplicated book records + taste profile."""
import csv
import io
import re
import statistics
import unicodedata
from collections import Counter, defaultdict

SERIES_RE = re.compile(r"\s*\(([^()]*?),?\s*#\s*([\d.]+)(?:-[\d.]+)?\)\s*$")
STATUS_ORDER = {"read": 0, "reading": 1, "dnf": 2, "to-read": 3}
POS_RE = re.compile(r"to-read \(#(\d+)\)")


def norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def title_key(title, author):
    """Series-agnostic key used to match recommendations against the library."""
    base = SERIES_RE.sub("", title or "")
    return f"{norm(base)}|{norm(author.split()[-1] if author else '')}"


def series_key(series, num, author):
    if not (series and num):
        return None
    return f"s:{norm(series)}:{num}|{norm(author.split()[-1] if author else '')}"


def _clean_author(name):
    return " ".join((name or "").split())


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _year(row):
    for k in ("Original Publication Year", "Year Published"):
        if row.get(k, "").strip().isdigit():
            return int(row[k])
    return None


def _record(row):
    title_full = " ".join(row["Title"].split())
    m = SERIES_RE.search(title_full)
    series, num, base = (m.group(1).strip(), m.group(2), title_full[: m.start()]) if m else (None, None, title_full)
    author = _clean_author(row["Author"])
    shelves = [s.strip() for s in row.get("Bookshelves", "").split(",") if s.strip()]
    excl = row.get("Exclusive Shelf", "")
    if excl == "currently-reading":
        status = "reading"
    elif "not-going-to-finish" in shelves:
        status = "dnf"
    elif excl == "read":
        status = "read"
    else:
        status = "to-read"
    pos = POS_RE.search(row.get("Bookshelves with positions", ""))
    pages = row.get("Number of Pages", "").strip()
    return {
        "id": row["Book Id"],
        "title": base.strip(),
        "series": series,
        "series_num": num,
        "author": author,
        "pages": int(pages) if pages.isdigit() else None,
        "year": _year(row),
        "rating": _num(row.get("My Rating")),
        "shelves": shelves,
        "status": status,
        "date_read": row.get("Date Read", ""),
        "date_added": row.get("Date Added", ""),
        "to_read_pos": int(pos.group(1)) if pos else None,
        "_key": f"s:{norm(series)}:{num}|{norm(author.split()[-1])}" if series else f"t:{norm(base)}|{norm(author.split()[-1])}",
    }


def parse(text):
    """Return de-duplicated books. Editions of the same title are merged: finished beats reading beats DNF beats to-read."""
    rows = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    merged = {}
    for row in rows:
        if not row.get("Title"):
            continue
        r = _record(row)
        cur = merged.get(r["_key"])
        if cur is None:
            merged[r["_key"]] = r
            continue
        best, other = (r, cur) if STATUS_ORDER[r["status"]] < STATUS_ORDER[cur["status"]] else (cur, r)
        best["rating"] = max(best["rating"], other["rating"])
        best["shelves"] = sorted(set(best["shelves"]) | set(other["shelves"]))
        best["date_read"] = max(best["date_read"], other["date_read"])
        best["pages"] = best["pages"] or other["pages"]
        best["year"] = best["year"] or other["year"]
        merged[r["_key"]] = best
    books = list(merged.values())
    for b in books:
        b["key"] = title_key(b["title"], b["author"])
        b["skey"] = b["_key"] if b["_key"].startswith("s:") else None  # series+number key: catches alternate titles
        del b["_key"]
    return books


def build_profile(books):
    """Aggregate what the library says about taste. Shelves are a stronger signal than stars (most books are unrated)."""
    by = defaultdict(lambda: {"read": 0, "dnf": 0, "to_read": 0, "ratings": [], "trusted": False})
    for b in books:
        a = by[b["author"]]
        if b["status"] == "read":
            a["read"] += 1
        elif b["status"] == "dnf":
            a["dnf"] += 1
        elif b["status"] == "to-read":
            a["to_read"] += 1
        if b["rating"]:
            a["ratings"].append(b["rating"])
        if "trusted-author" in b["shelves"]:
            a["trusted"] = True
    authors = [
        {"author": n, "read": v["read"], "dnf": v["dnf"], "to_read": v["to_read"], "trusted": v["trusted"],
         "avg": round(statistics.mean(v["ratings"]), 1) if v["ratings"] else None}
        for n, v in by.items() if v["read"] >= 2 or (v["trusted"] and v["read"])
    ]
    authors.sort(key=lambda a: (-a["read"], a["author"]))

    def show(b):
        return f'{b["title"]} — {b["author"]}'

    read = [b for b in books if b["status"] == "read"]
    rated = [b for b in read if b["rating"]]
    series = defaultdict(lambda: {"read": 0, "open": 0, "author": ""})
    for b in books:
        if b["series"]:
            s = series[b["series"]]
            s["author"] = b["author"]
            if b["status"] in ("read", "dnf"):
                s["read"] += 1
            elif b["status"] in ("to-read", "reading"):
                s["open"] += 1
    return {
        "counts": dict(Counter(b["status"] for b in books)),
        "top_authors": authors[:40],
        "loved": [show(b) for b in rated if b["rating"] >= 5][:60],
        "disliked": [show(b) for b in rated if b["rating"] <= 2][:30],
        "dnf": [show(b) for b in books if b["status"] == "dnf"],
        "reading": [show(b) for b in books if b["status"] == "reading"],
        "recent": [show(b) for b in sorted(read, key=lambda b: b["date_read"], reverse=True) if b["date_read"]][:25],
        "series_in_progress": [f'{n} ({s["author"]}): {s["read"]} done, {s["open"]} queued'
                               for n, s in series.items() if s["read"] and s["open"]],
        "shelves": dict(Counter(s for b in books for s in b["shelves"]).most_common(20)),
        "median_pages_finished": int(statistics.median([b["pages"] for b in read if b["pages"]] or [0])),
        "to_read": [{"id": b["id"], "text": show(b), "pos": b["to_read_pos"]}
                    for b in sorted((b for b in books if b["status"] == "to-read"), key=lambda b: -(b["to_read_pos"] or 0))],
    }


def profile_text(p):
    """Compact prompt-ready rendering of the profile."""
    lines = [f'Library: {p["counts"]}. Median finished length: {p["median_pages_finished"]} pages.',
             "Custom shelves the reader uses (counts): " + ", ".join(f"{k}={v}" for k, v in p["shelves"].items()),
             "Shelf meanings: trusted-author = will read anything by them; long/short = length; "
             "not-going-to-finish = abandoned; extreme/corrosive/cooldown-needed = intense books needing a break.",
             "", "Most-read authors (n read, avg rating, * = trusted):"]
    lines += [f'- {a["author"]}{"*" if a["trusted"] else ""}: {a["read"]} read'
              + (f', avg {a["avg"]}' if a["avg"] else "") + (f', {a["dnf"]} abandoned' if a["dnf"] else "")
              for a in p["top_authors"]]
    lines += ["", "5-star books:"] + [f"- {x}" for x in p["loved"]]
    lines += ["", "Rated 1-2 stars:"] + [f"- {x}" for x in p["disliked"]]
    lines += ["", "Abandoned (did not finish):"] + [f"- {x}" for x in p["dnf"]]
    lines += ["", "Recently finished:"] + [f"- {x}" for x in p["recent"]]
    lines += ["", "Currently reading:"] + [f"- {x}" for x in p["reading"]]
    lines += ["", "Series partly read with more queued:"] + [f"- {x}" for x in p["series_in_progress"]]
    lines += ["", "To-read queue (shows current interests):"] + [f'- {t["text"]}' for t in p["to_read"]]
    return "\n".join(lines)
