/* bubble-fix bubble: report an issue (text, pasted or picked images) and see this cycle's list.
 *
 * One script, no build step:  <script src="http://127.0.0.1:8797/bubble.js" defer></script>
 * It talks only to the collector that served it. It sits outside <body>, so htmx body swaps
 * keep it, and takes the host app's colors and font (override with --bubble-fix-* custom properties).
 * It never writes console errors: a page's own error checks stay clean.
 */
(() => {
  "use strict";
  if (window.customElements.get("bubble-fix") || window.customElements.get("fix-heal-loop-bubble")) return;  // one bubble, even beside the old loader

  const SCRIPT = document.currentScript || [...document.scripts].find((s) => /\/bubble\.js(\?|$)/.test(s.src));
  const API = SCRIPT ? new URL(SCRIPT.src).origin : "http://127.0.0.1:8797";
  const KEY = "bubble-fix:" + location.origin;
  const recentErrors = [];
  const sentErrors = new Set();
  let errorQueue = [];
  let errorTimer = 0;

  /* ---------------------------------------------------------------- browser errors (no click needed) */
  function noteError(message, source, line, column, stack) {
    if (!message || String(source || "").startsWith(API)) return;
    const err = { message: String(message).slice(0, 2000), source: String(source || ""), line: line || 0,
      column: column || 0, stack: String(stack || "").slice(0, 8000), page: location.href, at: new Date().toISOString() };
    recentErrors.push(err);
    if (recentErrors.length > 20) recentErrors.shift();
    const key = err.message + "|" + err.source + "|" + err.line;
    if (sentErrors.has(key)) return;
    sentErrors.add(key);
    errorQueue.push(err);
    clearTimeout(errorTimer);
    errorTimer = setTimeout(flushErrors, 2000);
  }
  function flushErrors() {
    const errors = errorQueue.splice(0, 20);
    if (errors.length) call("POST", "/api/browser-errors", { errors }).catch(() => {});
  }
  window.addEventListener("error", (e) => {
    if (e.target && e.target !== window) return; // a failed image or script load, not a script error
    noteError(e.message, e.filename, e.lineno, e.colno, e.error && e.error.stack);
  });
  window.addEventListener("unhandledrejection", (e) => {
    const r = e.reason;
    noteError(r && r.message ? r.message : String(r), "", 0, 0, r && r.stack);
  });

  async function call(method, path, body) {
    const res = await fetch(API + path, {
      method, mode: "cors", credentials: "omit",
      headers: body ? { "Content-Type": "application/json" } : {},
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "The collector said no (" + res.status + ")");
    return data;
  }

  /* ---------------------------------------------------------------- look: take the host app's style */
  function hostTheme() {
    const root = getComputedStyle(document.documentElement);
    const body = document.body ? getComputedStyle(document.body) : root;
    const pick = (...names) => names.map((n) => root.getPropertyValue(n).trim()).find(Boolean);
    const bg = body.backgroundColor && body.backgroundColor !== "rgba(0, 0, 0, 0)" ? body.backgroundColor : "#ffffff";
    return {
      "--_ink": pick("--bubble-fix-ink", "--text", "--ink", "--fg", "--color-text") || body.color || "#141414",
      "--_muted": pick("--bubble-fix-muted", "--text-3", "--muted", "--text-muted") || "#6b6560",
      "--_bg": pick("--bubble-fix-bg", "--white", "--panel", "--surface", "--bg-elevated") || "#ffffff",
      "--_soft": pick("--bubble-fix-soft", "--cream", "--panel2", "--surface-2", "--hover") || "#f3f0ec",
      "--_page": bg,
      "--_line": pick("--bubble-fix-line", "--border", "--line", "--border-color") || "rgba(0,0,0,.12)",
      "--_accent": pick("--bubble-fix-accent", "--text", "--accent", "--primary") || "#141414",
      "--_launch": pick("--bubble-fix-launch", "--green", "--ok", "--accent") || "#1f9d55",
      "--_on-accent": pick("--bubble-fix-on-accent", "--white") || "#ffffff",
      "--_good": pick("--bubble-fix-good", "--green-dark", "--green", "--ok") || "#1f7a45",
      "--_warn": pick("--bubble-fix-warn", "--orange", "--warn") || "#b45309",
      "--_info": pick("--bubble-fix-info", "--blue", "--accent") || "#1f5fa8",
      "--_radius": pick("--bubble-fix-radius", "--radius") || "8px",
      "--_font": pick("--bubble-fix-font", "--font") || body.fontFamily || "system-ui, sans-serif",
    };
  }

  const CSS = `
  :host{all:initial;position:fixed;z-index:2147483000;inset:auto 0 0 auto;font-family:var(--_font);color:var(--_ink);font-size:14px;line-height:1.45}
  *{box-sizing:border-box}
  button,select,textarea,input{font:inherit;color:inherit}
  .launch{position:fixed;right:20px;bottom:20px;width:56px;height:56px;border-radius:50%;border:none;background:var(--_launch);color:#fff;box-shadow:0 6px 18px rgba(0,0,0,.18);cursor:pointer;display:flex;align-items:center;justify-content:center;transition:transform .15s}
  .launch:hover{transform:scale(1.06)}
  .launch svg{width:28px;height:28px}
  .badge{position:absolute;top:-3px;right:-3px;min-width:20px;height:20px;padding:0 6px;border-radius:999px;background:var(--_good);color:#fff;font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center;border:2px solid var(--_page)}
  .badge[hidden]{display:none}
  .dim{position:fixed;inset:0;background:rgba(0,0,0,.12)}
  .drawer{position:fixed;top:0;right:0;bottom:0;width:min(440px,100vw);background:var(--_bg);border-left:1px solid var(--_line);box-shadow:-10px 0 30px rgba(0,0,0,.12);display:flex;flex-direction:column}
  [hidden]{display:none!important}
  .head{display:flex;align-items:center;gap:8px;padding:12px 12px 8px 18px}
  .head b{flex:1;font-size:16px}
  .icon{min-width:44px;height:44px;border:none;background:none;border-radius:var(--_radius);cursor:pointer;font-size:20px;color:var(--_muted)}
  .icon:hover{background:var(--_soft)}
  .tabs{display:flex;gap:4px;margin:0 18px;background:var(--_soft);border-radius:var(--_radius);padding:3px}
  .tab{flex:1;min-height:44px;border:none;background:none;border-radius:6px;cursor:pointer;color:var(--_muted)}
  .tab[aria-selected=true]{background:var(--_bg);color:var(--_ink);font-weight:600;box-shadow:0 1px 2px rgba(0,0,0,.08)}
  .panel{flex:1;overflow:auto;padding:12px 18px 16px}
  label.field{display:block;font-weight:600;margin-bottom:6px}
  textarea{width:100%;min-height:120px;border:1px solid var(--_line);border-radius:var(--_radius);padding:10px 12px;background:var(--_bg);resize:vertical}
  textarea:focus,select:focus,button:focus-visible{outline:2px solid var(--_info);outline-offset:1px}
  .thumbs{display:flex;flex-wrap:wrap;gap:8px;margin-top:10px}
  .thumb{position:relative;width:96px;height:64px;border:1px solid var(--_line);border-radius:6px;overflow:hidden;background:var(--_soft)}
  .thumb img{width:100%;height:100%;object-fit:cover;display:block}
  .thumb button{position:absolute;top:2px;right:2px;width:28px;height:28px;border-radius:50%;border:none;background:rgba(0,0,0,.6);color:#fff;cursor:pointer}
  .drop{border:1px dashed var(--_line);border-radius:var(--_radius);padding:10px;margin-top:10px;color:var(--_muted);font-size:13px;text-align:center}
  .drop.over{border-color:var(--_info);color:var(--_info)}
  .chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
  .chip{font-size:12px;background:var(--_soft);color:var(--_muted);border-radius:999px;padding:2px 10px}
  .row-btns{display:flex;flex-wrap:wrap;gap:8px;margin-top:12px;align-items:center}
  .btn{min-height:44px;padding:0 16px;border-radius:var(--_radius);border:1px solid var(--_line);background:var(--_bg);cursor:pointer;font-weight:550;white-space:nowrap}
  .btn:hover{background:var(--_soft)}
  .btn.primary{background:var(--_accent);color:var(--_on-accent);border-color:var(--_accent)}
  .btn:disabled{opacity:.45;cursor:default}
  .link{background:none;border:none;color:var(--_info);text-decoration:underline;cursor:pointer;min-height:44px;padding:0 4px}
  .msg{margin-top:10px;font-size:13px;color:var(--_muted)}
  .msg.bad{color:var(--_warn)}
  .status{border-radius:var(--_radius);padding:8px 12px;font-size:13px;margin-bottom:10px;background:var(--_soft)}
  .status.good{color:var(--_good)} .status.bad{color:var(--_warn)}
  .status a{display:inline-flex;align-items:center;min-height:44px;color:var(--_info)}
  .card{border:1px solid var(--_line);border-radius:var(--_radius);padding:10px 12px;margin-bottom:12px;background:var(--_soft);font-size:13px}
  .card.good{border-color:var(--_good)} .card.bad{color:var(--_warn)} .card.ready{border-color:var(--_accent);background:var(--_bg)}
  .card a{display:inline-flex;align-items:center;min-height:44px;color:var(--_info)}
  .card p{margin:6px 0}
  .card-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap;min-height:32px}
  .card-head b{font-size:14px} .card-head .sub{color:var(--_muted);font-size:12px;flex:1}
  .card-head .icon{margin-left:auto}
  .cyc li{display:flex;align-items:center;gap:8px;min-height:30px}
  .cyc .t{flex:1;min-width:0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .cyc .s{font-size:12px;color:var(--_muted)}
  .dot{width:9px;height:9px;border-radius:50%;flex:0 0 auto;border:2px solid var(--_muted);animation:bubble-fix-pulse 1.4s ease-in-out infinite}
  .dot.ok{background:var(--_good);border-color:var(--_good);animation:none}
  @keyframes bubble-fix-pulse{50%{opacity:.35}}
  @media (prefers-reduced-motion:reduce){.dot{animation:none}}
  .wait{margin:0 0 4px;font-size:13px;color:var(--_info)}
  .filters{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:6px}
  .filter{min-height:44px;padding:0 12px;border-radius:999px;border:1px solid var(--_line);background:var(--_bg);cursor:pointer;font-size:13px}
  .filter[aria-pressed=true]{background:var(--_accent);color:var(--_on-accent);border-color:var(--_accent)}
  ul{list-style:none;margin:0;padding:0}
  li.item{border-bottom:1px solid var(--_line);padding:4px 0}
  li.item.dragging{opacity:.5}
  .line{display:flex;align-items:center;gap:6px;min-height:52px}
  .grip{width:28px;min-height:44px;border:none;background:none;cursor:grab;color:var(--_muted);touch-action:none;font-size:16px;letter-spacing:-3px}
  .pick{display:flex;align-items:center;gap:10px;flex:1;min-width:0;min-height:44px;cursor:pointer}
  .pick input{width:18px;height:18px;flex:0 0 auto;accent-color:var(--_accent)}
  .txt{min-width:0;flex:1}
  .title{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .meta{font-size:12px;color:var(--_muted)}
  .where{font-size:12px;color:var(--_muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .meta b{color:var(--_warn);font-weight:600}
  .tag{font-size:12px;border-radius:999px;padding:2px 9px;background:var(--_soft);color:var(--_muted);white-space:nowrap}
  .tag.broken{color:var(--_warn)} .tag.confusing{color:var(--_info)}
  .more{padding:4px 0 10px 34px;font-size:13px}
  .more pre{white-space:pre-wrap;word-break:break-word;background:var(--_soft);border-radius:6px;padding:8px;margin:6px 0;max-height:180px;overflow:auto;font-size:12px}
  .more .imgs{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0}
  .more img{max-width:100%;max-height:160px;border:1px solid var(--_line);border-radius:6px}
  .menu{display:flex;flex-wrap:wrap;gap:6px;padding:0 0 10px 34px}
  .menu .btn{min-height:44px;padding:0 12px;font-size:13px}
  .empty{color:var(--_muted);padding:24px 0;text-align:center}
  .foot{border-top:1px solid var(--_line);padding:10px 18px 14px;background:var(--_bg)}
  .foot .count{font-size:13px;color:var(--_muted);margin-right:auto}
  .check{display:flex;align-items:center;gap:8px;min-height:44px;font-size:13px;color:var(--_muted);cursor:pointer}
  .sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
  `;

  const ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 11.5a8.5 8.5 0 0 1-12.3 7.6L3.5 20.5l1.4-4.6A8.5 8.5 0 1 1 21 11.5z"/><path d="M9 13.2c.8 1 1.8 1.5 3 1.5s2.2-.5 3-1.5"/><path d="M9.2 9.4h.01M14.8 9.4h.01" stroke-width="2.6"/></svg>';
  const LABELS = { broken: "Broken", confusing: "Confusing", polish: "Polish" };
  const HEAL_LIMIT = 3; // errors per heal run (lib/cycles.py HEAL_LIMIT)
  const KIND = { note: "Your note", error: "Error", browser_error: "Browser error" };

  const el = (tag, attrs = {}, ...kids) => {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "text") node.textContent = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else if (v === true) node.setAttribute(k, "");
      else if (v !== false && v != null) node.setAttribute(k, v);
    }
    for (const kid of kids.flat()) if (kid != null) node.append(kid);
    return node;
  };
  // Where an issue happened, short: the path (plus the host when it is another app, e.g. Live vs Development).
  const where = (page) => {
    try {
      const url = new URL(page);
      return (url.host === location.host ? "" : url.host) + url.pathname + url.search + url.hash;
    } catch (e) { return page; } // server errors log a bare path
  };
  // How long a cycle has run: "45 s", "3 min", "1 h 5 min".
  const since = (iso) => {
    const sec = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000));
    if (sec < 60) return sec + " s";
    const min = Math.floor(sec / 60);
    return min < 60 ? min + " min" : Math.floor(min / 60) + " h " + (min % 60) + " min";
  };
  // The cycle summary's first line, without the "Branch ... ready. Merge?" lead (the card has the button).
  const firstLine = (summary) => ((summary || "").split("\n").find((l) => l.trim()) || "")
    .replace(/^Branch \S+ ready[^.]*\.\s*(Merge\?\s*)?/, "").slice(0, 240);
  const toBase64 = (file) => new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.onerror = () => reject(new Error("Could not read " + file.name));
    reader.readAsDataURL(file);
  });

  class Bubble extends HTMLElement {
    constructor() {
      super();
      this.root = this.attachShadow({ mode: "open" });
      this.state = { items: [], cycle: null, project: "" };
      this.tab = "report";
      this.filter = "all";
      this.selected = new Set();
      this.open = new Set();
      this.images = [];
      this.imgCache = new Map();
      this.pollTimer = 0;
      this.seq = 0;
      this.notes = new Map();
      this.stale = false;
      this.healChoice = false;
    }

    connectedCallback() {
      if (this.built) return;
      this.built = true;
      for (const [k, v] of Object.entries(hostTheme())) this.style.setProperty(k, v);
      this.root.append(el("style", { text: CSS }));
      this.launch = el("button", { class: "launch", type: "button", "aria-label": "Report an issue or see the list",
        title: "bubble-fix: report an issue or see the list", onclick: () => this.toggle(true) });
      this.launch.innerHTML = ICON;
      this.badge = el("span", { class: "badge", hidden: true });
      this.launch.append(this.badge);
      this.dim = el("div", { class: "dim", hidden: true, onclick: () => this.toggle(false) });
      this.drawer = el("section", { class: "drawer", role: "dialog", "aria-label": "Issues", hidden: true });
      this.root.append(this.launch, this.dim, this.drawer);
      this.drawer.addEventListener("paste", (e) => this.onPaste(e));
      // A change held while the user typed is drawn when focus leaves the box, but never between a press and its
      // click (a redraw then would swap out the button they are pressing): after the click, or 400 ms after release.
      // Each release's fallback timer only ends its own press, never a newer one started within 400 ms.
      this.drawer.addEventListener("pointerdown", () => { this.pressing = true; this.press = (this.press || 0) + 1; }, true);
      this.drawer.addEventListener("click", () => { this.pressing = false; setTimeout(() => this.flushStale(), 0); }, true);
      const release = () => {
        const press = this.press;
        setTimeout(() => { if (press === this.press) { this.pressing = false; this.flushStale(); } }, 400);
      };
      window.addEventListener("pointerup", release);
      // A finger scroll or a long press ends with pointercancel, not pointerup: it ends the press too, at once
      // (no click follows), so updates never stay held.
      window.addEventListener("pointercancel", () => { this.press = (this.press || 0) + 1; this.pressing = false; this.flushStale(); });
      this.drawer.addEventListener("focusout", () => setTimeout(() => { if (!this.pressing) this.flushStale(); }, 0));
      // A finger on the drawer (a swipe through the list): changes wait until it lifts, so the list keeps scrolling.
      this.drawer.addEventListener("touchstart", () => { this.touching = true; }, { passive: true, capture: true });
      const lift = (e) => { if (!e.touches || !e.touches.length) { this.touching = false; setTimeout(() => this.flushStale(), 0); } };
      window.addEventListener("touchend", lift, { passive: true });
      window.addEventListener("touchcancel", lift, { passive: true });
      this.drawer.addEventListener("dragover", (e) => { e.preventDefault(); this.dropZone && this.dropZone.classList.add("over"); });
      this.drawer.addEventListener("dragleave", () => this.dropZone && this.dropZone.classList.remove("over"));
      this.drawer.addEventListener("drop", (e) => { e.preventDefault(); if (e.dataTransfer.files.length) this.addFiles(e.dataTransfer.files); });
      this.addEventListener("keydown", (e) => { if (e.key === "Escape" && !this.drawer.hidden) this.toggle(false); });
      document.addEventListener("visibilitychange", () => this.schedule());
      this.refresh();
    }

    toggle(show) {
      this.drawer.hidden = !show;
      this.dim.hidden = !show;
      this.launch.hidden = show;
      if (show) { this.render(); this.refresh(); }
      this.schedule();
    }

    schedule() {
      clearTimeout(this.pollTimer);
      if (document.hidden) return;
      const active = this.state.cycle && ["queued", "running"].includes(this.state.cycle.status);
      // Open: every second, so a run started anywhere (the bubble, Claude Code, Codex) shows within a second; a check
      // that brings nothing new draws nothing. Closed: just the badge.
      const wait = this.drawer.hidden ? (active ? 5000 : 15000) : 1000;
      this.pollTimer = setTimeout(() => this.refresh(), wait);
    }

    async refresh() {
      const seq = ++this.seq;
      let next = this.state;
      try {
        next = await call("GET", "/api/state");
        this.error = "";
      } catch (err) {
        this.error = err.message === "Failed to fetch" ? "The bubble-fix collector is not answering." : err.message;
      }
      if (seq !== this.seq) return; // a newer refresh (after a click) already answered
      const sig = JSON.stringify(next) + "|" + this.error;
      const changed = sig !== this.sig;
      this.sig = sig;
      this.state = next;
      const ids = new Set(this.listed().map((i) => i.id));
      for (const id of [...this.selected]) if (!ids.has(id)) this.selected.delete(id);
      const open = this.listed().filter((i) => i.status !== "next").length;
      this.badge.hidden = !open;
      this.badge.textContent = String(open);
      if (changed || this.stale) {
        if (this.pressing || this.touching) this.stale = true;  // never redraw mid-press or mid-swipe: drawn right after
        else this.paint();
      }
      this.tick();
      this.schedule();
    }

    // Draw the Issues tab, unless the user is typing there: then it stays stale and is drawn when they stop.
    paint() {
      const typing = !this.drawer.hidden && this.tab === "issues" && this.busyTyping();
      this.stale = typing;
      if (!this.drawer.hidden && this.tab === "issues" && !typing) this.render();
      else if (this.issuesTab) this.issuesTab.textContent = "Issues (" + this.listed().length + ")";
    }

    flushStale() {
      if (this.stale && !this.pressing && !this.touching && !this.busyTyping()) this.paint();
    }

    // The running cycle's clock moves every second without redrawing anything else; it stops once the cycle
    // is no longer running, even before a redraw.
    tick() {
      const c = this.state.cycle;
      if (this.clock && c && c.started_at && c.status === "running") this.clock.textContent = since(c.started_at);
    }

    // Items on the list: everything except what the running cycle has taken (that shows in its card).
    listed() {
      return this.state.items.filter((i) => i.status !== "fixing");
    }

    busyTyping() {
      const active = this.root.activeElement;
      return !!active && (active.tagName === "TEXTAREA" || active.dataset.keep === "1");
    }

    /* ------------------------------------------------------------ layout */
    render() {
      const tabBtn = (key, text) => el("button", { class: "tab", type: "button", role: "tab", "aria-selected": String(this.tab === key),
        "data-k": "tab-" + key, text, onclick: () => { this.tab = key; this.render(); } });
      const listed = this.listed().length;
      const before = this.root.querySelector(".panel");
      const keep = { tab: this.drawnTab, top: before ? before.scrollTop : 0,
        focus: this.root.activeElement && this.root.activeElement.dataset ? this.root.activeElement.dataset.k : "" };
      this.clock = null;
      this.drawnTab = this.tab;
      this.drawer.replaceChildren(
        el("div", { class: "head" },
          el("b", { text: "bubble-fix" + (this.state.project ? " · " + this.state.project : "") }),
          el("button", { class: "icon", type: "button", "aria-label": "Close issues", text: "×", onclick: () => this.toggle(false) })),
        el("div", { class: "tabs", role: "tablist" }, tabBtn("report", "Report"), this.issuesTab = tabBtn("issues", "Issues (" + listed + ")")),
        this.tab === "report" ? this.reportPanel() : this.issuesPanel(),
        ...(this.tab === "issues" ? [this.issuesFoot()] : []));
      const after = this.root.querySelector(".panel");
      if (after && keep.tab === this.tab) after.scrollTop = keep.top;
      if (keep.focus) {
        const node = [...this.root.querySelectorAll("[data-k]")].find((n) => n.dataset.k === keep.focus);
        if (node) node.focus({ preventScroll: true });
      }
      this.tick();
    }

    reportPanel() {
      const area = el("textarea", { id: "bubble-fix-text", rows: "5", placeholder: "A few words about what's wrong. Then paste a screenshot (Ctrl+V) if it helps.",
        oninput: (e) => { this.draft = e.target.value; if (this.sentId && this.msgEl) { this.sentId = 0; this.reportMsg = ""; this.msgEl.replaceChildren(); } if (this.sendBtn) this.sendBtn.disabled = !!this.sending || !this.draft.trim(); } });
      area.value = this.draft || "";
      const picker = el("input", { type: "file", class: "sr", accept: "image/png,image/jpeg,image/gif,image/webp", multiple: true,
        "aria-label": "Image files", onchange: (e) => { this.addFiles(e.target.files); e.target.value = ""; } });
      this.dropZone = el("div", { class: "drop", text: "Paste, or drop image files here" });
      const thumbs = el("div", { class: "thumbs" }, this.images.map((img, i) => el("div", { class: "thumb" },
        el("img", { src: img.url, alt: img.file.name || "pasted image" }),
        el("button", { type: "button", "aria-label": "Remove image", text: "×", onclick: () => this.removeImage(i) }))));
      const errs = recentErrors.length;
      return el("div", { class: "panel", role: "tabpanel" },
        el("label", { class: "field", for: "bubble-fix-text", text: "What's wrong?" }), area, thumbs, this.dropZone,
        el("div", { class: "chips" }, el("span", { class: "chip", text: "this page" }), el("span", { class: "chip", text: "the time" }),
          errs ? el("span", { class: "chip", text: errs + " browser error" + (errs > 1 ? "s" : "") }) : null),
        el("div", { class: "row-btns" },
          el("button", { class: "btn", type: "button", text: "Add image", onclick: () => picker.click() }), picker,
          el("span", { style: "flex:1" }),
          this.sendBtn = el("button", { class: "btn primary", type: "button", text: this.sending ? "Sending" : "Send",
            disabled: !!this.sending || !(this.draft || "").trim(),
            onclick: () => this.send() })),
        this.msgEl = el("div", { class: "msg" + (this.reportBad ? " bad" : ""), role: "status" }, this.reportMsg || "",
          this.sentId && !this.reportBad ? el("button", { class: "link", type: "button", text: "See the list",
            onclick: () => { this.tab = "issues"; this.render(); } }) : null));
    }

    // One card at the top of Issues: Fixing, then Ready to merge, then Merged (or Did not finish).
    flowCard() {
      const c = this.state.cycle;
      if (this.error) return el("div", { class: "card bad", text: this.error });
      if (!c) return null;
      const items = c.items || [];
      const fixedN = items.filter((i) => i.status === "fixed").length;
      const rows = (list, word) => el("ul", { class: "cyc", "aria-label": "Items in cycle " + c.id }, list.map((i) =>
        el("li", {}, el("span", { class: "dot" + (i.status === "fixed" ? " ok" : ""), "aria-hidden": "true" }),
          el("span", { class: "t", text: i.title }), el("span", { class: "s", text: i.status === "fixed" ? "Fixed" : word }))));
      const head = (text, ...extra) => el("div", { class: "card-head" }, el("b", { text }), ...extra);
      const close = () => el("button", { class: "icon", type: "button", "aria-label": "Hide this card", text: "×",
        onclick: () => this.hide(c.id) });
      if (c.mode === "plan" && ["queued", "running"].includes(c.status)) {
        // Plan in plan-html runs in rounds: a run writes the round (reads the code, groups the items, designs flows),
        // then the deck opens for the user; their answers start the next round; Finalize hands the plan to a fix cycle.
        const round = Number(c.round || 1);
        const stop = el("button", { class: "link", type: "button", text: "Stop planning", onclick: () => this.stopPlan(c.id) });
        if (c.phase === "serve") {
          return el("div", { class: "card good" }, head("Plan · round " + round, stop), c.url
            ? el("p", {}, "Your plan deck is ready: ", el("a", { href: c.url, target: "_blank", rel: "noopener", text: "open it" }))
            : el("p", { text: "Opening your plan deck." }));
        }
        return el("div", { class: "card", role: "status" }, head((round === 1 ? "Planning" : "Updating the plan") + " · round " + round, stop),
          el("p", { text: round === 1 ? "Reading the code and grouping your items into a plan. The deck opens here when it is ready."
            : "Working your answers into round " + round + ". The deck opens here when it is ready." }), rows(items, ""));
      }
      if (c.status === "queued") return el("div", { class: "card" }, head("Starting cycle " + c.id), rows(items, "Waiting"));
      if (c.status === "running") {
        this.clock = el("span", { class: "clock" });
        const verb = (c.mode === "heal" ? "Healing" : "Fixing") + (c.session ? " in your session" : "");
        return el("div", { class: "card", role: "status" },
          head(verb + " · cycle " + c.id, el("span", { class: "sub" }, fixedN + " of " + items.length + " fixed · ", this.clock)),
          rows(items, "Working"));
      }
      if (this.isHidden(c.id)) return null;
      const done = items.filter((i) => i.status === "fixed");
      if (c.status === "done" && c.merged_at) {
        return el("div", { class: "card good" }, head("Merged · cycle " + c.id, close()),
          el("p", { text: "In " + c.into + (c.merge_commit ? " (" + c.merge_commit + ")" : "") + ". " + done.length + " fixed." }));
      }
      if (c.status === "done" && c.can_merge) {
        return el("div", { class: "card ready" }, head("Ready to merge · cycle " + c.id),
          el("p", { text: firstLine(c.summary) }), rows(done, ""),
          this.mergeButtons(c));
      }
      if (c.status === "done") {
        const text = c.mode === "plan" ? firstLine(c.summary) : fixedN ? fixedN + " of " + items.length + " fixed. " + firstLine(c.summary)
          : "Nothing fixed. " + firstLine(c.summary);
        return el("div", { class: "card" + (fixedN || c.mode === "plan" ? " good" : " bad") }, head("Cycle " + c.id + " done", close()),
          el("p", { text }), c.merge_note && fixedN && c.work === "worktree" ? el("p", { class: "msg", text: "Merge: " + c.merge_note + "." }) : null);
      }
      if (c.status === "cancelled") return el("div", { class: "card" }, head("Cycle " + c.id + " stopped", close()), el("p", { text: firstLine(c.summary) }));
      return el("div", { class: "card bad" }, head("Cycle " + c.id + " did not finish", close()), el("p", { text: firstLine(c.summary) }));
    }

    mergeButtons(c) {
      const mine = this.mergeFor === c.id;
      return el("div", {},
        el("div", { class: "row-btns" },
          el("button", { class: "btn primary", type: "button", "data-k": "merge-" + c.id, disabled: !!this.merging,
            text: this.merging && mine ? "Merging" : "Merge into " + c.into, onclick: () => this.merge(c.id) }),
          el("button", { class: "btn", type: "button", text: "Not now", onclick: () => this.hide(c.id) })),
        el("p", { class: "msg" + (mine && this.mergeBad ? " bad" : ""), role: "status", text: mine ? this.mergeMsg || "" : "" }));
    }

    // Fixes from earlier cycles that still wait on their branch (a newer cycle started before Merge).
    waitingCards() {
      const c = this.state.cycle;
      const ready = [...(this.state.waiting || []).map((w) => w.id), ...(c && c.can_merge ? [c.id] : [])];
      const hiddenIds = ready.filter((id) => this.isHidden(id));
      const show = hiddenIds.length ? el("button", { class: "link", type: "button",
        text: "Show " + hiddenIds.length + " hidden fix" + (hiddenIds.length > 1 ? "es" : "") + " waiting to merge",
        onclick: () => { localStorage.setItem(KEY + ":hidden-cycles", JSON.stringify(this.hidden().filter((id) => !hiddenIds.includes(id)))); this.render(); } }) : null;
      return [...(this.state.waiting || []).filter((w) => !this.isHidden(w.id)).map((w) =>
        el("div", { class: "card ready" }, el("div", { class: "card-head" }, el("b", { text: "Ready to merge · cycle " + w.id })),
          el("p", { text: firstLine(w.summary) }),
          el("ul", { class: "cyc", "aria-label": "Items in cycle " + w.id }, w.items.map((i) => el("li", {},
            el("span", { class: "dot ok", "aria-hidden": "true" }), el("span", { class: "t", text: i.title }), el("span", { class: "s", text: "Fixed" })))),
          this.mergeButtons(w))), show];
    }

    hidden() {
      try { return JSON.parse(localStorage.getItem(KEY + ":hidden-cycles") || "[]"); } catch (e) { return []; }
    }

    isHidden(id) {
      return this.hidden().includes(id);
    }

    hide(id) {
      localStorage.setItem(KEY + ":hidden-cycles", JSON.stringify([...this.hidden(), id].slice(-50)));
      this.render();
    }

    visible() {
      const f = this.filter;
      return this.listed().filter((i) => f === "all" || (f === "mine" && i.kind === "note") || (f === "errors" && i.kind !== "note")
        || (f === "broken" && i.label === "broken") || (f === "next" && i.status === "next"));
    }

    issuesPanel() {
      const items = this.listed();
      const count = (fn) => items.filter(fn).length;
      const filters = [["all", "All", items.length], ["mine", "Mine", count((i) => i.kind === "note")],
        ["errors", "Errors", count((i) => i.kind !== "note")], ["broken", "Broken", count((i) => i.label === "broken")],
        ["next", "Next cycle", count((i) => i.status === "next")]];
      const shown = this.visible();
      const list = el("ul", { "aria-label": "Issues for this cycle" }, shown.map((i) => this.row(i)));
      return el("div", { class: "panel", role: "tabpanel" }, this.flowCard(), ...this.waitingCards(),
        el("div", { class: "filters", role: "group", "aria-label": "Filter" }, filters.map(([key, text, n]) =>
          el("button", { class: "filter", type: "button", "aria-pressed": String(this.filter === key), "data-k": "f-" + key, text: text + " " + n,
            onclick: () => { this.filter = key; this.render(); } }))),
        shown.length ? list : el("p", { class: "empty", text: items.length ? "Nothing in this filter." : "Nothing on the list. Report an issue on the first tab." }));
    }

    row(item) {
      const fixing = item.status === "fixing";
      const state = fixing ? "Fixing" : item.status === "next" ? "Next cycle" : item.size === "needs_call" ? "Needs your call"
        : item.kind !== "note" && item.heal_suggested ? "Heal suggested" : "";
      const meta = [KIND[item.kind], item.count > 1 ? item.count + "×" : ""].filter(Boolean).join(" · ");
      const box = el("input", { type: "checkbox", disabled: fixing, checked: this.selected.has(item.id), "data-k": "pick-" + item.id,
        onchange: (e) => { e.target.checked ? this.selected.add(item.id) : this.selected.delete(item.id); this.render(); } });
      const grip = el("button", { class: "grip", type: "button", "aria-label": "Move " + item.title + " (arrow keys)", text: "⋮⋮" });
      grip.addEventListener("pointerdown", (e) => this.dragStart(e, item.id));
      grip.addEventListener("keydown", (e) => this.keyMove(e, item.id));
      const li = el("li", { class: "item", "data-id": String(item.id) },
        el("div", { class: "line" }, grip,
          el("label", { class: "pick" }, box, el("div", { class: "txt" }, el("div", { class: "title", text: item.title }),
            item.page ? el("div", { class: "where", title: item.page, text: where(item.page) }) : null,
            el("div", { class: "meta" }, meta, state ? " · " : "", state ? el("b", { text: state }) : null))),
          el("span", { class: "tag " + (item.label || ""), text: LABELS[item.label] || "New" }),
          el("button", { class: "icon", type: "button", "aria-expanded": String(this.open.has(item.id)), "aria-label": "More for " + item.title, "data-k": "more-" + item.id,
            text: "⋯", onclick: () => { this.open.has(item.id) ? this.open.delete(item.id) : this.open.add(item.id); this.render(); } })));
      if (this.open.has(item.id)) li.append(this.details(item), this.menu(item));
      return li;
    }

    details(item) {
      const imgs = el("div", { class: "imgs" });
      for (const f of item.files) this.loadImage(item.id, f.name).then((url) => url && imgs.append(el("img", { src: url, alt: item.title })));
      return el("div", { class: "more" },
        item.body ? el("pre", { text: item.body }) : null,
        item.extra.trim() ? el("div", {}, el("b", { text: "Added later" }), el("pre", { text: item.extra.trim() })) : null,
        item.files.length ? imgs : null,
        item.proposal ? el("p", {}, el("b", { text: "Proposed fix: " }), item.proposal) : null,
        item.page ? el("p", { class: "meta", text: "Page: " + item.page }) : null);
    }

    menu(item) {
      const act = (action, extra) => this.itemAction(item.id, Object.assign({ action }, extra || {}));
      const label = (key) => el("button", { class: "btn", type: "button", "aria-pressed": String(item.label === key), text: LABELS[key],
        onclick: () => act("label", { label: key }) });
      const text = el("textarea", { rows: "2", "data-keep": "1", "data-k": "text-" + item.id, "aria-label": "Add text to " + item.title,
        placeholder: "Add text", oninput: (e) => this.notes.set(item.id, e.target.value) });
      text.value = this.notes.get(item.id) || "";
      return el("div", { class: "menu" }, label("broken"), label("confusing"), label("polish"),
        item.status === "next" ? el("button", { class: "btn", type: "button", text: "Back on the list", onclick: () => act("open") })
          : el("button", { class: "btn", type: "button", text: "Next cycle", disabled: item.status === "fixing", onclick: () => act("next") }),
        el("button", { class: "btn", type: "button", text: "Drop", disabled: item.status === "fixing", onclick: () => act("drop") }),
        el("div", { style: "flex-basis:100%" }, text,
          el("button", { class: "btn", type: "button", text: "Save text", onclick: () => {
            if (!text.value.trim()) return;
            this.notes.delete(item.id);
            act("text", { text: text.value });
          } })));
    }

    issuesFoot() {
      const items = this.listed();
      const cycle = this.state.cycle;
      const active = cycle && ["queued", "running"].includes(cycle.status);
      const openIds = items.filter((i) => i.status === "open").map((i) => i.id);
      const picked = items.filter((i) => this.selected.has(i.id) && i.status !== "fixing").map((i) => i.id);
      const errors = items.filter((i) => i.kind !== "note" && i.status === "open");
      const healIds = errors.filter((i) => this.selected.has(i.id)).map((i) => i.id).slice(0, HEAL_LIMIT);
      const healCount = Math.min(healIds.length || errors.length, HEAL_LIMIT);
      const healText = errors.length > HEAL_LIMIT && !healIds.length ? "Heal " + healCount + " of " + errors.length + " errors"
        : "Heal errors (" + healCount + ")";
      // Where Fix works is the build's registration, not a choice here: in place, unless the build's checkout is
      // shared (another agent builds there), when it is registered to fix in a new worktree.
      const where = this.state.default_work === "worktree" ? "worktree" : "here";
      const heal = (work) => this.startCycle("heal", healIds.length ? healIds : errors.slice(0, HEAL_LIMIT).map((i) => i.id), work);
      return el("div", { class: "foot" },
        active ? el("p", { class: "wait", text: "Cycle " + cycle.id + " is running. Pick items now; you can start Fix when it is done." }) : null,
        el("div", { class: "row-btns" }, el("span", { class: "count", text: picked.length + " selected" }),
          el("button", { class: "btn", type: "button", "data-k": "fix-all", text: "Fix all " + openIds.length, disabled: active || !openIds.length,
            onclick: () => this.startCycle("fix", openIds, where) }),
          el("button", { class: "btn primary", type: "button", "data-k": "fix-picked", text: "Fix selected", disabled: active || !picked.length,
            onclick: () => this.startCycle("fix", picked, where) })),
        el("div", { class: "row-btns" },
          errors.length ? el("button", { class: "btn", type: "button", disabled: active, "aria-expanded": String(this.healChoice),
            text: healText, onclick: () => { this.healChoice = !this.healChoice; this.render(); } }) : null,
          el("button", { class: "link", type: "button", text: "Plan in plan-html", disabled: active,
            onclick: () => this.startCycle("plan", picked, "here") })),
        this.healChoice && errors.length ? el("div", { class: "row-btns" }, el("span", { class: "count", text: "Heal in:" }),
          el("button", { class: "btn primary", type: "button", text: "New worktree", onclick: () => heal("worktree") }),
          el("button", { class: "btn", type: "button", text: "Same tree", onclick: () => heal("here") })) : null,
        el("div", { class: "msg" + (this.footBad ? " bad" : ""), role: "status", text: this.footMsg || "" }));
    }

    /* ------------------------------------------------------------ actions */
    async startCycle(mode, ids, work) {
      this.healChoice = false;
      const before = this.state;
      if (mode !== "plan") {  // show the Fixing card now; the collector confirms within a second
        const picked = new Set(ids);
        const items = before.items.map((i) => (picked.has(i.id) ? Object.assign({}, i, { status: "fixing" }) : i));
        const cycleItems = before.items.filter((i) => picked.has(i.id)).map((i) => ({ id: i.id, title: i.title, kind: i.kind, status: "fixing" }));
        this.state = Object.assign({}, before, { items, cycle: { id: "…", mode, work, status: "queued", items: cycleItems, item_ids: ids } });
      }
      this.selected.clear();
      this.footMsg = mode === "plan" ? "Planning started. The deck opens above when round 1 is ready." : "";
      this.footBad = false;
      this.render();
      try {
        await call("POST", "/api/cycle", { mode, ids, work });
      } catch (err) {
        this.state = before;
        this.footMsg = err.message;
        this.footBad = true;
        this.render();
      }
      this.refresh();
    }

    async stopPlan(id) {
      try {
        await call("POST", "/api/cycle/stop", { cycle: id });
        this.footMsg = "";
      } catch (err) {
        this.footMsg = err.message;
        this.footBad = true;
      }
      this.render();
      this.refresh();
    }

    async itemAction(id, body) {
      const before = this.state;
      const change = { next: { status: "next" }, open: { status: "open" }, drop: { status: "dropped" }, label: { label: body.label } }[body.action];
      if (change) {  // show it now
        const items = before.items.map((i) => (i.id === id ? Object.assign({}, i, change) : i)).filter((i) => i.status !== "dropped");
        this.state = Object.assign({}, before, { items });
        if (body.action === "drop") this.open.delete(id);
        this.render();
      }
      try {
        await call("POST", "/api/items/" + id, body);
        this.footMsg = "";
      } catch (err) {
        this.state = before;
        this.footMsg = err.message;
        this.footBad = true;
        this.render();
      }
      this.refresh();
    }

    async merge(cycleId) {
      this.merging = true;
      this.mergeFor = cycleId;
      this.mergeMsg = "";
      this.mergeBad = false;
      this.render();
      try {
        const done = await call("POST", "/api/merge", { cycle: cycleId });
        this.mergeMsg = done.message;
        this.mergeBad = !done.ok;
        if (done.ok && (!this.state.cycle || this.state.cycle.id !== cycleId)) { this.footMsg = done.message; this.footBad = false; }
      } catch (err) {
        this.mergeMsg = err.message;
        this.mergeBad = true;
      }
      this.merging = false;
      this.sig = "";  // draw the answer even if the state has not changed yet
      this.refresh();
    }

    async loadImage(id, name) {
      const key = id + "/" + name;
      if (!this.imgCache.has(key)) {
        this.imgCache.set(key, fetch(API + "/api/files/" + key, { mode: "cors", credentials: "omit" })
          .then((r) => (r.ok ? r.blob() : null)).then((b) => (b ? URL.createObjectURL(b) : null)).catch(() => null));
      }
      return this.imgCache.get(key);
    }

    onPaste(e) {
      const files = [...(e.clipboardData ? e.clipboardData.items : [])].filter((i) => i.kind === "file" && i.type.startsWith("image/"))
        .map((i) => i.getAsFile()).filter(Boolean);
      if (!files.length) return;
      e.preventDefault();
      if (this.tab !== "report") this.tab = "report";
      this.addFiles(files);
    }

    addFiles(list) {
      const ok = ["image/png", "image/jpeg", "image/gif", "image/webp"];
      let refused = 0;
      for (const file of [...(list || [])]) {
        if (!ok.includes(file.type) || file.size > 8 * 1024 * 1024 || this.images.length >= 10) { refused += 1; continue; }
        this.images.push({ file, url: URL.createObjectURL(file) });
      }
      this.reportMsg = refused ? "Some files were left out: PNG, JPEG, GIF or WebP, up to 8 MB, 10 at most." : "";
      this.reportBad = !!refused;
      this.tab = "report";
      this.render();
    }

    removeImage(index) {
      const [gone] = this.images.splice(index, 1);
      if (gone) URL.revokeObjectURL(gone.url);
      this.render();
    }

    async send() {
      const text = this.draft || "";
      if (!text.trim()) {
        this.reportMsg = "Add a few words about what's wrong.";
        this.reportBad = true;
        this.render();
        return;
      }
      this.sending = true;
      this.render();
      try {
        const images = await Promise.all(this.images.map(async (img) => ({ name: img.file.name || "pasted.png", type: img.file.type,
          data: await toBase64(img.file) })));
        const sent = await call("POST", "/api/report", { text, page: location.href, title: document.title,
          viewport: innerWidth + "x" + innerHeight, user_agent: navigator.userAgent, browser_errors: recentErrors.slice(-10), images });
        this.images.forEach((img) => URL.revokeObjectURL(img.url));
        this.images = [];
        this.draft = "";
        this.sentId = sent.id;
        this.reportMsg = "Sent, it's on the list. Write the next one, or ";
        this.reportBad = false;
      } catch (err) {
        this.reportMsg = err.message === "Failed to fetch" ? "Not sent: the collector is not answering." : "Not sent: " + err.message;
        this.reportBad = true;
        this.sentId = 0;
      }
      this.sending = false;
      this.render();
      this.refresh();
    }

    /* ------------------------------------------------------------ reorder (drag, or arrow keys on the grip) */
    saveOrder(visibleIds) {
      const all = this.state.items.map((i) => i.id);
      const shown = new Set(visibleIds);
      const slots = all.map((id, n) => (shown.has(id) ? n : -1)).filter((n) => n >= 0);
      slots.forEach((slot, k) => { all[slot] = visibleIds[k]; });
      const byId = new Map(this.state.items.map((i) => [i.id, i]));
      this.state.items = all.map((id) => byId.get(id));
      this.render();
      call("POST", "/api/order", { ids: all }).catch((err) => { this.footMsg = err.message; this.footBad = true; this.refresh(); });
    }

    keyMove(e, id) {
      if (e.key !== "ArrowUp" && e.key !== "ArrowDown") return;
      e.preventDefault();
      const ids = this.visible().map((i) => i.id);
      const at = ids.indexOf(id);
      const to = e.key === "ArrowUp" ? at - 1 : at + 1;
      if (to < 0 || to >= ids.length) return;
      [ids[at], ids[to]] = [ids[to], ids[at]];
      this.saveOrder(ids);
      const grip = this.root.querySelector('li[data-id="' + id + '"] .grip');
      if (grip) grip.focus();
    }

    dragStart(e, id) {
      const li = this.root.querySelector('li[data-id="' + id + '"]');
      if (!li) return;
      e.preventDefault();
      li.classList.add("dragging");
      const move = (ev) => {
        const over = this.root.elementFromPoint(ev.clientX, ev.clientY);
        const target = over && over.closest ? over.closest("li.item") : null;
        if (!target || target === li) return;
        const box = target.getBoundingClientRect();
        target.parentNode.insertBefore(li, ev.clientY < box.top + box.height / 2 ? target : target.nextSibling);
      };
      const up = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
        li.classList.remove("dragging");
        const ids = [...this.root.querySelectorAll("li.item")].map((n) => Number(n.dataset.id));
        this.saveOrder(ids);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    }
  }

  window.customElements.define("bubble-fix", Bubble);
  const mount = () => {
    if (!document.querySelector("bubble-fix")) document.documentElement.append(document.createElement("bubble-fix"));
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount);
  else mount();
  document.addEventListener("htmx:afterSettle", mount);
  window.addEventListener("popstate", mount);
})();
