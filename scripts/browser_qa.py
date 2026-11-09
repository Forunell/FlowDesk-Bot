"""Isolated real Edge acceptance tests. Never reads production credentials or calls Telegram."""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
ARTIFACTS = ROOT / "docs" / "screenshots"
RESULTS = ROOT / "test-results"
PASSWORD = "browser-test-only-password"


def main():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    RESULTS.mkdir(exist_ok=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="browser-v2-", dir=RESULTS) as directory:
        environment = {**os.environ, "FLOWDESK_DB": str(Path(directory) / "browser.sqlite3"),
                       "FLOWDESK_ADMIN_PASSWORD": PASSWORD, "FLOWDESK_COOKIE_SECURE": "false",
                       "FLOWDESK_OPERATOR_PASSWORD": "", "TELEGRAM_REPLIES_ENABLED": "false"}
        process = subprocess.Popen([sys.executable, "-m", "flowdesk", "demo", "--port", str(port)],
                                   cwd=ROOT, env=environment, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            for _ in range(100):
                try:
                    with urlopen(base + "/health", timeout=1) as response:
                        if response.status == 200:
                            break
                except OSError:
                    if process.poll() is not None:
                        raise RuntimeError("QA server exited") from None
                    time.sleep(.1)
            else:
                raise RuntimeError("QA server not ready")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel="msedge", headless=True)
                context = browser.new_context(viewport={"width": 1672, "height": 1000}, locale="uk-UA", reduced_motion="reduce")
                external, errors = [], []

                def route(request_route):
                    if request_route.request.url.startswith(base + "/"):
                        request_route.continue_()
                    else:
                        external.append(request_route.request.url)
                        request_route.abort()

                context.route("**/*", route)
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("dialog", lambda dialog: dialog.accept())
                page.goto(base + "/demo")
                page.get_by_role("button", name="Почати заявку").click()
                page.get_by_role("button", name="Комп’ютерна допомога", exact=True).click()
                for text in ["Марта Демо", "invalid", "marta@example.com", "Потрібно налаштувати навчальний ноутбук та резервне копіювання."]:
                    page.get_by_label("Ваше повідомлення").fill(text)
                    page.get_by_role("button", name="Надіслати повідомлення").click()
                    if text == "invalid":
                        expect(page.locator("#latest")).to_contain_text("Перевірте контакт")
                page.screenshot(path=str(ARTIFACTS / "01-demo-desktop.png"), full_page=True)
                page.get_by_role("button", name="Підтвердити", exact=True).click()
                expect(page.locator("#latest")).to_contain_text("FD-0001")
                page.goto(base + "/admin")
                page.get_by_label("Парольна фраза").fill(PASSWORD)
                page.get_by_role("button", name="Увійти до кабінету").click()
                expect(page.locator(".ticket-row")).to_have_count(1)
                page.get_by_role("button", name="+ Тестові заявки", exact=True).click()
                expect(page.locator(".ticket-row")).to_have_count(7)
                page.locator('.ticket-row[href^="/admin/requests/2?"]').click()
                expect(page.locator(".ticket-header")).to_contain_text("Не працює принтер")
                page.get_by_role("button", name="Призначити на себе", exact=True).click()
                expect(page.get_by_label("Виконавець заявки")).to_have_value("admin")
                page.goto(base + "/admin/requests/2")
                page.evaluate("window.scrollTo(0, 0)")
                page.locator(".ticket-items").evaluate("e => { e.scrollTop = 0; }")
                page.mouse.move(1000, 100)
                page.screenshot(path=str(ARTIFACTS / "02-admin-desktop.png"), full_page=True)
                page.get_by_label("Статус заявки", exact=True).select_option("waiting")
                page.get_by_role("button", name="Зберегти зміни", exact=True).click()
                expect(page.locator('[data-kpi="waiting"]')).to_have_text("2")
                page.get_by_role("button", name="Призначити на себе", exact=True).click()
                expect(page.get_by_label("Виконавець заявки")).to_have_value("admin")
                page.get_by_role("button", name="Додати нотатку").click()
                expect(page.get_by_label("Тип повідомлення")).to_have_value("note")
                page.get_by_label("Текст повідомлення").fill("Внутрішня браузерна нотатка")
                page.get_by_role("button", name="Надіслати відповідь").click()
                expect(page.locator(".message.note")).to_contain_text("Внутрішня браузерна нотатка")
                page.get_by_label("Тип повідомлення").select_option("reply")
                page.get_by_label("Текст повідомлення").fill("Перевірили налаштування. Тестова відповідь.")
                page.get_by_role("button", name="Надіслати відповідь").click()
                expect(page.locator(".message.reply").last).to_contain_text("Тестова відповідь")
                page.reload()
                expect(page.locator(".message.note")).to_contain_text("Внутрішня браузерна нотатка")
                page.get_by_label("Тип повідомлення").select_option("incoming")
                page.get_by_label("Текст повідомлення").fill("Дякую за допомогу, тестове повідомлення.")
                page.get_by_role("button", name="Надіслати відповідь").click()
                expect(page.locator(".message.incoming").last).to_contain_text("Дякую за допомогу, тестове повідомлення.")
                expect(page.locator(".message.incoming").last).to_contain_text("СИМУЛЯЦІЯ")
                page.get_by_role("link", name="Історія", exact=False).click()
                expect(page.locator(".event-list")).to_contain_text("У роботі → Очікує")
                page.go_back()
                expect(page.locator(".composer")).to_be_visible()
                context.grant_permissions(["clipboard-read", "clipboard-write"])
                page.get_by_role("button", name="Копіювати посилання").click()
                expect(page.locator("#feedback")).to_contain_text("Посилання скопійовано")
                assert page.evaluate("navigator.clipboard.readText()") == base + "/admin/requests/2"
                page.screenshot(path=str(ARTIFACTS / "03-request-desktop.png"), full_page=True)
                # Network failure preserves the draft and permits a retry.
                page.get_by_label("Текст повідомлення").fill("Чернетка під час збою мережі")
                context.set_offline(True)
                page.get_by_role("button", name="Надіслати відповідь").click()
                expect(page.locator("#feedback")).to_contain_text("Немає з’єднання")
                expect(page.get_by_label("Текст повідомлення")).to_have_value("Чернетка під час збою мережі")
                context.set_offline(False)
                page.get_by_role("button", name="Надіслати відповідь").click()
                expect(page.locator(".conversation")).to_contain_text("Чернетка під час збою мережі")
                # Combined filters, CSV and empty state.
                page.goto(base + "/admin")
                page.keyboard.press("Control+k")
                expect(page.get_by_label("Глобальний пошук")).to_be_focused()
                page.get_by_role("button", name="Фільтри", exact=True).click()
                page.get_by_label("Пошук", exact=True).fill("марта демо")
                page.get_by_label("Статус", exact=True).select_option("new")
                page.get_by_label("Сортування", exact=True).select_option("priority")
                page.get_by_role("button", name="Знайти", exact=True).click()
                expect(page.locator(".ticket-row")).to_have_count(1)
                with page.expect_download() as download:
                    page.get_by_role("link", name="↓ CSV", exact=True).click()
                download.value.save_as(str(RESULTS / "browser-export.csv"))
                assert "Марта Демо" in (RESULTS / "browser-export.csv").read_text(encoding="utf-8-sig")
                page.get_by_label("Пошук", exact=True).fill("does-not-exist")
                page.get_by_role("button", name="Знайти", exact=True).click()
                expect(page.get_by_role("heading", name="Нічого не знайдено")).to_be_visible()
                # Form creation, including native required-field validation.
                page.get_by_role("link", name="Нова заявка", exact=True).click()
                page.get_by_role("button", name="Створити заявку", exact=True).click()
                expect(page).to_have_url(base + "/admin/new")
                page.get_by_label("Тема", exact=True).fill("Браузерна заявка")
                page.get_by_label("Ім’я заявника").fill("Клієнт Приклад")
                page.get_by_label("Контакт", exact=True).fill("client@example.com")
                page.get_by_label("Опис", exact=True).fill("Повний тест створення заявки з форми.")
                page.get_by_role("button", name="Створити заявку", exact=True).click()
                expect(page.locator(".ticket-header")).to_contain_text("Браузерна заявка")
                page.reload()
                expect(page.locator(".ticket-header")).to_contain_text("Браузерна заявка")
                # Archive requires explicit confirmation; then restore.
                page.locator(".archive-action summary").click()
                page.get_by_label("Підтверджую дію").check()
                page.locator(".archive-action").get_by_role("button", name="Підтвердити", exact=True).click()
                expect(page.locator(".ticket-header")).to_contain_text("АРХІВ")
                page.locator(".archive-action summary").click()
                page.get_by_label("Підтверджую дію").check()
                page.locator(".archive-action").get_by_role("button", name="Підтвердити", exact=True).click()
                expect(page.locator(".composer")).to_be_visible()
                # Another browser tab creates a ticket; the list updates without a manual reload.
                page.goto(base + "/admin")
                expect(page.locator(".ticket-row")).to_have_count(8)
                other_context = browser.new_context()
                other_context.route("**/*", route)
                other = other_context.new_page()
                other.goto(base + "/demo")
                other.get_by_role("button", name="Почати заявку").click()
                other.get_by_role("button", name="Консультація", exact=True).click()
                for text in ["Інший Демо", "second@example.com", "Перевірка автоматичного оновлення списку заявок."]:
                    other.get_by_label("Ваше повідомлення").fill(text)
                    other.get_by_role("button", name="Надіслати повідомлення").click()
                other.get_by_role("button", name="Підтвердити", exact=True).click()
                expect(other.locator("#latest")).to_contain_text("збережено")
                other_context.close()
                page.bring_to_front()
                expect(page.locator(".ticket-row")).to_have_count(9, timeout=26000)
                checks = []
                for width in (360, 768, 1440, 1672):
                    page.set_viewport_size({"width": width, "height": 1000})
                    for path, name in [("/demo", "demo"), ("/admin", "admin"), ("/admin/requests/2", "request")]:
                        page.goto(base + path)
                        if not page.evaluate("document.documentElement.scrollWidth <= innerWidth"):
                            page.screenshot(path=str(RESULTS / "overflow.png"), full_page=True)
                            offenders = page.evaluate("Array.from(document.querySelectorAll('body *')).filter(e=>e.getBoundingClientRect().right>innerWidth).map(e=>[e.tagName,e.className,Math.round(e.getBoundingClientRect().right)])")
                            raise AssertionError(f"Overflow: {width} {path}: {offenders}")
                        expect(page.locator("h1")).to_be_visible()
                        if width == 360 and name == "request":
                            expect(page.locator(".composer")).to_be_visible()
                            page.get_by_role("link", name="До всіх заявок").click()
                            expect(page.locator(".ticket-list")).to_be_visible()
                            page.go_back()
                            expect(page.locator(".composer")).to_be_visible()
                        page.screenshot(path=str(ARTIFACTS / f"{name}-{width}.png"), full_page=True)
                        checks.append({"width": width, "page": path, "overflow": False})
                page.goto(base + "/admin/settings")
                page.get_by_label("Годин до першої відповіді").fill("4")
                page.get_by_role("button", name="Зберегти правило").click()
                page.reload()
                expect(page.get_by_label("Годин до першої відповіді")).to_have_value("4")
                page.goto(base + "/admin/clear")
                page.get_by_label("Для підтвердження введіть ВИДАЛИТИ").fill("ВИДАЛИТИ")
                page.get_by_role("button", name="Видалити демо-дані").click()
                expect(page.get_by_role("heading", name="Тут починається ваш сервіс")).to_be_visible()
                page.get_by_role("button", name="Вийти з кабінету", exact=False).click()
                page.goto(base + "/admin/export.csv")
                expect(page.get_by_role("heading", name="Раді бачити вас")).to_be_visible()
                page.keyboard.press("Tab")
                assert page.evaluate("document.activeElement.tagName !== 'BODY'")
                context.close()
                plain_context = browser.new_context(java_script_enabled=False)
                plain_context.route("**/*", route)
                plain = plain_context.new_page()
                plain.goto(base + "/demo")
                plain.get_by_role("button", name="Почати заявку").click()
                plain.get_by_role("button", name="Консультація", exact=True).click()
                expect(plain.get_by_label("Ваше повідомлення")).to_be_visible()
                plain.goto(base + "/admin")
                plain.get_by_label("Парольна фраза").fill(PASSWORD)
                plain.get_by_role("button", name="Увійти до кабінету").click()
                plain.get_by_role("button", name="+ Тестові заявки", exact=True).click()
                expect(plain.locator(".ticket-row")).to_have_count(6)
                plain_context.close()
                browser.close()
                assert not external, f"Unexpected external requests: {len(external)}"
                assert not errors, errors
                report = {"result": "PASS", "browser": "Microsoft Edge / Playwright", "viewports": checks,
                          "external_requests": 0, "javascript_errors": 0,
                          "scenarios": ["demo to admin", "invalid contact", "login", "seed", "status and KPI", "assignment", "internal note", "local reply", "simulation", "history", "reload persistence", "offline failure and retry", "combined search/sort/filters", "CSV", "empty search", "form validation and creation", "archive/restore", "mobile back and history", "SLA settings", "clear confirmation", "logout", "keyboard focus and Ctrl+K", "clipboard", "automatic cross-tab refresh", "JavaScript disabled"]}
                (RESULTS / "browser-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print("PASS: browser acceptance; 12 responsive checks; no external requests or JavaScript errors.")
        finally:
            process.terminate()
            process.wait(timeout=15)


if __name__ == "__main__":
    main()
