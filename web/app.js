let state = null;
let dirty = false;
let sliderDirty = false;
let bandKey = "";
let queued = false;
let chain = Promise.resolve();

const curveEl = () => document.getElementById("curve");

function post(body) {
  return {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  };
}

async function api(path, options) {
  const response = await fetch(path, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || "请求失败");
  return body;
}

function enqueue(job) {
  chain = chain.then(async () => {
    try {
      await job();
    } catch (error) {
      showError(error.message);
    }
  });
  return chain;
}

function showError(message) {
  const el = document.getElementById("form-error");
  el.hidden = false;
  el.textContent = message;
}

function clearError() {
  const el = document.getElementById("form-error");
  el.hidden = true;
  el.textContent = "";
}

function editing() {
  const node = document.activeElement;
  return Boolean(node && node.closest && node.closest("[data-edit]"));
}

function sensorLabel(name) {
  const fixed = {
    Inlet_Temp: "进风",
    Outlet_Temp: "出风",
    PCH_Temp: "PCH",
  };
  if (fixed[name]) return fixed[name];
  const patterns = [
    [/^CPU(\d+)_DTS$/i, (m) => `CPU${m[1]}`],
    [/^CPU(\d+)_Temp$/i, (m) => `CPU${m[1]} 表面`],
    [/^CPU(\d+)_VR_Temp$/i, (m) => `VR${m[1]}`],
    [/^DIMMG(\d+)_Temp$/i, (m) => `内存${m[1]}`],
    [/^PSU(\d+)_Temp$/i, (m) => `电源${m[1]}`],
    [/^FAN(\d+)_F_Speed$/i, (m) => `风扇${m[1]}前`],
    [/^FAN(\d+)_R_Speed$/i, (m) => `风扇${m[1]}后`],
  ];
  for (const [pattern, label] of patterns) {
    const match = name.match(pattern);
    if (match) return label(match);
  }
  return name;
}

function formatSensor(item) {
  if (item.value == null || Number.isNaN(Number(item.value))) return "—";
  if (item.unit === "RPM") return String(Math.round(item.value));
  return Number(item.value).toFixed(0);
}

function formatDuty(list) {
  if (!list || !list.length) return "—";
  const min = Math.min(...list);
  const max = Math.max(...list);
  return min === max ? `${min}%` : `${min}–${max}%`;
}

function formatTemp(value) {
  return value == null || Number.isNaN(value) ? "—" : Number(value).toFixed(1);
}

function renderSensors() {
  const root = document.getElementById("sensors");
  const items = state.sensors || [];
  root.hidden = items.length === 0;
  const key = JSON.stringify(items);
  if (root.dataset.key === key) return;
  root.dataset.key = key;
  root.replaceChildren();
  for (const item of items) {
    const wrap = document.createElement("div");
    const label = document.createElement("dt");
    label.textContent = sensorLabel(item.name);
    const value = document.createElement("dd");
    value.textContent = formatSensor(item);
    wrap.append(label, value);
    root.append(wrap);
  }
}

function percent(value) {
  return value == null || Number.isNaN(value) ? "—" : `${value}%`;
}

function readBands() {
  const cells = [...document.querySelectorAll(".band")];
  if (!cells.length) return null;
  const bands = [];
  for (const cell of cells) {
    const low = Number(cell.querySelector("[data-field=low]").value);
    const high = Number(cell.querySelector("[data-field=high]").value);
    const speed = Math.round(Number(cell.querySelector("[data-field=speed]").value));
    if (![low, high, speed].every(Number.isFinite)) return null;
    bands.push({ low, high, speed });
  }
  return bands;
}

function collect() {
  const bands = readBands();
  if (!bands) throw new Error("曲线里有空着的数字");
  const exitRaw = document.getElementById("exit-speed").value.trim();
  return {
    interface: document.getElementById("iface").value,
    host: document.getElementById("host").value.trim(),
    username: document.getElementById("username").value.trim(),
    password: document.getElementById("password").value,
    interval: Number(document.getElementById("interval").value),
    hysteresis: Number(document.getElementById("hysteresis").value),
    min_speed_on: Math.round(Number(document.getElementById("min-speed").value)),
    fallback_speed: Math.round(Number(document.getElementById("fallback").value)),
    exit_speed: exitRaw === "" ? "" : Math.round(Number(exitRaw)),
    bands,
  };
}

function predicted(temp, bands) {
  if (temp == null || !bands.length) return null;
  const minOn = Math.round(Number(document.getElementById("min-speed").value));
  const fallback = Math.round(Number(document.getElementById("fallback").value));
  const floor = Number.isFinite(minOn) ? minOn : state.config.min_speed_on;
  const top = Number.isFinite(fallback) ? fallback : state.config.fallback_speed;
  if (temp >= bands[bands.length - 1].high) return top;
  for (const band of bands) {
    if (temp >= band.low && temp < band.high) return Math.max(band.speed, floor);
  }
  if (temp < bands[0].low) return Math.max(bands[0].speed, floor);
  return top;
}

function stopHint() {
  const raw = document.getElementById("exit-speed").value.trim();
  document.getElementById("stop-hint").textContent = raw === ""
    ? "停止后保持当前转速"
    : `停止后回到 ${raw}%`;
}

function syncIface() {
  document.getElementById("host-label").hidden = document.getElementById("iface").value === "open";
}

function ensureOption(value) {
  const select = document.getElementById("iface");
  if ([...select.options].some((option) => option.value === value)) return;
  const option = document.createElement("option");
  option.value = value;
  option.textContent = value;
  select.append(option);
}

function field(value, name) {
  const input = document.createElement("input");
  input.inputMode = "decimal";
  input.dataset.field = name;
  input.value = String(value);
  input.setAttribute("aria-label", name === "low" ? "从" : name === "high" ? "到" : "转速");
  return input;
}

function buildBands(bands) {
  const root = document.getElementById("bands");
  root.replaceChildren();
  bands.forEach((band, index) => {
    const cell = document.createElement("div");
    cell.className = "band";
    cell.style.setProperty("--speed", String(band.speed));
    const span = document.createElement("div");
    span.className = "span";
    const dash = document.createElement("span");
    dash.textContent = "–";
    span.append(field(band.low, "low"), dash, field(band.high, "high"));
    const speed = document.createElement("label");
    speed.className = "speed";
    speed.append("转速");
    const wrap = document.createElement("span");
    wrap.className = "field";
    const unit = document.createElement("span");
    unit.textContent = "%";
    wrap.append(field(band.speed, "speed"), unit);
    speed.append(wrap);
    cell.append(span, speed);
    if (bands.length > 1) {
      const remove = document.createElement("button");
      remove.type = "button";
      remove.dataset.remove = String(index);
      remove.textContent = "删除";
      cell.append(remove);
    }
    root.append(cell);
  });
  bandKey = JSON.stringify(bands);
}

function paint(bands, temp) {
  document.querySelectorAll(".band").forEach((cell, index) => {
    const band = bands[index];
    if (!band) return;
    cell.style.setProperty("--speed", String(Math.max(0, Math.min(100, band.speed))));
    const hot = temp != null && temp >= band.low && temp < band.high;
    cell.classList.toggle("is-hot", hot);
  });
}

function svgEl(name, attrs) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", name);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  return node;
}

function draw(bands, temp) {
  const svg = curveEl();
  const w = 1000;
  const h = 250;
  const padL = 44;
  const padR = 16;
  const padT = 22;
  const padB = 32;
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.replaceChildren();
  if (!bands.length) return;
  const x = (t) => padL + (Math.min(100, Math.max(0, t)) / 100) * (w - padL - padR);
  const y = (p) => padT + (1 - Math.min(100, Math.max(0, p)) / 100) * (h - padT - padB);
  const font = "Avenir Next Condensed, Arial Narrow, sans-serif";

  for (const mark of [0, 25, 50, 75, 100]) {
    svg.append(svgEl("line", {
      x1: padL, x2: w - padR, y1: y(mark), y2: y(mark),
      stroke: "#c5d0d6", "stroke-width": 1,
    }));
    const label = svgEl("text", { x: 4, y: y(mark) + 4, fill: "#526066", "font-size": 12, "font-family": font });
    label.textContent = String(mark);
    svg.append(label);
  }

  if (temp != null) {
    const hot = bands.findIndex((band) => temp >= band.low && temp < band.high);
    if (hot >= 0) {
      const band = bands[hot];
      svg.append(svgEl("rect", {
        x: x(band.low),
        y: padT,
        width: Math.max(0, x(band.high) - x(band.low)),
        height: h - padT - padB,
        fill: "#1e6478",
        opacity: 0.16,
      }));
    }
  }

  let d = "";
  bands.forEach((band, index) => {
    const yy = y(band.speed);
    d += index === 0 ? `M ${x(band.low)} ${yy}` : ` V ${yy}`;
    d += ` H ${x(band.high)}`;
  });
  svg.append(svgEl("path", {
    d: `${d} V ${y(0)} H ${x(bands[0].low)} Z`,
    fill: "#1e6478",
    opacity: 0.2,
  }));
  svg.append(svgEl("path", { d, fill: "none", stroke: "#1a2428", "stroke-width": 2 }));

  for (const mark of [0, 20, 40, 60, 80, 100]) {
    const label = svgEl("text", {
      x: x(mark), y: h - 8, fill: "#526066", "font-size": 12,
      "text-anchor": "middle", "font-family": font,
    });
    label.textContent = String(mark);
    svg.append(label);
  }

  if (temp != null && Number.isFinite(temp)) {
    const px = x(temp);
    svg.append(svgEl("line", {
      x1: px, x2: px, y1: padT - 6, y2: h - padB,
      stroke: "#e36f12", "stroke-width": 2,
    }));
    const anchor = px > w - 80 ? "end" : "start";
    const text = svgEl("text", {
      x: anchor === "end" ? px - 6 : px + 6,
      y: 16,
      fill: "#e36f12",
      "font-size": 18,
      "font-weight": 600,
      "text-anchor": anchor,
      "font-family": font,
    });
    text.textContent = Number(temp).toFixed(1);
    svg.append(text);
  }

  const summary = bands.map((band) => `${band.low}–${band.high}° ${band.speed}%`).join("，");
  const here = temp == null ? "" : `，当前 ${Number(temp).toFixed(1)}°C`;
  svg.setAttribute("aria-label", `风扇曲线，${summary}${here}`);
}

function setIfIdle(id, value) {
  const node = document.getElementById(id);
  if (document.activeElement === node) return;
  node.value = value == null ? "" : String(value);
}

function fillForm(config) {
  ensureOption(config.interface);
  setIfIdle("iface", config.interface);
  setIfIdle("host", config.host || "");
  setIfIdle("username", config.username || "");
  setIfIdle("interval", config.interval);
  setIfIdle("hysteresis", config.hysteresis);
  setIfIdle("min-speed", config.min_speed_on);
  setIfIdle("fallback", config.fallback_speed);
  setIfIdle("exit-speed", config.exit_speed == null ? "" : config.exit_speed);
  document.getElementById("password").placeholder = state.has_password ? "留空保持" : "BMC 密码";
  document.getElementById("demo").checked = state.demo;
  syncIface();
  stopHint();
  const key = JSON.stringify(config.bands);
  if (key !== bandKey) buildBands(config.bands);
}

function renderLog() {
  const list = document.getElementById("log");
  const nearEnd = list.scrollHeight - list.scrollTop - list.clientHeight < 28;
  list.replaceChildren();
  for (const entry of state.logs) {
    const item = document.createElement("li");
    const time = document.createElement("time");
    time.textContent = entry.time;
    const message = document.createElement("span");
    message.textContent = entry.message;
    if (entry.level === "error") message.className = "error";
    item.append(time, message);
    list.append(item);
  }
  if (nearEnd) list.scrollTop = list.scrollHeight;
}

function render(next, form) {
  state = next;
  if (!dirty || state.error) clearError();
  const lamp = document.getElementById("lamp");
  lamp.className = "lamp";
  if (state.error) lamp.classList.add("is-fault");
  else if (state.running) lamp.classList.add("is-on");
  if (state.demo) lamp.classList.add("is-demo");
  const booting = state.decision && String(state.decision.reason).startsWith("开机中");
  const power = state.power_on === true ? "开机" : state.power_on === false ? "关机" : "";
  document.getElementById("status-label").textContent = state.error
    ? "读取失败"
    : booting
      ? "开机"
      : state.running
        ? "自动"
        : "待命";
  const place = state.demo
    ? "模拟"
    : state.config.interface === "open"
      ? "本机"
      : (state.config.host || "未填地址");
  document.getElementById("target").textContent = power && !state.demo ? `${power} · ${place}` : place;

  const temp = state.sample ? state.sample.temp : null;
  document.getElementById("temp").textContent = formatTemp(temp);
  const caption = document.getElementById("caption");
  caption.classList.toggle("is-fault", Boolean(state.error));
  if (state.error) caption.textContent = state.error;
  else if (!state.sample || !state.sample.valid) {
    if (state.power_on === false) caption.textContent = "主机关机";
    else caption.textContent = (state.decision && state.decision.reason) || "CPU 温度还读不到";
  } else if (state.sample.readings && state.sample.readings.length) {
    caption.textContent = state.sample.readings
      .map((item) => `${sensorLabel(item.name)} ${Number(item.temp).toFixed(1)}°C`)
      .join("  ");
  } else caption.textContent = `${state.sample.valid} 个 CPU 有读数`;

  const keep = !form && (dirty || editing());
  if (!keep) fillForm(state.config);
  const edited = keep ? readBands() : null;
  const bands = edited || state.config.bands;
  if (!keep || edited) {
    draw(bands, temp);
    paint(bands, temp);
  }
  document.getElementById("curve-duty").textContent = percent(predicted(temp, bands));
  document.getElementById("written-duty").textContent = percent(state.duty);
  document.getElementById("bmc-duty").textContent = formatDuty(state.fan_duty);
  renderSensors();
  document.getElementById("dirty-note").hidden = !dirty;

  const slider = document.getElementById("manual");
  if (!sliderDirty && state.duty != null) {
    slider.value = String(state.duty);
  }
  document.getElementById("manual-value").textContent = `${slider.value}%`;
  slider.setAttribute("aria-valuenow", slider.value);
  slider.disabled = state.running;
  document.getElementById("write-btn").disabled = state.running;
  const auto = document.getElementById("auto-btn");
  auto.textContent = state.running ? "停止" : "开始自动";
  auto.classList.toggle("primary", !state.running);
  auto.classList.toggle("is-stop", state.running);
  renderLog();
}

function markDirty() {
  dirty = true;
  document.getElementById("dirty-note").hidden = false;
  const bands = readBands();
  const temp = state && state.sample ? state.sample.temp : null;
  if (bands) {
    draw(bands, temp);
    paint(bands, temp);
    document.getElementById("curve-duty").textContent = percent(predicted(temp, bands));
  }
}

function addBand() {
  const bands = readBands();
  if (!bands) {
    showError("曲线里有空着的数字");
    return;
  }
  let widest = 0;
  bands.forEach((band, index) => {
    if (band.high - band.low > bands[widest].high - bands[widest].low) widest = index;
  });
  const band = bands[widest];
  const mid = Math.round((band.low + band.high) / 2);
  if (mid <= band.low || mid >= band.high) {
    showError("这一档太窄，分不开");
    return;
  }
  bands.splice(widest, 1,
    { low: band.low, high: mid, speed: band.speed },
    { low: mid, high: band.high, speed: band.speed });
  buildBands(bands);
  markDirty();
}

function removeBand(index) {
  const bands = readBands();
  if (!bands || bands.length < 2) return;
  const removed = bands[index];
  bands.splice(index, 1);
  if (index === 0) bands[0].low = removed.low;
  else bands[index - 1].high = removed.high;
  buildBands(bands);
  markDirty();
}

async function applyForm(save) {
  const payload = collect();
  payload.save = save;
  const next = await api("/api/settings", post(payload));
  if (save) dirty = false;
  render(next, true);
  if (!save) document.getElementById("dirty-note").hidden = !dirty;
}

document.getElementById("manual").addEventListener("input", () => {
  sliderDirty = true;
  const slider = document.getElementById("manual");
  document.getElementById("manual-value").textContent = `${slider.value}%`;
  slider.setAttribute("aria-valuenow", slider.value);
});

document.getElementById("bands").addEventListener("input", markDirty);
document.getElementById("bands").addEventListener("click", (event) => {
  const button = event.target.closest("[data-remove]");
  if (button) removeBand(Number(button.dataset.remove));
});
document.querySelector(".policy").addEventListener("input", () => {
  stopHint();
  markDirty();
});
document.getElementById("iface").addEventListener("change", syncIface);
document.getElementById("add-band").addEventListener("click", addBand);

document.getElementById("read-btn").addEventListener("click", () => {
  if (queued) return;
  queued = true;
  enqueue(async () => {
    try {
      await applyForm(false);
      render(await api("/api/probe", post()), false);
    } finally {
      queued = false;
    }
  });
});

document.getElementById("write-btn").addEventListener("click", () => {
  if (queued) return;
  queued = true;
  enqueue(async () => {
    try {
      await applyForm(false);
      const speed = Math.round(Number(document.getElementById("manual").value));
      render(await api("/api/manual", post({ speed })), false);
      sliderDirty = false;
    } finally {
      queued = false;
    }
  });
});

document.getElementById("auto-btn").addEventListener("click", () => {
  if (queued) return;
  queued = true;
  enqueue(async () => {
    try {
      if (state.running) {
        render(await api("/api/auto/stop", post()), false);
      } else {
        await applyForm(false);
        render(await api("/api/auto/start", post()), false);
      }
    } finally {
      queued = false;
    }
  });
});

document.getElementById("save-btn").addEventListener("click", () => {
  if (queued) return;
  queued = true;
  enqueue(async () => {
    try {
      await applyForm(true);
    } finally {
      queued = false;
    }
  });
});

document.getElementById("demo").addEventListener("change", () => {
  const enabled = document.getElementById("demo").checked;
  if (queued) return;
  queued = true;
  enqueue(async () => {
    try {
      render(await api("/api/demo", post({ enabled })), false);
    } finally {
      queued = false;
    }
  });
});

document.querySelector(".link").addEventListener("submit", (event) => event.preventDefault());

enqueue(async () => {
  render(await api("/api/state"), true);
});
setInterval(() => {
  if (queued) return;
  enqueue(async () => {
    render(await api("/api/state"), false);
  });
}, 1000);
