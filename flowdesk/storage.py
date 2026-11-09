import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone

CATEGORIES = ("Комп’ютерна допомога", "Налаштування мережі", "Сервіс техніки", "Консультація")
STATUSES = {"new": "Нова", "working": "У роботі", "waiting": "Очікує", "resolved": "Вирішена", "closed": "Закрита"}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class Database:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL, category TEXT NOT NULL, name TEXT NOT NULL,
                    contact TEXT NOT NULL, description TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'new' CHECK(status IN ('new','working','closed')),
                    notes TEXT NOT NULL DEFAULT '', source TEXT NOT NULL,
                    is_demo INTEGER NOT NULL CHECK(is_demo IN (0,1)),
                    fingerprint TEXT NOT NULL, created_at TEXT NOT NULL,
                    submitted_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS requests_actor ON requests(actor, submitted_at);
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, csrf TEXT NOT NULL, admin INTEGER NOT NULL DEFAULT 0,
                    expires REAL NOT NULL, conversation TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS attempts (peer TEXT NOT NULL, at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS attempts_peer ON attempts(peer, at);
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            from .migrations import migrate
            migrate(db)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.create_function("casefold", 1, lambda value: str(value).casefold())
        try:
            with db:
                yield db
        finally:
            db.close()

    def session(self, raw=None):
        sid = hashlib.sha256((raw or "").encode()).hexdigest()
        with self.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=? AND expires>?", (sid, time.time())).fetchone()
            if row:
                return dict(row), None
            raw = secrets.token_urlsafe(32)
            sid = hashlib.sha256(raw.encode()).hexdigest()
            csrf = secrets.token_urlsafe(32)
            db.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            db.execute("INSERT INTO sessions(id,csrf,expires) VALUES(?,?,?)", (sid, csrf, time.time() + 28800))
            return dict(db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()), raw

    def rotate(self, sid, admin, role="admin"):
        row, raw = self.session()
        with self.connect() as db:
            db.execute("UPDATE sessions SET admin=?,role=? WHERE id=?", (int(admin), role, row["id"]))
            db.execute("DELETE FROM sessions WHERE id=?", (sid,))
        return raw

    def list_requests(self, status="", start="", end="", query="", page=1,
                      priority="", category="", assignee="", sort="updated", archived="0", attention="", **unused):
        clauses, params = ["archived=?"], [int(archived == "1")]
        for sql, val in [("status=?", status), ("substr(created_at,1,10)>=?", start),
                         ("substr(created_at,1,10)<=?", end), ("priority=?", priority),
                         ("category=?", category), ("assignee=?", assignee)]:
            if val:
                clauses.append(sql)
                params.append(val)
        if query:
            clauses.append("instr(casefold(title||' '||name||' '||category||' '||description||' '||contact||' '||printf('FD-%04d',id)),?)>0")
            params.append(query.casefold())
        if attention in ("unanswered", "risk"):
            clauses.append("status NOT IN ('resolved','closed') AND NOT EXISTS (SELECT 1 FROM messages m WHERE m.request_id=requests.id AND m.kind='reply' AND m.delivery IN ('local','sent'))")
        if attention == "risk":
            clauses.append("EXISTS (SELECT 1 FROM metadata WHERE key='sla_hours' AND CAST(value AS INTEGER)>0 AND julianday(requests.created_at)+CAST(value AS INTEGER)/24.0<julianday('now'))")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as db:
            total = db.execute("SELECT count(*) FROM requests" + where, params).fetchone()[0]
            order = {"updated": "COALESCE(NULLIF(updated_at,''),created_at) DESC,id DESC", "created": "created_at DESC,id DESC", "priority": "CASE priority WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,id DESC"}.get(sort, "id DESC")
            sql = "SELECT * FROM requests" + where + " ORDER BY " + order
            if page is not None:
                sql += " LIMIT 25 OFFSET ?"
                params.append((page - 1) * 25)
            rows = db.execute(sql, params).fetchall()
        return rows, total

    def get(self, request_id):
        with self.connect() as db:
            return db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()

    def counts(self):
        with self.connect() as db:
            counts = {r["status"]: r["n"] for r in db.execute("SELECT status,count(*) n FROM requests WHERE archived=0 GROUP BY status")}
        return {status: counts.get(status, 0) for status in STATUSES}

    def seed(self):
        from .config import ROOT
        records = json.loads((ROOT / "TEST_DATA" / "requests.json").read_text(encoding="utf-8-sig"))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for index, item in enumerate(records):
                actor = f"seed:{index}"
                if db.execute("SELECT 1 FROM requests WHERE actor=?", (actor,)).fetchone():
                    continue
                cursor = db.execute("""INSERT INTO requests(actor,category,name,contact,description,status,source,
                    is_demo,fingerprint,created_at,submitted_at) VALUES(?,?,?,?,?,?,?,1,?,?,?)""",
                           (actor, item["category"], item["name"], item["contact"], item["description"],
                            item["status"], "seed", actor, now_iso(), time.time()))
                db.execute("UPDATE requests SET title=?,priority=?,assignee=?,updated_at=created_at WHERE id=?",
                           (item.get("title", item["description"][:80]), item.get("priority", "medium"),
                            item.get("assignee", ""), cursor.lastrowid))
                if index == 0:
                    for number, kind, body, author in [
                        (1, "reply", "Вітаю! Прийняли заявку в роботу. Перевіряємо налаштування драйвера та доступ до мережі.", "Оператор"),
                        (2, "incoming", "Дякую! Очікую на оновлення.", item["name"]),
                    ]:
                        db.execute("INSERT INTO messages(request_id,kind,body,author,created_at,nonce) VALUES(?,?,?,?,?,?)",
                                   (cursor.lastrowid, kind, body, author, now_iso(), f"seed:{cursor.lastrowid}:{number}"))


def csv_safe(value):
    text = str(value)
    if text.lstrip().startswith(("=", "+", "-", "@")) or text.startswith(("\t", "\r", "\n")):
        return "'" + text
    return text
