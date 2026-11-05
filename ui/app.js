// rasa UI. All PDF work is done by the local engine through the `engine` command.
const { invoke } = window.__TAURI__.core;
const dialog = window.__TAURI__.dialog;

const $ = (id) => document.getElementById(id);
let nextId = 1;

const state = {
  folder: null, // set when a folder is open
  files: [], // [{path, name, pages, unreadable, hits: Map(id -> {page, rect, term, enabled}), manual: [], error}]
  cur: -1,
  terms: [], // search terms, applied to every file
  ocr: false, // Windows text recognition available
};
const file = () => state.files[state.cur];
let renderToken = 0;

async function engine(cmd, args = {}) {
  return invoke("engine", { request: { id: nextId++, cmd, args } });
}

function busy(on, label = "") {
  document.body.classList.toggle("busy", on);
  for (const b of document.querySelectorAll("header button, form button"))
    b.disabled = on || (!b.dataset.always && !state.files.length);
  $("pending").textContent = label;
  if (!on) refresh();
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

const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
const fail = (e) => showModal("Something went wrong", `<p class="bad">${esc(e)}</p>`);
const plural = (n, word, many = word + "s") => `${n} ${n === 1 ? word : many}`;

function pagesPhrase(pages, total) {
  return pages.length === total ? "This file has"
    : pages.length === 1 ? `Page ${pages[0]} has` : `Pages ${pages.join(", ")} have`;
}

function blindText(pages, total) {
  return `${pagesPhrase(pages, total)} no searchable text. Search will find nothing there, so cover names with manual boxes.`;
}

function ocrText(pages, total) {
  return `${pagesPhrase(pages, total)} no text layer, so rasa reads ${pages.length === 1 ? "it" : "them"} with Windows text recognition. It can misread letters: look over ${pages.length === 1 ? "that page" : "those pages"} and box anything search missed.`;
}

// ------------------------------------------------------------- opening

async function openFiles(paths, folder) {
  busy(true, "Opening…");
  try {
    await engine("close_all");
    Object.assign(state, { folder, files: [], cur: -1 }); // terms carry over to the new files
    for (const [i, path] of paths.entries()) {
      $("pending").textContent = `Opening ${i + 1} of ${paths.length}…`;
      const name = folder ? path.slice(folder.length).replace(/^[\\/]+/, "") : path.split(/[\\/]/).pop();
      const f = { path, name, pages: [], unreadable: [], hits: new Map(), manual: [], error: null };
      try {
        const info = await engine("open", { path });
        f.pages = info.pages;
        f.unreadable = info.unreadable_pages || [];
        state.ocr = info.ocr_available;
      } catch (e) {
        f.error = String(e);
      }
      state.files.push(f);
    }
    $("filename").textContent = folder || paths[0] || "";
    $("term").disabled = false;
    if (!state.files.length) {
      showModal("No PDFs found", `<p>There are no PDF files in ${esc(folder)}.</p>`);
      return;
    }
    if (state.terms.length) await addTerms(state.terms, true);
    await select(0);
    const blind = state.files.filter((f) => f.unreadable.length);
    if (blind.length) {
      const what = state.ocr
        ? `rasa will read ${blind.length === 1 && state.files.length === 1 ? "them" : "those pages"} with Windows text recognition. It can misread letters, so look them over and box anything search missed.`
        : `Search will find nothing on those pages, so cover names with manual boxes.`;
      const list = state.files.length > 1 ? `<ul>${blind.map((f) => `<li>${esc(f.name)}: page ${f.unreadable.join(", ")}</li>`).join("")}</ul>` : "";
      showModal(state.ocr ? "Using text recognition" : "Search can't see some pages",
        `<p class="bad">Some pages have no text layer. ${what}</p>${list}<p>This happens with "Microsoft Print to PDF" and with scans: letters are stored as shapes or pictures, not text. Manual boxes still delete them permanently.</p><p>For emails, print again and choose <b>Save as PDF</b> (Chrome or Edge) instead of Microsoft Print to PDF. That keeps the text, so search is exact.</p>`);
    }
    const broken = state.files.filter((f) => f.error);
    if (broken.length && !blind.length)
      showModal("Some files could not be opened", `<ul>${broken.map((f) => `<li>${esc(f.name)}: ${esc(f.error)}</li>`).join("")}</ul><p>They will be skipped.</p>`);
  } catch (e) {
    fail(e);
  } finally {
    busy(false);
  }
}

async function select(i) {
  state.cur = i;
  const f = file();
  const token = ++renderToken;
  const host = $("pages");
  host.innerHTML = "";
  if (f.error) {
    host.innerHTML = `<div class="empty"><h1>Can't open this file</h1><p>${esc(f.error)}</p></div>`;
    refresh();
    return;
  }
  f.pages.forEach((p, n) => {
    const el = document.createElement("div");
    el.className = "page";
    el.dataset.page = n;
    el.style.aspectRatio = `${p.width} / ${p.height}`;
    el.innerHTML = `<span class="label">Page ${n + 1} of ${f.pages.length}</span><img alt=""><div class="overlay"></div>`;
    host.appendChild(el);
    wireDrawing(el.querySelector(".overlay"), n);
  });
  host.scrollTop = 0;
  refresh();
  for (let n = 0; n < f.pages.length; n++) {
    const { png } = await engine("render", { page: n, path: f.path });
    if (token !== renderToken) return; // another file was selected meanwhile
    host.children[n].querySelector("img").src = `data:image/png;base64,${png}`;
  }
}

$("open").onclick = async () => {
  const path = await dialog.open({ multiple: false, filters: [{ name: "PDF", extensions: ["pdf"] }] });
  if (path) openFiles([path], null);
};

$("open-folder").onclick = async () => {
  const folder = await dialog.open({ directory: true, multiple: false, title: "Choose a folder of PDFs" });
  if (!folder) return;
  try {
    const { paths } = await engine("list_pdfs", { folder });
    openFiles(paths, folder);
  } catch (e) {
    fail(e);
  }
};

// ------------------------------------------------------------- search terms

// Search every open file for each new term. `force` re-searches terms already listed
// (used when new files are opened with an existing term set).
async function addTerms(texts, force = false) {
  busy(true, "Searching…");
  try {
    for (const text of texts) {
      const known = state.terms.some((t) => t.toLowerCase() === text.toLowerCase());
      if (known && !force) continue;
      for (const f of state.files) {
        if (f.error) continue;
        const { hits } = await engine("search", { terms: [text], path: f.path });
        for (const h of hits) f.hits.set(nextId++, { ...h, enabled: true });
      }
      if (!known) state.terms.push(text);
    }
  } catch (e) {
    fail(e);
  } finally {
    busy(false);
  }
}

$("term-form").onsubmit = async (ev) => {
  ev.preventDefault();
  const inputs = $("term").value.split(/\n/).map((s) => s.replace(/\s+/g, " ").trim()).filter(Boolean);
  $("term").value = "";
  await addTerms(inputs);
  $("term").focus();
};

$("save-set").onclick = async () => {
  const path = await dialog.save({ defaultPath: "redaction-terms.txt", filters: [{ name: "Term set", extensions: ["txt"] }] });
  if (!path) return;
  try {
    await engine("save_terms", { path, terms: state.terms });
    showModal("Term set saved", `<p>${plural(state.terms.length, "term")} saved to ${esc(path)}.</p><p class="muted">It is a plain text file with one term per line. It contains the names you are redacting, so keep it somewhere private.</p>`);
  } catch (e) {
    fail(e);
  }
};

$("load-set").onclick = async () => {
  const path = await dialog.open({ multiple: false, filters: [{ name: "Term set", extensions: ["txt"] }] });
  if (!path) return;
  try {
    const { terms } = await engine("load_terms", { path });
    await addTerms(terms);
  } catch (e) {
    fail(e);
  }
};

function removeTerm(text) {
  for (const f of state.files)
    for (const [id, h] of f.hits) if (h.term === text) f.hits.delete(id);
  state.terms = state.terms.filter((t) => t !== text);
  refresh();
}

// ------------------------------------------------------------- manual boxes

function wireDrawing(overlay, page) {
  overlay.addEventListener("mousedown", (ev) => {
    if (ev.button !== 0 || ev.target !== overlay) return;
    const f = file();
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
      const sx = f.pages[page].width / area.width, sy = f.pages[page].height / area.height;
      f.manual.push({
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
  const f = file();
  if ((ev.ctrlKey || ev.metaKey) && ev.key === "z" && document.activeElement !== $("term") && f?.manual.length) {
    f.manual.pop();
    refresh();
  }
});

// ------------------------------------------------------------- drawing state

function boxEl(cls, p, rect, onClick, title) {
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

function pendingRedactions(f) {
  const out = [];
  for (const h of f.hits.values()) if (h.enabled) out.push({ page: h.page, rect: h.rect });
  for (const m of f.manual) out.push({ page: m.page, rect: m.rect });
  return out;
}

function refresh() {
  const f = file();
  const ready = state.files.filter((x) => !x.error);

  // Files (folder mode)
  $("files-section").hidden = state.files.length < 2;
  const flist = $("files");
  flist.innerHTML = "";
  state.files.forEach((x, i) => {
    const li = document.createElement("li");
    if (i === state.cur) li.className = "current";
    const n = x.error ? "error" : pendingRedactions(x).length;
    li.innerHTML = `<span class="t">${esc(x.name)}</span>${x.unreadable.length ? `<span class="flag" title="Pages without a text layer">OCR</span>` : ""}<span class="n ${x.error ? "zero" : ""}">${n}</span>`;
    li.onclick = () => select(i);
    flist.appendChild(li);
  });

  // Terms
  const list = $("terms");
  list.innerHTML = "";
  for (const text of state.terms) {
    let total = 0, live = 0;
    for (const x of state.files) for (const h of x.hits.values()) if (h.term === text) { total++; if (h.enabled) live++; }
    const li = document.createElement("li");
    const count = !ready.length ? "" : total ? (live === total ? live : `${live}/${total}`) : "not found";
    li.innerHTML = `<span class="t">${esc(text)}</span><span class="n ${total || !ready.length ? "" : "zero"}">${count}</span><button title="Remove">✕</button>`;
    li.querySelector("button").onclick = () => removeTerm(text);
    list.appendChild(li);
  }

  // Current-file banner and counts
  const blindNote = f && f.unreadable.length ? (state.ocr ? ocrText : blindText)(f.unreadable, f.pages.length) : "";
  $("blind").hidden = !blindNote;
  $("blind").textContent = blindNote;
  $("manual-count").textContent = f?.manual.length ? plural(f.manual.length, "box", "boxes") + (state.files.length > 1 ? " on this file" : "") : "None yet";
  if (!document.body.classList.contains("busy")) {
    const n = ready.reduce((s, x) => s + pendingRedactions(x).length, 0);
    $("pending").textContent = state.files.length ? `${plural(n, "redaction")} ready${state.files.length > 1 ? ` across ${plural(ready.length, "file")}` : ""}` : "";
    $("save").disabled = !ready.length;
  }
  $("save-set").disabled = !state.terms.length || document.body.classList.contains("busy");
  $("term").disabled = false;
  $("save").textContent = state.files.length > 1 ? "Save all redacted copies…" : "Save redacted copy…";

  // Page overlays
  for (const b of document.querySelectorAll(".overlay .box:not(.drawing)")) b.remove();
  if (!f || f.error) return;
  const overlayFor = (n) => document.querySelector(`.page[data-page="${n}"] .overlay`);
  for (const [id, h] of f.hits) {
    overlayFor(h.page)?.appendChild(boxEl(`hit${h.enabled ? "" : " skip"}`, f.pages[h.page], h.rect, () => {
      f.hits.get(id).enabled = !f.hits.get(id).enabled;
      refresh();
    }, `"${h.term}"${h.ocr ? " (text recognition)" : ""}: click to ${h.enabled ? "skip" : "include"}`));
  }
  for (const m of f.manual) {
    overlayFor(m.page)?.appendChild(boxEl("manual", f.pages[m.page], m.rect, () => {
      f.manual = f.manual.filter((x) => x.id !== m.id);
      refresh();
    }, "Your box: click to remove"));
  }
}

// ------------------------------------------------------------- saving

// One file's result as {level: "ok" | "check" | "bad", label, detail (html)}.
function judge(r) {
  if (!r.ok) return { level: "bad", label: "Something remains: do not share", detail: `<ul>${r.findings.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` };
  if (r.unreadable_pages?.length) return { level: "check", label: `Not checked on page ${r.unreadable_pages.join(", ")}: check by eye`, detail: "" };
  if (r.ocr_pages?.length) return { level: "ok", label: `Verified (text recognition on page ${r.ocr_pages.join(", ")}; look it over)`, detail: "" };
  return { level: "ok", label: "Verified", detail: "" };
}

const missedTerms = () => state.terms.filter((t) => !state.files.some((x) => [...x.hits.values()].some((h) => h.term === t)));

$("save").onclick = async () => {
  if (state.files.length > 1) return saveAll();
  const f = file();
  const base = f.path.replace(/\.pdf$/i, "");
  const out = await dialog.save({ defaultPath: `${base}-redacted.pdf`, filters: [{ name: "PDF", extensions: ["pdf"] }] });
  if (!out) return;
  busy(true, "Saving…");
  try {
    const r = await engine("apply", { out_path: out, redactions: pendingRedactions(f), terms: state.terms, path: f.path });
    const j = judge(r);
    const missed = missedTerms();
    showModal(j.level === "bad" ? "Saved, but something remains" : j.level === "check" ? "Saved, check it by eye" : "Redacted copy saved",
      `<p class="${j.level === "ok" ? "ok" : "bad"}"><b>${esc(j.label)}.</b></p>${j.detail}
       <p>${plural(r.count, "area")} permanently removed. The saved copy was re-opened and checked for your search terms in its text, raw file data, metadata, bookmarks, links, comments, form fields and attachments.</p>
       ${missed.length ? `<p class="bad">Never found, so nothing was removed for them: ${missed.map(esc).join(", ")}.</p>` : ""}
       <p class="muted">${esc(r.out_path)}</p><p class="muted">Black boxes still show how long the removed text was. Read the copy once before sending it.</p>`,
      [{ label: "Keep editing original" }, { label: "View redacted copy", primary: true, run: () => openFiles([r.out_path], null) }]);
  } catch (e) {
    fail(e);
  } finally {
    busy(false);
  }
};

async function saveAll() {
  const outDir = await dialog.open({ directory: true, multiple: false, title: "Choose a folder for the redacted copies" });
  if (!outDir) return;
  const rows = [];
  try {
    const todo = state.files.filter((x) => !x.error);
    for (const [i, f] of todo.entries()) {
      busy(true, `Saving ${i + 1} of ${todo.length}…`);
      try {
        const { out_path } = await engine("out_path", { path: f.path, folder: state.folder, out_dir: outDir });
        const r = await engine("apply", { out_path, redactions: pendingRedactions(f), terms: state.terms, path: f.path });
        rows.push({ f, r, j: judge(r) });
      } catch (e) {
        rows.push({ f, j: { level: "bad", label: `Failed: ${e}`, detail: "" } });
      }
    }
  } finally {
    busy(false);
  }
  const bad = rows.filter((x) => x.j.level !== "ok").length;
  const skipped = state.files.filter((x) => x.error);
  const missed = missedTerms();
  showModal(bad ? `Saved, ${plural(bad, "file")} need${bad === 1 ? "s" : ""} attention` : `${plural(rows.length, "redacted copy", "redacted copies")} saved`,
    `<table class="summary">${rows.map(({ f, r, j }) => `<tr><td>${esc(f.name)}</td><td class="n">${r ? r.count : ""}</td><td class="${j.level}">${esc(j.label)}${j.detail}</td></tr>`).join("")}</table>
     ${skipped.length ? `<p class="bad">Skipped (could not open): ${skipped.map((x) => esc(x.name)).join(", ")}.</p>` : ""}
     ${missed.length ? `<p class="bad">Never found in any file: ${missed.map(esc).join(", ")}.</p>` : ""}
     <p class="muted">Saved to ${esc(outDir)}, each name ending in "-redacted". Every file was scrubbed of metadata, bookmarks, links, comments, form fields and attachments, even with no matches.</p>`);
}

refresh();
