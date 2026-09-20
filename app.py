import glob
import os
import uuid
from datetime import datetime, timezone

from flask import Flask, jsonify, render_template, request

import ai
import goodreads
import store

app = Flask(__name__)
SHELFMARK_URL = os.environ.get("SHELFMARK_URL", "http://holobooks.local:8085")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def import_csv(text):
    books = goodreads.parse(text)
    store.save("library.json", {"imported": now(), "books": books})
    return books


def books():
    lib = store.load("library.json", None)
    if lib is None:  # first run: pick up a CSV dropped into the data dir
        for f in sorted(glob.glob(os.path.join(store.DATA_DIR, "*.csv"))):
            with open(f, encoding="utf-8-sig") as fh:
                return import_csv(fh.read())
        return []
    return lib["books"]


def context(b):
    """Library profile plus the reader's explicit preferences and feedback, as prompt text."""
    return goodreads.profile_text(goodreads.build_profile(b)) + ai.prefs_text(
        store.load("prefs.json", {}), store.load("feedback.json", []), store.load("list.json", []))


def err(msg, code=400):
    return jsonify(error=msg), code


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/config")
def config():
    books()  # triggers the first-run CSV import
    lib = store.load("library.json", {})
    return jsonify(backend=ai.backend(), model=ai.MODEL, shelfmark_url=SHELFMARK_URL,
                   imported=lib.get("imported"), book_count=len(lib.get("books", [])))


@app.post("/api/import")
def do_import():
    f = request.files.get("file")
    if not f:
        return err("No file uploaded")
    text = f.read().decode("utf-8-sig", errors="replace")
    if "Exclusive Shelf" not in text.split("\n", 1)[0]:
        return err("That doesn't look like a Goodreads library export")
    os.makedirs(store.DATA_DIR, exist_ok=True)
    with open(os.path.join(store.DATA_DIR, "goodreads_library_export.csv"), "w", encoding="utf-8") as out:
        out.write(text)
    return jsonify(imported=len(import_csv(text)))


@app.get("/api/library")
def library():
    return jsonify(books())


@app.get("/api/profile")
def profile():
    p = goodreads.build_profile(books())
    p.pop("to_read")
    return jsonify(p)


# --- recommendations -------------------------------------------------------

@app.get("/api/recs")
def recs():
    return jsonify([r for r in store.load("recs.json", []) if not r.get("dismissed")])


@app.post("/api/recommend")
def recommend():
    b = books()
    if not b:
        return err("Import your Goodreads CSV first")
    body = request.get_json(silent=True) or {}
    try:
        found, stats = ai.recommend(goodreads.profile_text(goodreads.build_profile(b)),
                                    {k for x in b for k in (x["key"], x.get("skey")) if k}, (body.get("focus") or "").strip()[:200],
                                    min(int(body.get("count", 12)), 25))
    except ai.AIError as e:
        return err(str(e), 502)
    seen = {goodreads.title_key(r["title"], r["author"]) for r in store.load("recs.json", [])}
    added = []
    for r in found:
        if goodreads.title_key(r["title"], r["author"]) in seen:
            continue
        r.update(id=uuid.uuid4().hex[:8], created=now(), focus=body.get("focus") or "", dismissed=False)
        added.append(r)
    store.mutate("recs.json", [], lambda d: d.extend(added))
    return jsonify(added=len(added), **stats)


@app.delete("/api/recs/<rid>")
def dismiss_rec(rid):
    def f(d):
        for r in d:
            if r["id"] == rid:
                r["dismissed"] = True  # kept, so it is never re-suggested
                return r
    hit = store.mutate("recs.json", [], f)
    if hit:
        add_feedback(hit["title"], hit["author"], "dislike")
    return "", 204


def add_feedback(title, author, verdict):
    k = goodreads.title_key(title, author)

    def f(d):
        d[:] = [x for x in d if goodreads.title_key(x["title"], x["author"]) != k]  # latest verdict wins
        d.append({"title": title, "author": author, "verdict": verdict, "at": now()})
    store.mutate("feedback.json", [], f)
    if verdict == "dislike":  # hide it everywhere it is currently shown
        store.mutate("prioritized.json", {"items": []}, lambda d: d.__setitem__(
            "items", [i for i in d["items"] if goodreads.title_key(i["title"], i["author"]) != k]))
        store.mutate("recs.json", [], lambda d: [r.__setitem__("dismissed", True) for r in d
                                                 if goodreads.title_key(r["title"], r["author"]) == k])


@app.get("/api/prefs")
def get_prefs():
    return jsonify(store.load("prefs.json", {"more": "", "less": "", "notes": ""}))


@app.put("/api/prefs")
def put_prefs():
    body = request.get_json(silent=True) or {}
    prefs = {k: str(body.get(k, ""))[:400].strip() for k in ("more", "less", "notes")}
    store.save("prefs.json", prefs)
    return jsonify(prefs)


@app.post("/api/feedback")
def feedback():
    body = request.get_json(silent=True) or {}
    if body.get("verdict") not in ("like", "dislike") or not body.get("title") or not body.get("author"):
        return err("title, author and verdict (like|dislike) required")
    add_feedback(body["title"], body["author"], body["verdict"])
    return "", 204


@app.get("/api/prioritized")
def prioritized():
    return jsonify(store.load("prioritized.json", {"created": None, "items": []}))


@app.post("/api/prioritize")
def prioritize():
    b = books()
    to_read = [{"id": x["id"], "text": f'{x["title"]} — {x["author"]}'} for x in b if x["status"] == "to-read"]
    if not to_read:
        return err("No to-read books found")
    try:
        ranked = ai.prioritize(context(b), to_read)
    except ai.AIError as e:
        return err(str(e), 502)
    by_id = {x["id"]: x for x in b}
    items = [{"id": str(r["id"]), "rank": r.get("rank"), "reason": r.get("reason", ""), "genre": r.get("genre", ""),
              "title": by_id[str(r["id"])]["title"], "author": by_id[str(r["id"])]["author"]} for r in ranked]
    items.sort(key=lambda i: (i["rank"] is None, i["rank"] or 0))
    store.save("prioritized.json", {"created": now(), "items": items})
    return jsonify(count=len(items))


# --- reading list ----------------------------------------------------------

@app.get("/api/list")
def get_list():
    return jsonify(store.load("list.json", []))


@app.post("/api/list")
def add_to_list():
    body = request.get_json(silent=True) or {}
    title, author = (body.get("title") or "").strip(), (body.get("author") or "").strip()
    if not title or not author:
        return err("title and author required")
    item = {"id": uuid.uuid4().hex[:8], "title": title, "author": author, "status": "queued", "note": "",
            "cover": body.get("cover"), "reason": body.get("reason", ""), "gr_id": body.get("gr_id"),
            "ol_key": body.get("ol_key"), "added": now()}

    def f(d):
        if any(goodreads.title_key(i["title"], i["author"]) == goodreads.title_key(title, author) for i in d):
            return False
        d.append(item)
        return True

    return (jsonify(item), 201) if store.mutate("list.json", [], f) else err("Already on your list", 409)


@app.patch("/api/list/<iid>")
def patch_item(iid):
    body = request.get_json(silent=True) or {}

    def f(d):
        for i in d:
            if i["id"] == iid:
                for k in ("status", "note"):
                    if k in body:
                        i[k] = body[k]
                return True
        return False

    return ("", 204) if store.mutate("list.json", [], f) else err("Not found", 404)


@app.post("/api/list/<iid>/move")
def move_item(iid):
    where = (request.get_json(silent=True) or {}).get("to")

    def f(d):
        idx = next((n for n, i in enumerate(d) if i["id"] == iid), None)
        if idx is None:
            return False
        item = d.pop(idx)
        d.insert({"top": 0, "up": max(idx - 1, 0), "down": idx + 1}.get(where, idx), item)
        return True

    return ("", 204) if store.mutate("list.json", [], f) else err("Not found", 404)


@app.delete("/api/list/<iid>")
def delete_item(iid):
    store.mutate("list.json", [], lambda d: d.__setitem__(slice(None), [i for i in d if i["id"] != iid]))
    return "", 204


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
