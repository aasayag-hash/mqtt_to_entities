const API_BASE = "";

let currentTopic = null;
let currentPayload = null;

function $(selector) {
  return document.querySelector(selector);
}

function initTabs() {
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
      document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
      btn.classList.add("active");
      $(`#tab-${btn.dataset.tab}`).classList.add("active");
      if (btn.dataset.tab === "entidades") loadMappings();
      if (btn.dataset.tab === "conexion") loadStatus();
    });
  });
}

async function loadTree() {
  const res = await fetch(`${API_BASE}/api/tree`);
  const tree = await res.json();
  const container = $("#topic-tree");
  container.innerHTML = "";
  container.appendChild(renderTreeNode(tree, ""));
}

function renderTreeNode(node, label) {
  const wrapper = document.createElement("div");
  wrapper.className = "tree-node";

  if (label) {
    const el = document.createElement("div");
    const isLeaf = node.__topic__ && !node.children;
    el.className = "tree-label" + (isLeaf ? " leaf" : "");
    el.textContent = label;
    if (node.__topic__) {
      el.addEventListener("click", () => selectTopic(node.__topic__));
    }
    wrapper.appendChild(el);
  }

  if (node.children) {
    Object.keys(node.children)
      .sort()
      .forEach((key) => {
        wrapper.appendChild(renderTreeNode(node.children[key], key));
      });
  }

  return wrapper;
}

async function selectTopic(topic) {
  currentTopic = topic;
  const res = await fetch(`${API_BASE}/api/topics/${encodeURIComponent(topic)}`);
  if (!res.ok) return;
  const data = await res.json();
  currentPayload = data.payload;
  $("#topic-title").textContent = topic;
  renderPayload(data.payload, data.field_paths);
}

function renderPayload(payload, fieldPaths) {
  const container = $("#topic-json");
  container.innerHTML = "";

  if (typeof payload !== "object" || payload === null) {
    const span = document.createElement("span");
    span.className = "field-value";
    span.textContent = JSON.stringify(payload);
    span.addEventListener("click", () => openMappingModal("", fieldPaths));
    container.appendChild(span);
    return;
  }

  const pathSet = new Set(fieldPaths || []);
  const pre = document.createElement("pre");
  pre.textContent = "";
  container.appendChild(pre);

  const list = document.createElement("div");
  (fieldPaths || []).forEach((path) => {
    const row = document.createElement("div");
    const link = document.createElement("span");
    link.className = "field-value";
    link.textContent = path;
    link.addEventListener("click", () => openMappingModal(path));
    row.appendChild(link);
    list.appendChild(row);
  });
  container.appendChild(document.createTextNode(JSON.stringify(payload, null, 2)));
  container.appendChild(document.createElement("hr"));
  container.appendChild(list);
  void pathSet;
}

let modalOptions = [];

function openMappingModal(fieldPath) {
  $("#mapping-topic").textContent = currentTopic;
  const select = $("#mapping-field-path");
  select.innerHTML = "";
  const opt = document.createElement("option");
  opt.value = fieldPath;
  opt.textContent = fieldPath || "(valor raíz)";
  select.appendChild(opt);
  select.value = fieldPath;
  $("#mapping-entity-id").value = "";
  $("#mapping-domain").value = "sensor";
  updateDomainConfigVisibility();
  $("#mapping-modal").classList.remove("hidden");
}

function closeMappingModal() {
  $("#mapping-modal").classList.add("hidden");
}

function updateDomainConfigVisibility() {
  const domain = $("#mapping-domain").value;
  $("#domain-config-sensor").classList.toggle("hidden", domain !== "sensor");
  $("#domain-config-onoff").classList.toggle("hidden", !["binary_sensor", "switch"].includes(domain));
  $("#domain-config-number").classList.toggle("hidden", domain !== "number");
  $("#domain-config-select").classList.toggle("hidden", domain !== "select");
}

function buildDomainConfig(domain) {
  if (domain === "sensor") {
    const unit = $("#cfg-unit").value.trim();
    return unit ? { unit_of_measurement: unit } : {};
  }
  if (domain === "binary_sensor" || domain === "switch") {
    return {
      on_values: splitCsv($("#cfg-on-values").value),
      off_values: splitCsv($("#cfg-off-values").value),
    };
  }
  if (domain === "number") {
    const cfg = {};
    if ($("#cfg-min").value !== "") cfg.min = parseFloat($("#cfg-min").value);
    if ($("#cfg-max").value !== "") cfg.max = parseFloat($("#cfg-max").value);
    if ($("#cfg-step").value !== "") cfg.step = parseFloat($("#cfg-step").value);
    return cfg;
  }
  if (domain === "select") {
    return { options: splitCsv($("#cfg-options").value) };
  }
  return {};
}

function splitCsv(value) {
  return value
    .split(",")
    .map((v) => v.trim())
    .filter((v) => v.length > 0);
}

async function submitMapping(event) {
  event.preventDefault();
  const domain = $("#mapping-domain").value;
  const body = {
    topic: currentTopic,
    field_path: $("#mapping-field-path").value,
    entity_id: $("#mapping-entity-id").value.trim(),
    domain,
    domain_config: buildDomainConfig(domain),
  };
  const editingId = $("#mapping-form").dataset.editingId;
  const url = editingId ? `${API_BASE}/api/mappings/${editingId}` : `${API_BASE}/api/mappings`;
  const method = editingId ? "PUT" : "POST";
  await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  delete $("#mapping-form").dataset.editingId;
  closeMappingModal();
  loadMappings();
}

async function loadMappings() {
  const res = await fetch(`${API_BASE}/api/mappings`);
  const mappings = await res.json();
  const tbody = document.querySelector("#mappings-table tbody");
  tbody.innerHTML = "";
  mappings.forEach((mapping) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(mapping.topic)}</td>
      <td>${escapeHtml(mapping.field_path)}</td>
      <td>${escapeHtml(mapping.entity_id)}</td>
      <td>${escapeHtml(mapping.domain)}</td>
      <td>${escapeHtml(String(mapping.last_value))}</td>
      <td>
        <button class="edit-btn" data-id="${mapping.id}">Editar</button>
        <button class="delete-btn" data-id="${mapping.id}">Borrar</button>
      </td>
    `;
    tbody.appendChild(tr);
  });

  tbody.querySelectorAll(".delete-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      await fetch(`${API_BASE}/api/mappings/${btn.dataset.id}`, { method: "DELETE" });
      loadMappings();
    });
  });

  tbody.querySelectorAll(".edit-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const mapping = mappings.find((m) => m.id === btn.dataset.id);
      if (!mapping) return;
      currentTopic = mapping.topic;
      openMappingModal(mapping.field_path);
      $("#mapping-entity-id").value = mapping.entity_id;
      $("#mapping-domain").value = mapping.domain;
      updateDomainConfigVisibility();
      fillDomainConfig(mapping.domain, mapping.domain_config || {});
      $("#mapping-form").dataset.editingId = mapping.id;
    });
  });
}

function fillDomainConfig(domain, config) {
  if (domain === "sensor") {
    $("#cfg-unit").value = config.unit_of_measurement || "";
  } else if (domain === "binary_sensor" || domain === "switch") {
    $("#cfg-on-values").value = (config.on_values || []).join(", ");
    $("#cfg-off-values").value = (config.off_values || []).join(", ");
  } else if (domain === "number") {
    $("#cfg-min").value = config.min ?? "";
    $("#cfg-max").value = config.max ?? "";
    $("#cfg-step").value = config.step ?? "";
  } else if (domain === "select") {
    $("#cfg-options").value = (config.options || []).join(", ");
  }
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value;
  return div.innerHTML;
}

async function loadStatus() {
  const res = await fetch(`${API_BASE}/api/status`);
  const data = await res.json();
  $("#conn-status").textContent = data.status;
  $("#conn-error").textContent = data.last_error || "";
}

async function submitConnection(event) {
  event.preventDefault();
  const body = {
    host: $("#conn-host").value.trim(),
    port: parseInt($("#conn-port").value, 10),
    username: $("#conn-username").value.trim() || null,
    password: $("#conn-password").value || null,
  };
  await fetch(`${API_BASE}/api/connect`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  loadStatus();
}

function init() {
  initTabs();
  loadTree();
  loadStatus();
  setInterval(loadTree, 5000);
  setInterval(loadStatus, 5000);

  $("#connection-form").addEventListener("submit", submitConnection);
  $("#btn-reconnect").addEventListener("click", async () => {
    await fetch(`${API_BASE}/api/reconnect`, { method: "POST" });
    loadStatus();
  });
  $("#mapping-form").addEventListener("submit", submitMapping);
  $("#mapping-cancel").addEventListener("click", closeMappingModal);
  $("#mapping-domain").addEventListener("change", updateDomainConfigVisibility);
}

document.addEventListener("DOMContentLoaded", init);
