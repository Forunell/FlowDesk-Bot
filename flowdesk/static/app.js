"use strict";
document.documentElement.classList.add("js");
const drafts = new Map();
let dirty = false, busy = false, navigation = 0, feedbackTimer;
let renderedUrl = location.href;
function feedback(text, error = false) {
  const box = document.getElementById("feedback");
  box.textContent = text; box.classList.toggle("error", error); box.hidden = false;
  clearTimeout(feedbackTimer); feedbackTimer = setTimeout(() => { box.hidden = true; }, error ? 16000 : 5000);
}
function init() {
  const transcript = document.getElementById("transcript");
  if (transcript) transcript.scrollTop = transcript.scrollHeight;
  const conversation = document.getElementById("conversation");
  if (conversation) conversation.scrollTop = conversation.scrollHeight;
  const form = document.querySelector("[data-draft]");
  if (form && drafts.has(form.dataset.draft)) {
    const draft = drafts.get(form.dataset.draft);
    form.elements.body.value = draft.body; form.elements.kind.value = draft.kind;
  }
  dirty = false;
}
async function replacePage(url, options = {}, mode = "push") {
  const current = ++navigation;
  const response = await fetch(url, {...options, credentials: "same-origin"});
  const html = new DOMParser().parseFromString(await response.text(), "text/html");
  if (current !== navigation) return false;
  if (mode === "refresh" && (dirty || busy || document.activeElement.closest("form"))) return false;
  if (!response.ok) {
    const message = html.querySelector(".confirmation-card p, .error p, .error, main p");
    throw new Error(message?.textContent.trim() || `Не вдалося зберегти (${response.status}). Спробуйте ще раз.`);
  }
  if (new URL(response.url).pathname === "/admin/login") {
    feedback("Сесію завершено. Відкрийте вхід в іншій вкладці; чернетка залишилася тут.", true); return false;
  }
  const next = html.getElementById("main");
  if (!next) throw new Error("Не вдалося оновити сторінку.");
  const expanded = document.querySelector("#advanced-filters.open");
  document.getElementById("main").replaceWith(next);
  document.title = html.title;
  if (mode === "push") history.pushState({}, "", response.url);
  if (mode === "replace") history.replaceState({}, "", response.url);
  renderedUrl = location.href;
  const crumb = html.querySelector(".breadcrumb");
  if (crumb) document.querySelector(".breadcrumb").replaceWith(crumb);
  if (expanded) {
    document.getElementById("advanced-filters")?.classList.add("open");
    document.querySelector("[data-toggle-filters]")?.setAttribute("aria-expanded", "true");
  }
  document.querySelectorAll(".sidebar .nav-link").forEach(link => {
    const path = new URL(link.href).pathname;
    const active = path === location.pathname || (path === "/admin" && location.pathname.startsWith("/admin/requests"));
    link.classList.toggle("active", active);
    if (active) link.setAttribute("aria-current", "page"); else link.removeAttribute("aria-current");
  });
  init();
  return true;
}
document.addEventListener("input", event => {
  if (event.target.closest("#main form")) dirty = true;
  const form = event.target.closest("[data-draft]");
  if (form) drafts.set(form.dataset.draft, {body: form.elements.body.value, kind: form.elements.kind.value});
});
document.addEventListener("change", event => { if (event.target.closest("#main form")) dirty = true; });
document.addEventListener("submit", async event => {
  const form = event.target;
  if (!form.action.includes("/admin") || form.action.endsWith("/login") || form.action.endsWith("/logout")) return;
  event.preventDefault();
  if (busy) return;
  busy = true; form.setAttribute("aria-busy", "true");
  const button = event.submitter;
  button?.classList.add("submitting");
  const body = new FormData(form);
  if (button?.name) body.set(button.name, button.value);
  const method = form.method.toUpperCase();
  const draftKey = form.dataset.draft;
  const previousDraft = draftKey ? drafts.get(draftKey) : null;
  feedback(method === "POST" ? "Зберігаємо…" : "Завантажуємо…");
  try {
    if (draftKey) drafts.delete(draftKey);
    const ok = await replacePage(method === "GET" ? form.action.split("?")[0] + "?" + new URLSearchParams(body) : form.action,
      method === "GET" ? {} : {method, body});
    if (!ok && draftKey && previousDraft) drafts.set(draftKey, previousDraft);
    if (ok) feedback(method === "POST" ? "Зміни збережено." : "Список оновлено.");
  } catch (error) {
    if (draftKey && previousDraft) drafts.set(draftKey, previousDraft);
    feedback(error instanceof TypeError ? "Немає з’єднання. Дані залишилися у формі. Повторіть надсилання." : error.message, true);
  } finally { busy = false; form.removeAttribute("aria-busy"); button?.classList.remove("submitting"); }
});
document.addEventListener("click", async event => {
  const toggle = event.target.closest("[data-toggle-filters]");
  if (toggle) { const panel = document.getElementById("advanced-filters"); panel.classList.toggle("open"); toggle.setAttribute("aria-expanded", String(panel.classList.contains("open"))); return; }
  const copy = event.target.closest("[data-copy]");
  if (copy) { try { await navigator.clipboard.writeText(location.origin + copy.dataset.copy); feedback("Посилання скопійовано."); } catch { feedback("Не вдалося скопіювати. Скопіюйте адресу з рядка браузера.", true); } return; }
  if (event.target.closest("[data-priority]")) { const field = document.querySelector('.ticket-properties [name="priority"]'); field?.focus(); return; }
  if (event.target.closest("[data-note]")) {
    if (!document.querySelector("#message-kind")) {
      if (dirty && !confirm("Перейти без збереження змін форми?")) return;
      const id = document.querySelector("[data-copy]").dataset.copy;
      await replacePage(id).catch(error => feedback(error.message, true));
    }
    const kind = document.getElementById("message-kind");
    if (kind) { kind.value = "note"; document.getElementById("reply-body").focus(); }
    else feedback("Відновіть заявку з архіву, щоб додати нотатку.", true);
    return;
  }
  const link = event.target.closest("a");
  if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey || event.button !== 0) return;
  const url = new URL(link.href);
  if (url.origin !== location.origin || !url.pathname.startsWith("/admin") || url.pathname.endsWith(".csv") || url.pathname.endsWith("/login") || !document.querySelector("[data-workspace], .new-form")) return;
  event.preventDefault();
  if (busy) return;
  // Text replies survive local navigation. Other edited forms require explicit choice.
  if (dirty && !document.querySelector("[data-draft] textarea:focus") && !confirm("Перейти без збереження змін форми? Чернетка повідомлення залишиться доступною в цій вкладці.")) return;
  feedback("Завантажуємо…");
  try { await replacePage(url.href); document.getElementById("feedback").hidden = true; } catch(error) { feedback(error.message, true); }
});
window.addEventListener("popstate", () => {
  if (dirty && !confirm("Перейти без збереження змін форми? Чернетка повідомлення залишиться в цій вкладці.")) {
    history.pushState({}, "", renderedUrl); return;
  }
  replacePage(location.href, {}, "none").catch(error => feedback(error.message, true));
});
window.addEventListener("beforeunload", event => {
  if (dirty || [...drafts.values()].some(d => d.body)) { event.preventDefault(); event.returnValue = ""; }
});
document.addEventListener("keydown", event => {
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    const global = document.getElementById("global-query");
    (global?.offsetParent ? global : document.getElementById("list-query"))?.focus();
  }
});
setInterval(() => {
  if (!document.hidden && !dirty && !busy && document.querySelector("[data-workspace]") && !document.activeElement.closest("form") && !document.querySelector("details[open]")) {
    replacePage(location.href, {}, "refresh").catch(() => feedback("Не вдалося оновити дані. Перевірте з’єднання.", true));
  }
}, 20000);
init();
