import csv
import hashlib
import hmac
import io
import json
import secrets
import sqlite3
import time
from datetime import date

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .config import ROOT, Settings
from .dialogue import FIELDS, initial, interact, options
from .storage import CATEGORIES, STATUSES, Database, csv_safe
from .workspace import ASSIGNEES, PRIORITIES, change, install_workspace

COOKIE = "flowdesk_session"


def create_app(settings=None):
    settings = settings or Settings.load()
    database = Database(settings.database)
    with database.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        stored = db.execute("SELECT value FROM metadata WHERE key='admin_credentials'").fetchone()
        previous = json.loads(stored["value"]) if stored else None
        salt = bytes.fromhex(previous["salt"]) if previous else secrets.token_bytes(16)
        password_hash = hashlib.scrypt(settings.password.encode(), salt=salt, n=16384, r=8, p=1)
        if previous and not hmac.compare_digest(previous["hash"], password_hash.hex()):
            db.execute("UPDATE sessions SET admin=0 WHERE admin=1")
        db.execute("INSERT OR REPLACE INTO metadata VALUES('admin_credentials',?)",
                   (json.dumps({"salt": salt.hex(), "hash": password_hash.hex()}),))
        operator_hash = hashlib.scrypt(settings.operator_password.encode(), salt=salt, n=16384, r=8, p=1)
        previous_operator = db.execute("SELECT value FROM metadata WHERE key='operator_credentials'").fetchone()
        if previous_operator and previous_operator[0] != operator_hash.hex():
            db.execute("UPDATE sessions SET admin=0 WHERE role='operator'")
        db.execute("INSERT OR REPLACE INTO metadata VALUES('operator_credentials',?)", (operator_hash.hex(),))
    app = FastAPI(title="FlowDesk", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.database = database
    templates = Jinja2Templates(directory=ROOT / "flowdesk" / "templates")
    templates.env.globals["statuses"] = STATUSES
    app.mount("/static", StaticFiles(directory=ROOT / "flowdesk" / "static"), name="static")

    @app.middleware("http")
    async def session_and_security(request, call_next):
        if request.method == "POST":
            length = request.headers.get("content-length", "0")
            if not length.isdigit() or int(length) > 16384:
                return PlainTextResponse("Завеликий запит", status_code=413)
            if len(await request.body()) > 16384:
                return PlainTextResponse("Завеликий запит", status_code=413)
        session, cookie = database.session(request.cookies.get(COOKIE))
        request.state.session = session
        request.state.new_cookie = cookie
        response = await call_next(request)
        if request.state.new_cookie:
            response.set_cookie(COOKIE, request.state.new_cookie, max_age=28800,
                                httponly=True, secure=settings.secure_cookie, samesite="strict")
        response.headers.update({
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
        })
        return response

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])

    def render(request, template, status=200, **context):
        return templates.TemplateResponse(request=request, name=template, context={
            "csrf": request.state.session["csrf"], "is_admin": bool(request.state.session["admin"]),
            "path": request.url.path, **context,
        }, status_code=status)

    def require_admin(request):
        if not request.state.session["admin"]:
            raise HTTPException(303, headers={"Location": "/admin/login"})

    async def form_data(request, admin=False):
        if admin:
            require_admin(request)
        form = await request.form()
        csrf = form.get("csrf", "")
        if not isinstance(csrf, str) or not secrets.compare_digest(csrf, request.state.session["csrf"]):
            raise HTTPException(403, "Сторінка застаріла. Оновіть її та спробуйте ще раз.")
        return form

    def filters(request):
        values = {key: request.query_params.get(key, "").strip() for key in ("status", "start", "end", "query", "priority", "category", "assignee", "sort", "archived", "attention")}
        if values["status"] and values["status"] not in STATUSES:
            raise HTTPException(400, "Невідомий статус.")
        for key in ("start", "end"):
            if values[key]:
                try:
                    values[key] = date.fromisoformat(values[key]).isoformat()
                except ValueError:
                    raise HTTPException(400, "Перевірте дату.") from None
        if values["start"] and values["end"] and values["start"] > values["end"]:
            raise HTTPException(400, "Початок періоду має бути раніше завершення.")
        if len(values["query"]) > 100:
            raise HTTPException(400, "Пошуковий запит має містити до 100 символів.")
        for key, allowed in (("priority", PRIORITIES), ("category", CATEGORIES), ("assignee", ASSIGNEES),
                             ("sort", ("updated", "created", "priority")), ("archived", ("0", "1")), ("attention", ("unanswered", "risk"))):
            if values[key] and values[key] not in allowed:
                raise HTTPException(400, "Невідомий фільтр.")
        return values

    @app.exception_handler(HTTPException)
    async def http_error(request, exc):
        if exc.status_code == 303:
            return RedirectResponse(exc.headers["Location"], status_code=303)
        return render(request, "error.html", status=exc.status_code, code=exc.status_code, message=str(exc.detail))

    @app.exception_handler(sqlite3.Error)
    async def database_error(request, exc):
        return render(request, "error.html", status=503, code=503,
                      message="Сховище тимчасово недоступне. Перевірте вільне місце та права на папку data. Оновіть сторінку: повторне підтвердження не створить дублікат.")

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request):
        return RedirectResponse("/demo", 303)

    @app.get("/health")
    def health():
        return {"status": "ok", "mode": "local"}

    @app.get("/demo", response_class=HTMLResponse)
    def demo(request: Request):
        state = json.loads(request.state.session["conversation"]) or initial()
        return render(request, "demo.html", state=state, options=options(state), fields=FIELDS)

    @app.post("/demo/message")
    async def message(request: Request):
        form = await form_data(request)
        incoming = form.get("message")
        interact(database, request.state.session["id"], incoming)
        return RedirectResponse("/demo#latest", 303)

    @app.get("/admin/login", response_class=HTMLResponse)
    def login(request: Request):
        if request.state.session["admin"]:
            return RedirectResponse("/admin", 303)
        return render(request, "login.html", error="")

    @app.post("/admin/login")
    async def login_post(request: Request):
        form = await form_data(request)
        peer = request.client.host if request.client else "local"
        now = time.time()
        with database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM attempts WHERE at<?", (now - 900,))
            count = db.execute("SELECT count(*) FROM attempts WHERE peer=?", (peer,)).fetchone()[0]
            if count >= 5:
                return render(request, "login.html", status=429, error="Забагато спроб. Спробуйте через 15 хвилин.")
            db.execute("INSERT INTO attempts VALUES(?,?)", (peer, now))
        password = form.get("password", "")
        valid = isinstance(password, str) and len(password) <= 1024
        candidate = hashlib.scrypt(password.encode() if valid else b"", salt=salt, n=16384, r=8, p=1)
        admin_match = hmac.compare_digest(candidate, password_hash)
        operator_match = hmac.compare_digest(candidate, operator_hash) and bool(settings.operator_password)
        if not valid or not (admin_match or operator_match):
            return render(request, "login.html", status=401, error="Неправильна парольна фраза. Спробуйте ще раз.")
        with database.connect() as db:
            db.execute("DELETE FROM attempts WHERE peer=?", (peer,))
        request.state.new_cookie = database.rotate(request.state.session["id"], True,
                                                   "admin" if admin_match else "operator")
        return RedirectResponse("/admin", 303)

    @app.post("/admin/logout")
    async def logout(request: Request):
        await form_data(request, admin=True)
        request.state.new_cookie = database.rotate(request.state.session["id"], False)
        return RedirectResponse("/admin/login", 303)

    @app.get("/admin", response_class=HTMLResponse)
    def dashboard(request: Request):
        require_admin(request)
        return workspace_page(request)

    @app.get("/admin/export.csv")
    def export(request: Request):
        require_admin(request)
        rows, _ = database.list_requests(**filters(request), page=None)
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["Номер", "Категорія", "Ім’я", "Контакт", "Опис", "Статус", "Нотатки", "Джерело", "Дата UTC", "Тема", "Пріоритет", "Виконавець", "Оновлено UTC"])
        for row in rows:
            writer.writerow([csv_safe(value) for value in [f"FD-{row['id']:04d}", row["category"], row["name"],
                row["contact"], row["description"], STATUSES[row["status"]], row["notes"], row["source"], row["created_at"],
                row["title"], PRIORITIES[row["priority"]], ASSIGNEES[row["assignee"]], row["updated_at"] or row["created_at"]]])
        return Response(content="\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="flowdesk-requests.csv"'})

    @app.get("/admin/requests/{request_id}", response_class=HTMLResponse)
    def detail(request: Request, request_id: int):
        require_admin(request)
        return workspace_page(request, selected=request_id)

    @app.post("/admin/requests/{request_id}")
    async def update(request: Request, request_id: int):
        form = await form_data(request, admin=True)
        status, notes = form.get("status"), form.get("notes", "")
        if status not in STATUSES or not isinstance(notes, str) or len(notes) > 5000:
            raise HTTPException(400, "Перевірте статус і довжину нотатки (до 5000 символів).")
        if "version" not in form:
            raise HTTPException(409, "Оновіть картку перед збереженням.")
        change(database, request_id, {"status": status, "notes": notes},
               ASSIGNEES[request.state.session.get("role", "admin")], form.get("version"))
        return RedirectResponse(f"/admin/requests/{request_id}?saved=1", 303)

    @app.post("/admin/seed")
    async def seed(request: Request):
        await form_data(request, admin=True)
        admin_only(request)
        database.seed()
        return RedirectResponse("/admin?notice=seeded", 303)

    @app.get("/admin/clear", response_class=HTMLResponse)
    def clear_confirmation(request: Request):
        require_admin(request)
        admin_only(request)
        return render(request, "clear.html")

    @app.post("/admin/clear")
    async def clear(request: Request):
        form = await form_data(request, admin=True)
        admin_only(request)
        if form.get("confirmation") != "ВИДАЛИТИ":
            raise HTTPException(400, "Введіть ВИДАЛИТИ, щоб підтвердити очищення демо-даних.")
        with database.connect() as db:
            db.execute("DELETE FROM requests WHERE is_demo=1")
        return RedirectResponse("/admin?notice=cleared", 303)

    workspace_page, admin_only = install_workspace(app, database, render, require_admin, form_data, filters)
    return app
