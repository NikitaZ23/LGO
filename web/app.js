const healthStrip = document.querySelector("#healthStrip");
const refreshHealth = document.querySelector("#refreshHealth");
const restartServiceButton = document.querySelector("#restartService");
const shutdownServiceButton = document.querySelector("#shutdownService");
const form = document.querySelector("#jobForm");
const addTextureButton = document.querySelector("#addTextureButton");
const rebakeTextureButton = document.querySelector("#rebakeTextureButton");
const modeInput = document.querySelector("#mode");
const qualityInput = document.querySelector("#quality");
const objectTypeInput = document.querySelector("#objectType");
const textureInput = document.querySelector("#texture");
const textureQualityInput = document.querySelector("#textureQuality");
const rebakeAlbedoInput = document.querySelector("#rebakeAlbedo");
const rebakeAlbedoValue = document.querySelector("#rebakeAlbedoValue");
const textureColorInput = document.querySelector("#textureColor");
const textureColorValue = document.querySelector("#textureColorValue");
const singleFields = document.querySelector("#singleFields");
const multiFields = document.querySelector("#multiFields");
const jobStatus = document.querySelector("#jobStatus");
const modeButtons = document.querySelectorAll("[data-mode]");
const qualityButtons = document.querySelectorAll("[data-quality]");
const objectTypeButtons = document.querySelectorAll("[data-object-type]");
const textureButtons = document.querySelectorAll("[data-texture]");
const textureQualityButtons = document.querySelectorAll("[data-texture-quality]");
const progressFill = document.querySelector("#progressFill");
const progressPercent = document.querySelector("#progressPercent");
const progressStatus = document.querySelector("#progressStatus");
const runBadge = document.querySelector("#runBadge");
const sceneStatus = document.querySelector("#sceneStatus");
const resultTextureToggle = document.querySelector("#resultTextureToggle");
const resultTextureInput = document.querySelector("#resultTextureInput");
const sceneEmpty = document.querySelector("#sceneEmpty");
const modelViewer = document.querySelector("#modelViewer");
const sceneFrame = document.querySelector(".scene-frame");
const viewerChoiceBar = document.querySelector("#viewerChoices");
const outputLinks = document.querySelector("#outputLinks");
const ratingPanel = document.querySelector("#ratingPanel");
const historyList = document.querySelector("#historyList");
const refreshHistory = document.querySelector("#refreshHistory");
const historyFilterButtons = document.querySelectorAll("[data-history-filter]");
const fileInputs = document.querySelectorAll("[data-preview-target]");

const terminalStatuses = new Set(["completed", "completed_with_warnings", "failed", "needs_runtime"]);
const progressByStatus = {
  created: 4,
  prepared: 8,
  queued: 12,
  running: 18,
  preprocessing_images: 24,
  loading_shape_model: 32,
  generating_shape: 62,
  postprocessing_mesh: 72,
  queued_texture: 76,
  running_texture: 78,
  applying_texture: 82,
  rebaking_texture: 88,
  converting_outputs: 92,
  completed_with_warnings: 100,
  completed: 100,
  failed: 100,
  needs_runtime: 100,
};

let pollTimer = null;
let currentJobId = null;
let currentJob = null;
let textureButtonJobId = null;
let rebakeTextureJobId = null;
let historyJobs = [];
let historyLoaded = false;
let historyFilter = localStorage.getItem("lgo.historyFilter") || "all";
let currentSceneFilename = "";
let currentSceneOutputs = null;
const previewUrls = new Map();
const persistedFiles = new Map();
const inputDbName = "lgo-input-cache";
const inputStoreName = "files";
let inputDbPromise = null;
const objectTypeValues = ["organic", "hard_surface", "rock"];
const rebakeAlbedoRange = {
  min: 0.5,
  max: 1.8,
  defaultValue: 1,
};
const textureColorRange = {
  min: 0.35,
  max: 1.6,
  defaultValue: 1,
};
const sceneZoom = {
  minRadius: 0.05,
  maxRadius: 160,
  defaultRadius: 12,
  wheelSensitivity: 0.00035,
};

function badge(label, ok, warn = false) {
  const className = ok ? "ok" : warn ? "warn" : "bad";
  return `<span class="badge ${className}">${label}</span>`;
}

async function loadHealth() {
  healthStrip.innerHTML = badge("Checking", true, true);
  try {
    const response = await fetch("/api/health");
    const data = await response.json();
    const gpu = data.gpu.raw ? data.gpu.raw.split(",").map((part) => part.trim()) : [];
    healthStrip.innerHTML = [
      badge("Python " + (data.python.version || "missing"), data.python.ok),
      badge("Blender " + ((data.blender.version || [])[0] || "missing"), data.blender.ok),
      badge(gpu[0] || "GPU missing", data.gpu.ok),
      badge("Venv", data.venv.ok, !data.venv.ok),
      badge("Torch", data.venv.torch.ok, !data.venv.torch.ok),
      badge("Deps", Object.values(data.venv.packages || {}).every((item) => item.ok), true),
      badge("PBR native", data.ready_for_pbr_texture, true),
      badge("Single model", data.models.single_shape.ok),
      badge("Multiview model", data.models.multiview_shape.ok),
      badge("Paint model", data.models.paint_model.ok),
      badge("Runtime source", data.runtime.hunyuan_source.ok, !data.runtime.hunyuan_source.ok),
    ].join("");
  } catch (error) {
    healthStrip.innerHTML = badge("Server offline", false);
  }
}

function setMode(mode) {
  modeInput.value = mode;
  localStorage.setItem("lgo.mode", mode);
  modeButtons.forEach((button) => {
    button.classList.toggle("active", button.dataset.mode === mode);
  });
  singleFields.classList.toggle("hidden", mode !== "single");
  multiFields.classList.toggle("hidden", mode !== "multiview");
}

modeButtons.forEach((button) => {
  button.addEventListener("click", () => setMode(button.dataset.mode));
});

function setQuality(quality) {
  qualityInput.value = quality;
  localStorage.setItem("lgo.quality", quality);
  qualityButtons.forEach((button) => {
    button.classList.toggle("active", button.dataset.quality === quality);
  });
}

qualityButtons.forEach((button) => {
  button.addEventListener("click", () => setQuality(button.dataset.quality));
});

function setObjectType(objectType) {
  const selected = objectTypeValues.includes(objectType) ? objectType : "organic";
  objectTypeInput.value = selected;
  localStorage.setItem("lgo.objectType", selected);
  objectTypeButtons.forEach((button) => {
    button.classList.toggle("active", button.dataset.objectType === selected);
  });
}

objectTypeButtons.forEach((button) => {
  button.addEventListener("click", () => setObjectType(button.dataset.objectType));
});

function setTexture(enabled) {
  textureInput.value = enabled;
  localStorage.setItem("lgo.texture", enabled);
  textureButtons.forEach((button) => {
    button.classList.toggle("active", button.dataset.texture === enabled);
  });
}

textureButtons.forEach((button) => {
  button.addEventListener("click", () => setTexture(button.dataset.texture));
});

function setTextureQuality(quality) {
  const selected = ["fast", "balanced", "high"].includes(quality) ? quality : "fast";
  textureQualityInput.value = selected;
  localStorage.setItem("lgo.textureQuality", selected);
  textureQualityButtons.forEach((button) => {
    button.classList.toggle("active", button.dataset.textureQuality === selected);
  });
}

textureQualityButtons.forEach((button) => {
  button.addEventListener("click", () => setTextureQuality(button.dataset.textureQuality));
});

function setRebakeAlbedo(value) {
  const selected = normalizeRebakeAlbedo(value);
  if (rebakeAlbedoInput) {
    rebakeAlbedoInput.value = selected.toFixed(2);
  }
  if (rebakeAlbedoValue) {
    rebakeAlbedoValue.textContent = formatAlbedo(selected);
  }
  localStorage.setItem("lgo.rebakeAlbedo", selected.toFixed(2));
}

function normalizeRebakeAlbedo(value) {
  const parsed = Number.parseFloat(String(value ?? "").replace(",", "."));
  if (!Number.isFinite(parsed)) {
    return rebakeAlbedoRange.defaultValue;
  }
  const clamped = Math.max(rebakeAlbedoRange.min, Math.min(rebakeAlbedoRange.max, parsed));
  return Math.round(clamped * 100) / 100;
}

function currentRebakeAlbedo() {
  return normalizeRebakeAlbedo(rebakeAlbedoInput?.value);
}

function formatAlbedo(value) {
  return `${normalizeRebakeAlbedo(value).toFixed(2)}x`;
}

if (rebakeAlbedoInput) {
  rebakeAlbedoInput.addEventListener("input", () => setRebakeAlbedo(rebakeAlbedoInput.value));
}

function setTextureColor(value) {
  const selected = normalizeTextureColor(value);
  if (textureColorInput) {
    textureColorInput.value = selected.toFixed(2);
  }
  if (textureColorValue) {
    textureColorValue.textContent = formatTextureColor(selected);
  }
  localStorage.setItem("lgo.textureColor", selected.toFixed(2));
}

function normalizeTextureColor(value) {
  const parsed = Number.parseFloat(String(value ?? "").replace(",", "."));
  if (!Number.isFinite(parsed)) {
    return textureColorRange.defaultValue;
  }
  const clamped = Math.max(textureColorRange.min, Math.min(textureColorRange.max, parsed));
  return Math.round(clamped * 100) / 100;
}

function currentTextureColor() {
  return normalizeTextureColor(textureColorInput?.value);
}

function formatTextureColor(value) {
  return `${normalizeTextureColor(value).toFixed(2)}x`;
}

if (textureColorInput) {
  textureColorInput.addEventListener("input", () => setTextureColor(textureColorInput.value));
}

fileInputs.forEach((input) => {
  input.addEventListener("change", () => {
    void updateImagePreview(input);
  });
});

refreshHealth.addEventListener("click", loadHealth);
if (restartServiceButton) {
  restartServiceButton.addEventListener("click", restartService);
}
if (shutdownServiceButton) {
  shutdownServiceButton.addEventListener("click", shutdownService);
}
if (refreshHistory) {
  refreshHistory.addEventListener("click", loadHistory);
}
if (resultTextureInput) {
  resultTextureInput.addEventListener("change", () => toggleResultTexture(resultTextureInput.checked));
}

historyFilterButtons.forEach((button) => {
  button.addEventListener("click", () => setHistoryFilter(button.dataset.historyFilter));
});

addTextureButton.addEventListener("click", addTextureToCurrentJob);
if (rebakeTextureButton) {
  rebakeTextureButton.addEventListener("click", rebakeTextureForCurrentJob);
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
  const data = new FormData(form);
  appendPersistedFiles(data);
  if (!data.has("formats")) {
    data.append("formats", "glb");
  }

  setProgress("queued", "Creating job...");
  runBadge.textContent = "Starting";
  jobStatus.textContent = "Creating job...";
  currentJob = null;
  updateAddTextureButton(null);
  clearScene();

  try {
    const response = await fetch("/api/jobs", {
      method: "POST",
      body: data,
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || response.statusText);
    }
    localStorage.setItem("lgo.lastJobId", payload.id);
    renderJob(payload);
    void loadHistory();
    if (payload.id && !terminalStatuses.has(payload.status)) {
      pollJob(payload.id);
    }
  } catch (error) {
    setProgress("failed", "LGO server is not responding.");
    runBadge.textContent = "Failed";
    jobStatus.textContent = [
      "LGO server is not responding.",
      "",
      "Run start-lgo-background.bat or start-lgo.bat from E:\\AI\\Projects\\LGO, then refresh this page.",
      "",
      String(error),
    ].join("\n");
  }
});

function renderJob(job, options = {}) {
  currentJobId = job.id || currentJobId;
  currentJob = job;
  setProgress(job.status, job.message || job.status);
  runBadge.textContent = readableStatus(job.status);

  const lines = [
    `Job: ${job.id}`,
    `Status: ${job.status}`,
    `Message: ${job.message || ""}`,
  ];

  if (job.payload) {
    lines.push(`Quality: ${job.payload.quality || "default"}`);
    lines.push(`Object type: ${objectTypeLabel(job.payload.object_type || job.object_type?.selected)}`);
    lines.push(`Texture: ${job.payload.texture ? "PBR texture" : "No texture"}`);
    lines.push(`Texture speed: ${textureQualityLabel(job.payload.texture_quality || job.texture_quality?.selected)}`);
    if (job.payload.texture_color !== undefined) {
      lines.push(`Texture color: ${formatTextureColor(job.payload.texture_color)}`);
      setTextureColor(job.payload.texture_color);
    }
    if (job.payload.rebake_albedo !== undefined) {
      lines.push(`Re-bake albedo: ${formatAlbedo(job.payload.rebake_albedo)}`);
      setRebakeAlbedo(job.payload.rebake_albedo);
    }
  }
  if (job.quality) {
    lines.push(`Applied preset: ${job.quality.label || job.quality.selected}`);
  }
  if (job.object_type) {
    lines.push(`Applied object preset: ${job.object_type.label || objectTypeLabel(job.object_type.selected)}`);
  }
  if (job.texture_quality) {
    lines.push(`Applied texture preset: ${job.texture_quality.label || job.texture_quality.selected}`);
  }

  if (job.process_id) {
    lines.push(`Process: ${job.process_id}`);
  }
  if (job.log) {
    lines.push(`Log: ${job.log}`);
  }
  if (job.run_dir) {
    lines.push(`Run dir: ${job.run_dir}`);
  }
  if (job.warnings && job.warnings.length) {
    lines.push("", "Warnings:", ...job.warnings.map((warning) => `- ${warning}`));
  }
  if (job.error) {
    lines.push("", "Error:", job.error);
  }

  jobStatus.textContent = lines.join("\n");
  renderOutputs(job, options);
  updateAddTextureButton(job);
  highlightHistoryJob(job.id);
  loadJobLog(job, lines.join("\n"));
}

async function pollJob(jobId) {
  try {
    const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`);
    const payload = await response.json();
    renderJob(payload);
    if (!terminalStatuses.has(payload.status)) {
      pollTimer = setTimeout(() => pollJob(jobId), 5000);
    } else {
      void loadHistory();
    }
  } catch (error) {
    jobStatus.textContent += `\n\nPolling failed: ${error}`;
  }
}

async function loadJobLog(job, fallbackText) {
  if (!job.id) {
    return;
  }
  try {
    const response = await fetch(`/api/jobs/${encodeURIComponent(job.id)}/log`);
    if (!response.ok) {
      return;
    }
    const contentType = response.headers.get("content-type") || "";
    if (!contentType.includes("text/plain")) {
      return;
    }
    const log = await response.text();
    if (currentJobId !== job.id) {
      return;
    }
    const readableLog = formatRunLog(log);
    jobStatus.textContent = readableLog.trim()
      ? `${fallbackText}\n\nLog tail (progress time: elapsed < ETA):\n${readableLog}`
      : fallbackText;
    jobStatus.scrollTop = jobStatus.scrollHeight;
  } catch (error) {
    if (currentJobId === job.id) {
      jobStatus.textContent = fallbackText;
    }
  }
}

function formatRunLog(log) {
  return String(log || "")
    .replace(/\r\n/g, "\n")
    .replace(/\r/g, "\n")
    .replace(/\u001b\[[0-9;?]*[ -/]*[@-~]/g, "")
    .replace(/(\])(?=(Diffusion Sampling::|Volume Decoding:))/g, "$1\n")
    .split("\n")
    .map((line) => formatLogLine(line.trimEnd()))
    .filter(Boolean)
    .join("\n");
}

function formatLogLine(line) {
  const trimmed = line.trim();
  if (!trimmed) {
    return "";
  }

  const progress = trimmed.match(/^(.+?):{1,2}\s+(\d{1,3})%\|[^|]*\|\s*(\d+)\/(\d+)\s+\[([^<,\]]+)(?:<([^,\]]+))?(?:,\s*([^\]]+))?\]/);
  if (!progress) {
    return line;
  }

  const [, label, percent, current, total, elapsed, eta, rate] = progress;
  const parts = [
    `${label.trim()}: ${percent}% (${current}/${total})`,
    `elapsed ${elapsed.trim()}`,
  ];
  if (eta && eta.trim()) {
    parts.push(`ETA ${eta.trim()}`);
  }
  if (rate && rate.trim()) {
    parts.push(rate.trim().replace(/\s+/g, " "));
  }
  return parts.join(", ");
}

function setProgress(status, message) {
  const percent = progressByStatus[status] ?? 18;
  progressFill.style.width = `${percent}%`;
  progressPercent.textContent = `${percent}%`;
  progressStatus.textContent = message || readableStatus(status);
  progressFill.classList.toggle("failed", status === "failed" || status === "needs_runtime");
}

async function loadHistory() {
  if (!historyList) {
    return;
  }
  historyList.textContent = "Loading history...";
  try {
    const response = await fetch("/api/jobs?limit=60");
    if (!response.ok) {
      throw new Error(response.status === 404 ? "History API is not active yet." : response.statusText);
    }
    const payload = await response.json();
    historyJobs = payload.jobs || [];
    historyLoaded = true;
    renderHistory(historyJobs);
  } catch (error) {
    historyLoaded = false;
    historyList.textContent = `History unavailable. Restart LGO service to activate it. ${error}`;
  }
}

function setHistoryFilter(filter) {
  historyFilter = ["all", "models", "no-model"].includes(filter) ? filter : "all";
  localStorage.setItem("lgo.historyFilter", historyFilter);
  historyFilterButtons.forEach((button) => {
    button.classList.toggle("active", button.dataset.historyFilter === historyFilter);
  });
  if (historyList && historyLoaded) {
    renderHistory(historyJobs);
  }
}

function filteredHistoryJobs(jobs) {
  if (historyFilter === "models") {
    return jobs.filter((job) => job.has_model);
  }
  if (historyFilter === "no-model") {
    return jobs.filter((job) => !job.has_model);
  }
  return jobs;
}

function renderHistory(jobs) {
  const visibleJobs = filteredHistoryJobs(jobs);
  if (!visibleJobs.length) {
    historyList.textContent = jobs.length ? "No items for this filter." : "No generations yet.";
    return;
  }

  const fragment = document.createDocumentFragment();
  visibleJobs.forEach((job) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = `history-item${job.id === currentJobId ? " active" : ""}${job.has_model ? "" : " no-model"}`;
    item.dataset.jobId = job.id;
    item.addEventListener("click", () => loadHistoryJob(job.id));

    const text = document.createElement("span");
    const title = document.createElement("span");
    title.className = "history-main";
    title.textContent = job.display_name || job.id;
    const meta = document.createElement("span");
    meta.className = "history-meta";
    meta.textContent = historyMeta(job);
    text.append(title, meta);

    const action = document.createElement("span");
    action.className = "history-load";
    action.textContent = job.has_model ? "Load" : "Open";
    item.append(text, action);
    fragment.appendChild(item);
  });
  historyList.replaceChildren(fragment);
}

async function loadHistoryJob(jobId) {
  if (!jobId) {
    return;
  }
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }

  runBadge.textContent = "Loading";
  try {
    const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`);
    const job = await response.json();
    if (!response.ok) {
      throw new Error(job.error || response.statusText);
    }
    localStorage.setItem("lgo.lastJobId", job.id);
    renderJob(job, { preferredScene: "white" });
    if (!terminalStatuses.has(job.status)) {
      pollJob(job.id);
    }
  } catch (error) {
    runBadge.textContent = "Failed";
    jobStatus.textContent = `Could not load history item ${jobId}.\n\n${error}`;
  }
}

function historyMeta(job) {
  const mode = job.mode === "multiview" ? "4 views" : "1 image";
  const texture = job.texture ? "texture" : "no texture";
  const quality = job.quality || "default";
  const objectType = objectTypeLabel(job.object_type);
  const textureQuality = `${textureQualityLabel(job.texture_quality)} tex`;
  const albedo = albedoMeta(job.rebake_albedo);
  const color = textureColorMeta(job.texture_color);
  const output = job.primary_output ? job.primary_output.label || job.primary_output.filename : "no model";
  const ratings = ratingMeta(job.ratings);
  return [mode, quality, objectType, texture, textureQuality, albedo, color, job.status, output, ratings]
    .filter(Boolean)
    .join(" · ");
}

function albedoMeta(value) {
  if (value === undefined || value === null || value === "") {
    return "";
  }
  return `albedo ${formatAlbedo(value)}`;
}

function textureColorMeta(value) {
  if (value === undefined || value === null || value === "") {
    return "";
  }
  return `color ${formatTextureColor(value)}`;
}

function ratingMeta(ratings) {
  const parts = [];
  const whiteRating = ratingValue({ ratings }, "white");
  const textureRating = ratingValue({ ratings }, "texture");
  if (whiteRating) {
    parts.push(`white ${whiteRating}★`);
  }
  if (textureRating) {
    parts.push(`tex ${textureRating}★`);
  }
  return parts.join(" · ");
}

function highlightHistoryJob(jobId) {
  if (!historyList) {
    return;
  }
  historyList.querySelectorAll(".history-item").forEach((item) => {
    item.classList.toggle("active", item.dataset.jobId === jobId);
  });
}

function renderOutputs(job, options = {}) {
  outputLinks.innerHTML = "";
  if (viewerChoiceBar) {
    viewerChoiceBar.innerHTML = "";
  }
  const outputs = job.outputs || [];
  const sceneOutputs = collectSceneOutputs(job);
  const { glbOutputs, whiteGlb, texturedGlb, fallbackGlb } = sceneOutputs;
  currentSceneOutputs = sceneOutputs;
  const primaryGlb = choosePrimaryGlb(sceneOutputs, options.preferredScene);

  const viewerChoices = [whiteGlb, texturedGlb, fallbackGlb]
    .filter(Boolean)
    .filter((output, index, list) => list.findIndex((item) => item.filename === output.filename) === index);

  renderViewerChoices(job, viewerChoices);
  renderRatings(job, whiteGlb, texturedGlb);

  outputs.forEach((output) => {
    const link = document.createElement("a");
    link.href = outputUrl(job.id, output.filename, outputCacheKey(job, output));
    link.textContent = output.label || output.format.toUpperCase();
    link.target = "_blank";
    outputLinks.appendChild(link);
  });

  if (primaryGlb) {
    showSceneOutput(job, primaryGlb);
    modelViewer.classList.remove("hidden");
    sceneEmpty.classList.add("hidden");
    return;
  }

  updateResultTextureToggle(null, null);

  if (terminalStatuses.has(job.status) && outputs.length) {
    sceneStatus.textContent = "No GLB";
    sceneEmpty.textContent = "No GLB output.";
    sceneEmpty.classList.remove("hidden");
    modelViewer.classList.add("hidden");
    return;
  }

  sceneStatus.textContent = terminalStatuses.has(job.status) ? "Empty" : "Waiting";
  sceneEmpty.textContent = terminalStatuses.has(job.status) ? "No model output." : "Generation in progress.";
  sceneEmpty.classList.remove("hidden");
  modelViewer.classList.add("hidden");
}

function renderViewerChoices(job, choices) {
  if (!viewerChoiceBar) {
    return;
  }
  viewerChoiceBar.innerHTML = "";
  viewerChoiceBar.classList.toggle("hidden", choices.length < 2);
  choices.forEach((output) => {
    const button = document.createElement("button");
    button.type = "button";
    button.dataset.filename = output.filename;
    button.dataset.cacheKey = outputCacheKey(job, output);
    button.textContent = sceneChoiceLabel(output);
    button.addEventListener("click", () => showSceneOutput(job, output));
    viewerChoiceBar.appendChild(button);
  });
  updateViewerChoiceState();
}

function collectSceneOutputs(job) {
  const glbOutputs = (job?.outputs || []).filter((output) => output.format === "glb");
  const texturedGlb = glbOutputs.find((output) => output.filename === "textured_mesh.glb")
    || glbOutputs.find((output) => output.filename === "textured_mesh_stable.glb")
    || null;
  const whiteGlb = glbOutputs.find((output) => output.filename === "white_mesh.glb") || null;
  const fallbackGlb = glbOutputs.find((output) => output !== whiteGlb && output !== texturedGlb) || null;
  return { glbOutputs, whiteGlb, texturedGlb, fallbackGlb };
}

function choosePrimaryGlb(sceneOutputs, preferredScene) {
  const { glbOutputs, whiteGlb, texturedGlb, fallbackGlb } = sceneOutputs;
  if (preferredScene === "white" && whiteGlb) {
    return whiteGlb;
  }
  if (preferredScene === "texture" && texturedGlb) {
    return texturedGlb;
  }
  return texturedGlb || whiteGlb || fallbackGlb || glbOutputs[0] || null;
}

function showSceneOutput(job, output) {
  if (!job?.id || !output) {
    return;
  }
  setSceneSource(job.id, output.filename, outputCacheKey(job, output));
  sceneStatus.textContent = sceneLoadedLabel(output);
  updateResultTextureToggle(output, currentSceneOutputs);
}

function updateResultTextureToggle(selectedOutput, sceneOutputs = currentSceneOutputs) {
  if (!resultTextureToggle || !resultTextureInput) {
    return;
  }
  const hasWhite = Boolean(sceneOutputs?.whiteGlb);
  const hasTexture = Boolean(sceneOutputs?.texturedGlb);
  const selectedFilename = selectedOutput?.filename || currentSceneFilename;
  const texturedFilename = sceneOutputs?.texturedGlb?.filename || "";
  const isTextured = hasTexture && (!hasWhite || selectedFilename === texturedFilename);

  resultTextureToggle.classList.toggle("hidden", !hasWhite && !hasTexture);
  resultTextureInput.disabled = !hasTexture || !hasWhite;
  resultTextureInput.checked = isTextured;
  const title = !hasTexture
    ? "Textured mesh is not available for this result."
    : !hasWhite
      ? "Only textured mesh is available for this result."
      : "Switch between the white mesh and textured mesh.";
  resultTextureToggle.title = title;
  resultTextureInput.title = title;
}

function toggleResultTexture(showTexture) {
  if (!currentJob) {
    return;
  }
  const sceneOutputs = currentSceneOutputs || collectSceneOutputs(currentJob);
  const target = showTexture ? sceneOutputs.texturedGlb : sceneOutputs.whiteGlb;
  if (!target) {
    updateResultTextureToggle(null, sceneOutputs);
    return;
  }
  showSceneOutput(currentJob, target);
}

function sceneChoiceLabel(output) {
  if (output.filename === "white_mesh.glb") {
    return "White mesh";
  }
  if (output.filename === "textured_mesh_stable.glb" || output.filename === "textured_mesh.glb") {
    return "Textured mesh";
  }
  return output.label || output.filename;
}

function updateViewerChoiceState() {
  if (!viewerChoiceBar) {
    return;
  }
  viewerChoiceBar.querySelectorAll("button").forEach((button) => {
    button.classList.toggle("active", button.dataset.filename === currentSceneFilename);
  });
}

function sceneLoadedLabel(output) {
  const label = sceneChoiceLabel(output);
  return output?.modified_at
    ? `Loaded ${label} - ${output.modified_at.replace("T", " ")}`
    : `Loaded ${label}`;
}

function setSceneSource(jobId, filename, cacheKey = "") {
  const url = outputUrl(jobId, filename, cacheKey);
  currentSceneFilename = filename;
  updateViewerChoiceState();
  if (modelViewer.getAttribute("src") === url) {
    return;
  }
  modelViewer.src = url;
  applySceneCameraOrbit({ theta: 0, phi: 78, radius: sceneZoom.defaultRadius }, true);
}

function clearScene() {
  modelViewer.removeAttribute("src");
  currentSceneFilename = "";
  modelViewer.classList.add("hidden");
  sceneEmpty.textContent = "Generation in progress.";
  sceneEmpty.classList.remove("hidden");
  sceneStatus.textContent = "Waiting";
  outputLinks.innerHTML = "";
  if (viewerChoiceBar) {
    viewerChoiceBar.innerHTML = "";
  }
  if (ratingPanel) {
    ratingPanel.innerHTML = "";
    ratingPanel.classList.add("hidden");
  }
  currentSceneOutputs = null;
  updateResultTextureToggle(null, null);
}

function renderRatings(job, whiteGlb, texturedGlb) {
  if (!ratingPanel) {
    return;
  }

  const rows = [];
  if (whiteGlb) {
    rows.push({ target: "white", label: "White mesh" });
  }
  if (texturedGlb) {
    rows.push({ target: "texture", label: "Textured mesh" });
  }

  if (!rows.length) {
    ratingPanel.innerHTML = "";
    ratingPanel.classList.add("hidden");
    return;
  }

  const fragment = document.createDocumentFragment();
  rows.forEach((row) => {
    fragment.appendChild(createRatingRow(job, row));
  });
  ratingPanel.replaceChildren(fragment);
  ratingPanel.classList.remove("hidden");
}

function createRatingRow(job, row) {
  const currentRating = ratingValue(job, row.target);
  const wrapper = document.createElement("div");
  wrapper.className = "rating-row";

  const label = document.createElement("span");
  label.className = "rating-label";
  label.textContent = row.label;

  const stars = document.createElement("span");
  stars.className = "rating-stars";
  for (let value = 1; value <= 5; value += 1) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `rating-star${value <= currentRating ? " active" : ""}`;
    button.textContent = "★";
    button.title = `${row.label}: ${value} / 5`;
    button.setAttribute("aria-label", `${row.label}: ${value} of 5`);
    button.addEventListener("click", () => saveRating(row.target, value));
    stars.appendChild(button);
  }

  const valueText = document.createElement("span");
  valueText.className = "rating-value";
  valueText.textContent = currentRating ? `${currentRating}/5` : "not rated";

  wrapper.append(label, stars, valueText);
  return wrapper;
}

function ratingValue(job, target) {
  const value = Number(job?.ratings?.[target] || 0);
  return Number.isInteger(value) && value >= 1 && value <= 5 ? value : 0;
}

function ratingOutputs(job) {
  const glbOutputs = (job?.outputs || []).filter((output) => output.format === "glb");
  return {
    white: glbOutputs.find((output) => output.filename === "white_mesh.glb"),
    texture: glbOutputs.find((output) => output.filename === "textured_mesh.glb")
      || glbOutputs.find((output) => output.filename === "textured_mesh_stable.glb"),
  };
}

async function saveRating(target, rating) {
  if (!currentJobId || !currentJob) {
    return;
  }

  const jobId = currentJobId;
  const previousRatings = { ...(currentJob.ratings || {}) };
  currentJob.ratings = {
    ...previousRatings,
    [target]: rating,
  };
  const outputs = ratingOutputs(currentJob);
  renderRatings(currentJob, outputs.white, outputs.texture);

  try {
    const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}/rating`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ target, rating }),
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || response.statusText);
    }
    if (currentJobId === jobId) {
      currentJob.ratings = payload.ratings || currentJob.ratings || {};
      const refreshedOutputs = ratingOutputs(currentJob);
      renderRatings(currentJob, refreshedOutputs.white, refreshedOutputs.texture);
    }
    void loadHistory();
  } catch (error) {
    if (currentJobId === jobId) {
      currentJob.ratings = previousRatings;
      const refreshedOutputs = ratingOutputs(currentJob);
      renderRatings(currentJob, refreshedOutputs.white, refreshedOutputs.texture);
    }
    jobStatus.textContent += `\n\nRating save failed: ${error}`;
  }
}

function setupSmoothSceneZoom() {
  modelViewer.addEventListener("wheel", handleSceneWheel, { passive: false, capture: true });
  if (sceneFrame) {
    sceneFrame.addEventListener("wheel", handleSceneWheel, { passive: false, capture: true });
  }
}

function handleSceneWheel(event) {
  if (modelViewer.classList.contains("hidden") || !hasSceneModel()) {
    return;
  }

  event.preventDefault();
  event.stopPropagation();
  if (typeof event.stopImmediatePropagation === "function") {
    event.stopImmediatePropagation();
  }

  const orbit = getCurrentCameraOrbit();
  const delta = normalizeWheelDelta(event);
  const limitedDelta = Math.max(-260, Math.min(260, delta));
  const nextRadius = clamp(
    orbit.radius * Math.exp(limitedDelta * sceneZoom.wheelSensitivity),
    sceneZoom.minRadius,
    sceneZoom.maxRadius,
  );
  applySceneCameraOrbit({ ...orbit, radius: nextRadius }, true);
}

function hasSceneModel() {
  return Boolean(modelViewer.getAttribute("src") || modelViewer.src);
}

function applySceneCameraOrbit(orbit, jump = false) {
  const value = `${orbit.theta}deg ${orbit.phi}deg ${orbit.radius}m`;
  applyViewerCameraOrbit(modelViewer, value, jump);
}

function applyViewerCameraOrbit(viewer, value, jump = false) {
  if (!viewer) {
    return;
  }
  viewer.setAttribute("camera-orbit", value);
  try {
    viewer.cameraOrbit = value;
  } catch (error) {
    console.warn("Could not set cameraOrbit property", error);
  }
  if (jump && typeof viewer.jumpCameraToGoal === "function") {
    viewer.jumpCameraToGoal();
  }
}

function normalizeWheelDelta(event) {
  if (event.deltaMode === WheelEvent.DOM_DELTA_LINE) {
    return event.deltaY * 18;
  }
  if (event.deltaMode === WheelEvent.DOM_DELTA_PAGE) {
    return event.deltaY * 120;
  }
  return event.deltaY;
}

function getCurrentCameraOrbit() {
  if (typeof modelViewer.getCameraOrbit === "function") {
    const orbit = modelViewer.getCameraOrbit();
    return {
      theta: cameraAngleToDegrees(orbit.theta, 0),
      phi: cameraAngleToDegrees(orbit.phi, 78),
      radius: toNumber(orbit.radius, 12),
    };
  }
  return parseCameraOrbit(modelViewer.cameraOrbit || modelViewer.getAttribute("camera-orbit"));
}

function cameraAngleToDegrees(value, fallback) {
  if (typeof value === "number" && Number.isFinite(value)) {
    return radiansToDegrees(value);
  }
  if (value && typeof value === "object" && "value" in value) {
    const numeric = toNumber(value.value, Number.NaN);
    if (Number.isFinite(numeric)) {
      return String(value.unit || "").includes("deg") ? numeric : radiansToDegrees(numeric);
    }
  }
  return parseAngle(value, fallback);
}

function parseCameraOrbit(value) {
  const parts = String(value || "0deg 78deg 12m").trim().split(/\s+/);
  return {
    theta: parseAngle(parts[0], 0),
    phi: parseAngle(parts[1], 78),
    radius: parseDistance(parts[2], 12),
  };
}

function parseAngle(value, fallback) {
  const text = String(value || "");
  const parsed = Number.parseFloat(text);
  if (!Number.isFinite(parsed)) {
    return fallback;
  }
  return text.includes("rad") ? radiansToDegrees(parsed) : parsed;
}

function parseDistance(value, fallback) {
  const parsed = Number.parseFloat(String(value || ""));
  return Number.isFinite(parsed) ? parsed : fallback;
}

function toNumber(value, fallback) {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }
  if (value && typeof value === "object" && "value" in value) {
    const nested = Number.parseFloat(String(value.value));
    return Number.isFinite(nested) ? nested : fallback;
  }
  const parsed = Number.parseFloat(String(value || ""));
  return Number.isFinite(parsed) ? parsed : fallback;
}

function radiansToDegrees(value) {
  return value * 180 / Math.PI;
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

async function updateImagePreview(input) {
  const target = input.dataset.previewTarget;
  const file = input.files && input.files[0];

  if (!file) {
    return;
  }

  setPreviewFromFile(target, file);
  persistedFiles.set(target, file);
  await saveStoredFile(target, file);
}

function setPreviewFromFile(target, file) {
  const box = document.querySelector(`[data-preview-box="${target}"]`);
  const image = document.querySelector(`[data-preview-image="${target}"]`);
  const placeholder = document.querySelector(`[data-preview-box="${target}"] .drop-placeholder`);
  const fileName = document.querySelector(`[data-file-name="${target}"]`);

  if (!image || !placeholder) {
    return;
  }
  revokePreviewUrl(target);
  const url = URL.createObjectURL(file);
  previewUrls.set(target, url);
  if (fileName) {
    fileName.textContent = file.name || `${target}.png`;
  }
  image.src = url;
  image.classList.remove("hidden");
  placeholder.classList.add("hidden");
  if (box) {
    box.classList.add("has-preview");
  }
}

function revokePreviewUrl(target) {
  if (!previewUrls.has(target)) {
    return;
  }
  URL.revokeObjectURL(previewUrls.get(target));
  previewUrls.delete(target);
}

function appendPersistedFiles(data) {
  const mode = modeInput.value;
  fileInputs.forEach((input) => {
    const target = input.dataset.previewTarget;
    const isActive = mode === "single" ? target === "single" : target !== "single";
    data.delete(input.name);
    if (!isActive) {
      return;
    }

    const file = (input.files && input.files[0]) || persistedFiles.get(target);
    if (file) {
      data.append(input.name, file, file.name || `${target}.png`);
    }
  });
}

function openInputDb() {
  if (!("indexedDB" in window)) {
    return Promise.resolve(null);
  }
  if (inputDbPromise) {
    return inputDbPromise;
  }
  inputDbPromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(inputDbName, 1);
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(inputStoreName)) {
        db.createObjectStore(inputStoreName, { keyPath: "target" });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  return inputDbPromise;
}

async function saveStoredFile(target, file) {
  try {
    const db = await openInputDb();
    if (!db) {
      return;
    }
    await runInputStoreTransaction(db, "readwrite", (store) => {
      store.put({
        target,
        file,
        name: file.name,
        type: file.type,
        lastModified: file.lastModified,
        savedAt: Date.now(),
      });
    });
  } catch (error) {
    console.warn("Could not cache selected image", error);
  }
}

async function restoreStoredImages() {
  try {
    const db = await openInputDb();
    if (!db) {
      return;
    }
    await Promise.all(
      Array.from(fileInputs).map(async (input) => {
        const target = input.dataset.previewTarget;
        const record = await readStoredFile(db, target);
        if (!record || !record.file) {
          return;
        }
        const file = restoreFile(record, target);
        persistedFiles.set(target, file);
        setPreviewFromFile(target, file);
      }),
    );
  } catch (error) {
    console.warn("Could not restore selected images", error);
  }
}

function readStoredFile(db, target) {
  return new Promise((resolve, reject) => {
    const transaction = db.transaction(inputStoreName, "readonly");
    const store = transaction.objectStore(inputStoreName);
    const request = store.get(target);
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}

function runInputStoreTransaction(db, mode, write) {
  return new Promise((resolve, reject) => {
    const transaction = db.transaction(inputStoreName, mode);
    const store = transaction.objectStore(inputStoreName);
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error);
    transaction.onabort = () => reject(transaction.error);
    write(store);
  });
}

function restoreFile(record, target) {
  if (record.file instanceof File) {
    return record.file;
  }
  return new File([record.file], record.name || `${target}.png`, {
    type: record.type || record.file.type || "image/png",
    lastModified: record.lastModified || Date.now(),
  });
}

function updateAddTextureButton(job) {
  const textureAvailable = canAddTexture(job);
  textureButtonJobId = textureAvailable ? job.id : null;
  addTextureButton.disabled = !textureAvailable;
  addTextureButton.textContent = textureActionLabel(job);

  const rebakeBlockReason = rebakeTextureBlockReason(job);
  const rebakeAvailable = !rebakeBlockReason;
  rebakeTextureJobId = rebakeAvailable ? job.id : null;
  if (rebakeTextureButton) {
    rebakeTextureButton.disabled = !rebakeAvailable;
    rebakeTextureButton.title = rebakeAvailable
      ? "Re-apply the existing texture colors to the white mesh geometry."
      : rebakeBlockReason || "Available after a textured model exists.";
  }
}

function canAddTexture(job) {
  if (!job || !job.id || !terminalStatuses.has(job.status)) {
    return false;
  }
  if (job.status === "failed" || job.status === "needs_runtime") {
    return false;
  }
  return hasWhiteMesh(job);
}

function hasWhiteMesh(job) {
  return (job?.outputs || []).some((output) => output.format === "glb" && output.filename === "white_mesh.glb");
}

function hasTexturedMesh(job) {
  return (job?.outputs || []).some((output) => output.format === "glb" && output.filename.startsWith("textured_mesh"));
}

function canRebakeTexture(job) {
  return !rebakeTextureBlockReason(job);
}

function rebakeTextureBlockReason(job) {
  if (!job || !job.id) {
    return "Load a finished textured generation first.";
  }
  if (!terminalStatuses.has(job.status)) {
    return "Wait until the current texture operation finishes.";
  }
  if (job.status === "failed" || job.status === "needs_runtime") {
    return "The current job is not ready for texture re-bake.";
  }
  if (!hasWhiteMesh(job)) {
    return "white_mesh.glb is required for color re-bake.";
  }
  if (!hasTexturedMesh(job)) {
    return "A textured GLB is required before color re-bake.";
  }
  if (job.rebake_texture_ready === false) {
    const missing = Array.isArray(job.rebake_texture_missing) && job.rebake_texture_missing.length
      ? ` Missing: ${job.rebake_texture_missing.join(", ")}.`
      : "";
    return `Texture source files are required for color re-bake.${missing}`;
  }
  return "";
}

function textureActionLabel(job) {
  if (!job || !hasWhiteMesh(job)) {
    return "Add texture";
  }
  return hasTexturedMesh(job) || job.payload?.texture ? "Re-run texture" : "Add texture";
}

async function addTextureToCurrentJob() {
  if (!textureButtonJobId) {
    return;
  }
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }

  addTextureButton.disabled = true;
  const textureQuality = textureQualityInput.value || "fast";
  const objectType = objectTypeInput.value || "organic";
  const textureColor = currentTextureColor();
  setProgress("queued_texture", `Queuing ${textureQualityLabel(textureQuality)} texture pass, color ${formatTextureColor(textureColor)}...`);
  runBadge.textContent = "queued texture";

  try {
    const response = await fetch(
      `/api/jobs/${encodeURIComponent(textureButtonJobId)}/texture?texture_quality=${encodeURIComponent(textureQuality)}&object_type=${encodeURIComponent(objectType)}&color=${encodeURIComponent(textureColor.toFixed(2))}`,
      {
        method: "POST",
      },
    );
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || response.statusText);
    }
    localStorage.setItem("lgo.lastJobId", payload.id);
    renderJob(payload);
    void loadHistory();
    if (payload.id && !terminalStatuses.has(payload.status)) {
      pollJob(payload.id);
    }
  } catch (error) {
    setProgress("failed", "Texture pass could not be started.");
    runBadge.textContent = "Failed";
    jobStatus.textContent += `\n\nAdd texture failed: ${error}`;
    updateAddTextureButton(currentJob);
  }
}

async function rebakeTextureForCurrentJob() {
  if (!rebakeTextureJobId) {
    return;
  }
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }

  if (rebakeTextureButton) {
    rebakeTextureButton.disabled = true;
  }
  const textureQuality = textureQualityInput.value || "fast";
  const objectType = objectTypeInput.value || "organic";
  const albedo = currentRebakeAlbedo();
  const textureColor = currentTextureColor();
  setProgress(
    "queued_texture",
    `Queuing ${textureQualityLabel(textureQuality)} color re-bake, albedo ${formatAlbedo(albedo)}, color ${formatTextureColor(textureColor)}...`,
  );
  runBadge.textContent = "queued rebake";

  try {
    const response = await fetch(
      `/api/jobs/${encodeURIComponent(rebakeTextureJobId)}/rebake-texture?texture_quality=${encodeURIComponent(textureQuality)}&object_type=${encodeURIComponent(objectType)}&albedo=${encodeURIComponent(albedo.toFixed(2))}&color=${encodeURIComponent(textureColor.toFixed(2))}`,
      {
        method: "POST",
      },
    );
    const payload = await readApiPayload(response);
    if (!response.ok) {
      throw new Error(payload.error || response.statusText);
    }
    localStorage.setItem("lgo.lastJobId", payload.id);
    renderJob(payload, { preferredScene: "texture" });
    void loadHistory();
    if (payload.id && !terminalStatuses.has(payload.status)) {
      pollJob(payload.id);
    }
  } catch (error) {
    const message = errorMessage(error);
    setProgress("failed", `Texture color re-bake could not be started. ${message}`);
    runBadge.textContent = "Failed";
    jobStatus.textContent += `\n\nRe-bake color failed: ${message}`;
    updateAddTextureButton(currentJob);
  }
}

async function readApiPayload(response) {
  const text = await response.text();
  if (!text.trim()) {
    return {};
  }
  try {
    return JSON.parse(text);
  } catch (error) {
    return { error: text.trim() || errorMessage(error) };
  }
}

function errorMessage(error) {
  return error instanceof Error ? error.message : String(error);
}

async function shutdownService() {
  if (!window.confirm("Shutdown LGO service?")) {
    return;
  }
  setServiceControlsBusy(true);
  try {
    const payload = await postServiceCommand("shutdown");
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
    healthStrip.innerHTML = badge("Server shutting down", true, true);
    runBadge.textContent = "Stopping";
    setProgress("needs_runtime", payload.message || "LGO shutdown command sent.");
    appendRunMessage(payload.message || "LGO shutdown command sent.");
  } catch (error) {
    appendRunMessage(`Shutdown failed: ${error}`);
    setServiceControlsBusy(false);
  }
}

async function restartService() {
  if (!window.confirm("Restart LGO service?")) {
    return;
  }
  setServiceControlsBusy(true);
  try {
    const payload = await postServiceCommand("restart");
    if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
    }
    healthStrip.innerHTML = badge("Server restarting", true, true);
    runBadge.textContent = "Restarting";
    setProgress("queued", payload.message || "LGO restart command sent.");
    appendRunMessage(payload.message || "LGO restart command sent.");
    waitForServiceBack();
  } catch (error) {
    appendRunMessage(`Restart failed: ${error}`);
    setServiceControlsBusy(false);
  }
}

async function postServiceCommand(command) {
  const response = await fetch(`/api/${command}`, {
    method: "POST",
    cache: "no-store",
  });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : {};
  if (!response.ok || payload.ok === false) {
    throw new Error(payload.error || payload.message || response.statusText);
  }
  return payload;
}

function waitForServiceBack(attempt = 0) {
  const maxAttempts = 24;
  window.setTimeout(async () => {
    try {
      const response = await fetch("/api/health", { cache: "no-store" });
      if (!response.ok) {
        throw new Error(response.statusText);
      }
      setServiceControlsBusy(false);
      runBadge.textContent = "Online";
      setProgress("created", "LGO service restarted.");
      await loadHealth();
      await loadHistory();
      restoreLastJob();
    } catch (error) {
      if (attempt + 1 >= maxAttempts) {
        setServiceControlsBusy(false);
        healthStrip.innerHTML = badge("Restart not confirmed", false);
        setProgress("failed", "Restart was not confirmed. Start LGO with start-lgo-background.bat.");
        appendRunMessage(`Restart check failed: ${error}`);
        return;
      }
      waitForServiceBack(attempt + 1);
    }
  }, attempt === 0 ? 2500 : 1500);
}

function setServiceControlsBusy(busy) {
  [refreshHealth, restartServiceButton, shutdownServiceButton].forEach((button) => {
    if (button) {
      button.disabled = busy;
    }
  });
}

function appendRunMessage(message) {
  const prefix = jobStatus.textContent && jobStatus.textContent !== "No job yet." ? `${jobStatus.textContent}\n\n` : "";
  jobStatus.textContent = `${prefix}${message}`;
  jobStatus.scrollTop = jobStatus.scrollHeight;
}

function outputCacheKey(job, output) {
  return output?.cache_key || output?.modified_at || job?.updated_at || job?.id || "";
}

function outputUrl(jobId, filename, cacheKey = "") {
  const base = `/api/jobs/${encodeURIComponent(jobId)}/outputs/${encodeURIComponent(filename)}`;
  return cacheKey ? `${base}?v=${encodeURIComponent(cacheKey)}` : base;
}

function readableStatus(status) {
  return (status || "idle").replaceAll("_", " ");
}

function textureQualityLabel(value) {
  const selected = typeof value === "string" ? value : value?.selected;
  const labels = {
    fast: "Fast",
    balanced: "Balanced",
    high: "High",
  };
  return labels[selected] || labels.fast;
}

function objectTypeLabel(value) {
  const selected = typeof value === "string" ? value : value?.selected;
  const labels = {
    organic: "Organic",
    hard_surface: "Hard surface",
    rock: "Rock / stone",
  };
  return labels[selected] || labels.organic;
}

function restoreLastJob() {
  const query = new URLSearchParams(window.location.search);
  const lastJobId = query.get("job") || localStorage.getItem("lgo.lastJobId");
  if (lastJobId) {
    localStorage.setItem("lgo.lastJobId", lastJobId);
    pollJob(lastJobId);
  }
}

function restoreFormState() {
  const storedMode = localStorage.getItem("lgo.mode");
  if (storedMode === "single" || storedMode === "multiview") {
    setMode(storedMode);
  }

  const storedQuality = localStorage.getItem("lgo.quality");
  if (storedQuality === "fast" || storedQuality === "balanced" || storedQuality === "high") {
    setQuality(storedQuality);
  }

  const storedObjectType = localStorage.getItem("lgo.objectType");
  if (objectTypeValues.includes(storedObjectType)) {
    setObjectType(storedObjectType);
  } else {
    setObjectType(objectTypeInput.value || "organic");
  }

  const storedTexture = localStorage.getItem("lgo.texture");
  if (storedTexture === "true" || storedTexture === "false") {
    setTexture(storedTexture);
  }

  const storedTextureQuality = localStorage.getItem("lgo.textureQuality");
  if (storedTextureQuality === "fast" || storedTextureQuality === "balanced" || storedTextureQuality === "high") {
    setTextureQuality(storedTextureQuality);
  } else {
    setTextureQuality(textureQualityInput.value || "fast");
  }

  setRebakeAlbedo(localStorage.getItem("lgo.rebakeAlbedo") || rebakeAlbedoRange.defaultValue);
  setTextureColor(localStorage.getItem("lgo.textureColor") || textureColorRange.defaultValue);
}

restoreFormState();
void restoreStoredImages();
setupSmoothSceneZoom();
loadHealth();
setHistoryFilter(historyFilter);
void loadHistory();
restoreLastJob();
