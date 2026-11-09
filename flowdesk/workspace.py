"""Workspace domain rules and routes. All mutations use the existing session and CSRF."""
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from .dialogue import FIELDS, validate
from .storage import CATEGORIES, STATUSES, now_iso

PRIORITIES = {"high": "Високий", "medium": "Середній", "low": "Низький"}
ASSIGNEES = {"": "Не призначено", "admin": "Адміністратор", "operator": "Оператор"}
FIELD_LABELS = {"status": "Статус", "priority": "Пріоритет", "category": "Категорія",
                "assignee": "Виконавець", "archived": "Архів", "notes": "Нотатки"}


def event(db, ticket, actor, field, old, new):
    db.execute("INSERT INTO events(request_id,author,field,old_value,new_value,created_at) VALUES(?,?,?,?,?,?)",
               (ticket, actor, field, str(old), str(new), now_iso()))


def change(database, ticket, values, actor, version=None):
    with database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM requests WHERE id=?", (ticket,)).fetchone()
        if not row:
            raise HTTPException(404, "Заявку не знайдено.")
        if version is not None and str(row["version"]) != str(version):
            raise HTTPException(409, "Заявку вже змінив інший користувач. Відкрийте актуальну картку; ваші введені дані залишилися у формі.")
        changed = False
        allowed = {"status": STATUSES, "priority": PRIORITIES, "category": CATEGORIES,
                   "assignee": ASSIGNEES, "archived": ("0", "1")}
        for key, value in values.items():
            if key not in (*allowed, "notes") or not isinstance(value, str):
                raise HTTPException(400, "Невідоме поле.")
            if key in allowed and value not in allowed[key]:
                raise HTTPException(400, "Перевірте значення поля.")
            if key == "notes" and len(value) > 5000:
                raise HTTPException(400, "Нотатка має містити до 5000 символів.")
            if str(row[key]) != value:
                db.execute(f"UPDATE requests SET {key}=? WHERE id=?", (value, ticket))
                event(db, ticket, actor, key, row[key], value)
                changed = True
        if changed:
            db.execute("UPDATE requests SET version=version+1,updated_at=? WHERE id=?", (now_iso(), ticket))


def add_message(database, ticket, kind, body, actor, nonce):
    if kind not in ("reply", "note", "incoming") or not isinstance(body, str) or not 1 <= len(body.strip()) <= 5000:
        raise HTTPException(400, "Введіть повідомлення від 1 до 5000 символів.")
    if not isinstance(nonce, str) or not 16 <= len(nonce) <= 100:
        raise HTTPException(400, "Оновіть форму повідомлення.")
    with database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM requests WHERE id=?", (ticket,)).fetchone()
        if not row:
            raise HTTPException(404, "Заявку не знайдено.")
        if row["archived"]:
            raise HTTPException(409, "Спочатку відновіть заявку з архіву.")
        if kind == "incoming" and not row["is_demo"]:
            raise HTTPException(400, "Симуляція доступна лише для демо-заявок.")
        duplicate = db.execute("SELECT request_id,kind,body FROM messages WHERE nonce=?", (nonce,)).fetchone()
        if duplicate:
            if (duplicate["request_id"], duplicate["kind"], duplicate["body"]) != (ticket, kind, body.strip()):
                raise HTTPException(409, "Цю форму вже використано. Оновіть картку.")
            return
        delivery = "local"
        if kind == "reply" and row["source"] == "telegram":
            if os.getenv("TELEGRAM_REPLIES_ENABLED", "false").lower() != "true":
                raise HTTPException(409, "Надсилання Telegram вимкнено. Увімкніть серверний адаптер або збережіть внутрішню нотатку.")
            delivery = "queued"
        db.execute("INSERT INTO messages(request_id,kind,body,author,created_at,delivery,nonce) VALUES(?,?,?,?,?,?,?)",
                   (ticket, kind, body.strip(), actor, now_iso(), delivery, nonce))
        event(db, ticket, actor, "message", "", {"reply": "Відповідь", "note": "Внутрішня нотатка", "incoming": "СИМУЛЯЦІЯ · вхідне повідомлення"}[kind])
        db.execute("UPDATE requests SET version=version+1,updated_at=? WHERE id=?", (now_iso(), ticket))


def sla_state(row, messages, hours):
    if not hours:
        return {"label": "Не налаштовано", "risk": False}
    if row["status"] in ("resolved", "closed") or any(m["kind"] == "reply" and m["delivery"] in ("local", "sent") for m in messages):
        return {"label": "Відповідь надано / завершено", "risk": False}
    due = datetime.fromisoformat(row["created_at"]) + timedelta(hours=hours)
    return {"label": due.strftime("%d.%m %H:%M UTC"), "risk": due < datetime.now(timezone.utc)}


def install_workspace(app, database, render, require_admin, form_data, filters):
    def role(request):
        return request.state.session.get("role", "admin")

    def admin_only(request):
        require_admin(request)
        if role(request) != "admin":
            raise HTTPException(403, "Ця дія доступна лише адміністратору.")

    def page(request, selected=None, section="tickets"):
        require_admin(request)
        values = filters(request)
        try:
            page_number = max(1, min(1000000, int(request.query_params.get("page", "1"))))
        except ValueError:
            raise HTTPException(400, "Перевірте номер сторінки.") from None
        rows, total = database.list_requests(**values, page=page_number)
        item = database.get(selected) if selected else (rows[0] if rows else None)
        if selected and not item:
            raise HTTPException(404, "Заявку не знайдено.")
        with database.connect() as db:
            messages = db.execute("SELECT * FROM messages WHERE request_id=? ORDER BY id", (item["id"] if item else 0,)).fetchall()
            events = db.execute("SELECT * FROM events WHERE request_id=? ORDER BY id DESC", (item["id"] if item else 0,)).fetchall()
            config = db.execute("SELECT value FROM metadata WHERE key='sla_hours'").fetchone()
            hours = int(config[0]) if config else 0
            recent = db.execute("SELECT e.*,r.title FROM events e JOIN requests r ON r.id=e.request_id ORDER BY e.id DESC LIMIT 10").fetchall()
        all_rows, _ = database.list_requests(page=None)
        daily = []
        today = datetime.now(timezone.utc).date()
        for offset in range(6, -1, -1):
            day = (today - timedelta(days=offset)).isoformat()
            daily.append({"day": day[5:], "count": sum(r["created_at"][:10] == day for r in all_rows)})
        peak = max(1, max(point["count"] for point in daily))
        points = [(i * 36 + 4, 48 - point["count"] * 40 / peak) for i, point in enumerate(daily)]
        graph_line = "M" + " L".join(f"{x},{y:.1f}" for x, y in points)
        period_rows, _ = database.list_requests(**values, page=None)
        distribution = {p: sum(r["priority"] == p for r in period_rows) for p in PRIORITIES}
        period_statuses = {s: sum(r["status"] == s for r in period_rows) for s in STATUSES}
        active_tab = request.query_params.get("tab", "dialog")
        if active_tab not in ("dialog", "details", "history"):
            active_tab = "dialog"
        notice = {"seeded": "Тестові заявки додано. Повторні приклади не дублюються.", "cleared": "Демо-дані очищено.",
                  "saved": "Зміни збережено.", "message": "Повідомлення збережено.", "created": "Заявку створено."}.get(request.query_params.get("notice"), "")
        if request.query_params.get("saved") == "1":
            notice = "Зміни збережено."
        return render(request, "workspace.html", rows=rows, total=total, item=item, messages=messages,
                      events=events, counts=database.counts(), priorities=PRIORITIES, assignees=ASSIGNEES,
                      categories=CATEGORIES, filters=values, query=urlencode(values), page=page_number,
                      section=section, tab=active_tab, selected=selected, notice=notice,
                      nonce=secrets.token_urlsafe(24), role=role(request), sla_hours=hours,
                      sla=sla_state(item, messages, hours) if item else None, recent=recent,
                      daily=daily, distribution=distribution, period_total=len(period_rows),
                      graph_line=graph_line, graph_area=graph_line + " L220,52 L4,52 Z",
                      period_statuses=period_statuses, demo_only=all(r["is_demo"] for r in all_rows),
                      field_labels=FIELD_LABELS,
                      value_labels={"status": STATUSES, "priority": PRIORITIES, "assignee": ASSIGNEES,
                                    "archived": {"0": "Активна", "1": "В архіві"}})

    @app.get("/admin/overview")
    def overview(request: Request):
        return page(request, section="overview")

    @app.get("/admin/settings")
    def settings_page(request: Request):
        return page(request, section="settings")

    @app.post("/admin/settings")
    async def settings_save(request: Request):
        form = await form_data(request, admin=True)
        admin_only(request)
        try:
            hours = int(form.get("sla_hours", ""))
            if not 0 <= hours <= 168:
                raise ValueError
        except (TypeError, ValueError):
            raise HTTPException(400, "Вкажіть від 0 до 168 годин.") from None
        with database.connect() as db:
            db.execute("INSERT OR REPLACE INTO metadata VALUES('sla_hours',?)", (str(hours),))
        return RedirectResponse("/admin/settings?notice=saved", 303)

    @app.get("/admin/new")
    def new_page(request: Request):
        require_admin(request)
        return render(request, "new.html", categories=CATEGORIES, priorities=PRIORITIES,
                      nonce=secrets.token_urlsafe(24))

    @app.post("/admin/new")
    async def create(request: Request):
        form = await form_data(request, admin=True)
        for field in FIELDS:
            value = form.get(field, "")
            if not isinstance(value, str) or validate(field, value.strip()):
                raise HTTPException(400, "Перевірте поле: " + field)
        title, priority, nonce = form.get("title", ""), form.get("priority"), form.get("nonce", "")
        if not all(isinstance(value, str) for value in (title, priority, nonce)):
            raise HTTPException(400, "Поля мають містити текст, а не файли.")
        title = title.strip()
        if not 3 <= len(title) <= 120 or priority not in PRIORITIES or not 16 <= len(nonce) <= 100:
            raise HTTPException(400, "Перевірте тему (3–120 символів) та пріоритет.")
        with database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT id FROM requests WHERE actor=?", ("form:" + nonce,)).fetchone()
            if existing:
                ticket = existing[0]
            else:
                ticket = db.execute("""INSERT INTO requests(actor,category,name,contact,description,source,is_demo,
                    fingerprint,created_at,submitted_at,title,priority,updated_at)
                    VALUES(?,?,?,?,?,'web-demo',1,?,?,?,?,?,?)""",
                    ("form:" + nonce, *(form[f].strip() for f in FIELDS), nonce, now_iso(), time.time(), title, priority, now_iso())).lastrowid
                event(db, ticket, ASSIGNEES[role(request)], "created", "", "Заявку створено")
        return RedirectResponse(f"/admin/requests/{ticket}?notice=created", 303)

    @app.post("/admin/requests/{ticket}/change")
    async def update_fields(request: Request, ticket: int):
        form = await form_data(request, admin=True)
        if "version" not in form:
            raise HTTPException(409, "Оновіть картку перед зміною.")
        values = {k: form[k] for k in FIELD_LABELS if k in form}
        if "archived" in values and form.get("confirmation") != "yes":
            raise HTTPException(400, "Підтвердьте архівацію або відновлення заявки.")
        change(database, ticket, values, ASSIGNEES[role(request)], form["version"])
        return RedirectResponse(f"/admin/requests/{ticket}?notice=saved", 303)

    @app.post("/admin/requests/{ticket}/messages")
    async def send_message(request: Request, ticket: int):
        form = await form_data(request, admin=True)
        add_message(database, ticket, form.get("kind"), form.get("body"), ASSIGNEES[role(request)], form.get("nonce"))
        return RedirectResponse(f"/admin/requests/{ticket}?notice=message", 303)

    return page, admin_only
