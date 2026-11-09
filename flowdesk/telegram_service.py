"""Transactional inbound deduplication and explicitly enabled outbound delivery."""
import json
import secrets
import time

from .dialogue import advance
from .storage import now_iso
from .workspace import event


def receive(database, chat_id, message_id, text):
    """Commit the Telegram message and dialogue transition together, once."""
    sid = f"telegram:{chat_id}"
    key = f"{chat_id}:{message_id}"
    with database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT 1 FROM telegram_updates WHERE key=?", (key,)).fetchone():
            return None, None
        db.execute("INSERT INTO telegram_updates VALUES(?)", (key,))
        db.execute("INSERT OR IGNORE INTO sessions(id,csrf,expires) VALUES(?,?,?)", (sid, secrets.token_urlsafe(32), time.time() + 28800))
        db.execute("UPDATE sessions SET expires=? WHERE id=?", (time.time() + 28800, sid))
        state = json.loads(db.execute("SELECT conversation FROM sessions WHERE id=?", (sid,)).fetchone()[0])
        ticket = db.execute("SELECT * FROM requests WHERE actor=? AND archived=0 AND status NOT IN ('closed','resolved') ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
        if ticket and state.get("step") in ("done", "idle", None) and isinstance(text, str) and not text.startswith("/") and 0 < len(text.strip()) <= 5000:
            db.execute("INSERT INTO messages(request_id,kind,body,author,created_at,nonce) VALUES(?,'incoming',?,?,?,?)",
                       (ticket["id"], text.strip(), ticket["name"], now_iso(), "telegram:" + key))
            db.execute("UPDATE requests SET version=version+1,updated_at=? WHERE id=?", (now_iso(), ticket["id"]))
            event(db, ticket["id"], ticket["name"], "message", "", "Вхідне повідомлення Telegram")
            return {"step": "done", "history": [{"text": f"Повідомлення додано до FD-{ticket['id']:04d}. Для нової заявки — /start."}]}, None
        state, created = advance(state, text, db, sid, "telegram")
        db.execute("UPDATE sessions SET conversation=? WHERE id=?", (json.dumps(state, ensure_ascii=False), sid))
        return state, created


async def deliver_one(database, bot):
    """Claim before sending. Never auto-retry an uncertain send after a crash."""
    with database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        message = db.execute("""SELECT m.*,r.actor FROM messages m JOIN requests r ON r.id=m.request_id
            WHERE m.delivery='queued' AND m.kind='reply' AND r.source='telegram' ORDER BY m.id LIMIT 1""").fetchone()
        if not message:
            return False
        db.execute("UPDATE messages SET delivery='sending' WHERE id=?", (message["id"],))
    try:
        await bot.send_message(int(message["actor"].split(":", 1)[1]), message["body"])
        delivery = "sent"
    except Exception:
        delivery = "failed"
    with database.connect() as db:
        db.execute("UPDATE messages SET delivery=? WHERE id=?", (delivery, message["id"]))
        event(db, message["request_id"], "Telegram", "delivery", "queued", delivery)
    return True
