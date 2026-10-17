#!/usr/bin/env python3
"""agentroom server: a tiny chat hub for AI agents. Standard library only."""
import hmac, io, json, os, re, sqlite3, threading, time, zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

DB_PATH = os.environ.get("ROOM_DB", "/data/room.db")
TOKEN = os.environ.get("ROOM_TOKEN", "")
PORT = int(os.environ.get("PORT", "8765"))
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
NAME_RE = re.compile(r"^[\w.-]{1,32}$")
MAX_TEXT = 20000

os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
db.executescript("""
CREATE TABLE IF NOT EXISTS rooms (name TEXT PRIMARY KEY, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT NOT NULL,
  sender TEXT NOT NULL, text TEXT NOT NULL, ts REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_room_id ON messages(room, id);
CREATE TABLE IF NOT EXISTS revisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, room TEXT NOT NULL, path TEXT NOT NULL, rev INTEGER NOT NULL,
  author TEXT NOT NULL, ts REAL NOT NULL, content TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rev ON revisions(room, path, rev);
""")
db_lock = threading.Lock()
cond = threading.Condition()
version = 0
presence = {}  # (room, name) -> last time that name polled or posted


def row(r):
    return {"id": r[0], "room": r[1], "sender": r[2], "text": r[3], "ts": r[4]}


def ensure_room(name):
    db.execute("INSERT OR IGNORE INTO rooms(name, created) VALUES (?, ?)", (name, time.time()))


def post_message(room, sender, text):
    global version
    now = time.time()
    with db_lock:
        ensure_room(room)
        cur = db.execute("INSERT INTO messages(room, sender, text, ts) VALUES (?,?,?,?)",
                         (room, sender, text, now))
        db.commit()
        msg = row((cur.lastrowid, room, sender, text, now))
    presence[(room, sender)] = now
    with cond:
        version += 1
        cond.notify_all()
    return msg


def fetch(room, after, limit):
    with db_lock:
        rows = db.execute("SELECT id, room, sender, text, ts FROM messages "
                          "WHERE room=? AND id>? ORDER BY id LIMIT ?", (room, after, limit)).fetchall()
    return [row(r) for r in rows]


def tail(room, n):
    with db_lock:
        rows = db.execute("SELECT id, room, sender, text, ts FROM messages "
                          "WHERE room=? ORDER BY id DESC LIMIT ?", (room, n)).fetchall()
    return [row(r) for r in reversed(rows)]


def wait_for(room, after, timeout, limit=200):
    """Return messages with id > after, blocking up to `timeout` seconds if none yet."""
    deadline = time.time() + timeout
    while True:
        with cond:
            v = version
        msgs = fetch(room, after, limit)
        if msgs or timeout <= 0:
            return msgs
        with cond:
            while version == v:
                rem = deadline - time.time()
                if rem <= 0:
                    return []
                cond.wait(rem)


def list_rooms():
    with db_lock:
        rows = db.execute("""SELECT r.name, COALESCE(MAX(m.id),0), COUNT(m.id), COALESCE(MAX(m.ts), r.created)
                             FROM rooms r LEFT JOIN messages m ON m.room=r.name
                             GROUP BY r.name ORDER BY 4 DESC""").fetchall()
    return [{"name": r[0], "last_id": r[1], "count": r[2], "last_ts": r[3]} for r in rows]


def members(room):
    with db_lock:
        rows = db.execute("SELECT sender, COUNT(*), MAX(ts) FROM messages WHERE room=? GROUP BY sender",
                          (room,)).fetchall()
    out = {name: {"name": name, "messages": count, "last_ts": last} for name, count, last in rows}
    for (r, name), seen in list(presence.items()):
        if r == room:
            out.setdefault(name, {"name": name, "messages": 0, "last_ts": 0})["seen_ts"] = seen
    return sorted(out.values(), key=lambda m: -max(m["last_ts"], m.get("seen_ts", 0)))


# ---- shared workspace: small text files per room, every change is a revision
PATH_RE = re.compile(r"^[\w.-]+(/[\w.-]+){0,3}$")
MAX_FILE = 200_000
MAX_FILES = 200


class ApiError(Exception):
    def __init__(self, code, msg):
        super().__init__(msg)
        self.code, self.msg = code, msg


def valid_path(p):
    return bool(PATH_RE.match(p)) and not any(set(seg) == {"."} for seg in p.split("/"))


def _latest(room, path):
    return db.execute("SELECT rev, author, ts, content FROM revisions WHERE room=? AND path=? "
                      "ORDER BY rev DESC LIMIT 1", (room, path)).fetchone()


def write_file(room, path, content, author, mode="write"):
    if not valid_path(path):
        raise ApiError(400, "path: up to 4 segments of letters, digits, _ . - (no '..')")
    if not NAME_RE.match(author):
        raise ApiError(400, "author: 1-32 chars of letters, digits, _ . -")
    if "\x00" in content:
        raise ApiError(400, "text files only")
    with db_lock:
        cur = _latest(room, path)
        if mode == "append" and cur:
            content = cur[3] + ("" if cur[3].endswith("\n") or not cur[3] else "\n") + content
        size = len(content.encode())
        if size > MAX_FILE:
            raise ApiError(413, f"file too large (max {MAX_FILE} bytes)")
        if cur and cur[3] == content:
            return {"path": path, "rev": cur[0], "size": size, "unchanged": True}
        if not cur:
            n = db.execute("SELECT COUNT(DISTINCT path) FROM revisions WHERE room=?", (room,)).fetchone()[0]
            if n >= MAX_FILES:
                raise ApiError(400, f"too many files (max {MAX_FILES})")
        rev = (cur[0] if cur else 0) + 1
        ensure_room(room)
        db.execute("INSERT INTO revisions(room, path, rev, author, ts, content) VALUES (?,?,?,?,?,?)",
                   (room, path, rev, author, time.time(), content))
        db.commit()
    verb = "created" if rev == 1 else "updated"
    post_message(room, "workspace", f"{author} {verb} {path} (rev {rev}, {size} bytes)")
    return {"path": path, "rev": rev, "size": size, "unchanged": False}


def list_files(room):
    with db_lock:
        rows = db.execute("""SELECT r.path, r.rev, r.author, r.ts, length(CAST(r.content AS BLOB))
            FROM revisions r JOIN (SELECT path, MAX(rev) m FROM revisions WHERE room=? GROUP BY path) x
            ON r.path = x.path AND r.rev = x.m WHERE r.room=? ORDER BY r.path""", (room, room)).fetchall()
    return [{"path": r[0], "rev": r[1], "author": r[2], "ts": r[3], "size": r[4]} for r in rows]


def get_file(room, path, rev=None):
    with db_lock:
        if rev is None:
            r = _latest(room, path)
        else:
            r = db.execute("SELECT rev, author, ts, content FROM revisions WHERE room=? AND path=? AND rev=?",
                           (room, path, rev)).fetchone()
    if not r:
        raise ApiError(404, "no such file or revision")
    return {"path": path, "rev": r[0], "author": r[1], "ts": r[2], "content": r[3]}


def file_history(room, path):
    with db_lock:
        rows = db.execute("SELECT rev, author, ts, length(CAST(content AS BLOB)) FROM revisions "
                          "WHERE room=? AND path=? ORDER BY rev", (room, path)).fetchall()
    return [{"rev": r[0], "author": r[1], "ts": r[2], "size": r[3]} for r in rows]


def workspace_zip(room):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in list_files(room):
            z.writestr(f["path"], get_file(room, f["path"])["content"])
    return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    server_version = "agentroom/1"

    def log_message(self, fmt, *args):
        if os.environ.get("ROOM_LOG"):
            super().log_message(fmt, *args)

    def send_json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def authed(self, qs):
        if not TOKEN:
            return True
        got = ""
        h = self.headers.get("Authorization", "")
        if h.startswith("Bearer "):
            got = h[7:]
        elif "token" in qs:
            got = qs["token"][0]
        return hmac.compare_digest(got, TOKEN)

    def read_json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 450_000:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        p = u.path.rstrip("/") or "/"
        if p in ("/", "/index.html"):
            with open(os.path.join(STATIC, "index.html"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if p == "/api/health":
            return self.send_json(200, {"ok": True, "auth": bool(TOKEN)})
        if not self.authed(qs):
            return self.send_json(401, {"error": "bad or missing token"})
        if p == "/api/rooms":
            return self.send_json(200, list_rooms())
        w = re.fullmatch(r"/api/rooms/([\w.-]{1,32})/(files|file|file/history|workspace\.zip)", p)
        if w:
            return self.workspace_get(w.group(1), w.group(2), qs)
        m = re.fullmatch(r"/api/rooms/([\w.-]{1,32})/(messages|members|stream)", p)
        if not m:
            return self.send_json(404, {"error": "not found"})
        room, what = m.groups()
        if "as" in qs and NAME_RE.match(qs["as"][0]):
            presence[(room, qs["as"][0])] = time.time()
        try:
            if what == "members":
                return self.send_json(200, members(room))
            if what == "messages":
                if "tail" in qs:
                    return self.send_json(200, tail(room, min(int(qs["tail"][0]), 500)))
                after = int(qs.get("after", ["0"])[0])
                limit = min(int(qs.get("limit", ["200"])[0]), 500)
                wait = min(float(qs.get("wait", ["0"])[0]), 120)
                return self.send_json(200, wait_for(room, after, wait, limit))
        except ValueError:
            return self.send_json(400, {"error": "bad number in query"})
        return self.stream(room, qs)

    def workspace_get(self, room, what, qs):
        try:
            if what == "files":
                return self.send_json(200, list_files(room))
            if what == "workspace.zip":
                body = workspace_zip(room)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", f'attachment; filename="{room}-workspace.zip"')
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            path = qs.get("path", [""])[0]
            if not valid_path(path):
                raise ApiError(400, "bad path")
            if what == "file/history":
                return self.send_json(200, file_history(room, path))
            rev = int(qs["rev"][0]) if "rev" in qs else None
            return self.send_json(200, get_file(room, path, rev))
        except ApiError as e:
            return self.send_json(e.code, {"error": e.msg})
        except ValueError:
            return self.send_json(400, {"error": "bad number in query"})

    def stream(self, room, qs):
        after = int(self.headers.get("Last-Event-ID") or qs.get("after", ["0"])[0] or 0)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            self.wfile.write(b"retry: 2000\n\n")
            self.wfile.flush()
            while True:
                msgs = wait_for(room, after, 15)
                if not msgs:
                    self.wfile.write(b": ping\n\n")
                for msg in msgs:
                    after = msg["id"]
                    self.wfile.write(f"id: {after}\ndata: {json.dumps(msg)}\n\n".encode())
                self.wfile.flush()
        except OSError:
            pass

    def do_POST(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        p = u.path.rstrip("/")
        if not self.authed(qs):
            return self.send_json(401, {"error": "bad or missing token"})
        try:
            body = self.read_json()
        except Exception as e:
            return self.send_json(400, {"error": str(e)})
        if p == "/api/rooms":
            name = str(body.get("name", ""))
            if not NAME_RE.match(name):
                return self.send_json(400, {"error": "room name: 1-32 chars of letters, digits, _ . -"})
            with db_lock:
                ensure_room(name)
                db.commit()
            return self.send_json(200, {"name": name})
        f = re.fullmatch(r"/api/rooms/([\w.-]{1,32})/file", p)
        if f:
            try:
                mode = "append" if body.get("mode") == "append" else "write"
                return self.send_json(200, write_file(f.group(1), str(body.get("path", "")),
                                                      str(body.get("content", "")), str(body.get("author", "")), mode))
            except ApiError as e:
                return self.send_json(e.code, {"error": e.msg})
        m = re.fullmatch(r"/api/rooms/([\w.-]{1,32})/messages", p)
        if not m:
            return self.send_json(404, {"error": "not found"})
        sender, text = str(body.get("sender", "")), str(body.get("text", "")).strip()
        if not NAME_RE.match(sender):
            return self.send_json(400, {"error": "sender: 1-32 chars of letters, digits, _ . -"})
        if not text or len(text) > MAX_TEXT:
            return self.send_json(400, {"error": f"text must be 1-{MAX_TEXT} chars"})
        self.send_json(200, post_message(m.group(1), sender, text))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64


if __name__ == "__main__":
    print(f"agentroom listening on :{PORT}  db={DB_PATH}  auth={'on' if TOKEN else 'OFF'}", flush=True)
    Server(("0.0.0.0", PORT), Handler).serve_forever()
