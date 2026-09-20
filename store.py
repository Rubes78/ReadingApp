import json
import os
import tempfile
import threading

DATA_DIR = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data"))
_lock = threading.RLock()


def _path(name):
    return os.path.join(DATA_DIR, name)


def load(name, default):
    try:
        with open(_path(name), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(name, data):
    os.makedirs(DATA_DIR, exist_ok=True)
    with _lock:
        fd, tmp = tempfile.mkstemp(dir=DATA_DIR)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=1, ensure_ascii=False)
        os.replace(tmp, _path(name))


def mutate(name, default, fn):
    """Read-modify-write under one lock. fn receives the data and returns the value to hand back."""
    with _lock:
        data = load(name, default)
        result = fn(data)
        save(name, data)
        return result
