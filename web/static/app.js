const state = { drawn: null, overlays: {}, layers: [], aoiLayer: null, layerColors: {} };
const $ = (id) => document.getElementById(id);

const map = L.map("map", { center: [35.8617, 104.1954], zoom: 4, zoomControl: true, attributionControl: false });

const street = L.tileLayer("https://webrd04.is.autonavi.com/appmaptile?lang=zh_cn&size=1&scale=1&style=7&x={x}&y={y}&z={z}", {
  maxZoom: 19,
  attribution: "Leaflet | 高德地图"
});
const satellite = L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}", {
  maxZoom: 19,
  attribution: "Leaflet | Esri World Imagery"
});
const satelliteLabels = L.tileLayer("https://webst04.is.autonavi.com/appmaptile?style=8&x={x}&y={y}&z={z}", {
  maxZoom: 19,
  opacity: 0.88,
  attribution: "行政区划与道路标注"
});
satellite.addTo(map);
satelliteLabels.addTo(map);

const drawnItems = new L.FeatureGroup().addTo(map);
map.addControl(new L.Control.Draw({
  draw: { marker: false, circle: false, circlemarker: false, polyline: false, polygon: true, rectangle: true },
  edit: { featureGroup: drawnItems }
}));

map.on(L.Draw.Event.CREATED, (event) => {
  drawnItems.clearLayers();
  drawnItems.addLayer(event.layer);
  state.drawn = event.layer.toGeoJSON().geometry;
  map.fitBounds(event.layer.getBounds(), { padding: [24, 24] });
});

map.on("mousemove", (event) => {
  $("coordText").textContent = `经度: ${event.latlng.lng.toFixed(6)} 纬度: ${event.latlng.lat.toFixed(6)}`;
});
map.on("mouseout", () => { $("coordText").textContent = "经度: -- 纬度: --"; });
map.on("zoomend moveend resize", () => requestAnimationFrame(updateScaleText));

function log(text) {
  $("statusText").textContent = text;
}

function setProgress(value, text) {
  $("downloadProgress").style.width = `${Math.max(0, Math.min(100, value))}%`;
  $("downloadProgressText").textContent = text;
}

document.querySelectorAll(".tab").forEach(btn => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(b => b.classList.remove("active"));
    document.querySelectorAll(".panel").forEach(p => p.classList.remove("active"));
    btn.classList.add("active");
    $(btn.dataset.panel).classList.add("active");
  });
});

$("streetBtn").onclick = () => switchBase(street, "streetBtn");
$("satelliteBtn").onclick = () => switchBase(satellite, "satelliteBtn");

function switchBase(layer, activeId) {
  map.removeLayer(street);
  map.removeLayer(satellite);
  map.removeLayer(satelliteLabels);
  layer.addTo(map);
  if (activeId === "satelliteBtn") satelliteLabels.addTo(map);
  $("streetBtn").classList.toggle("active", activeId === "streetBtn");
  $("satelliteBtn").classList.toggle("active", activeId === "satelliteBtn");
  $("creditText").textContent = activeId === "satelliteBtn" ? "Leaflet | Esri World Imagery" : "Leaflet | 高德地图";
}

function updateScaleText() {
  const size = map.getSize();
  if (!size.x || !size.y) return setScaleBar("--");
  const left = map.containerPointToLatLng([0, size.y / 2]);
  const right = map.containerPointToLatLng([100, size.y / 2]);
  const meters = left.distanceTo(right);
  if (!Number.isFinite(meters) || meters <= 0) return setScaleBar("--");
  setScaleBar(meters >= 1000 ? `${Math.max(1, Math.round(meters / 1000))} km` : `${Math.max(1, Math.round(meters))} m`);
}

function setScaleBar(text) {
  $("scaleText").innerHTML = `<span class="scale-line"></span><span>${text}</span>`;
}

async function init() {
  const defaults = await fetch("/api/defaults").then(r => r.json());
  $("opticalSafeDir").value = defaults.safe_dir;
  $("downloadSafeDir").value = defaults.safe_dir;
  $("zipDir").value = defaults.zip_dir;
  $("outputDir").value = defaults.output_dir;
  $("startDate").value = defaults.start_date;
  $("endDate").value = defaults.end_date;
  $("processStartDate").value = defaults.start_date;
  $("processEndDate").value = defaults.end_date;
  setTimeout(updateScaleText, 150);
}

window.addEventListener("load", () => setTimeout(updateScaleText, 250));

async function showVectorOnMap(path) {
  if (!path) return;
  const res = await fetch(`/api/vector-bounds?path=${encodeURIComponent(path)}`).then(r => r.json());
  if (!res.ok) {
    log(res.error || "处理范围定位失败");
    return;
  }
  if (state.aoiLayer) map.removeLayer(state.aoiLayer);
  if (res.geojson) {
    state.aoiLayer = L.geoJSON(res.geojson, {
      style: { color: "#f5d142", weight: 3, fillColor: "#f5d142", fillOpacity: 0.08 }
    });
    if (!$("showBoundaryPreview") || $("showBoundaryPreview").checked) state.aoiLayer.addTo(map);
  }
  if (res.bounds) map.fitBounds(res.bounds, { padding: [28, 28] });
}

$("showBoundaryPreview").onchange = () => {
  if (!state.aoiLayer) return;
  if ($("showBoundaryPreview").checked) state.aoiLayer.addTo(map);
  else map.removeLayer(state.aoiLayer);
};

$("aoiFile").addEventListener("change", async () => {
  const file = $("aoiFile").files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append("file", file);
  const res = await fetch("/api/aoi", { method: "POST", body: fd }).then(r => r.json());
  if (res.ok) {
    $("aoiPath").value = res.path;
    $("processAoiPath").value = res.path;
    $("processScopeFilePath").value = res.path;
    await showVectorOnMap(res.path);
    log(`区域文件已上传：${res.path}`);
  } else {
    log(res.error || "区域文件上传失败");
  }
});

$("saveDrawnAoi").onclick = async () => {
  if (!state.drawn) {
    log("请先在地图上绘制多边形或矩形区域");
    return;
  }
  const res = await fetch("/api/aoi", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ geometry: state.drawn })
  }).then(r => r.json());
  if (res.ok) {
    $("aoiPath").value = res.path;
    $("processAoiPath").value = res.path;
    $("processScopeFilePath").value = res.path;
    await showVectorOnMap(res.path);
    log(`绘制区域已保存：${res.path}`);
  } else {
    log(res.error || "绘制区域保存失败");
  }
};

async function chooseFolder(targetId) {
  const current = $(targetId).value || "";
  const res = await fetch("/api/choose-folder", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ current })
  }).then(r => r.json());
  if (res.ok && res.path) {
    $(targetId).value = res.path;
    if (targetId === "downloadSafeDir") $("opticalSafeDir").value = res.path;
  } else if (res.error) {
    log(res.error);
  }
}

async function chooseVectorFile(targetId) {
  const current = $(targetId).value || $("aoiPath").value || "";
  const res = await fetch("/api/choose-vector-file", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ current })
  }).then(r => r.json());
  if (res.ok && res.path) {
    $(targetId).value = res.path;
    await showVectorOnMap(res.path);
    log(`已选择处理范围：${res.path}`);
  } else if (res.error) {
    log(res.error);
  }
}

$("chooseZipDir").onclick = () => chooseFolder("zipDir");
$("chooseSafeDir").onclick = () => chooseFolder("downloadSafeDir");
$("chooseOpticalSafeDir").onclick = () => chooseFolder("opticalSafeDir");
$("chooseRadarSafeDir").onclick = () => chooseFolder("radarSafeDir");
$("chooseOutputDir").onclick = () => chooseFolder("outputDir");
$("chooseProcessScopeFile").onclick = async () => {
  await chooseVectorFile("processScopeFilePath");
  if ($("processScopeFilePath").value) $("processAoiPath").value = $("processScopeFilePath").value;
};

function updateToggleAllButton() {
  const outputs = Array.from(document.querySelectorAll(".process-output"));
  const allChecked = outputs.length > 0 && outputs.every(input => input.checked);
  $("toggleAllOutputs").textContent = allChecked ? "取消全选" : "全选";
}

$("toggleAllOutputs").onclick = () => {
  const outputs = Array.from(document.querySelectorAll(".process-output"));
  const shouldCheck = !outputs.every(input => input.checked);
  outputs.forEach(input => { input.checked = shouldCheck; });
  updateToggleAllButton();
};

document.querySelectorAll(".process-output").forEach(input => input.addEventListener("change", updateToggleAllButton));

$("syncProcessAoi").onclick = async () => {
  if ($("aoiPath").value) {
    $("processAoiPath").value = $("aoiPath").value;
    $("processScopeFilePath").value = $("aoiPath").value;
    await showVectorOnMap($("aoiPath").value);
    log("已同步下载页 AOI 到处理 AOI 路径");
    return;
  }
  if (!state.drawn) {
    log("请先在地图上绘制多边形或矩形区域，或在下载页上传 AOI 文件");
    return;
  }
  const res = await fetch("/api/aoi", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ geometry: state.drawn })
  }).then(r => r.json());
  if (res.ok) {
    $("aoiPath").value = res.path;
    $("processAoiPath").value = res.path;
    $("processScopeFilePath").value = res.path;
    await showVectorOnMap(res.path);
    log(`地图绘制范围已保存并同步：${res.path}`);
  } else {
    log(res.error || "地图绘制范围同步失败");
  }
};

function downloadParams() {
  const workers = Math.max(1, Math.min(10, Number($("workers").value || 2)));
  $("workers").value = workers;
  const rawLimit = $("limit").value.trim();
  return {
    aoi_path: $("aoiPath").value,
    start_date: $("startDate").value,
    end_date: $("endDate").value,
    product_type: $("productType").value,
    contains: $("contains").value,
    limit: rawLimit ? Number(rawLimit) : "",
    max_workers: workers,
    safe_dir: $("downloadSafeDir").value || $("opticalSafeDir").value,
    zip_dir: $("zipDir").value
  };
}

$("searchBtn").onclick = async () => {
  setProgress(5, "正在提交检索任务");
  await fetch("/api/search", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(downloadParams()) });
  pollDownload();
};

$("downloadBtn").onclick = async () => {
  setProgress(5, "正在提交下载任务");
  await fetch("/api/download", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(downloadParams()) });
  pollDownload();
};

$("stopDownloadBtn").onclick = async () => {
  const res = await fetch("/api/download/stop", { method: "POST" }).then(r => r.json());
  log(res.message || "已请求停止下载/检索");
  pollDownload();
};

async function pollDownload() {
  const timer = setInterval(async () => {
    const s = await fetch("/api/download/status").then(r => r.json());
    const percent = s.percent ?? (s.status === "done" ? 100 : s.status === "running" ? 35 : 0);
    const isS1 = ($("productType").value || "").includes("SENTINEL-1");
    let progressText = s.progress_text || s.status;
    let message = s.message || "";
    if (isS1) {
      if (!progressText.includes("ASF")) {
        progressText = s.status === "running" ? "ASF 检索/下载中" : progressText;
      }
      if (!message || message.includes("Copernicus")) {
        message = "Sentinel-1 正在使用 ASF / NASA Earthdata 检索下载。";
      }
    }
    setProgress(percent, progressText);
    log(`数据下载/检索：${s.status}\n${message}`);
    if (s.status !== "running" && s.status !== "stopping") clearInterval(timer);
  }, 1200);
}

function controlValue(input) {
  if (input.type === "checkbox") return input.checked;
  if (input.tagName === "SELECT") return input.value;
  if (input.type === "text") return input.value;
  if (input.value === "") return "";
  const value = Number(input.value);
  return Number.isFinite(value) ? value : input.value;
}

function selectedOutputs() {
  return Array.from(document.querySelectorAll(".process-output:checked")).map(input => input.value);
}

function processParams() {
  const params = {};
  document.querySelectorAll("[data-param]").forEach(input => { params[input.dataset.param] = controlValue(input); });
  const aoiPath = $("processScopeFilePath").value || $("processAoiPath").value || $("aoiPath").value;
  return {
    optical_safe_dir: $("opticalSafeDir").value,
    radar_safe_dir: $("radarSafeDir").value,
    safe_dir: $("opticalSafeDir").value,
    output_dir: $("outputDir").value,
    process_scope: "aoi",
    process_scope_path: $("processScopeFilePath").value,
    process_start_date: $("processStartDate").value,
    process_end_date: $("processEndDate").value,
    aoi_path: aoiPath,
    selected_outputs: selectedOutputs(),
    processing_params: params
  };
}

function resetItemProgress(outputs) {
  document.querySelectorAll(".process-item").forEach(item => {
    const key = item.dataset.outputItem;
    const fill = item.querySelector(".item-progress-fill");
    const text = item.querySelector(".item-progress span");
    fill.style.width = "0%";
    text.textContent = outputs.includes(key) ? "等待" : "未选择";
  });
}

function updateItemProgress(itemProgress) {
  Object.entries(itemProgress || {}).forEach(([key, info]) => {
    const item = document.querySelector(`.process-item[data-output-item="${key}"]`);
    if (!item) return;
    const percent = Math.max(0, Math.min(100, Number(info.percent || 0)));
    item.querySelector(".item-progress-fill").style.width = `${percent}%`;
    item.querySelector(".item-progress span").textContent = `${info.status || "处理中"} ${percent}%`;
  });
}

$("processBtn").onclick = async () => {
  const payload = processParams();
  if (!payload.selected_outputs.length) {
    log("请至少选择一个处理项。");
    return;
  }
  if (!payload.aoi_path) {
    log("请先选择处理范围，或同步地图绘制范围。");
    return;
  }
  resetItemProgress(payload.selected_outputs);
  await fetch("/api/process", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(payload)
  });
  const timer = setInterval(async () => {
    const s = await fetch("/api/process/status").then(r => r.json());
    updateItemProgress(s.item_progress);
    log(`数据处理：${s.status}\n${s.message || ""}\n总体进度：${s.percent || 0}%`);
    if (s.status !== "running") {
      clearInterval(timer);
      updateItemProgress(s.item_progress);
      log(`数据处理：${s.status}\n${s.message || ""}\n处理完成后可在“结果图层”中手动加载。`);
    }
  }, 1000);
};

$("stopProcessBtn").onclick = async () => {
  const res = await fetch("/api/process/stop", { method: "POST" }).then(r => r.json());
  log(res.message || "已请求停止处理");
};

$("loadLayersBtn").onclick = loadLayers;
$("batchExportBtn").onclick = () => {
  const out = $("outputDir").value || "";
  window.location.href = `/api/tif-batch?output_dir=${encodeURIComponent(out)}`;
};

async function loadLayers() {
  const out = $("outputDir").value || "";
  const res = await fetch(`/api/layers?output_dir=${encodeURIComponent(out)}`).then(r => r.json());
  if (!res.ok) return;
  clearOverlays();
  state.layers = res.items;
  $("layerList").innerHTML = "";
  res.items.forEach(layer => addLayerControl(layer, false));
  if (res.items[0]) map.fitBounds(res.items[0].bounds);
  $("legend").classList.add("hidden");
  log(`已发现 ${res.items.length} 个处理结果图层，请勾选后叠加到地图。`);
}

function addLayerControl(layer, visible) {
  const card = document.createElement("div");
  card.className = "layer-card";
  const childHtml = (layer.children || []).map(child => `
    <div class="layer-child" data-layer-child="${child.key}">
      <label><input type="radio" name="layer-child-${layer.key}" value="${child.key}"> ${child.name}</label>
      <a href="${child.tif}">导出 TIF</a>
    </div>
  `).join("");
  card.innerHTML = `
    <label><input type="checkbox" ${visible ? "checked" : ""}> ${layer.name}</label>
    <a href="${layer.tif}">导出主结果</a>
    ${childHtml ? `<div class="layer-children">${childHtml}</div>` : ""}
  `;
  const checkbox = card.querySelector("input[type='checkbox']");
  checkbox.onchange = () => {
    if (!checkbox.checked) {
      card.querySelectorAll(".layer-child input").forEach(input => { input.checked = false; });
      hideLayerFamily(layer);
    }
  };
  card.querySelectorAll(".layer-child input").forEach(input => {
    input.onchange = () => {
      const child = (layer.children || []).find(item => item.key === input.value);
      if (!child) return;
      checkbox.checked = true;
      hideLayerFamily(layer);
      toggleLayer({ ...layer, ...child, children: [] }, input.checked);
    };
  });
  $("layerList").appendChild(card);
}

function clearOverlays() {
  Object.values(state.overlays).forEach(layer => map.removeLayer(layer));
  state.overlays = {};
  $("legendItems").innerHTML = "";
}

function hideLayerFamily(layer) {
  const keys = [layer.key, ...(layer.children || []).map(child => child.key)];
  keys.forEach(key => {
    if (state.overlays[key]) {
      map.removeLayer(state.overlays[key]);
      delete state.overlays[key];
    }
  });
  if (Object.keys(state.overlays).length === 0) $("legend").classList.add("hidden");
}

function toggleLayer(layer, visible) {
  Object.keys(state.overlays).forEach((key) => {
    if ((layer.children || []).some(child => child.key === key) && !visible) {
      map.removeLayer(state.overlays[key]);
      delete state.overlays[key];
    }
  });
  if (state.overlays[layer.key]) map.removeLayer(state.overlays[layer.key]);
  delete state.overlays[layer.key];
  if (!visible) {
    if (Object.keys(state.overlays).length === 0) $("legend").classList.add("hidden");
    return;
  }
  const overlay = L.imageOverlay(layerPngUrl(layer) + `&t=${Date.now()}`, layer.bounds, { opacity: 0.72, interactive: false });
  overlay.addTo(map);
  state.overlays[layer.key] = overlay;
  updateLegend(layer);
}

function layerPngUrl(layer) {
  const colors = state.layerColors[layer.key];
  if (!colors) return layer.png;
  const sep = layer.png.includes("?") ? "&" : "?";
  return `${layer.png}${sep}colors=${encodeURIComponent(JSON.stringify(colors))}`;
}

function refreshVisibleLayer(layerKey) {
  const layer = state.layers.find(item => item.key === layerKey);
  if (!layer || !state.overlays[layerKey]) return;
  toggleLayer(layer, true);
}

function updateLegend(layer) {
  $("legend").classList.remove("hidden");
  $("legendItems").innerHTML = "";
  layer.legend.forEach((item) => {
    if (item.kind === "note") {
      const noteRow = document.createElement("div");
      noteRow.className = "legend-note";
      noteRow.innerHTML = `<strong>${item.name || ""}</strong>${item.note ? `<small>${item.note}</small>` : ""}`;
      $("legendItems").appendChild(noteRow);
      return;
    }
    const value = item.value ?? item[2];
    const name = item.name ?? item[0];
    const color = item.color ?? item[1];
    const note = item.note || "";
    const row = document.createElement("div");
    row.className = "legend-row";
    row.innerHTML = `
      <input class="legend-color" type="color" value="${color}">
      <span class="legend-text"><strong>${name}</strong>${note ? `<small>${note}</small>` : ""}</span>
    `;
    row.querySelector(".legend-color").oninput = (event) => {
      if (value === undefined || value === null) return;
      if (!state.layerColors[layer.key]) state.layerColors[layer.key] = {};
      state.layerColors[layer.key][value] = event.target.value;
      refreshVisibleLayer(layer.key);
    };
    $("legendItems").appendChild(row);
  });
}

init();
updateToggleAllButton();
