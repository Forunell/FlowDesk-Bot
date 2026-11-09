import hashlib
import json
import re
import time

from .storage import CATEGORIES, now_iso

FIELDS = ("category", "name", "contact", "description")
LABELS = {"category": "Категорія", "name": "Ім’я", "contact": "Контакт", "description": "Опис"}
PROMPTS = {
    "category": "З чим допомогти? Оберіть категорію послуги.",
    "name": "Як до вас звертатися? Вкажіть ім’я (2–80 символів).",
    "contact": "Залиште email, телефон у форматі +380… або Telegram @username.",
    "description": "Опишіть, що потрібно зробити (10–2000 символів).",
}


def initial():
    return {"step": "idle", "data": {}, "history": [{"role": "bot", "text":
        "Вітаю у FlowDesk! Допоможемо з технікою, мережею та цифровими справами. Почнімо із заявки."}]}


def options(state):
    step = state["step"]
    if step in ("idle", "done"):
        return ["/start"]
    if step == "category":
        return list(CATEGORIES)
    if step == "confirm":
        return ["Підтвердити", "Редагувати", "Скасувати"]
    if step == "edit":
        return list(LABELS.values()) + ["Назад"]
    return []


def validate(field, value):
    if any(ord(char) < 32 and char not in "\n\t" for char in value):
        return "Приберіть службові символи з тексту."
    if field == "category" and value not in CATEGORIES:
        return "Оберіть одну з категорій нижче."
    if field == "name" and not 2 <= len(value) <= 80:
        return PROMPTS[field]
    if field == "contact":
        email = re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}", value)
        phone = re.fullmatch(r"\+[1-9]\d{7,14}", value)
        telegram = re.fullmatch(r"@[A-Za-z][A-Za-z0-9_]{4,31}", value)
        if len(value) > 254 or not (email or phone or telegram):
            return "Перевірте контакт: name@example.com, +380000000000 або @demo_user."
    if field == "description" and not 10 <= len(value) <= 2000:
        return PROMPTS[field]
    return None


def summary(data):
    return "Перевірте заявку перед надсиланням:\n\n" + "\n".join(f"{LABELS[key]}: {data[key]}" for key in FIELDS)


def advance(state, incoming, db, actor, source):
    """One shared state machine. Called inside the same transaction as persistence."""
    state = state or initial()
    if not isinstance(incoming, str) or not incoming.strip():
        state["history"].append({"role": "bot", "text": "Надішліть текстове повідомлення, будь ласка."})
        state["history"] = state["history"][-40:]
        return state, None
    value = incoming.strip()
    if len(value) > 2000:
        state["history"].append({"role": "bot", "text": "Повідомлення задовге. Максимум — 2000 символів."})
        state["history"] = state["history"][-40:]
        return state, None
    state["history"].append({"role": "user", "text": value})
    step, created = state["step"], None
    if value == "/start":
        state.update(step="category", data={}, editing=False)
        reply = PROMPTS["category"]
    elif value in ("/cancel", "Скасувати"):
        state.update(step="idle", data={}, editing=False)
        reply = "Заявку скасовано. Дані не надіслано. Можна почати знову."
    elif step in ("idle", "done"):
        reply = "Натисніть «Почати заявку», щоб створити нове звернення."
    elif step == "edit":
        reverse = {v: k for k, v in LABELS.items()}
        if value == "Назад":
            state["step"] = "confirm"
            reply = summary(state["data"])
        elif value in reverse:
            state.update(step=reverse[value], editing=True)
            reply = PROMPTS[state["step"]]
        else:
            reply = "Оберіть поле, яке потрібно змінити."
    elif step == "confirm":
        if value == "Редагувати":
            state["step"] = "edit"
            reply = "Що потрібно змінити?"
        elif value == "Підтвердити":
            data = state["data"]
            fingerprint = hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True).casefold().encode()).hexdigest()
            stamp = time.time()
            duplicate = db.execute("SELECT id FROM requests WHERE actor=? AND fingerprint=? AND submitted_at>?", (actor, fingerprint, stamp - 86400)).fetchone()
            recent = db.execute("SELECT id FROM requests WHERE actor=? AND submitted_at>?", (actor, stamp - 30)).fetchone()
            if duplicate:
                reply = f"Цю заявку вже збережено: FD-{duplicate['id']:04d}. Повтор не створено."
                state.update(step="done", data={})
            elif recent:
                reply = "Зачекайте 30 секунд після попередньої заявки та підтвердьте ще раз."
            else:
                cursor = db.execute("""INSERT INTO requests(actor,category,name,contact,description,
                    source,is_demo,fingerprint,created_at,submitted_at) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (actor, *(data[field] for field in FIELDS), source, int(source != "telegram"), fingerprint, now_iso(), stamp))
                created = cursor.lastrowid
                db.execute("UPDATE requests SET title=?,updated_at=created_at WHERE id=?", (data["description"][:80], created))
                db.execute("INSERT INTO events(request_id,author,field,old_value,new_value,created_at) VALUES(?,?,?,?,?,?)",
                           (created, data["name"], "created", "", "Заявку створено", now_iso()))
                state.update(step="done", data={})
                reply = f"Готово! Заявку FD-{created:04d} збережено. Дякуємо за звернення."
        else:
            reply = "Підтвердьте заявку, змініть дані або скасуйте її."
    else:
        error = validate(step, value)
        if error:
            reply = error
        else:
            state["data"][step] = value
            if state.get("editing") or step == "description":
                state.update(step="confirm", editing=False)
                reply = summary(state["data"])
            else:
                state["step"] = FIELDS[FIELDS.index(step) + 1]
                reply = PROMPTS[state["step"]]
    state["history"].append({"role": "bot", "text": reply})
    state["history"] = state["history"][-40:]
    return state, created


def interact(database, sid, incoming, source="demo"):
    with database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT conversation FROM sessions WHERE id=?", (sid,)).fetchone()
        if row is None:
            raise ValueError("Session expired")
        state, created = advance(json.loads(row["conversation"]), incoming, db, sid, source)
        db.execute("UPDATE sessions SET conversation=? WHERE id=?", (json.dumps(state, ensure_ascii=False), sid))
    return state, created
