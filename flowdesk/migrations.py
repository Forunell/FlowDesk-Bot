"""Additive v2 migration; rebuild only the legacy status CHECK, in one transaction."""


def migrate(db):
    db.execute("BEGIN IMMEDIATE")
    columns = {row[1] for row in db.execute("PRAGMA table_info(requests)")}
    if "version" not in columns:
        sequence = db.execute("SELECT seq FROM sqlite_sequence WHERE name='requests'").fetchone()
        definition = db.execute("SELECT sql FROM sqlite_master WHERE name='requests'").fetchone()[0]
        definition = definition.replace("CREATE TABLE requests", "CREATE TABLE requests_v2")
        definition = definition.replace("'new','working','closed'", "'new','working','waiting','resolved','closed'")
        db.execute(definition)
        names = ",".join(row[1] for row in db.execute("PRAGMA table_info(requests)"))
        db.execute(f"INSERT INTO requests_v2({names}) SELECT {names} FROM requests")
        db.execute("DROP TABLE requests")
        db.execute("ALTER TABLE requests_v2 RENAME TO requests")
        if sequence:
            db.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name='requests'", (sequence[0],))
        for definition in (
            "title TEXT NOT NULL DEFAULT ''", "priority TEXT NOT NULL DEFAULT 'medium'",
            "assignee TEXT NOT NULL DEFAULT ''", "updated_at TEXT NOT NULL DEFAULT ''",
            "version INTEGER NOT NULL DEFAULT 1", "archived INTEGER NOT NULL DEFAULT 0",
        ):
            db.execute("ALTER TABLE requests ADD COLUMN " + definition)
        db.execute("UPDATE requests SET title=substr(description,1,80),updated_at=created_at")
        db.execute("CREATE INDEX requests_actor ON requests(actor,submitted_at)")
    if "role" not in {r[1] for r in db.execute("PRAGMA table_info(sessions)")}:
        db.execute("ALTER TABLE sessions ADD COLUMN role TEXT NOT NULL DEFAULT 'admin'")
    db.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
        kind TEXT NOT NULL, body TEXT NOT NULL, author TEXT NOT NULL, created_at TEXT NOT NULL,
        delivery TEXT NOT NULL DEFAULT 'local', nonce TEXT UNIQUE NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL REFERENCES requests(id) ON DELETE CASCADE,
        author TEXT NOT NULL, field TEXT NOT NULL, old_value TEXT NOT NULL, new_value TEXT NOT NULL,
        created_at TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS messages_ticket ON messages(request_id,id)")
    db.execute("CREATE INDEX IF NOT EXISTS events_ticket ON events(request_id,id)")
    db.execute("CREATE TABLE IF NOT EXISTS telegram_updates (key TEXT PRIMARY KEY)")
