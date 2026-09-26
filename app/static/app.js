/* 徒步轨迹分享前端：无底图 Canvas 预览、遮蔽圆与检查点编辑、调用后端裁切/分析与导出 */
"use strict";

const METERS_PER_DEG_LAT = 111319.9;

const state = {
  trackId: null,
  name: null,
  segments: [],   // 原始轨迹 [[{lat,lon,ele,time},...],...]
  circles: [],    // [{lat, lon, radius_m}]
  checkpoints: [], // [{lat, lon, radius_m, name}]，最多 10 个
  editMode: "mask", // 预览图点击模式："mask"（遮蔽圆）| "checkpoint"（检查点）
  masked: null,   // /api/mask 返回结果
  elevation: null, // /api/elevation 返回结果
  steep: null,    // /api/steep-sections 返回结果
  drift: null,    // /api/drift 返回结果
  stays: null,    // /api/stays 返回结果
  simplify: null, // /api/simplify 返回结果
  otherTrackId: null, // 对比轨迹（第二条上传的 GPX）
  otherName: null,
  encounters: null, // /api/encounters 返回结果
  checkpointVisits: null, // /api/checkpoint-visits 返回结果
  transform: null,
};

const MAX_CHECKPOINTS = 10;

const canvas = document.getElementById("map");
const ctx = canvas.getContext("2d");
const profileCanvas = document.getElementById("profile");
const profileCtx = profileCanvas.getContext("2d");

/* ---------- 坐标变换：经纬度 <-> 画布像素 ---------- */

function computeTransform() {
  if (!state.segments.length) {
    state.transform = null;
    return;
  }
  let minLat = 90, maxLat = -90, minLon = 180, maxLon = -180;
  for (const seg of state.segments) {
    for (const p of seg) {
      if (p.lat < minLat) minLat = p.lat;
      if (p.lat > maxLat) maxLat = p.lat;
      if (p.lon < minLon) minLon = p.lon;
      if (p.lon > maxLon) maxLon = p.lon;
    }
  }
  const dpr = window.devicePixelRatio || 1;
  const pad = 36 * dpr;
  const W = canvas.width, H = canvas.height;
  const meanLat = (minLat + maxLat) / 2;
  const cosLat = Math.cos((meanLat * Math.PI) / 180);
  const dx = Math.max((maxLon - minLon) * cosLat, 1e-9);
  const dy = Math.max(maxLat - minLat, 1e-9);
  const scale = Math.min((W - 2 * pad) / dx, (H - 2 * pad) / dy);
  state.transform = {
    minLat, minLon, cosLat, scale, pad,
    offX: (W - 2 * pad - dx * scale) / 2,
    offY: (H - 2 * pad - dy * scale) / 2,
  };
}

function toPx(lat, lon) {
  const t = state.transform;
  const x = t.pad + t.offX + (lon - t.minLon) * t.cosLat * t.scale;
  const y = canvas.height - (t.pad + t.offY + (lat - t.minLat) * t.scale);
  return [x, y];
}

function toLatLon(x, y) {
  const t = state.transform;
  const lat = t.minLat + (canvas.height - t.pad - t.offY - y) / t.scale;
  const lon = t.minLon + (x - t.pad - t.offX) / (t.cosLat * t.scale);
  return [lat, lon];
}

/* ---------- 绘制 ---------- */

function drawPolyline(seg, style, width, dash) {
  if (!seg || seg.length < 2) return;
  ctx.strokeStyle = style;
  ctx.lineWidth = width;
  ctx.setLineDash(dash || []);
  ctx.beginPath();
  seg.forEach((p, i) => {
    const [x, y] = toPx(p.lat, p.lon);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  ctx.stroke();
  ctx.setLineDash([]);
}

function draw() {
  const dpr = window.devicePixelRatio || 1;
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.fillStyle = "#f7f7f5";
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  if (!state.transform) {
    ctx.fillStyle = "#9e9e9e";
    ctx.font = `${15 * dpr}px sans-serif`;
    ctx.textAlign = "center";
    ctx.fillText("上传 GPX 后在此预览轨迹（无底图）", canvas.width / 2, canvas.height / 2);
    return;
  }

  // 原始轨迹（灰）
  for (const seg of state.segments) drawPolyline(seg, "#c4c4c4", 2 * dpr);

  // 裁切结果：隐藏段（红虚线）+ 可见段（蓝）
  if (state.masked) {
    for (const seg of state.masked.hidden_segments) {
      drawPolyline(seg, "#e53935", 2.5 * dpr, [7 * dpr, 6 * dpr]);
    }
    for (const seg of state.masked.visible_segments) {
      drawPolyline(seg, "#1e88e5", 2.5 * dpr);
    }
  }

  // 精简结果（青绿）
  if (state.simplify) {
    for (const seg of state.simplify.segments) {
      drawPolyline(seg, "#00897b", 2.5 * dpr);
    }
  }

  // 遮蔽圆
  const t = state.transform;
  for (const c of state.circles) {
    const [x, y] = toPx(c.lat, c.lon);
    const ry = (c.radius_m / METERS_PER_DEG_LAT) * t.scale;
    const rx =
      (c.radius_m / (METERS_PER_DEG_LAT * Math.cos((c.lat * Math.PI) / 180))) *
      t.cosLat * t.scale;
    ctx.beginPath();
    ctx.ellipse(x, y, Math.max(rx, 2), Math.max(ry, 2), 0, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(229, 57, 53, 0.12)";
    ctx.fill();
    ctx.strokeStyle = "#e53935";
    ctx.lineWidth = 1.5 * dpr;
    ctx.setLineDash([5 * dpr, 4 * dpr]);
    ctx.stroke();
    ctx.setLineDash([]);
  }

  // 检查点（靛蓝实线圆 + 名称标签）
  state.checkpoints.forEach((c, i) => {
    const [x, y] = toPx(c.lat, c.lon);
    const ry = (c.radius_m / METERS_PER_DEG_LAT) * t.scale;
    const rx =
      (c.radius_m / (METERS_PER_DEG_LAT * Math.cos((c.lat * Math.PI) / 180))) *
      t.cosLat * t.scale;
    ctx.beginPath();
    ctx.ellipse(x, y, Math.max(rx, 2), Math.max(ry, 2), 0, 0, Math.PI * 2);
    ctx.fillStyle = "rgba(57, 73, 171, 0.10)";
    ctx.fill();
    ctx.strokeStyle = "#3949ab";
    ctx.lineWidth = 1.5 * dpr;
    ctx.stroke();
    const label = (c.name || "").trim() || `#${i + 1}`;
    const ly = y - Math.max(ry, 2) - 3 * dpr;
    ctx.font = `${11 * dpr}px sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "bottom";
    ctx.lineWidth = 3 * dpr;
    ctx.strokeStyle = "rgba(255, 255, 255, 0.9)";
    ctx.strokeText(label, x, ly);
    ctx.fillStyle = "#3949ab";
    ctx.fillText(label, x, ly);
    ctx.textBaseline = "alphabetic";
  });

  // 起点 / 终点标记
  const first = state.segments[0] && state.segments[0][0];
  const lastSeg = state.segments[state.segments.length - 1];
  const last = lastSeg && lastSeg[lastSeg.length - 1];
  if (first) drawMarker(first, "#2e7d32", "起", dpr);
  if (last) drawMarker(last, "#c62828", "终", dpr);

  // 漂移跳点（橙色）
  if (state.drift) {
    for (const h of state.drift.hits) drawMarker(h, "#fb8c00", "漂", dpr);
  }

  // 停留点（紫色）
  if (state.stays) {
    for (const s of state.stays.stays) drawMarker(s, "#8e24aa", "停", dpr);
  }

  // 相遇点（粉红）
  if (state.encounters) {
    for (const e of state.encounters.encounters) drawMarker(e, "#d81b60", "遇", dpr);
  }
}

function drawMarker(p, color, label, dpr) {
  const [x, y] = toPx(p.lat, p.lon);
  ctx.beginPath();
  ctx.arc(x, y, 7 * dpr, 0, Math.PI * 2);
  ctx.fillStyle = color;
  ctx.fill();
  ctx.fillStyle = "#fff";
  ctx.font = `${8 * dpr}px sans-serif`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(label, x, y);
  ctx.textBaseline = "alphabetic";
}

/* ---------- 后端交互 ---------- */

let maskTimer = null;
function scheduleMask() {
  clearTimeout(maskTimer);
  maskTimer = setTimeout(refreshMask, 250);
}

async function refreshMask() {
  if (!state.trackId) return;
  try {
    const res = await fetch("/api/mask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ track_id: state.trackId, circles: state.circles }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "裁切计算失败");
    state.masked = data;
    renderStats(data);
    draw();
  } catch (err) {
    showMessage(err.message);
  }
}

async function uploadFile(file) {
  const fd = new FormData();
  fd.append("file", file);
  const res = await fetch("/api/upload", { method: "POST", body: fd });
  const data = await res.json();
  if (!res.ok) throw new Error(errDetail(data, "上传失败"));
  state.trackId = data.track_id;
  state.name = data.name;
  state.segments = data.segments;
  state.circles = [];
  state.checkpoints = [];
  state.checkpointVisits = null;
  setEditMode("mask");
  state.masked = null;
  state.elevation = null;
  state.steep = null;
  state.drift = null;
  state.stays = null;
  state.simplify = null;
  state.encounters = null;
  document.getElementById("download-fixed").disabled = true;
  document.getElementById("download-simplified").disabled = true;
  computeTransform();
  renderTrackInfo(data.stats);
  renderCircleList();
  renderCheckpointList();
  await refreshMask();
  await refreshPace();
  await refreshElevation();
  await refreshSteep();
  await refreshDrift();
  await refreshStays();
  await refreshSimplify();
  await refreshEncounters();
  await refreshCheckpointVisits();
}

let paceTimer = null;
function schedulePace() {
  clearTimeout(paceTimer);
  paceTimer = setTimeout(refreshPace, 250);
}

function splitKmValue() {
  const input = document.getElementById("split-km");
  let v = Number(input.value);
  if (!input.value || !Number.isFinite(v)) v = 1;
  return Math.min(10, Math.max(0.1, v));
}

async function refreshPace() {
  if (!state.trackId) return;
  const el = document.getElementById("pace");
  try {
    const res = await fetch("/api/pace", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ track_id: state.trackId, split_km: splitKmValue() }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(errDetail(data, "配速计算失败"));
    renderPace(data);
  } catch (err) {
    el.classList.remove("muted");
    el.innerHTML = `<div class="pace-error">无法计算分段配速：${escapeHtml(err.message)}</div>`;
  }
}

async function refreshElevation() {
  if (!state.trackId) return;
  const el = document.getElementById("elevation-summary");
  try {
    const res = await fetch("/api/elevation", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ track_id: state.trackId }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(errDetail(data, "海拔分析失败"));
    state.elevation = data;
    renderElevation(data);
  } catch (err) {
    state.elevation = null;
    el.classList.remove("muted");
    el.innerHTML = `<div class="pace-error">无法计算海拔剖面：${escapeHtml(err.message)}</div>`;
  }
  drawProfile();
}

async function refreshSteep() {
  if (!state.trackId) return;
  const el = document.getElementById("steep");
  try {
    const res = await fetch("/api/steep-sections", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ track_id: state.trackId }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(errDetail(data, "陡坡分析失败"));
    state.steep = data;
    renderSteep(data);
  } catch (err) {
    state.steep = null;
    el.classList.remove("muted");
    el.innerHTML = `<div class="pace-error">无法分析持续陡坡：${escapeHtml(err.message)}</div>`;
  }
  drawProfile();
}

async function downloadGpx() {
  if (!state.trackId) return;
  try {
    const res = await fetch("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ track_id: state.trackId, circles: state.circles }),
    });
    if (!res.ok) {
      const data = await res.json();
      throw new Error(data.detail || "导出失败");
    }
    saveBlob(await res.blob(), `${state.name || "track"}-masked.gpx`);
  } catch (err) {
    showMessage(err.message);
  }
}

async function refreshDrift() {
  if (!state.trackId) return;
  const el = document.getElementById("drift");
  try {
    const res = await fetch("/api/drift", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ track_id: state.trackId }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(errDetail(data, "漂移检测失败"));
    state.drift = data;
    renderDrift(data);
  } catch (err) {
    state.drift = null;
    el.classList.remove("muted");
    el.innerHTML = `<div class="pace-error">无法检测单点漂移：${escapeHtml(err.message)}</div>`;
    document.getElementById("download-fixed").disabled = true;
  }
  draw();
}

async function downloadFixedGpx() {
  if (!state.trackId) return;
  try {
    const res = await fetch("/api/drift/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ track_id: state.trackId }),
    });
    if (!res.ok) {
      const data = await res.json();
      throw new Error(errDetail(data, "导出失败"));
    }
    saveBlob(await res.blob(), `${state.name || "track"}-fixed.gpx`);
  } catch (err) {
    showMessage(err.message);
  }
}

async function refreshStays() {
  if (!state.trackId) return;
  const el = document.getElementById("stay");
  try {
    const res = await fetch("/api/stays", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ track_id: state.trackId }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(errDetail(data, "停留识别失败"));
    state.stays = data;
    renderStays(data);
  } catch (err) {
    state.stays = null;
    el.classList.remove("muted");
    el.innerHTML = `<div class="pace-error">无法识别停留：${escapeHtml(err.message)}</div>`;
  }
  draw();
}

let simplifyTimer = null;
function scheduleSimplify() {
  clearTimeout(simplifyTimer);
  simplifyTimer = setTimeout(refreshSimplify, 250);
}

function toleranceValue() {
  const input = document.getElementById("tolerance-m");
  let v = Number(input.value);
  if (!input.value || !Number.isFinite(v)) v = 10;
  return Math.min(100, Math.max(1, v));
}

async function refreshSimplify() {
  if (!state.trackId) return;
  const el = document.getElementById("simplify");
  try {
    const res = await fetch("/api/simplify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ track_id: state.trackId, tolerance_m: toleranceValue() }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(errDetail(data, "轨迹精简失败"));
    state.simplify = data;
    renderSimplify(data);
  } catch (err) {
    state.simplify = null;
    el.classList.remove("muted");
    el.innerHTML = `<div class="pace-error">无法精简轨迹：${escapeHtml(err.message)}</div>`;
    document.getElementById("download-simplified").disabled = true;
  }
  draw();
}

async function downloadSimplifiedGpx() {
  if (!state.trackId) return;
  try {
    const res = await fetch("/api/simplify/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ track_id: state.trackId, tolerance_m: toleranceValue() }),
    });
    if (!res.ok) {
      const data = await res.json();
      throw new Error(errDetail(data, "导出失败"));
    }
    saveBlob(await res.blob(), `${state.name || "track"}-simplified.gpx`);
  } catch (err) {
    showMessage(err.message);
  }
}

/* ---------- 双人轨迹相遇 ---------- */

async function uploadOtherFile(file) {
  const fd = new FormData();
  fd.append("file", file);
  const res = await fetch("/api/upload", { method: "POST", body: fd });
  const data = await res.json();
  if (!res.ok) throw new Error(errDetail(data, "对比轨迹上传失败"));
  state.otherTrackId = data.track_id;
  state.otherName = data.name;
  renderTrack2Info(data.stats);
  await refreshEncounters();
}

async function refreshEncounters() {
  const el = document.getElementById("encounter");
  if (!state.trackId || !state.otherTrackId) {
    state.encounters = null;
    el.classList.add("muted");
    el.textContent = state.otherTrackId
      ? "请先上传当前轨迹"
      : "上传对比轨迹后自动分析相遇";
    draw();
    return;
  }
  try {
    const res = await fetch("/api/encounters", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        track_id: state.trackId,
        other_track_id: state.otherTrackId,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(errDetail(data, "相遇分析失败"));
    state.encounters = data;
    renderEncounters(data);
  } catch (err) {
    state.encounters = null;
    el.classList.remove("muted");
    el.innerHTML = `<div class="pace-error">无法分析双人轨迹相遇：${escapeHtml(err.message)}</div>`;
  }
  draw();
}

function saveBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

/* ---------- 检查点到访 ---------- */

let checkpointTimer = null;
function scheduleCheckpoints() {
  clearTimeout(checkpointTimer);
  checkpointTimer = setTimeout(refreshCheckpointVisits, 250);
}

function cpRadiusValue() {
  const input = document.getElementById("cp-radius");
  let v = Number(input.value);
  if (!input.value || !Number.isFinite(v)) v = 50;
  return Math.min(1000, Math.max(1, v));
}

async function refreshCheckpointVisits() {
  const el = document.getElementById("checkpoint-visits");
  if (!state.trackId || !state.checkpoints.length) {
    state.checkpointVisits = null;
    el.classList.add("muted");
    el.textContent = state.trackId
      ? "添加检查点后自动计算到访记录"
      : "上传 GPX 并添加检查点后自动计算到访记录";
    draw();
    return;
  }
  try {
    const res = await fetch("/api/checkpoint-visits", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        track_id: state.trackId,
        checkpoints: state.checkpoints,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(errDetail(data, "检查点到访计算失败"));
    state.checkpointVisits = data;
    renderCheckpointVisits(data);
  } catch (err) {
    state.checkpointVisits = null;
    el.classList.remove("muted");
    el.innerHTML = `<div class="pace-error">无法计算检查点到访：${escapeHtml(err.message)}</div>`;
  }
  draw();
}

/* ---------- 界面渲染 ---------- */

function fmtKm(m) {
  return (m / 1000).toFixed(2);
}

function fmtDuration(sec) {
  const t = Math.max(0, Math.round(sec));
  const h = Math.floor(t / 3600);
  const m = Math.floor((t % 3600) / 60);
  const s = t % 60;
  const ss = String(s).padStart(2, "0");
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${ss}`;
  return `${m}:${ss}`;
}

function fmtPace(paceMinPerKm) {
  if (paceMinPerKm === null || paceMinPerKm === undefined ||
      !Number.isFinite(paceMinPerKm)) {
    return "—";  // 零里程段不显示无穷配速
  }
  const totalSec = Math.round(paceMinPerKm * 60);
  return `${Math.floor(totalSec / 60)}:${String(totalSec % 60).padStart(2, "0")}`;
}

function fmtTime(iso) {
  return new Date(iso).toLocaleTimeString("zh-CN", { hour12: false });
}

function errDetail(data, fallback) {
  if (data && typeof data.detail === "string") return data.detail;
  if (data && Array.isArray(data.detail) && data.detail.length) {
    return data.detail.map((d) => d.msg || JSON.stringify(d)).join("；");
  }
  return fallback;
}

function renderPace(data) {
  const el = document.getElementById("pace");
  el.classList.remove("muted");
  let html = "";
  for (const seg of data.segments) {
    if (data.segments.length > 1) {
      html += `<div class="pace-seg-head">原始分段 #${seg.seg_index}` +
        `（${fmtKm(seg.distance_m)} km）</div>`;
    }
    const rows = seg.splits.map((s) =>
      "<tr>" +
      `<td>${s.split_index}</td>` +
      `<td>${fmtKm(s.distance_m)}</td>` +
      `<td>${fmtDuration(s.duration_s)}</td>` +
      `<td>${fmtPace(s.pace_min_per_km)}</td>` +
      `<td>${fmtTime(s.start_time)} ~ ${fmtTime(s.end_time)}</td>` +
      "</tr>"
    ).join("");
    html +=
      '<table class="pace-table"><thead><tr>' +
      "<th>段</th><th>距离(km)</th><th>耗时</th><th>配速(分/km)</th><th>起止时间</th>" +
      `</tr></thead><tbody>${rows}</tbody></table>`;
  }
  const t = data.totals;
  html += `<div class="pace-total">合计：<b>${fmtKm(t.distance_m)} km</b>，` +
    `耗时 <b>${fmtDuration(t.duration_s)}</b>，` +
    `平均配速 <b>${fmtPace(t.pace_min_per_km)}</b> 分/km</div>`;
  el.innerHTML = html;
}

function renderElevation(data) {
  const el = document.getElementById("elevation-summary");
  el.classList.remove("muted");
  if (!data.has_ele) {
    el.innerHTML = "<div>无海拔数据</div>";
    return;
  }
  el.innerHTML =
    `<div>总爬升：<b>${Math.round(data.ascent_m)} m</b>，` +
    `总下降：<b>${Math.round(data.descent_m)} m</b></div>` +
    `<div>海拔范围：<b>${data.ele_min.toFixed(0)}</b> ~ ` +
    `<b>${data.ele_max.toFixed(0)}</b> m，里程 ${fmtKm(data.distance_m)} km</div>`;
}

function renderSteep(data) {
  const el = document.getElementById("steep");
  el.classList.remove("muted");
  if (!data.has_ele) {
    el.innerHTML = "<div>无海拔数据，无法分析持续陡坡</div>";
    return;
  }
  if (!data.sections.length) {
    el.innerHTML =
      "<div>未发现持续陡坡：没有总长 ≥ 100 米、坡度 ≥ 8%（上坡）" +
      "或 ≤ −8%（下坡）的连续区间</div>";
    return;
  }
  const rows = data.sections.map((s, i) =>
    "<tr>" +
    `<td>${i + 1}</td>` +
    `<td>${s.seg_index}</td>` +
    `<td>${fmtKm(s.start_m)} ~ ${fmtKm(s.end_m)} km</td>` +
    `<td>${Math.round(s.length_m)}</td>` +
    `<td>${s.direction === "up" ? "上坡" : "下坡"}</td>` +
    `<td>${Math.abs(s.max_grade_pct).toFixed(1)}%</td>` +
    "</tr>"
  ).join("");
  el.innerHTML =
    `<div>持续陡坡区间：<b>${data.sections.length}</b> 段` +
    `（完整 100 米窗口、20 米前移，坡度 ≥ 8% 或 ≤ −8%，合并后总长 ≥ 100 米）</div>` +
    '<table class="pace-table steep-table"><thead><tr>' +
    "<th>序</th><th>原始分段</th><th>起止里程</th><th>长度(m)</th>" +
    "<th>方向</th><th>最大绝对坡度</th>" +
    `</tr></thead><tbody>${rows}</tbody></table>`;
}

/* 海拔剖面图：x 轴为全程里程，y 轴为海拔；每个连续有效区间一条折线，区间间断开 */
function drawProfile() {
  const dpr = window.devicePixelRatio || 1;
  const rect = profileCanvas.getBoundingClientRect();
  profileCanvas.width = Math.max(1, Math.round(rect.width * dpr));
  profileCanvas.height = Math.max(1, Math.round(rect.height * dpr));
  const W = profileCanvas.width, H = profileCanvas.height;
  profileCtx.clearRect(0, 0, W, H);
  profileCtx.fillStyle = "#f7f7f5";
  profileCtx.fillRect(0, 0, W, H);

  const data = state.elevation;
  if (!data || !data.has_ele || !data.runs.length) {
    profileCtx.fillStyle = "#9e9e9e";
    profileCtx.font = `${14 * dpr}px sans-serif`;
    profileCtx.textAlign = "center";
    profileCtx.textBaseline = "middle";
    profileCtx.fillText(
      data && !data.has_ele ? "无海拔数据" : "上传 GPX 后在此显示海拔剖面",
      W / 2, H / 2
    );
    profileCtx.textBaseline = "alphabetic";
    return;
  }

  const padL = 46 * dpr, padR = 14 * dpr, padT = 12 * dpr, padB = 26 * dpr;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const dist = Math.max(data.distance_m, 1e-9);
  let eMin = data.ele_min, eMax = data.ele_max;
  if (eMax - eMin < 1) {  // 近乎水平：扩量程避免除零
    const mid = (eMax + eMin) / 2;
    eMin = mid - 0.5;
    eMax = mid + 0.5;
  }
  const px = (d) => padL + (d / dist) * plotW;
  const py = (e) => padT + plotH - ((e - eMin) / (eMax - eMin)) * plotH;

  // 网格与刻度
  profileCtx.strokeStyle = "#e4e4e1";
  profileCtx.fillStyle = "#999";
  profileCtx.lineWidth = 1 * dpr;
  profileCtx.font = `${10 * dpr}px sans-serif`;
  const xTicks = 5;
  for (let i = 0; i <= xTicks; i++) {
    const d = (dist / xTicks) * i;
    const x = px(d);
    profileCtx.beginPath();
    profileCtx.moveTo(x, padT);
    profileCtx.lineTo(x, padT + plotH);
    profileCtx.stroke();
    profileCtx.textAlign = i === 0 ? "left" : i === xTicks ? "right" : "center";
    profileCtx.fillText((d / 1000).toFixed(1) + (i === xTicks ? " km" : ""),
      x, padT + plotH + 15 * dpr);
  }
  const yTicks = 4;
  for (let i = 0; i <= yTicks; i++) {
    const e = eMin + ((eMax - eMin) / yTicks) * i;
    const y = py(e);
    profileCtx.beginPath();
    profileCtx.moveTo(padL, y);
    profileCtx.lineTo(padL + plotW, y);
    profileCtx.stroke();
    profileCtx.textAlign = "right";
    profileCtx.fillText(e.toFixed(0) + (i === yTicks ? " m" : ""),
      padL - 5 * dpr, y + 3 * dpr);
  }

  // 持续陡坡区间色带：上坡橙、下坡青绿，画在海拔折线之下；不跨区间（缺口/分段处天然断开）
  if (state.steep && state.steep.has_ele) {
    for (const sec of state.steep.sections) {
      const x0 = px(sec.start_m), x1 = px(sec.end_m);
      const up = sec.direction === "up";
      profileCtx.fillStyle = up ? "rgba(245, 124, 32, 0.16)" : "rgba(0, 137, 123, 0.16)";
      profileCtx.fillRect(x0, padT, x1 - x0, plotH);
      profileCtx.strokeStyle = up ? "#f57c20" : "#00897b";
      profileCtx.lineWidth = 1.5 * dpr;
      profileCtx.beginPath();
      profileCtx.moveTo(x0, padT);
      profileCtx.lineTo(x1, padT);
      profileCtx.stroke();
      if (x1 - x0 >= 34 * dpr) {
        profileCtx.fillStyle = up ? "#e65100" : "#00695c";
        profileCtx.font = `${10 * dpr}px sans-serif`;
        profileCtx.textAlign = "center";
        profileCtx.fillText(
          `${up ? "上坡" : "下坡"} ${Math.abs(sec.max_grade_pct).toFixed(1)}%`,
          (x0 + x1) / 2, padT + plotH - 6 * dpr
        );
      }
    }
  }

  // 海拔折线：每个连续有效区间一条，区间之间断开
  profileCtx.strokeStyle = "#1e88e5";
  profileCtx.fillStyle = "#1e88e5";
  profileCtx.lineWidth = 2 * dpr;
  profileCtx.lineJoin = "round";
  for (const run of data.runs) {
    if (run.points.length === 1) {  // 单点区间：画一个小圆点
      const p = run.points[0];
      profileCtx.beginPath();
      profileCtx.arc(px(p.d), py(p.ele), 2.5 * dpr, 0, Math.PI * 2);
      profileCtx.fill();
      continue;
    }
    profileCtx.beginPath();
    run.points.forEach((p, i) => {
      if (i === 0) profileCtx.moveTo(px(p.d), py(p.ele));
      else profileCtx.lineTo(px(p.d), py(p.ele));
    });
    profileCtx.stroke();
  }
}

function renderTrackInfo(s) {
  const el = document.getElementById("track-info");
  el.classList.remove("muted");
  const t0 = s.start_time ? new Date(s.start_time).toLocaleString() : "-";
  const t1 = s.end_time ? new Date(s.end_time).toLocaleString() : "-";
  el.innerHTML =
    `<div>轨迹名称：<b>${escapeHtml(state.name)}</b></div>` +
    `<div>轨迹点：<b>${s.points}</b> 个，原始分段：<b>${s.segments}</b> 个</div>` +
    `<div>总距离：<b>${fmtKm(s.distance_m)} km</b></div>` +
    `<div>时间：${t0}<br>至　${t1}</div>`;
}

function renderStats(d) {
  const el = document.getElementById("stats");
  el.classList.remove("muted");
  const hiddenKm = fmtKm(d.distance_before_m - d.distance_after_m);
  el.innerHTML =
    `<div>隐藏段数：<b>${d.hidden_intervals}</b></div>` +
    `<div>裁切前距离：<b>${fmtKm(d.distance_before_m)} km</b></div>` +
    `<div>裁切后距离：<b>${fmtKm(d.distance_after_m)} km</b>（隐藏 ${hiddenKm} km）</div>` +
    `<div>可见分段：<b>${d.visible_segments.length}</b> 个 trkseg（导出时互不连接）</div>`;
  document.getElementById("download").disabled = false;
}

function renderDrift(data) {
  const el = document.getElementById("drift");
  el.classList.remove("muted");
  const savedKm = fmtKm(data.distance_before_m - data.distance_after_m);
  let html =
    `<div>命中跳点：<b>${data.hit_count}</b> 个` +
    `（轨迹点 ${data.points_before} → ${data.points_after}）</div>` +
    `<div>修复前里程：<b>${fmtKm(data.distance_before_m)} km</b></div>` +
    `<div>修复后里程：<b>${fmtKm(data.distance_after_m)} km</b>（减少 ${savedKm} km）</div>`;
  if (data.hits.length) {
    const rows = data.hits.map((h, i) =>
      "<tr>" +
      `<td>${i + 1}</td>` +
      `<td>分段${h.seg_index} · 第${h.point_index + 1}点</td>` +
      `<td>${fmtTime(h.time)}</td>` +
      `<td>${h.offset_m.toFixed(0)}</td>` +
      `<td>${h.speed_in_mps.toFixed(1)} / ${h.speed_out_mps.toFixed(1)}</td>` +
      `<td>${h.speed_bypass_mps.toFixed(1)}</td>` +
      "</tr>"
    ).join("");
    html +=
      '<table class="pace-table drift-table"><thead><tr>' +
      "<th>序</th><th>位置</th><th>时间</th><th>偏移(m)</th>" +
      "<th>进/出速度(m/s)</th><th>绕过速度(m/s)</th>" +
      `</tr></thead><tbody>${rows}</tbody></table>`;
  }
  el.innerHTML = html;
  document.getElementById("download-fixed").disabled = false;
}

function renderStays(data) {
  const el = document.getElementById("stay");
  el.classList.remove("muted");
  let html =
    `<div>全程记录时长：<b>${fmtDuration(data.recording_s)}</b>，` +
    `其中停留 <b>${fmtDuration(data.stay_total_s)}</b>，` +
    `移动 <b>${fmtDuration(data.moving_s)}</b></div>` +
    `<div>识别到停留：<b>${data.stays.length}</b> 次</div>`;
  if (data.stays.length) {
    const rows = data.stays.map((s, i) =>
      "<tr>" +
      `<td>${i + 1}</td>` +
      `<td>(${s.lat.toFixed(5)}, ${s.lon.toFixed(5)})</td>` +
      `<td>${fmtTime(s.start_time)} ~ ${fmtTime(s.end_time)}</td>` +
      `<td>${fmtDuration(s.duration_s)}</td>` +
      `<td>${s.points}</td>` +
      "</tr>"
    ).join("");
    html +=
      '<table class="pace-table stay-table"><thead><tr>' +
      "<th>序</th><th>位置</th><th>起止时间</th><th>停留时长</th><th>点数</th>" +
      `</tr></thead><tbody>${rows}</tbody></table>`;
  }
  el.innerHTML = html;
}

function renderSimplify(data) {
  const el = document.getElementById("simplify");
  el.classList.remove("muted");
  const removed = data.points_before - data.points_after;
  const ratio = data.points_before > 0
    ? ((removed / data.points_before) * 100).toFixed(1) : "0.0";
  el.innerHTML =
    `<div>误差阈值：<b>${data.tolerance_m} m</b></div>` +
    `<div>轨迹点：<b>${data.points_before}</b> → <b>${data.points_after}</b>` +
    `（减少 ${removed} 个，${ratio}%）</div>` +
    `<div>精简前距离：<b>${fmtKm(data.distance_before_m)} km</b></div>` +
    `<div>精简后距离：<b>${fmtKm(data.distance_after_m)} km</b></div>` +
    `<div>分段：<b>${data.segments.length}</b> 个 trkseg（与原始分段一一对应，互不连接）</div>`;
  document.getElementById("download-simplified").disabled = false;
}

function renderTrack2Info(s) {
  const el = document.getElementById("track2-info");
  el.classList.remove("muted");
  const t0 = s.start_time ? new Date(s.start_time).toLocaleString() : "-";
  const t1 = s.end_time ? new Date(s.end_time).toLocaleString() : "-";
  el.innerHTML =
    `<div>对比轨迹：<b>${escapeHtml(state.otherName)}</b></div>` +
    `<div>轨迹点：<b>${s.points}</b> 个，原始分段：<b>${s.segments}</b> 个</div>` +
    `<div>总距离：<b>${fmtKm(s.distance_m)} km</b></div>` +
    `<div>时间：${t0}<br>至　${t1}</div>`;
}

function renderEncounters(data) {
  const el = document.getElementById("encounter");
  el.classList.remove("muted");
  if (!data.encounters.length) {
    el.innerHTML =
      "<div>未发现相遇：两条轨迹在时间重叠范围内没有持续 ≥ 60 秒、" +
      "距离 ≤ 50 米的区间</div>";
    return;
  }
  const rows = data.encounters.map((e, i) =>
    "<tr>" +
    `<td>${i + 1}</td>` +
    `<td>${fmtTime(e.start_time)} ~ ${fmtTime(e.end_time)}</td>` +
    `<td>${fmtDuration(e.duration_s)}</td>` +
    `<td>${e.min_distance_m.toFixed(1)}</td>` +
    "</tr>"
  ).join("");
  el.innerHTML =
    `<div>相遇次数：<b>${data.encounters.length}</b> 次` +
    `（距离 ≤ 50 米且持续 ≥ 60 秒）</div>` +
    '<table class="pace-table encounter-table"><thead><tr>' +
    "<th>序</th><th>起止时间</th><th>持续时长</th><th>最近距离(m)</th>" +
    `</tr></thead><tbody>${rows}</tbody></table>`;
}

function renderCircleList() {
  const ul = document.getElementById("circle-list");
  ul.innerHTML = "";
  state.circles.forEach((c, i) => {
    const li = document.createElement("li");
    const coord = document.createElement("span");
    coord.className = "coord";
    coord.textContent = `#${i + 1} (${c.lat.toFixed(5)}, ${c.lon.toFixed(5)})`;
    const input = document.createElement("input");
    input.type = "number";
    input.min = "1";
    input.max = "100000";
    input.value = Math.round(c.radius_m);
    input.title = "半径（米）";
    input.addEventListener("change", () => {
      const v = Number(input.value);
      c.radius_m = Number.isFinite(v) && v > 0 ? Math.min(v, 100000) : c.radius_m;
      input.value = Math.round(c.radius_m);
      scheduleMask();
    });
    const del = document.createElement("button");
    del.textContent = "删除";
    del.addEventListener("click", () => {
      state.circles.splice(i, 1);
      renderCircleList();
      scheduleMask();
    });
    li.appendChild(coord);
    li.appendChild(input);
    li.appendChild(del);
    ul.appendChild(li);
  });
  draw();
}

function renderCheckpointList() {
  const ul = document.getElementById("checkpoint-list");
  ul.innerHTML = "";
  state.checkpoints.forEach((c, i) => {
    const li = document.createElement("li");
    const nameInput = document.createElement("input");
    nameInput.type = "text";
    nameInput.className = "cp-name";
    nameInput.placeholder = `检查点 ${i + 1}`;
    nameInput.maxLength = 50;
    nameInput.value = c.name || "";
    nameInput.title = "检查点名称";
    nameInput.addEventListener("input", () => {
      c.name = nameInput.value;  // 名称不影响计算，直接重渲染结果与预览
      if (state.checkpointVisits) renderCheckpointVisits(state.checkpointVisits);
      draw();
    });
    const coord = document.createElement("span");
    coord.className = "coord";
    coord.textContent = `(${c.lat.toFixed(5)}, ${c.lon.toFixed(5)})`;
    const input = document.createElement("input");
    input.type = "number";
    input.min = "1";
    input.max = "1000";
    input.value = Math.round(c.radius_m);
    input.title = "半径（米）";
    input.addEventListener("change", () => {
      const v = Number(input.value);
      c.radius_m = Number.isFinite(v) && v >= 1 ? Math.min(v, 1000) : c.radius_m;
      input.value = Math.round(c.radius_m);
      scheduleCheckpoints();
    });
    const del = document.createElement("button");
    del.textContent = "删除";
    del.addEventListener("click", () => {
      state.checkpoints.splice(i, 1);
      renderCheckpointList();
      scheduleCheckpoints();
    });
    li.appendChild(nameInput);
    li.appendChild(coord);
    li.appendChild(input);
    li.appendChild(del);
    ul.appendChild(li);
  });
  draw();
}

function cpDisplayName(cp, i) {
  const local = state.checkpoints[i];
  const name = ((local && local.name) || cp.name || "").trim();
  return name || `检查点 ${cp.index}`;
}

function renderCheckpointVisits(data) {
  const el = document.getElementById("checkpoint-visits");
  if (!data || !data.checkpoints.length) {
    el.classList.add("muted");
    el.textContent = "添加检查点后自动计算到访记录";
    return;
  }
  el.classList.remove("muted");
  let html = "";
  data.checkpoints.forEach((cp, i) => {
    html +=
      `<div class="cp-block"><div class="cp-head">#${cp.index} ` +
      `${escapeHtml(cpDisplayName(cp, i))}` +
      `<span class="cp-meta">（半径 ${Math.round(cp.radius_m)} 米）</span></div>`;
    if (!cp.visits.length) {
      html += '<div class="cp-none">未到访</div>';
    } else {
      const rows = cp.visits.map((v, j) =>
        "<tr>" +
        `<td>${j + 1}</td>` +
        `<td>${fmtTime(v.start_time)}</td>` +
        `<td>${fmtTime(v.end_time)}</td>` +
        `<td>${v.duration_s.toFixed(1)}</td>` +
        "</tr>"
      ).join("");
      html +=
        `<div>到访 <b>${cp.visit_count}</b> 次，` +
        `累计停留 <b>${cp.total_duration_s.toFixed(1)}</b> 秒</div>` +
        '<table class="pace-table cp-table"><thead><tr>' +
        "<th>序</th><th>进入时间</th><th>离开时间</th><th>停留秒数</th>" +
        `</tr></thead><tbody>${rows}</tbody></table>`;
    }
    html += "</div>";
  });
  el.innerHTML = html;
}

let messageTimer = null;
function showMessage(text) {
  const el = document.getElementById("message");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(messageTimer);
  messageTimer = setTimeout(() => { el.hidden = true; }, 4000);
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

/* ---------- 事件绑定 ---------- */

function resizeCanvas() {
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  canvas.width = Math.max(1, Math.round(rect.width * dpr));
  canvas.height = Math.max(1, Math.round(rect.height * dpr));
  computeTransform();
  draw();
  drawProfile();
}

document.getElementById("file").addEventListener("change", (e) => {
  const file = e.target.files && e.target.files[0];
  if (file) uploadFile(file).catch((err) => showMessage(err.message));
});

document.getElementById("file2").addEventListener("change", (e) => {
  const file = e.target.files && e.target.files[0];
  if (file) uploadOtherFile(file).catch((err) => showMessage(err.message));
});

canvas.addEventListener("click", (e) => {
  if (!state.transform) return;
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const x = (e.clientX - rect.left) * dpr;
  const y = (e.clientY - rect.top) * dpr;
  const [lat, lon] = toLatLon(x, y);
  if (state.editMode === "checkpoint") {
    if (state.checkpoints.length >= MAX_CHECKPOINTS) {
      showMessage(`最多添加 ${MAX_CHECKPOINTS} 个检查点`);
      return;
    }
    state.checkpoints.push({ lat, lon, radius_m: cpRadiusValue(), name: "" });
    renderCheckpointList();
    scheduleCheckpoints();
    return;
  }
  const radiusInput = Number(document.getElementById("radius").value);
  const radius = Number.isFinite(radiusInput) && radiusInput > 0 ? radiusInput : 200;
  state.circles.push({ lat, lon, radius_m: radius });
  renderCircleList();
  scheduleMask();
});

const MODE_HINTS = {
  mask: "当前为遮蔽圆模式：在右侧预览图上<strong>点击</strong>即可添加遮蔽圆。",
  checkpoint:
    "当前为检查点模式：在右侧预览图上<strong>点击</strong>即可添加检查点（最多 10 个）。",
};

function setEditMode(mode) {
  state.editMode = mode;
  document.querySelectorAll('input[name="edit-mode"]').forEach((r) => {
    r.checked = r.value === mode;
  });
  document.getElementById("mode-hint").innerHTML = MODE_HINTS[mode];
}

document.querySelectorAll('input[name="edit-mode"]').forEach((r) => {
  r.addEventListener("change", (e) => setEditMode(e.target.value));
});

document.getElementById("clear-circles").addEventListener("click", () => {
  if (!state.circles.length) return;
  state.circles = [];
  renderCircleList();
  scheduleMask();
});

document.getElementById("clear-checkpoints").addEventListener("click", () => {
  if (!state.checkpoints.length) return;
  state.checkpoints = [];
  renderCheckpointList();
  scheduleCheckpoints();
});

document.getElementById("cp-radius").addEventListener("change", (e) => {
  e.target.value = cpRadiusValue();  // 越界/非法输入收敛到 1～1000
});

document.getElementById("download").addEventListener("click", downloadGpx);
document.getElementById("download-fixed").addEventListener("click", downloadFixedGpx);
document.getElementById("download-simplified").addEventListener("click", downloadSimplifiedGpx);

document.getElementById("tolerance-m").addEventListener("input", scheduleSimplify);
document.getElementById("tolerance-m").addEventListener("change", (e) => {
  e.target.value = toleranceValue();  // 越界/非法输入收敛到 1～100
  refreshSimplify();
});

document.getElementById("split-km").addEventListener("input", schedulePace);
document.getElementById("split-km").addEventListener("change", (e) => {
  e.target.value = splitKmValue();  // 越界/非法输入收敛到 0.1～10
  refreshPace();
});

window.addEventListener("resize", resizeCanvas);
resizeCanvas();
