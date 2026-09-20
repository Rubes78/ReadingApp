"""Claude-backed recommendations and to-read prioritisation, with Open Library verification."""
import json
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor

import requests

from goodreads import norm, series_key, title_key

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5")

SYSTEM = (
    "You are a well-read book advisor for one specific reader. You are given their Goodreads library profile. "
    "Ground every suggestion in their actual taste; cite books from their library. "
    "Only suggest real, published books you are confident exist, with the correct author. "
    "Note: the 'trusted-author' shelf is applied loosely, so weigh it against ratings and abandoned books. "
    "Respond with exactly one JSON array and nothing else: no drafts, no corrections, no prose, no code fences."
)


class AIError(Exception):
    pass


def backend():
    """'api' when an Anthropic key is set, 'cli' when Claude Code is installed (uses its existing login), else None."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "api"
    return "cli" if _claude_bin() else None


def _claude_bin():
    return os.environ.get("CLAUDE_BIN") or shutil.which("claude")


def _via_api(user, max_tokens):
    r = requests.post(
        API_URL,
        headers={"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": MODEL, "max_tokens": max_tokens, "system": SYSTEM,
              "messages": [{"role": "user", "content": user}]},
        timeout=180,
    )
    if r.status_code != 200:
        raise AIError(f"Claude API {r.status_code}: {r.text[:300]}")
    return "".join(b.get("text", "") for b in r.json().get("content", []))


def _via_cli(user, max_tokens):
    """Headless Claude Code: no tools, no saved session, run from a neutral cwd so no project context is loaded."""
    cmd = [_claude_bin(), "-p", "--output-format", "text", "--tools", "", "--no-session-persistence",
           "--disable-slash-commands", "--system-prompt", SYSTEM]
    if os.environ.get("CLAUDE_MODEL"):
        cmd += ["--model", MODEL]
    try:
        r = subprocess.run(cmd, input=user, capture_output=True, text=True, timeout=600, cwd=tempfile.gettempdir())
    except subprocess.TimeoutExpired:
        raise AIError("Claude Code timed out after 10 minutes")
    if r.returncode != 0:
        raise AIError(f"Claude Code failed ({r.returncode}): {(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def _call(user, max_tokens=8000):
    b = backend()
    if not b:
        raise AIError("No AI backend: set ANTHROPIC_API_KEY or install/log in to Claude Code")
    text = (_via_api if b == "api" else _via_cli)(user, max_tokens)
    return _extract_array(text)


def _extract_array(text):
    """Longest JSON array of objects in the text. The CLI sometimes emits a draft array, a 'Correction:', then the real one,
    and wraps output in prose or fences; picking the longest array handles all of that."""
    dec, i, found = json.JSONDecoder(), text.find("["), []
    while i != -1:
        try:
            val, end = dec.raw_decode(text, i)
            if isinstance(val, list) and val and all(isinstance(v, dict) for v in val):
                found.append(val)
                i = text.find("[", end)  # skip past this array so nested lists are not re-read
                continue
        except json.JSONDecodeError:
            pass
        i = text.find("[", i + 1)
    if not found:
        raise AIError(f"Claude returned no usable JSON array. Output began: {text.strip()[:200]!r}")
    return max(found, key=len)


def verify(rec):
    """Confirm the book exists via Open Library and attach cover/year/pages. Returns rec, or None if it is not found."""
    try:
        r = requests.get(
            "https://openlibrary.org/search.json",
            params={"title": rec["title"], "author": rec["author"], "limit": 5,
                    "fields": "key,title,author_name,first_publish_year,cover_i,number_of_pages_median"},
            timeout=15,
        )
        r.raise_for_status()
        docs = r.json().get("docs", [])
    except requests.RequestException:
        rec["verified"] = None  # Open Library unreachable: keep, but flag
        return rec
    last = norm(rec["author"].split()[-1])
    want = norm(rec["title"])
    for d in docs:
        if last in norm(" ".join(d.get("author_name", []))) and (want in norm(d.get("title", "")) or norm(d.get("title", "")) in want):
            rec.update(verified=True, year=d.get("first_publish_year"), pages=d.get("number_of_pages_median"),
                       cover=f'https://covers.openlibrary.org/b/id/{d["cover_i"]}-M.jpg' if d.get("cover_i") else None,
                       ol_key=d.get("key"))
            return rec
    return None


def recommend(profile_text, owned_keys, focus="", count=12, rounds=3):
    """Ask Claude for picks, drop what is owned/unusable/unverifiable, and top up in further rounds if it under-delivers."""
    picks, seen = [], set()
    stats = {"asked": 0, "already_owned": 0, "unverified": 0, "rounds": 0}

    def owned(r):
        return title_key(r["title"], r["author"]) in owned_keys or \
            series_key(r.get("series"), str(r.get("series_num") or ""), r["author"]) in owned_keys

    def usable(r):  # drop the model's discarded drafts ("Placeholder.", "Skip."): a real pick explains itself and cites the library
        return r.get("title") and r.get("author") and len(str(r.get("why", ""))) >= 30 and r.get("similar_to")

    for _ in range(rounds):
        need = count - len(picks)
        if need <= 0:
            break
        stats["rounds"] += 1
        user = (
            f"{profile_text}\n\n---\nRecommend {need + 10} books this reader has NOT already got in their library. "
            + (f"Focus on: {focus}. " if focus else "Mix comfort-zone picks with a few adjacent stretches. ")
            + "Prefer first books of series, or standalones, unless a later entry is the obvious starting point. "
            "Do not suggest another edition or alternate (UK/US) title of a book already in the library, or any book from a series "
            "entry they have already read. No placeholders, commentary or self-corrections in any field. "
            "Return a JSON array of objects with keys: title, author, series (series name only, or null), series_num (integer or null), "
            "why (1-2 sentences tied to specific books from their library), similar_to (array of up to 3 titles from their library)."
            + (f"\n\nAlready proposed, do not repeat: {'; '.join(sorted(seen))}" if seen else "")
        )
        raw = _call(user)
        stats["asked"] += len(raw)
        good = [r for r in raw if usable(r)]
        fresh = [r for r in good if not owned(r) and title_key(r["title"], r["author"]) not in seen]
        stats["already_owned"] += len(good) - len(fresh)
        for r in raw:
            if r.get("title") and r.get("author"):
                seen.add(f'{r["title"]} ({r["author"]})')
        with ThreadPoolExecutor(6) as ex:
            checked = [r for r in ex.map(verify, fresh) if r]
        stats["unverified"] += len(fresh) - len(checked)
        picks += [r for r in checked if all(title_key(r["title"], r["author"]) != title_key(p["title"], p["author"]) for p in picks)]
    return picks[:count], stats


def prefs_text(prefs, feedback, listed):
    """Explicit reader steering. Placed after the library profile and told to win over it."""
    out = []
    if prefs.get("more"):
        out.append(f"- Wants MORE of: {prefs['more']}")
    if prefs.get("less"):
        out.append(f"- Wants LESS of: {prefs['less']}. Only include such a book if it is exceptional AND fits what they want more of.")
    if prefs.get("notes"):
        out.append(f"- Notes: {prefs['notes']}")
    likes = [f'{f["title"]} — {f["author"]}' for f in feedback if f["verdict"] == "like"][-40:]
    likes += [f'{i["title"]} — {i["author"]}' for i in listed][-40:]
    dislikes = [f'{f["title"]} — {f["author"]}' for f in feedback if f["verdict"] == "dislike"][-60:]
    if likes:
        out.append("- Liked / put on their list (find more like these): " + "; ".join(likes))
    if dislikes:
        out.append("- Rejected (never suggest; avoid similar): " + "; ".join(dislikes))
    if not out:
        return ""
    return ("\n\nEXPLICIT READER PREFERENCES. These override any conflicting pattern in the library history above, "
            "e.g. if the library is mostly SF but they want less SF, obey the preference:\n" + "\n".join(out))


def prioritize(profile_text, to_read, top=25):
    """Rank the reader's own to-read queue against their taste. to_read: [{id, text}]."""
    listing = "\n".join(f'{t["id"]}\t{t["text"]}' for t in to_read)
    user = (
        f"{profile_text}\n\n---\nBelow is the reader's to-read queue as 'id<TAB>title — author'. "
        f"Pick the {top} they would most enjoy reading NEXT, best first, judging by their taste, "
        "authors they have loved or abandoned, and how well each fits their current interests. "
        "Obey the explicit reader preferences strictly: books in categories they want less of must not fill the top ranks. "
        "Also list up to 8 items they will probably dislike (rank null) with a reason.\n\n"
        f"{listing}\n\nReturn a JSON array of objects: id (exact id from the list), rank (1-based integer, or null for skip), genre (2-3 word label, e.g. 'ancient historical fiction' or 'hard SF'), reason (one sentence)."
    )
    valid = {t["id"] for t in to_read}
    return [r for r in _call(user, 6000) if isinstance(r, dict) and str(r.get("id")) in valid]
