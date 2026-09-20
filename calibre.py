"""Read-only view of the Calibre library (metadata.db) so books you already own can be marked and excluded."""
import os
import sqlite3

import goodreads

DB_PATH = os.environ.get("CALIBRE_DB", "/calibre/metadata.db")
_cache = {"mtime": None, "books": [], "keys": set()}

QUERY = """
SELECT b.id, b.title, b.series_index,
       (SELECT s.name FROM books_series_link l JOIN series s ON s.id = l.series WHERE l.book = b.id),
       (SELECT group_concat(a.name, ' & ') FROM books_authors_link l JOIN authors a ON a.id = l.author WHERE l.book = b.id),
       (SELECT group_concat(t.name, ', ') FROM books_tags_link l JOIN tags t ON t.id = l.tag WHERE l.book = b.id)
FROM books b ORDER BY b.title
"""


def _load():
    """Re-read the database only when the file changed. Returns [] if the library is not mounted."""
    try:
        mtime = os.path.getmtime(DB_PATH)
    except OSError:
        return _cache["books"] if _cache["mtime"] else []
    if mtime == _cache["mtime"]:
        return _cache["books"]
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        rows = con.execute(QUERY).fetchall()
    finally:
        con.close()
    books = [{"id": r[0], "title": r[1], "series": r[3], "series_num": r[2] if r[3] else None,
              "author": (r[4] or "").replace("|", ","), "tags": r[5] or ""} for r in rows]
    keys = set()
    for b in books:
        for a in b["author"].split(" & "):  # co-authored books match on any author
            keys.add(goodreads.title_key(b["title"], a))
    _cache.update(mtime=mtime, books=books, keys=keys)
    return books


def books():
    return _load()


def keys():
    _load()
    return _cache["keys"]


def owns(title, author):
    ks = keys()
    return any(goodreads.title_key(title, a) in ks for a in (author or "").replace(" and ", " & ").split(" & "))
