#!/usr/bin/env python3
"""Local workbench for one decomp project.

Open http://127.0.0.1:8765/?project=<folder>
The same /api routes are what an AI calls. tools/mcp_server.py exposes them over MCP.
"""

import json
import os
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import workspace

PORT = 8765
PROJECTS = {}


def project_for(path):
    root = os.path.abspath(path)
    if root not in PROJECTS:
        PROJECTS[root] = workspace.Project(root)
    return PROJECTS[root]


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>PC Decomp Workbench</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body {
    margin: 0;
    height: 100vh;
    overflow: hidden;
    display: grid;
    grid-template-rows: 40px minmax(0, 1fr);
    background: #1e1e1e;
    color: #cccccc;
    font: 13px "Segoe UI", sans-serif;
  }
  header {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 0 12px;
    background: #3c3c3c;
    color: #ffffff;
    min-width: 0;
  }
  .brand { font-weight: 600; letter-spacing: .01em; }
  #project { color: #cccccc; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 280px; }
  #fnlabel { color: #9cdcfe; font: 12px Consolas, "Cascadia Mono", monospace; }
  #projstats {
    margin-left: auto;
    display: flex;
    align-items: center;
    gap: 8px;
    font: 12px Consolas, "Cascadia Mono", monospace;
    color: #cccccc;
    white-space: nowrap;
  }
  #projstats .meter { width: 72px; }
  #match { font: 12px Consolas, monospace; color: #858585; }
  #match.done, .pct.done { color: #89d185; }
  #match.mid, .pct.mid { color: #d7ba7d; }
  #match.bad, .pct.bad { color: #f48771; }
  button {
    background: #3c3c3c;
    color: #ffffff;
    border: 1px solid #555;
    border-radius: 2px;
    padding: 3px 10px;
    font: inherit;
  }
  button.primary { background: #0e639c; border-color: #0e639c; }
  button:disabled { opacity: .5; }
  .workspace { display: grid; grid-template-columns: 320px minmax(0, 1fr); min-height: 0; }
  aside {
    display: grid;
    grid-template-rows: auto auto minmax(0, 1fr) auto;
    background: #252526;
    border-right: 1px solid #2b2b2b;
    min-height: 0;
  }
  .side-pad { padding: 8px; display: flex; flex-direction: column; gap: 6px; }
  label { color: #858585; font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
  select, input {
    width: 100%;
    background: #3c3c3c;
    color: #cccccc;
    border: 1px solid #3c3c3c;
    border-radius: 2px;
    padding: 4px 6px;
    font: inherit;
  }
  #bank { display: none; }
  #bankbox { position: relative; }
  #bankbtn, #banklist button {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 3.4em 2.6em;
    align-items: center;
    gap: 8px;
    width: 100%;
    background: #3c3c3c;
    color: #cccccc;
    border: 1px solid #3c3c3c;
    border-radius: 2px;
    padding: 4px 6px;
    font: 12px Consolas, "Cascadia Mono", monospace;
    text-align: left;
  }
  #bankbtn .name, #banklist .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #banklist {
    position: absolute;
    z-index: 5;
    left: 0;
    right: 0;
    max-height: 320px;
    overflow: auto;
    background: #252526;
    border: 1px solid #555;
  }
  #banklist[hidden] { display: none; }
  #banklist button { background: transparent; border: 0; border-radius: 0; }
  #banklist button.on { background: #094771; }
  #banklist button:hover { background: #2a2d2e; }
  #banklist button.on:hover { background: #094771; }
  #fns { overflow: auto; min-height: 0; }
  #fns button {
    display: grid;
    grid-template-columns: 7.6em minmax(0, 1fr) 2.6em;
    align-items: center;
    width: 100%;
    gap: 8px;
    background: transparent;
    border: 0;
    color: #cccccc;
    text-align: left;
    padding: 4px 10px;
    font: 12px Consolas, "Cascadia Mono", monospace;
    border-radius: 0;
  }
  #fns button.on { background: #094771; color: #ffffff; }
  #fns button:hover { background: #2a2d2e; }
  #fns button.on:hover { background: #094771; }
  #progress { color: #858585; font-size: 11px; }
  .meter {
    display: block;
    height: 8px;
    background: #555;
    border-radius: 2px;
    overflow: hidden;
  }
  #fns button.on .meter { background: #063255; }
  .meter i { display: block; height: 100%; width: 0; }
  .meter.bad i { background: #f14c4c; }
  .meter.mid i { background: #d7ba7d; }
  .meter.done i { background: #89d185; }
  .pct { text-align: right; color: #858585; }
  #status { padding: 8px; color: #858585; font-size: 12px; border-top: 1px solid #2b2b2b; min-height: 32px; }
  #status.bad { color: #f48771; }
  .panes {
    display: grid;
    grid-template-columns: minmax(0, 1.15fr) minmax(0, 1fr);
    grid-template-rows: minmax(0, 1.2fr) minmax(0, 1fr);
    min-height: 0;
    background: #2b2b2b;
    gap: 1px;
  }
  section { display: flex; flex-direction: column; min-width: 0; min-height: 0; background: #1e1e1e; }
  section h2 {
    margin: 0;
    padding: 4px 10px;
    display: flex;
    justify-content: space-between;
    align-items: baseline;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: .04em;
    text-transform: uppercase;
    color: #cccccc;
    background: #252526;
  }
  section h2 span { color: #858585; font-weight: 400; letter-spacing: 0; text-transform: none; }
  .code { display: grid; grid-template-columns: 3.4em minmax(0, 1fr); min-height: 0; flex: 1; }
  .gutter, textarea, .view {
    margin: 0;
    border: 0;
    padding: 6px 0 12px;
    background: transparent;
    color: #d4d4d4;
    font: 13px/1.45 Consolas, "Cascadia Mono", monospace;
    white-space: pre;
    tab-size: 4;
  }
  .gutter { overflow: hidden; text-align: right; padding-right: 8px; color: #858585; user-select: none; }
  textarea, .view { overflow: auto; padding-left: 10px; padding-right: 12px; resize: none; outline: none; }
  .view.notice { color: #cccccc; white-space: pre-wrap; }
  .addr { color: #858585; }
  .op { color: #dcdcaa; }
</style>
</head>
<body>
<header>
  <div class="brand">PC Decomp</div>
  <div id="project"></div>
  <div id="projstats">
    <span id="projbar" class="meter"><i></i></span>
    <span id="projpct" class="pct">—</span>
    <span id="projcount">0 complete · 0/0 scored</span>
  </div>
  <div id="fnlabel"></div>
  <div id="match">—</div>
  <button id="draft" disabled>Draft C</button>
  <button id="pass">Ghidra pass</button>
  <button id="allpass">All banks</button>
  <button id="ask" disabled>Ask AI</button>
  <button id="save" disabled>Save</button>
  <button id="score" class="primary" disabled>Score</button>
  <button id="report">Report</button>
</header>
<div class="workspace">
  <aside>
    <div class="side-pad">
      <label for="bankbtn">Bank</label>
      <div id="bankbox">
        <select id="bank"></select>
        <button type="button" id="bankbtn"></button>
        <div id="banklist" hidden></div>
      </div>
    </div>
    <div class="side-pad" style="padding-top:0">
      <label for="find">Function</label>
      <input id="find" placeholder="Address or name" autocomplete="off">
      <div id="progress">No scores yet</div>
    </div>
    <div id="fns"></div>
    <div id="status"></div>
  </aside>
  <div class="panes">
    <section>
      <h2>C/C++ Code <span>your match source</span></h2>
      <div class="code"><pre class="gutter" id="cpp-gutter"></pre><textarea id="cpp" spellcheck="false"></textarea></div>
    </section>
    <section>
      <h2>Pseudo C Code <span>Ghidra</span></h2>
      <div class="code"><pre class="gutter" id="pseudo-gutter"></pre><pre class="view" id="pseudo"></pre></div>
    </section>
    <section>
      <h2>Source Assembly <span>original executable</span></h2>
      <div class="code"><pre class="gutter" id="asm-gutter"></pre><pre class="view" id="asm"></pre></div>
    </section>
    <section>
      <h2>Compiled Assembly <span>from your C/C++</span></h2>
      <div class="code"><pre class="gutter" id="srcasm-gutter"></pre><pre class="view" id="srcasm"></pre></div>
    </section>
  </div>
</div>
<script>
const params = new URLSearchParams(location.search);
const project = params.get("project");
const projectName = decodeURIComponent(project || "").split(/[\\/]/).filter(Boolean).pop() || "";
document.getElementById("project").textContent = projectName;
document.title = projectName ? projectName + " — PC Decomp" : "PC Decomp Workbench";

let functions = [];
let current = null;
let saved = "";
let loadToken = 0;
let cppOwner = "load";

function hex(n) {
  return Number(n).toString(16).toUpperCase().padStart(8, "0");
}
function esc(text) {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;");
}
function setStatus(text, bad) {
  const node = document.getElementById("status");
  node.textContent = text || "";
  node.className = bad ? "bad" : "";
}
async function api(path, options) {
  const sep = path.includes("?") ? "&" : "?";
  const response = await fetch(path + sep + "project=" + encodeURIComponent(project), options);
  const data = await response.json();
  if (!response.ok || data.error) throw new Error(data.error || response.statusText);
  return data;
}
function lineCount(text) {
  if (!text) return 1;
  return text.split("\n").length;
}
function syncGutter(view, gutter) {
  const text = view.value !== undefined ? view.value : view.textContent;
  const count = lineCount(text);
  let numbers = "";
  for (let i = 1; i <= count; i++) numbers += (i === 1 ? "" : "\n") + i;
  gutter.textContent = numbers;
  gutter.scrollTop = view.scrollTop;
}
function watch(viewId) {
  const view = document.getElementById(viewId);
  const gutter = document.getElementById(viewId + "-gutter");
  view.addEventListener("scroll", () => { gutter.scrollTop = view.scrollTop; });
  return () => syncGutter(view, gutter);
}
const syncCpp = watch("cpp");
const syncPseudo = watch("pseudo");
const syncAsm = watch("asm");
const syncSrc = watch("srcasm");
document.getElementById("cpp").addEventListener("input", () => {
  cppOwner = "user";
  syncCpp();
  document.getElementById("save").textContent = document.getElementById("cpp").value === saved ? "Save" : "Save *";
});

function paintAsm(text) {
  return (text || "").split("\n").map(line => {
    const match = /^([0-9A-Fa-f]+)\s+(\S+)(?:\s+(.*))?$/.exec(line);
    if (!match) return esc(line);
    const rest = match[3] ? " " + esc(match[3]) : "";
    return '<span class="addr">' + match[1] + "</span>  <span class=\"op\">" + esc(match[2]) + "</span>" + rest;
  }).join("\n");
}
function setPlain(id, text, notice) {
  const view = document.getElementById(id);
  view.classList.toggle("notice", !!notice);
  view.textContent = text || "";
  syncGutter(view, document.getElementById(id + "-gutter"));
}
function setAsm(id, text) {
  const view = document.getElementById(id);
  view.classList.remove("notice");
  view.innerHTML = paintAsm(text || "");
  syncGutter(view, document.getElementById(id + "-gutter"));
}
function tone(percent) {
  if (percent == null || percent === "") return "none";
  const n = Number(percent);
  if (n >= 100) return "done";
  if (n >= 50) return "mid";
  return "bad";
}
function pctText(percent) {
  if (percent == null || percent === "") return "—";
  const n = Number(percent);
  if (n >= 100) return "100";
  if (n <= 0) return "0";
  return n < 10 ? n.toFixed(1) : String(Math.round(n));
}
function renderFunctions() {
  const query = document.getElementById("find").value.trim().toLowerCase().replace(/^0x/, "");
  const list = document.getElementById("fns");
  list.innerHTML = "";
  let shown = 0;
  let complete = 0;
  let scored = 0;
  functions.forEach(fn => {
    if (Number(fn.match_percent) >= 100) complete += 1;
    if (fn.match_percent != null && fn.match_percent !== "") scored += 1;
    const addr = hex(fn.addr);
    if (query && addr.toLowerCase().indexOf(query) < 0 && fn.name.toLowerCase().indexOf(query) < 0) return;
    shown += 1;
    const button = document.createElement("button");
    button.type = "button";
    if (current && current.addr === fn.addr) button.className = "on";
    const level = tone(fn.match_percent);
    button.title = addr + "  " + fn.size + " bytes" + (level === "none" ? "" : "  " + pctText(fn.match_percent) + "%");
    const addrNode = document.createElement("span");
    addrNode.className = "addr";
    addrNode.textContent = addr;
    const meter = document.createElement("span");
    meter.className = "meter " + level;
    const fill = document.createElement("i");
    const width = fn.match_percent == null || fn.match_percent === "" ? 0 : Math.max(0, Math.min(100, Number(fn.match_percent)));
    fill.style.width = (fn.match_percent == null || fn.match_percent === "" ? 0 : Math.max(width, 8)) + "%";
    meter.appendChild(fill);
    const pct = document.createElement("span");
    pct.className = "pct " + level;
    pct.textContent = pctText(fn.match_percent);
    button.appendChild(addrNode);
    button.appendChild(meter);
    button.appendChild(pct);
    button.addEventListener("click", () => loadFunction(fn.addr));
    list.appendChild(button);
  });
  const progress = document.getElementById("progress");
  if (!functions.length) progress.textContent = "No functions";
  else if (!scored) progress.textContent = functions.length + " functions · none scored";
  else progress.textContent = complete + " complete · " + scored + " scored";
  if (query) progress.textContent += " · " + shown + " shown";
}
function setMatch(percent) {
  const node = document.getElementById("match");
  if (percent == null || percent === "") {
    node.textContent = "—";
    node.className = "";
    return;
  }
  node.textContent = pctText(percent) + "%";
  node.className = tone(percent);
}
let bankRows = [];
function bankCells(item) {
  const level = item && item.scored ? tone(item.match_percent) : "none";
  const name = document.createElement("span");
  name.className = "name";
  name.textContent = item ? item.name : "";
  const meter = document.createElement("span");
  meter.className = "meter " + level;
  const fill = document.createElement("i");
  const scored = item && item.scored;
  fill.style.width = scored ? Math.max(8, Math.min(100, Number(item.match_percent))) + "%" : "0";
  meter.appendChild(fill);
  const pct = document.createElement("span");
  pct.className = "pct " + level;
  pct.textContent = scored ? pctText(item.match_percent) : "—";
  return [name, meter, pct];
}
function paintBanks(rows) {
  bankRows = rows;
  const select = document.getElementById("bank");
  const list = document.getElementById("banklist");
  const selected = select.value;
  select.innerHTML = "";
  list.innerHTML = "";
  rows.forEach(item => {
    const opt = document.createElement("option");
    opt.value = item.name;
    select.appendChild(opt);
    const row = document.createElement("button");
    row.type = "button";
    if (item.name === selected) row.className = "on";
    bankCells(item).forEach(cell => row.appendChild(cell));
    row.title = item.scored ? item.scored + "/" + item.functions + " scored, " + item.complete + " complete" : item.functions + " functions, none scored";
    row.addEventListener("click", () => {
      select.value = item.name;
      list.hidden = true;
      paintBanks(bankRows);
      select.dispatchEvent(new Event("change"));
    });
    list.appendChild(row);
  });
  if (selected && [...select.options].some(opt => opt.value === selected)) select.value = selected;
  paintProject(rows);
  const current = rows.find(item => item.name === select.value) || rows[0];
  const button = document.getElementById("bankbtn");
  button.innerHTML = "";
  if (current) {
    bankCells(current).forEach(cell => button.appendChild(cell));
    button.title = current.scored ? current.scored + "/" + current.functions + " scored" : "No scores yet";
  }
}
function paintProject(rows) {
  let total = 0;
  let scored = 0;
  let complete = 0;
  let sum = 0;
  rows.forEach(item => {
    total += item.functions || 0;
    scored += item.scored || 0;
    complete += item.complete || 0;
    if (item.scored && item.match_percent != null) sum += Number(item.match_percent) * item.scored;
  });
  const average = total ? sum / total : null;
  const level = average == null ? "none" : tone(average);
  const bar = document.getElementById("projbar");
  bar.className = "meter " + level;
  bar.firstElementChild.style.width = (average == null ? 0 : Math.max(0, Math.min(100, average))) + "%";
  const pct = document.getElementById("projpct");
  pct.className = "pct " + level;
  pct.textContent = average == null ? "—" : pctText(average) + "%";
  document.getElementById("projcount").textContent = complete + " complete · " + scored + "/" + total + " scored";
}
document.getElementById("bankbtn").addEventListener("click", event => {
  event.stopPropagation();
  const list = document.getElementById("banklist");
  list.hidden = !list.hidden;
});
document.addEventListener("click", event => {
  if (!document.getElementById("bankbox").contains(event.target)) document.getElementById("banklist").hidden = true;
});
async function loadBanks() {
  const banks = await api("/api/banks");
  paintBanks(banks);
  if (!banks.length) {
    setStatus("No banks in config/splits.txt", true);
    return;
  }
  await loadFunctions();
}
async function refreshBanks() {
  paintBanks(await api("/api/banks"));
}
async function loadFunctions() {
  const name = document.getElementById("bank").value;
  functions = await api("/api/functions?bank=" + encodeURIComponent(name));
  setStatus(functions.length + " functions");
  renderFunctions();
  if (functions.length) await loadFunction(functions[0].addr);
}
function showPseudo(token, addr, text) {
  const waiting = text.indexOf("Ghidra is analyzing") >= 0;
  const notice = waiting || text.indexOf("{") < 0;
  setPlain("pseudo", text, notice);
  if (!waiting) {
    const editor = document.getElementById("cpp");
    if (current && cppOwner === "load" && isStub(editor.value, current.addr) && text.indexOf("{") >= 0) {
      applyDraft(token, text, false).catch(err => setStatus(err.message, true));
    }
    return;
  }
  setTimeout(() => {
    if (token !== loadToken) return;
    api("/api/pseudo?addr=" + addr).then(again => {
      if (token !== loadToken) return;
      showPseudo(token, addr, again.text || "");
    }).catch(err => {
      if (token === loadToken) setPlain("pseudo", err.message, true);
    });
  }, 3000);
}

async function loadFunction(addr) {
  const token = ++loadToken;
  setStatus("Loading " + hex(addr));
  document.getElementById("score").disabled = true;
  document.getElementById("save").disabled = true;
  document.getElementById("draft").disabled = true;
  document.getElementById("ask").disabled = true;
  const data = await api("/api/function?addr=" + addr);
  if (token !== loadToken) return;
  current = data;
  saved = data.cpp || "";
  cppOwner = "load";
  document.getElementById("cpp").value = saved;
  document.querySelector("#cpp").closest("section").querySelector("h2 span").textContent = "your match source";
  document.getElementById("fnlabel").textContent = hex(data.addr) + "   " + data.size + " bytes";
  const listed = functions.find(fn => fn.addr === data.addr);
  setMatch(listed ? listed.match_percent : null);
  document.getElementById("save").textContent = "Save";
  document.getElementById("save").disabled = false;
  document.getElementById("score").disabled = false;
  document.getElementById("draft").disabled = false;
  document.getElementById("ask").disabled = false;
  syncCpp();
  setAsm("asm", data.assembly);
  setPlain("srcasm", data.source_assembly, true);
  setPlain("pseudo", "Starting Ghidra…", true);
  renderFunctions();
  const selected = document.querySelector("#fns button.on");
  if (selected) selected.scrollIntoView({block: "nearest"});
  setStatus(hex(data.addr));
  try {
    const pseudo = await api("/api/pseudo?addr=" + addr);
    if (token !== loadToken) return;
    showPseudo(token, addr, pseudo.text || "");
  } catch (err) {
    if (token !== loadToken) return;
    setPlain("pseudo", err.message, true);
  }
}
async function saveCode() {
  if (!current) return;
  const button = document.getElementById("save");
  button.disabled = true;
  button.textContent = "Saving";
  document.getElementById("progress").textContent = "Saving " + hex(current.addr);
  setStatus("Saving");
  try {
    const data = await api("/api/save", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({addr: current.addr, code: document.getElementById("cpp").value}),
    });
    saved = document.getElementById("cpp").value;
    const file = (data.path || "").split(/[\\/]/).pop();
    button.textContent = "Saved";
    document.getElementById("progress").textContent = "Saved " + file;
    setStatus("Saved " + file);
  } finally {
    button.disabled = false;
    setTimeout(() => {
      if (button.textContent === "Saved") button.textContent = "Save";
    }, 1500);
  }
}
async function scoreCode() {
  if (!current) return;
  document.getElementById("score").disabled = true;
  setStatus("Compiling");
  try {
    const data = await api("/api/score", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({addr: current.addr, code: document.getElementById("cpp").value}),
    });
    saved = document.getElementById("cpp").value;
    document.getElementById("save").textContent = "Save";
    setAsm("srcasm", data.source_assembly || data.note || "");
    const row = functions.find(fn => fn.addr === current.addr);
    if (row) row.match_percent = data.match_percent;
    setMatch(data.match_percent);
    renderFunctions();
    refreshBanks().catch(() => {});
    setStatus(data.note || (data.match_percent == null ? "No score" : pctText(data.match_percent) + "%"));
  } finally {
    document.getElementById("score").disabled = false;
  }
}
document.getElementById("bank").addEventListener("change", () => {
  document.getElementById("find").value = "";
  loadFunctions().catch(err => setStatus(err.message, true));
});
document.getElementById("find").addEventListener("input", renderFunctions);
function isStub(code, addr) {
  const name = "_fn_" + hex(addr);
  return code.indexOf(name + "(void)") >= 0 && code.indexOf("return 0;") >= 0 && code.split("\n").length <= 6;
}
function noteCpp(text) {
  document.querySelector("#cpp").closest("section").querySelector("h2 span").textContent = text;
}
function putCpp(code) {
  const editor = document.getElementById("cpp");
  const before = editor.value.replace(/\s+/g, "");
  editor.value = code;
  editor.scrollTop = 0;
  document.getElementById("save").textContent = "Save *";
  syncCpp();
  return before !== code.replace(/\s+/g, "");
}
async function applyDraft(token, pseudo, force) {
  if (!current) return;
  if (!force && cppOwner !== "load") return;
  const data = await api("/api/draft", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({addr: current.addr, pseudo: pseudo || ""}),
  });
  if (token !== loadToken || (!force && cppOwner !== "load")) return;
  if (!data.code) {
    setStatus(data.note || "No draft", true);
    return;
  }
  cppOwner = "draft";
  putCpp(data.code);
  noteCpp("drafted from Ghidra");
  setStatus("Drafted C from Ghidra. Save, then Score.");
}
document.getElementById("ask").addEventListener("click", () => {
  if (!current) return;
  const token = loadToken;
  const button = document.getElementById("ask");
  button.disabled = true;
  cppOwner = "ai-pending";
  setStatus("Asking qwen3.6. The first reply can take a few minutes while the model loads.");
  noteCpp("asking the local model");
  api("/api/ask", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({
      addr: current.addr,
      pseudo: document.getElementById("pseudo").textContent,
      cpp: document.getElementById("cpp").value,
      assembly: document.getElementById("asm").textContent,
      compiled: document.getElementById("srcasm").textContent,
      score: (functions.find(fn => fn.addr === current.addr) || {}).match_percent,
    }),
  }).then(data => {
    if (token !== loadToken) return;
    if (data.end && current) {
      const row = functions.find(fn => fn.addr === current.addr);
      if (row) {
        row.end = data.end;
        row.size = data.size;
        row.match_percent = null;
      }
      document.getElementById("fnlabel").textContent = hex(current.addr) + "   " + data.size + " bytes";
      renderFunctions();
      setMatch(null);
    }
    if (data.assembly) setAsm("asm", data.assembly);
    if (data.pseudo) showPseudo(token, current.addr, data.pseudo);
    if (!data.code) {
      cppOwner = "user";
      noteCpp("your match source");
      setStatus(data.note || (data.end ? "Ghidra split updated." : "No draft"), !data.end);
      return;
    }
    const changed = putCpp(data.code);
    cppOwner = "ai";
    if (changed) {
      noteCpp("replaced by " + (data.model || "the local model"));
      setStatus(data.note || "Replaced the C draft. Save, then Score.");
    } else {
      noteCpp("model returned the same C");
      setStatus("The model returned the same C, so the pane stayed as it was.");
    }
  }).catch(err => {
    cppOwner = "user";
    noteCpp("your match source");
    setStatus(err.message, true);
  }).finally(() => { if (token === loadToken) button.disabled = false; });
});
let passTimer = 0;
function passButtons(busy, which) {
  document.getElementById("pass").disabled = busy;
  document.getElementById("allpass").disabled = busy;
  if (!busy) {
    document.getElementById("pass").textContent = "Ghidra pass";
    document.getElementById("allpass").textContent = "All banks";
    return;
  }
  document.getElementById(which).textContent = which === "allpass" ? "Passing all" : "Passing";
}
function passPlace(data) {
  if (!data.all || !data.banks_total) return "";
  const current = Math.min((data.banks_done || 0) + (data.running ? 1 : 0), data.banks_total);
  const label = String(data.bank || "").replace(/^bank\//, "");
  return "Bank " + current + "/" + data.banks_total + (label ? "  " + label : "");
}
document.getElementById("pass").addEventListener("click", () => startPass(false));
document.getElementById("allpass").addEventListener("click", () => startPass(true));
function startPass(all) {
  const name = document.getElementById("bank").value;
  if (passTimer || (!all && !name)) return;
  passButtons(true, all ? "allpass" : "pass");
  setStatus(all ? "Starting the Ghidra pass on every bank" : "Starting the Ghidra pass");
  api("/api/ghidra-pass", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(all ? {all: true} : {bank: name}),
  }).then(() => pollPass(name)).catch(err => {
    passButtons(false);
    setStatus(err.message, true);
  });
}
async function pollPass(name) {
  const data = await api("/api/ghidra-pass");
  const addr = data.addr ? hex(data.addr) : "";
  const place = passPlace(data);
  if (data.running) {
    if (data.phase === "drafting") {
      document.getElementById("progress").textContent = (place ? place + "  " : "Ghidra pass ") + (data.done || 0) + "/" + (data.total || 0) + (addr ? "  " + addr : "");
      setStatus((place ? place + "  " : "") + "Drafting " + (addr || "the next function"));
    } else if (data.phase === "compiling") {
      document.getElementById("progress").textContent = (place ? place + "  " : "") + "Compiling the Ghidra drafts";
      setStatus((place ? place + "  " : "") + "Compiling the Ghidra drafts");
    } else {
      document.getElementById("progress").textContent = (place ? place + "  " : "") + "Scoring the Ghidra drafts";
      setStatus((place ? place + "  " : "") + "Scoring the Ghidra drafts");
    }
    passTimer = setTimeout(() => pollPass(name).catch(err => setStatus(err.message, true)), 1000);
    return;
  }
  passTimer = 0;
  passButtons(false);
  const open = document.getElementById("bank").value;
  if (data.all || open === (data.bank || name)) {
    functions = await api("/api/functions?bank=" + encodeURIComponent(open));
    renderFunctions();
    const listed = current && functions.find(fn => fn.addr === current.addr);
    if (listed) setMatch(listed.match_percent);
  }
  refreshBanks().catch(() => {});
  const status = data.error && data.note ? data.note + " " + data.error : (data.error || data.note || "Ghidra pass finished.");
  setStatus(status, !!data.error);
}
document.getElementById("draft").addEventListener("click", () => {
  const token = loadToken;
  cppOwner = "load";
  setStatus("Drafting C");
  applyDraft(token, document.getElementById("pseudo").textContent, true).catch(err => setStatus(err.message, true));
});
document.getElementById("save").addEventListener("click", () => saveCode().catch(err => setStatus(err.message, true)));
document.getElementById("score").addEventListener("click", () => scoreCode().catch(err => {
  setPlain("srcasm", err.message, true);
  setStatus(err.message, true);
  document.getElementById("score").disabled = false;
}));
document.getElementById("report").addEventListener("click", async () => {
  const button = document.getElementById("report");
  button.disabled = true;
  button.textContent = "Reporting";
  document.getElementById("progress").textContent = "Building report";
  setStatus("Building report");
  try {
    const data = await api("/api/report", {method: "POST"});
    const name = document.getElementById("bank").value;
    functions = await api("/api/functions?bank=" + encodeURIComponent(name));
    renderFunctions();
    refreshBanks().catch(() => {});
    const listed = current && functions.find(fn => fn.addr === current.addr);
    if (listed) setMatch(listed.match_percent);
    const complete = data.complete_functions == null ? 0 : data.complete_functions;
    const scored = data.scored_functions == null ? 0 : data.scored_functions;
    button.textContent = "Reported";
    document.getElementById("progress").textContent = complete + " complete · " + scored + " scored";
    setStatus("Report written. " + complete + " complete, " + scored + " scored.");
  } catch (err) {
    button.textContent = "Report";
    document.getElementById("progress").textContent = "Report failed";
    setStatus(err.message, true);
  } finally {
    button.disabled = false;
    setTimeout(() => {
      if (button.textContent === "Reported") button.textContent = "Report";
    }, 1500);
  }
});
document.addEventListener("keydown", event => {
  if (!(event.ctrlKey || event.metaKey) || !current) return;
  if (event.key === "s") {
    event.preventDefault();
    saveCode().catch(err => setStatus(err.message, true));
  } else if (event.key === "Enter") {
    event.preventDefault();
    scoreCode().catch(err => setStatus(err.message, true));
  }
});
if (!project) {
  setStatus("Open this page from the builder.", true);
} else {
  loadBanks().catch(err => setStatus(err.message, true));
}
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def _project(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        raw = (query.get("project") or [""])[0]
        if not raw:
            self._json({"error": "missing project"}, 400)
            return None
        return project_for(raw)

    def _json(self, payload, code=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            body = HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        proj = self._project()
        if proj is None:
            return
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if parsed.path == "/api/banks":
                self._json(proj.banks())
            elif parsed.path == "/api/functions":
                self._json(proj.functions(query["bank"][0]))
            elif parsed.path == "/api/function":
                addr = int(query["addr"][0], 0)
                bank = workspace.bank_of(proj, addr)
                rows = proj.functions(bank["name"])
                row = next(item for item in rows if item["addr"] == addr)
                self._json({
                    "addr": addr,
                    "name": row["name"],
                    "size": row["size"],
                    "bank": bank["name"],
                    "cpp": proj.read_cpp(bank["name"], addr),
                    "assembly": proj.assembly(addr, row["size"]),
                    "source_assembly": proj.source_assembly(bank["name"], addr),
                })
            elif parsed.path == "/api/pseudo":
                addr = int(query["addr"][0], 0)
                self._json({"text": proj.pseudo_c(addr)})
            elif parsed.path == "/api/ghidra-pass":
                self._json(proj.ghidra_pass_status())
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:
            self._json({"error": str(exc)}, 500)

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        proj = self._project()
        if proj is None:
            return
        body = self._read_body()
        try:
            if parsed.path == "/api/save":
                addr = int(body["addr"])
                bank = workspace.bank_of(proj, addr)
                row = next(item for item in proj.functions(bank["name"]) if item["addr"] == addr)
                path = proj.save_cpp(bank["name"], addr, row["size"], body.get("code") or "")
                self._json({"path": path})
            elif parsed.path == "/api/score":
                addr = int(body["addr"])
                bank = workspace.bank_of(proj, addr)
                row = next(item for item in proj.functions(bank["name"]) if item["addr"] == addr)
                code = body.get("code") or ""
                proj.save_cpp(bank["name"], addr, row["size"], code)
                try:
                    result = proj.score(bank["name"], addr, row["size"])
                except Exception as exc:
                    import local_ai
                    local_ai.remember(proj.root, addr, code, None, str(exc))
                    raise
                import local_ai
                local_ai.remember(proj.root, addr, code, result.get("match_percent"), result.get("note") or "")
                self._json(result)
            elif parsed.path == "/api/draft":
                addr = int(body["addr"])
                self._json(proj.draft_cpp(addr, body.get("pseudo") or ""))
            elif parsed.path == "/api/ask":
                import local_ai
                addr = int(body["addr"])
                result = local_ai.ask(
                    proj.root,
                    addr,
                    body.get("pseudo") or "",
                    body.get("cpp") or "",
                    body.get("assembly") or "",
                    body.get("compiled") or "",
                    body.get("score"),
                )
                if result.get("split_end"):
                    applied = proj.apply_split(addr, int(result["split_end"]))
                    result["pseudo"] = applied.get("pseudo") or ""
                    result["assembly"] = applied.get("assembly") or ""
                    result["size"] = applied.get("size")
                    result["end"] = applied.get("end")
                    extra = applied.get("note") or ""
                    result["note"] = ((result.get("note") or "") + "\n" + extra).strip()
                self._json(result)
            elif parsed.path == "/api/ghidra-pass":
                if body.get("all"):
                    self._json(proj.start_ghidra_all())
                    return
                bank = body.get("bank") or ""
                if not bank:
                    self._json({"error": "missing bank"}, 400)
                    return
                self._json(proj.start_ghidra_pass(bank))
            elif parsed.path == "/api/report":
                self._json(proj.build_report())
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:
            self._json({"error": str(exc)}, 500)


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: workbench.py <project folder>")
    project_for(sys.argv[1])
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print("http://127.0.0.1:%s/?project=%s" % (PORT, urllib.parse.quote(os.path.abspath(sys.argv[1]))))
    server.serve_forever()


if __name__ == "__main__":
    main()
