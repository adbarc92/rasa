// rasa UI. All PDF work is done by the local engine through the `engine` command.
const { invoke } = window.__TAURI__.core;
const dialog = window.__TAURI__.dialog;

const $ = (id) => document.getElementById(id);
let nextId = 1;

const state = {
  path: null,
  pages: [], // [{width, height}] in PDF points
  terms: [], // [{text, hitIds: []}]
  hits: new Map(), // id -> {page, rect, term, enabled}
  manual: [], // [{id, page, rect}]
};

async function engine(cmd, args = {}) {
  return invoke("engine", { request: { id: nextId++, cmd, args } });
}

function busy(on) {
  document.body.classList.toggle("busy", on);
  for (const b of document.querySelectorAll("header button, form button")) b.disabled = on || (b.id !== "open" && !state.path);
}

function showModal(title, html, actions = [{ label: "Close" }]) {
  $("modal-title").textContent = title;
  $("modal-body").innerHTML = html;
  const box = $("modal-actions");
  box.innerHTML = "";
  for (const a of actions) {
    const b = document.createElement("button");
    b.textContent = a.label;
    if (a.primary) b.className = "primary";
    b.onclick = () => { $("modal").hidden = true; a.run?.(); };
    box.appendChild(b);
  }
  $("modal").hidden = false;
}

function blindText(pages, total) {
  const which = pages.length === total ? "This file has"
    : pages.length === 1 ? `Page ${pages[0]} has` : `Pages ${pages.join(", ")} have`;
  return `${which} no searchable text. Search will find nothing there, so cover names with manual boxes.`;
}

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const fail = (e) => showModal("Something went wrong", `<p class="bad">${esc(e)}</p>`);

// ------------------------------------------------------------- opening

async function openPdf(path) {
  busy(true);
  try {
    const info = await engine("open", { path });
    Object.assign(state, { path, pages: info.pages, terms: [], hits: new Map(), manual: [] });
    $("filename").textContent = path;
    $("term").disabled = false;
    const host = $("pages");
    host.innerHTML = "";
    info.pages.forEach((p, i) => {
      const el = document.createElement("div");
      el.className = "page";
      el.dataset.page = i;
      el.style.aspectRatio = `${p.width} / ${p.height}`;
      el.innerHTML = `<span class="label">Page ${i + 1} of ${info.pages.length}</span><img alt=""><div class="overlay"></div>`;
      host.appendChild(el);
      wireDrawing(el.querySelector(".overlay"), i);
    });
    state.unreadable = info.unreadable_pages || [];
    $("blind").hidden = !state.unreadable.length;
    $("blind").textContent = blindText(state.unreadable, info.pages.length);
    if (state.unreadable.length) showModal("Search can't see this file", `<p class="bad">${esc(blindText(state.unreadable, info.pages.length))}</p><p>This happens with "Microsoft Print to PDF" and with scans: letters are stored as shapes or pictures, not text. Manual boxes still delete them permanently.</p><p>For emails, print again and choose <b>Save as PDF</b> (Chrome or Edge) instead of Microsoft Print to PDF. That keeps the text, and search will work.</p>`);
    refresh();
    for (let i = 0; i < info.pages.length; i++) {
      const { png } = await engine("render", { page: i });
      host.children[i].querySelector("img").src = `data:image/png;base64,${png}`;
    }
  } catch (e) {
    fail(e);
  } finally {
    busy(false);
    refresh();
  }
}

$("open").onclick = async () => {
  const path = await dialog.open({ multiple: false, filters: [{ name: "PDF", extensions: ["pdf"] }] });
  if (path) openPdf(path);
};

// ------------------------------------------------------------- search terms

$("term-form").onsubmit = async (ev) => {
  ev.preventDefault();
  const inputs = $("term").value.split(/\n/).map((s) => s.replace(/\s+/g, " ").trim()).filter(Boolean);
  $("term").value = "";
  for (const text of inputs) {
    if (state.terms.some((t) => t.text.toLowerCase() === text.toLowerCase())) continue;
    try {
      const { hits } = await engine("search", { terms: [text] });
      const term = { text, hitIds: [] };
      for (const h of hits) {
        const id = nextId++;
        state.hits.set(id, { ...h, enabled: true });
        term.hitIds.push(id);
      }
      state.terms.push(term);
    } catch (e) {
      fail(e);
    }
  }
  refresh();
  $("term").focus();
};

function removeTerm(i) {
  for (const id of state.terms[i].hitIds) state.hits.delete(id);
  state.terms.splice(i, 1);
  refresh();
}

// ------------------------------------------------------------- manual boxes

function wireDrawing(overlay, page) {
  overlay.addEventListener("mousedown", (ev) => {
    if (ev.button !== 0 || ev.target !== overlay) return;
    const area = overlay.getBoundingClientRect();
    const x0 = ev.clientX - area.left, y0 = ev.clientY - area.top;
    const ghost = document.createElement("div");
    ghost.className = "box drawing";
    overlay.appendChild(ghost);
    let x1 = x0, y1 = y0;
    const place = () => Object.assign(ghost.style, {
      left: `${Math.min(x0, x1)}px`, top: `${Math.min(y0, y1)}px`,
      width: `${Math.abs(x1 - x0)}px`, height: `${Math.abs(y1 - y0)}px`,
    });
    const move = (e) => {
      x1 = Math.max(0, Math.min(area.width, e.clientX - area.left));
      y1 = Math.max(0, Math.min(area.height, e.clientY - area.top));
      place();
    };
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      ghost.remove();
      if (Math.abs(x1 - x0) < 4 || Math.abs(y1 - y0) < 4) return;
      const sx = state.pages[page].width / area.width, sy = state.pages[page].height / area.height;
      state.manual.push({
        id: nextId++, page,
        rect: [Math.min(x0, x1) * sx, Math.min(y0, y1) * sy, Math.max(x0, x1) * sx, Math.max(y0, y1) * sy],
      });
      refresh();
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  });
}

document.addEventListener("keydown", (ev) => {
  if ((ev.ctrlKey || ev.metaKey) && ev.key === "z" && document.activeElement !== $("term") && state.manual.length) {
    state.manual.pop();
    refresh();
  }
});

// ------------------------------------------------------------- drawing state

function boxEl(cls, page, rect, onClick, title) {
  const p = state.pages[page];
  const el = document.createElement("div");
  el.className = `box ${cls}`;
  el.title = title;
  Object.assign(el.style, {
    left: `${(rect[0] / p.width) * 100}%`, top: `${(rect[1] / p.height) * 100}%`,
    width: `${((rect[2] - rect[0]) / p.width) * 100}%`, height: `${((rect[3] - rect[1]) / p.height) * 100}%`,
  });
  el.addEventListener("mousedown", (e) => e.stopPropagation());
  el.addEventListener("click", (e) => { e.stopPropagation(); onClick(); });
  return el;
}

function pendingRedactions() {
  const out = [];
  for (const h of state.hits.values()) if (h.enabled) out.push({ page: h.page, rect: h.rect });
  for (const m of state.manual) out.push({ page: m.page, rect: m.rect });
  return out;
}

function refresh() {
  // Sidebar
  const list = $("terms");
  list.innerHTML = "";
  state.terms.forEach((t, i) => {
    const live = t.hitIds.filter((id) => state.hits.get(id)?.enabled).length;
    const li = document.createElement("li");
    li.innerHTML = `<span class="t">${esc(t.text)}</span><span class="n ${t.hitIds.length ? "" : "zero"}">${
      t.hitIds.length ? (live === t.hitIds.length ? live : `${live}/${t.hitIds.length}`) : "not found"
    }</span><button title="Remove">✕</button>`;
    li.querySelector("button").onclick = () => removeTerm(i);
    list.appendChild(li);
  });
  $("manual-count").textContent = state.manual.length ? `${state.manual.length} box${state.manual.length > 1 ? "es" : ""}` : "None yet";
  const n = pendingRedactions().length;
  $("pending").textContent = state.path ? `${n} redaction${n === 1 ? "" : "s"} ready` : "";
  $("save").disabled = !state.path || n === 0;
  $("add-term").disabled = !state.path;

  // Page overlays
  for (const pageEl of document.querySelectorAll(".page")) {
    const overlay = pageEl.querySelector(".overlay");
    for (const b of overlay.querySelectorAll(".box:not(.drawing)")) b.remove();
  }
  const overlayFor = (i) => document.querySelector(`.page[data-page="${i}"] .overlay`);
  for (const [id, h] of state.hits) {
    overlayFor(h.page)?.appendChild(boxEl(`hit${h.enabled ? "" : " skip"}`, h.page, h.rect, () => {
      state.hits.get(id).enabled = !state.hits.get(id).enabled;
      refresh();
    }, `"${h.term}": click to ${h.enabled ? "skip" : "include"}`));
  }
  for (const m of state.manual) {
    overlayFor(m.page)?.appendChild(boxEl("manual", m.page, m.rect, () => {
      state.manual = state.manual.filter((x) => x.id !== m.id);
      refresh();
    }, "Your box: click to remove"));
  }
}

// ------------------------------------------------------------- saving

$("save").onclick = async () => {
  const redactions = pendingRedactions();
  if (!redactions.length) return;
  const base = state.path.replace(/\.pdf$/i, "");
  const out = await dialog.save({ defaultPath: `${base}-redacted.pdf`, filters: [{ name: "PDF", extensions: ["pdf"] }] });
  if (!out) return;
  busy(true);
  try {
    const terms = state.terms.map((t) => t.text);
    const r = await engine("apply", { out_path: out, redactions, terms });
    const missed = state.terms.filter((t) => !t.hitIds.length).map((t) => t.text);
    const blind = r.unreadable_pages || [];
    const blindNote = blind.length
      ? `<p class="bad"><b>Not fully checked.</b> ${esc(blindText(blind, state.pages.length))} rasa cannot read those pages, so it cannot confirm your terms are gone from them. Check them by eye.</p>`
      : "";
    const body = r.ok && blind.length
      ? `<p>${r.count} area${r.count === 1 ? "" : "s"} permanently removed. Metadata, bookmarks, links, comments, form fields and attachments were stripped.</p>${blindNote}`
      : r.ok
      ? `<p class="ok"><b>Verified.</b> ${r.count} area${r.count === 1 ? "" : "s"} permanently removed. The saved copy was re-opened and none of your search terms can be found in its text, its raw file data, metadata, bookmarks, links, comments, form fields or attachments.</p>`
      : `<p class="bad"><b>Check failed. Do not share this file yet.</b></p><ul>${r.findings.map((f) => `<li>${esc(f)}</li>`).join("")}</ul><p>Add a manual box over what remains, or a search term for it, and save again.</p>`;
    const warn = missed.length
      ? `<p class="bad">These terms were never found, so nothing was removed for them: ${missed.map(esc).join(", ")}.</p>`
      : "";
    showModal(!r.ok ? "Saved, but something remains" : blind.length ? "Saved, check it by eye" : "Redacted copy saved",
      `${body}${warn}<p class="muted">${esc(r.out_path)}</p><p class="muted">Black boxes still show how long the removed text was. Read the copy once before sending it.</p>`,
      [{ label: "Keep editing original" }, { label: "View redacted copy", primary: true, run: () => openPdf(r.out_path) }]);
  } catch (e) {
    fail(e);
  } finally {
    busy(false);
    refresh();
  }
};

refresh();
