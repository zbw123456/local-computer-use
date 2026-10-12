"use strict";
const $ = id => document.getElementById(id);
const fragment = new URLSearchParams(location.hash.slice(1));
if (fragment.has("token")) {
  sessionStorage.setItem("localcu-token", fragment.get("token"));
  history.replaceState(null, "", location.pathname);
}
const token = sessionStorage.getItem("localcu-token") || "";
let state = null, windows = [], lastGeneration = "", lastEvents = "", timer = null;
const statusNames = {idle: "准备就绪", observing: "正在观察", thinking: "正在思考", awaiting_approval: "等待你批准", executing: "正在执行", completed: "任务结束", cancelled: "已停止", error: "需要处理", stopping: "正在停止"};
const actionNames = {click: "点击控件", double_click: "双击位置", set_value: "设置控件内容", type_text: "输入文字", press_key: "按下快捷键", scroll: "滚动窗口"};

function notify(message) { $("notice").textContent = message; $("notice").hidden = !message; }
async function api(path, body) {
  const response = await fetch("/api/" + path, {method: body === undefined ? "GET" : "POST",
    headers: {Authorization: "Bearer " + token, ...(body === undefined ? {} : {"Content-Type": "application/json"})},
    body: body === undefined ? undefined : JSON.stringify(body)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "请求失败");
  return result;
}
async function safely(fn) {
  try { notify(""); await fn(); await poll(); } catch (error) { notify(error.message); }
}
function target() {
  const item = windows[Number($("window").value)];
  if (!item || $("window").value === "") throw new Error("请先选择一个窗口。");
  return {bundle_id: item.bundle_id, window_id: item.window_id};
}
async function refresh() {
  const previous = windows[Number($("window").value)];
  windows = (await api("windows")).windows;
  $("window").replaceChildren();
  if (!windows.length) {
    const option = new Option("没有可用窗口，请先打开一个应用", ""); $("window").append(option);
  } else {
    windows.forEach((item, i) => $("window").append(new Option(`${item.app} · ${item.title || "未命名窗口"}`, String(i))));
    const match = windows.findIndex(w => previous && w.bundle_id === previous.bundle_id && w.window_id === previous.window_id);
    if (match >= 0) $("window").value = String(match);
  }
}
async function doctor() {
  const result = await api("doctor");
  for (const [id, key] of [["ax-check", "accessibility"], ["screen-check", "screen_recording"]]) {
    $(id).textContent = state?.demo ? "演示已就绪" : (result[key] ? "已连接" : "需要授权");
    $(id).className = result[key] ? "ok" : "warn";
  }
}
function renderObservation(observation) {
  if (!observation || observation.generation === lastGeneration) return;
  lastGeneration = observation.generation;
  $("screenshot").src = observation.screenshot.data_url;
  $("screenshot").hidden = false; $("empty").hidden = true;
  $("window-title").textContent = observation.target.title || observation.target.bundle_id;
  $("observation-label").textContent = `最新观察 · ${observation.screenshot.width} × ${observation.screenshot.height}`;
  $("element-count").textContent = String(observation.elements.length);
  $("elements").replaceChildren();
  for (const element of observation.elements) {
    const row = document.createElement("div"); row.className = "element";
    const code = document.createElement("code"); code.textContent = element.id;
    const title = document.createElement("span"); title.textContent = element.title || element.description || "未命名控件";
    const role = document.createElement("small"); role.textContent = " · " + element.role;
    row.append(code, title, role);
    if (element.value) { const p = document.createElement("p"); p.textContent = element.value; row.append(p); }
    $("elements").append(row);
  }
}
function details(action) {
  const fields = {element_id: "控件", text: "内容", key: "按键", x: "X 坐标", y: "Y 坐标", scroll_y: "滚动量"};
  $("action-details").replaceChildren();
  for (const [key, label] of Object.entries(fields)) {
    if (action[key] === undefined || action[key] === null) continue;
    const div = document.createElement("div"), dt = document.createElement("dt"), dd = document.createElement("dd");
    dt.textContent = label; dd.textContent = String(action[key]); div.append(dt, dd); $("action-details").append(div);
  }
}
function render(value) {
  state = value;
  $("mode").textContent = value.demo ? "本地演示模式" : "macOS 原生模式";
  $("model-check").textContent = value.configured ? value.model : "未配置 API Key";
  $("model-check").className = value.configured ? "ok" : "warn";
  $("permissions").hidden = value.demo;
  if (value.demo) {
    $("privacy").textContent = "演示模式完全离线，不读取真实窗口，也不调用模型。";
    $("task-hint").textContent = "演示固定流程：填写一句话 → 保存 → 检查结果。";
    if (!$("task").value) $("task").value = "在练习便签中写下“你好，Local Computer Use！”，然后保存。";
  }
  $("status").textContent = statusNames[value.status] || value.status;
  $("status").className = "status" + (value.status === "awaiting_approval" ? " waiting" : value.status === "error" ? " error" : "");
  $("message").textContent = value.message;
  for (const id of ["start", "window", "task", "refresh", "observe", "permissions", "check"]) $(id).disabled = value.busy;
  $("stop").disabled = !value.busy || value.status === "stopping";
  $("proposal").hidden = !value.pending;
  if (value.pending) {
    const action = value.pending.action;
    $("action-kind").textContent = actionNames[action.kind] || action.kind;
    $("reason").textContent = action.reason;
    $("step-label").textContent = `STEP ${String(value.step).padStart(2, "0")}`;
    details(action);
    $("approve").disabled = false; $("reject").disabled = false;
  }
  renderObservation(value.observation);
  const eventKey = value.events.map(e => e.id).join(",");
  if (lastEvents !== eventKey) {
    lastEvents = eventKey;
    $("events").replaceChildren();
    for (const event of [...value.events].reverse()) {
      const li = document.createElement("li"), time = document.createElement("time"), text = document.createElement("span");
      time.textContent = new Date(event.time).toLocaleTimeString([], {hour: "2-digit", minute: "2-digit"});
      text.textContent = event.message; li.append(time, text); $("events").append(li);
    }
    $("event-count").textContent = String(value.events.length);
  }
}
async function poll() {
  clearTimeout(timer);
  try { render(await api("state")); } catch (error) { notify(error.message); }
  timer = setTimeout(poll, 1000);
}
$("refresh").onclick = () => safely(refresh);
$("check").onclick = () => safely(doctor);
$("observe").onclick = () => safely(async () => renderObservation(await api("observe", {target: target()})));
$("permissions").onclick = () => safely(async () => { await api("permissions", {}); await doctor(); });
$("start").onclick = () => safely(async () => { await api("start", {task: $("task").value, target: target()}); });
$("stop").onclick = () => safely(() => api("stop", {}));
for (const route of ["approve", "reject"]) $(route).onclick = () => safely(async () => {
  const id = state?.pending?.id;
  if (!id) return;
  $("approve").disabled = true; $("reject").disabled = true;
  await api(route, {id});
});
for (const type of ["screen", "elements"]) $("tab-" + type).onclick = () => {
  for (const name of ["screen", "elements"]) {
    const active = type === name;
    $("tab-" + name).classList.toggle("active", active);
    $("tab-" + name).setAttribute("aria-selected", String(active));
    $(name + "-panel").hidden = !active;
  }
};
(async () => {
  await poll();
  await safely(async () => { await refresh(); await doctor(); if (state?.demo) renderObservation(await api("observe", {target: target()})); });
})();
