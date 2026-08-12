const API_BASE = new URL(".", window.location.href).pathname;

let currentTopic = null;
let currentPayload = null;

// Expansion is tracked out here, keyed by the node's full path, so the 5s tree
// refresh re-renders without collapsing whatever the user opened. Everything
// starts collapsed because nothing is in the set.
const expandedPaths = new Set();
let treeFilter = "";

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
  const res = await fetch(`${API_BASE}api/tree`);
  const tree = await res.json();
  renderTree(tree);
}

let lastTree = null;

function renderTree(tree) {
  lastTree = tree;
  const container = $("#topic-tree");
  container.innerHTML = "";

  const children = tree.children || {};
  const keys = Object.keys(children).sort();
  const rendered = keys
    .map((key) => renderTreeNode(children[key], key, key))
    .filter(Boolean);

  if (rendered.length === 0) {
    const empty = document.createElement("div");
    empty.className = "tree-empty";
    empty.textContent = treeFilter
      ? "Sin coincidencias"
      : "Sin topics todavía. Conectate al broker.";
    container.appendChild(empty);
    return;
  }

  rendered.forEach((el) => container.appendChild(el));
}

// Returns null when the node and none of its descendants match the active
// filter, which is what prunes non-matching branches out of the render.
function renderTreeNode(node, label, path) {
  if (!matchesFilter(node, label, path)) return null;

  const wrapper = document.createElement("div");
  wrapper.className = "tree-node";

  const hasChildren = Boolean(node.children && Object.keys(node.children).length);
  // While filtering, auto-open so matches deep in the tree are visible without
  // the user having to expand every ancestor by hand.
  const isExpanded = treeFilter ? true : expandedPaths.has(path);

  const row = document.createElement("div");
  row.className = "tree-row";
  if (node.__topic__) row.classList.add("has-topic");

  const chevron = document.createElement("span");
  chevron.className = "tree-chevron";
  if (hasChildren) {
    chevron.textContent = isExpanded ? "⌄" : "›";
    chevron.classList.add("clickable");
    chevron.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleExpanded(path);
    });
  }
  row.appendChild(chevron);

  const name = document.createElement("span");
  name.className = "tree-name";
  if (node.__topic__) name.classList.add("leaf");
  name.textContent = label;
  row.appendChild(name);

  if (hasChildren) {
    const childBadge = document.createElement("span");
    childBadge.className = "tree-badge children";
    childBadge.textContent = `↳${node.__child_count__ || 0}`;
    row.appendChild(childBadge);
  }

  if (node.__message_total__) {
    const msgBadge = document.createElement("span");
    msgBadge.className = "tree-badge messages";
    msgBadge.textContent = `✉${node.__message_total__}`;
    row.appendChild(msgBadge);
  }

  if (node.__topic__ && node.__preview__) {
    const preview = document.createElement("span");
    preview.className = "tree-preview";
    preview.textContent = node.__preview__;
    row.appendChild(preview);
  }

  // Clicking the row opens the payload for real topics; for pure branch nodes
  // it just expands, which is the least surprising behavior.
  row.addEventListener("click", () => {
    if (node.__topic__) {
      selectTopic(node.__topic__);
      highlightRow(row);
    } else if (hasChildren) {
      toggleExpanded(path);
    }
  });

  wrapper.appendChild(row);

  if (hasChildren && isExpanded) {
    const childContainer = document.createElement("div");
    childContainer.className = "tree-children";
    Object.keys(node.children)
      .sort()
      .forEach((key) => {
        const child = renderTreeNode(node.children[key], key, `${path}/${key}`);
        if (child) childContainer.appendChild(child);
      });
    wrapper.appendChild(childContainer);
  }

  return wrapper;
}

function matchesFilter(node, label, path) {
  if (!treeFilter) return true;
  const needle = treeFilter.toLowerCase();
  if (label.toLowerCase().includes(needle)) return true;
  if (path.toLowerCase().includes(needle)) return true;
  if (node.__preview__ && node.__preview__.toLowerCase().includes(needle)) return true;
  if (node.children) {
    return Object.keys(node.children).some((key) =>
      matchesFilter(node.children[key], key, `${path}/${key}`)
    );
  }
  return false;
}

function toggleExpanded(path) {
  if (expandedPaths.has(path)) {
    expandedPaths.delete(path);
  } else {
    expandedPaths.add(path);
  }
  if (lastTree) renderTree(lastTree);
}

function highlightRow(row) {
  document.querySelectorAll(".tree-row.selected").forEach((el) => el.classList.remove("selected"));
  row.classList.add("selected");
}

async function selectTopic(topic) {
  currentTopic = topic;
  const res = await fetch(`${API_BASE}api/topics/${encodeURIComponent(topic)}`);
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
  const url = editingId ? `${API_BASE}api/mappings/${editingId}` : `${API_BASE}api/mappings`;
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
  const res = await fetch(`${API_BASE}api/mappings`);
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
      await fetch(`${API_BASE}api/mappings/${btn.dataset.id}`, { method: "DELETE" });
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

const STATUS_LABELS = {
  connected: "conectado",
  disconnected: "desconectado",
  error: "error",
};

async function loadStatus() {
  const res = await fetch(`${API_BASE}api/status`);
  const data = await res.json();
  applyStatus(data.status, data.last_error);
}

function applyStatus(status, lastError) {
  const pill = $("#conn-status");
  pill.textContent = STATUS_LABELS[status] || status;
  pill.classList.remove("connected", "disconnected", "error");
  pill.classList.add(status === "connected" ? "connected" : status === "error" ? "error" : "disconnected");

  const connected = status === "connected";
  const connectBtn = $("#btn-connect");
  connectBtn.textContent = connected ? "Conectado" : "Conectar";
  connectBtn.classList.toggle("connected", connected);
  connectBtn.disabled = connected;
  $("#btn-disconnect").disabled = !connected;

  $("#conn-error").textContent = lastError || "";
}

async function submitConnection(event) {
  event.preventDefault();
  const body = {
    host: $("#conn-host").value.trim(),
    port: parseInt($("#conn-port").value, 10),
    username: $("#conn-username").value.trim() || null,
    password: $("#conn-password").value || null,
  };
  const res = await fetch(`${API_BASE}api/connect`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (!res.ok) {
    applyStatus("error", `HTTP ${res.status} al conectar`);
    return;
  }

  // connect_async returns before the broker handshake finishes, so poll a few
  // times to pick up the transition to "connected" without waiting for the
  // regular 5s refresh.
  await loadStatus();
  pollStatusBriefly();
}

function pollStatusBriefly(attempts = 6) {
  if (attempts <= 0) return;
  setTimeout(async () => {
    await loadStatus();
    if ($("#conn-status").textContent !== STATUS_LABELS.connected) {
      pollStatusBriefly(attempts - 1);
    }
  }, 500);
}

function init() {
  initTabs();
  loadTree();
  loadStatus();
  setInterval(loadTree, 5000);
  setInterval(loadStatus, 5000);

  $("#connection-form").addEventListener("submit", submitConnection);
  $("#btn-disconnect").addEventListener("click", async () => {
    await fetch(`${API_BASE}api/disconnect`, { method: "POST" });
    loadStatus();
  });

  $("#tree-search").addEventListener("input", (event) => {
    treeFilter = event.target.value.trim();
    if (lastTree) renderTree(lastTree);
  });

  $("#tree-collapse-all").addEventListener("click", () => {
    expandedPaths.clear();
    if (lastTree) renderTree(lastTree);
  });
  $("#mapping-form").addEventListener("submit", submitMapping);
  $("#mapping-cancel").addEventListener("click", closeMappingModal);
  $("#mapping-domain").addEventListener("change", updateDomainConfigVisibility);
}

document.addEventListener("DOMContentLoaded", init);
