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
      if (btn.dataset.tab === "conexion") loadBrokers();
    });
  });
}

function brokerQuery(prefix = "?") {
  return selectedBrokerId ? `${prefix}broker_id=${encodeURIComponent(selectedBrokerId)}` : "";
}

// Sequence guards: a slow response must never paint over a newer one. Without
// these, switching brokers could render the previous broker's tree, and two
// quick topic clicks could leave the panel and currentTopic disagreeing.
let treeRequestSeq = 0;
let topicRequestSeq = 0;

async function loadTree() {
  const seq = ++treeRequestSeq;
  const requestedBroker = selectedBrokerId;
  const res = await fetch(`${API_BASE}api/tree${brokerQuery()}`);
  const tree = await res.json();
  // Stale response, or the broker changed while it was in flight.
  if (seq !== treeRequestSeq || requestedBroker !== selectedBrokerId) return;
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
  const seq = ++topicRequestSeq;
  const requestedBroker = selectedBrokerId;

  const res = await fetch(`${API_BASE}api/topics/${encodeURIComponent(topic)}${brokerQuery()}`);

  // A newer click (or a broker switch) already won; drop this response so the
  // panel and currentTopic can't end up describing different topics.
  if (seq !== topicRequestSeq || requestedBroker !== selectedBrokerId) return;

  if (!res.ok) {
    currentTopic = null;
    currentPayload = null;
    $("#topic-title").textContent = topic;
    $("#topic-json").innerHTML =
      '<div class="payload-error">Sin datos para este topic todavía.</div>';
    return;
  }

  const data = await res.json();
  if (seq !== topicRequestSeq || requestedBroker !== selectedBrokerId) return;

  // Assigned only once the response is known to be current.
  currentTopic = topic;
  currentPayload = data.payload;
  $("#topic-title").textContent = topic;
  renderPayload(data.payload, data.field_paths);
}

function renderPayload(payload, fieldPaths) {
  const container = $("#topic-json");
  container.innerHTML = "";

  // Scalar payload: the whole value is the only thing mappable.
  if (typeof payload !== "object" || payload === null) {
    container.appendChild(buildFieldRow("", payload, "(valor completo)"));
    return;
  }

  const raw = document.createElement("pre");
  raw.className = "payload-raw";
  raw.textContent = JSON.stringify(payload, null, 2);
  container.appendChild(raw);

  const heading = document.createElement("div");
  heading.className = "fields-heading";
  heading.textContent = "Campos disponibles";
  container.appendChild(heading);

  const list = document.createElement("div");
  list.className = "fields-list";
  (fieldPaths || []).forEach((path) => {
    list.appendChild(buildFieldRow(path, resolveLocalPath(payload, path), path));
  });
  container.appendChild(list);
}

// Mirrors backend/json_paths.resolve_path so each field can show its current
// value, including the "[field=value]" array syntax the backend emits.
function resolveLocalPath(payload, path) {
  if (!path) return payload;

  let current = payload;
  for (const segment of splitLocalPath(path)) {
    if (current === null || current === undefined) return undefined;

    const match = /^([^.[\]]*)(?:\[([^=\]]+)\])?(?:\[([^=\]]+)=([^\]]+)\])?$/.exec(segment);
    if (!match) return undefined;
    const [, key, index, field, fieldMatch] = match;

    if (key) {
      if (typeof current !== "object" || !(key in current)) return undefined;
      current = current[key];
    }
    if (index !== undefined) {
      if (!Array.isArray(current)) return undefined;
      current = current[parseInt(index, 10)];
    }
    if (field !== undefined) {
      if (!Array.isArray(current)) return undefined;
      current = current.find((item) => item && String(item[field]) === fieldMatch);
    }
  }
  return current;
}

function splitLocalPath(path) {
  const tokens = [];
  let buf = "";
  let depth = 0;
  for (const ch of path) {
    if (ch === "." && depth === 0) {
      tokens.push(buf);
      buf = "";
      continue;
    }
    if (ch === "[") depth += 1;
    else if (ch === "]") depth -= 1;
    buf += ch;
  }
  if (buf) tokens.push(buf);
  return tokens;
}

function buildFieldRow(path, value, label) {
  const row = document.createElement("div");
  row.className = "field-row";

  const name = document.createElement("span");
  name.className = "field-path";
  name.textContent = label;
  row.appendChild(name);

  const val = document.createElement("span");
  val.className = "field-current-value";
  val.textContent = value === undefined ? "" : JSON.stringify(value);
  row.appendChild(val);

  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "create-entity-btn";
  btn.textContent = "+ Crear entidad";
  btn.addEventListener("click", () => openMappingModal(path));
  row.appendChild(btn);

  return row;
}

let modalOptions = [];

// Home Assistant's standard units, grouped the way its device classes are.
// "Otra…" keeps the field open for units HA has no constant for (kVArh and
// friends show up on Victron/vebus brokers).
const UNIT_GROUPS = [
  ["Temperatura", ["°C", "°F", "K"]],
  ["Potencia", ["W", "kW", "MW", "VA", "kVA", "var", "kvar"]],
  ["Energía", ["Wh", "kWh", "MWh", "GJ", "cal", "kcal"]],
  ["Corriente / Tensión", ["A", "mA", "V", "mV", "kV"]],
  ["Frecuencia", ["Hz", "kHz", "MHz", "GHz", "rpm"]],
  ["Porcentaje", ["%"]],
  ["Presión", ["Pa", "hPa", "kPa", "bar", "mbar", "cbar", "mmHg", "inHg", "psi"]],
  ["Caudal", ["m³/h", "L/min", "ft³/min", "gal/min"]],
  ["Volumen", ["L", "mL", "m³", "ft³", "gal", "fl. oz."]],
  ["Masa", ["g", "kg", "mg", "µg", "oz", "lb"]],
  ["Distancia", ["mm", "cm", "m", "km", "in", "ft", "yd", "mi"]],
  ["Velocidad", ["m/s", "km/h", "mph", "kn", "ft/s", "in/d", "mm/d"]],
  ["Datos", ["bit", "B", "kB", "MB", "GB", "TB", "PB", "KiB", "MiB", "GiB", "TiB"]],
  ["Velocidad de datos", ["bit/s", "kbit/s", "Mbit/s", "B/s", "kB/s", "MB/s", "GB/s"]],
  // "m" (month) collides with "m" (meter) under Distancia, so it carries an
  // explicit label; the value sent to HA is still plain "m". Re-opening such a
  // mapping highlights the Distancia entry, which is harmless since both send
  // the same string -- HA itself overloads "m" the same way.
  ["Tiempo", ["ms", "s", "min", "h", "d", "w", { value: "m", label: "m (meses)" }, "y"]],
  ["Luz / Radiación", ["lx", "lm", "W/m²", "UV index", "Bq/m³", "µSv/h"]],
  ["Calidad de aire", ["µg/m³", "mg/m³", "ppm", "ppb", "p/m³"]],
  ["Señal", ["dB", "dBm", "dBA"]],
  ["Otros", ["pH", "S/cm", "µS/cm", "Ω", "kΩ", "MΩ", "F", "µF", "nF", "pF"]],
];

const UNIT_CUSTOM_VALUE = "__custom__";

const PRECISION_OPTIONS = [
  { value: "", label: "Sin redondeo (valor original)" },
  { value: "0", label: "0 decimales (entero)" },
  { value: "1", label: "1 decimal" },
  { value: "2", label: "2 decimales" },
  { value: "3", label: "3 decimales" },
  { value: "4", label: "4 decimales" },
];

const PRECISION_SAMPLE = 57.560001373291016;

// Kept in sync with DEFAULT_STALE_TIMEOUT_SECONDS in the backend.
const DEFAULT_STALE_TIMEOUT = 300;

const STALE_TIMEOUT_OPTIONS = [
  { value: "0", label: "Nunca (no marcar desconocido)" },
  { value: "60", label: "1 minuto" },
  { value: "300", label: "5 minutos (por defecto)" },
  { value: "900", label: "15 minutos" },
  { value: "1800", label: "30 minutos" },
  { value: "3600", label: "1 hora" },
  { value: "86400", label: "1 día" },
];

function populateStaleTimeoutSelect() {
  const select = $("#cfg-stale-timeout");
  select.innerHTML = "";
  STALE_TIMEOUT_OPTIONS.forEach((opt) => {
    const el = document.createElement("option");
    el.value = opt.value;
    el.textContent = opt.label;
    select.appendChild(el);
  });
  select.value = String(DEFAULT_STALE_TIMEOUT);
}

function getSelectedStaleTimeout() {
  return parseInt($("#cfg-stale-timeout").value, 10);
}

function setSelectedStaleTimeout(seconds) {
  const select = $("#cfg-stale-timeout");
  const value = seconds === null || seconds === undefined ? DEFAULT_STALE_TIMEOUT : seconds;
  const known = Array.from(select.options).some((o) => o.value === String(value));
  // An unlisted value (set by hand via the API) is preserved as its own option.
  if (!known) {
    const el = document.createElement("option");
    el.value = String(value);
    el.textContent = `${value} segundos`;
    select.appendChild(el);
  }
  select.value = String(value);
}

function populatePrecisionSelect() {
  const select = $("#cfg-precision");
  select.innerHTML = "";
  PRECISION_OPTIONS.forEach((opt) => {
    const el = document.createElement("option");
    el.value = opt.value;
    el.textContent = opt.label;
    select.appendChild(el);
  });
}

// Mirrors backend _apply_precision so the hint shows the real result.
function updatePrecisionExample() {
  const raw = $("#cfg-precision").value;
  const target = $("#precision-example");
  if (!target) return;

  if (raw === "") {
    target.textContent = String(PRECISION_SAMPLE);
    return;
  }
  const digits = parseInt(raw, 10);
  const rounded = Number(PRECISION_SAMPLE.toFixed(digits));
  target.textContent = digits === 0 ? String(Math.round(PRECISION_SAMPLE)) : String(rounded);
}

function getSelectedPrecision() {
  const raw = $("#cfg-precision").value;
  return raw === "" ? null : parseInt(raw, 10);
}

function setSelectedPrecision(precision) {
  $("#cfg-precision").value =
    precision === null || precision === undefined ? "" : String(precision);
  updatePrecisionExample();
}

function populateUnitSelect() {
  const select = $("#cfg-unit");
  select.innerHTML = "";

  const none = document.createElement("option");
  none.value = "";
  none.textContent = "(sin unidad)";
  select.appendChild(none);

  UNIT_GROUPS.forEach(([groupLabel, units]) => {
    const group = document.createElement("optgroup");
    group.label = groupLabel;
    units.forEach((unit) => {
      const opt = document.createElement("option");
      opt.value = typeof unit === "string" ? unit : unit.value;
      opt.textContent = typeof unit === "string" ? unit : unit.label;
      group.appendChild(opt);
    });
    select.appendChild(group);
  });

  const custom = document.createElement("option");
  custom.value = UNIT_CUSTOM_VALUE;
  custom.textContent = "Otra…";
  select.appendChild(custom);
}

function updateUnitCustomVisibility() {
  const isCustom = $("#cfg-unit").value === UNIT_CUSTOM_VALUE;
  $("#cfg-unit-custom-label").classList.toggle("hidden", !isCustom);
  if (!isCustom) $("#cfg-unit-custom").value = "";
}

function getSelectedUnit() {
  const value = $("#cfg-unit").value;
  if (value === UNIT_CUSTOM_VALUE) return $("#cfg-unit-custom").value.trim();
  return value;
}

// Selects a unit that may not be one of the presets: falls back to the
// "Otra…" branch so editing an existing mapping never loses its unit.
function setSelectedUnit(unit) {
  const select = $("#cfg-unit");
  const value = unit || "";
  const known = Array.from(select.options).some((opt) => opt.value === value);

  if (value && !known) {
    select.value = UNIT_CUSTOM_VALUE;
    updateUnitCustomVisibility();
    $("#cfg-unit-custom").value = value;
    return;
  }

  select.value = value;
  updateUnitCustomVisibility();
}

function openMappingModal(fieldPath) {
  // Guards against creating a mapping with no topic, which the API would reject
  // anyway after the user filled in the whole form.
  if (!currentTopic) {
    window.alert("Elegí un topic del árbol antes de crear una entidad.");
    return;
  }

  // Creating, not editing: clear any leftover edit target, or saving would PUT
  // over the mapping that was opened earlier instead of creating a new one.
  delete $("#mapping-form").dataset.editingId;
  $("#mapping-modal-title").textContent = "Nuevo mapeo";

  $("#mapping-topic").textContent = currentTopic;
  const select = $("#mapping-field-path");
  select.innerHTML = "";
  const opt = document.createElement("option");
  opt.value = fieldPath;
  opt.textContent = fieldPath || "(valor raíz)";
  select.appendChild(opt);
  select.value = fieldPath;
  $("#mapping-domain").value = "sensor";
  $("#mapping-entity-id").value = suggestEntityId(currentTopic, fieldPath, "sensor");
  resetDomainConfigFields();
  updateDomainConfigVisibility();
  showMappingError("");
  $("#mapping-modal").classList.remove("hidden");
}

// Every per-domain field, so a new mapping never inherits the previous one's
// on/off values, min/max, options or custom unit.
function resetDomainConfigFields() {
  setSelectedUnit("");
  setSelectedPrecision(null);
  setSelectedStaleTimeout(null);
  $("#cfg-on-values").value = "";
  $("#cfg-off-values").value = "";
  $("#cfg-min").value = "";
  $("#cfg-max").value = "";
  $("#cfg-step").value = "";
  $("#cfg-options").value = "";
}

function closeMappingModal() {
  $("#mapping-modal").classList.add("hidden");
  showMappingError("");
  // Cancelling an edit must not leave the form pointing at that mapping.
  delete $("#mapping-form").dataset.editingId;
}

function showMappingError(message) {
  const el = $("#mapping-error");
  el.textContent = message || "";
  el.classList.toggle("hidden", !message);
}

// Builds a valid "<domain>.<object_id>" from the topic tail plus field path.
// HA rejects spaces, capitals and missing domains, which is the most common
// way a hand-typed entity ID silently fails to receive any value.
function suggestEntityId(topic, fieldPath, domain) {
  if (!topic) return "";

  const segments = topic.split("/").filter(Boolean);
  // Skip a trailing generic "value"-ish field so the name comes from the topic.
  const meaningfulField = fieldPath && !/^value$/i.test(fieldPath) ? fieldPath : "";
  const parts = segments.slice(-3).concat(meaningfulField ? [meaningfulField] : []);

  const objectId = parts
    .join("_")
    .normalize("NFD")
    .replace(/[̀-ͯ]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .replace(/_{2,}/g, "_");

  return objectId ? `${domain}.${objectId}` : "";
}

function updateDomainConfigVisibility() {
  const domain = $("#mapping-domain").value;
  $("#domain-config-sensor").classList.toggle("hidden", domain !== "sensor");
  $("#domain-config-onoff").classList.toggle("hidden", !["binary_sensor", "switch"].includes(domain));
  $("#domain-config-number").classList.toggle("hidden", domain !== "number");
  $("#domain-config-select").classList.toggle("hidden", domain !== "select");
  // Rounding only makes sense for the numeric domains.
  $("#domain-config-precision").classList.toggle(
    "hidden",
    !["sensor", "number"].includes(domain)
  );
}

function buildDomainConfig(domain) {
  // stale_timeout applies to every domain, so it is merged in once at the end.
  return { ...buildDomainSpecificConfig(domain), stale_timeout: getSelectedStaleTimeout() };
}

function buildDomainSpecificConfig(domain) {
  const precision = getSelectedPrecision();

  if (domain === "sensor") {
    const cfg = {};
    const unit = getSelectedUnit();
    if (unit) cfg.unit_of_measurement = unit;
    if (precision !== null) cfg.precision = precision;
    return cfg;
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
    if (precision !== null) cfg.precision = precision;
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
    broker_id: selectedBrokerId,
  };
  const editingId = $("#mapping-form").dataset.editingId;
  const url = editingId ? `${API_BASE}api/mappings/${editingId}` : `${API_BASE}api/mappings`;
  const method = editingId ? "PUT" : "POST";
  const res = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  // Keep the modal open on validation errors so the entity ID can be fixed
  // without retyping everything else.
  if (!res.ok) {
    let detail = `Error HTTP ${res.status}`;
    try {
      const data = await res.json();
      if (data && data.detail) detail = data.detail;
    } catch (err) {
      void err;
    }
    showMappingError(detail);
    return;
  }

  delete $("#mapping-form").dataset.editingId;
  closeMappingModal();
  loadMappings();
}

let allMappings = [];
let mappingsFilter = "";

async function loadMappings() {
  const res = await fetch(`${API_BASE}api/mappings`);
  allMappings = await res.json();
  renderMappings();
}

function renderMappings() {
  const mappings = filterMappings(allMappings, mappingsFilter);
  const tbody = document.querySelector("#mappings-table tbody");
  tbody.innerHTML = "";

  const count = $("#mappings-count");
  if (count) {
    count.textContent = mappingsFilter
      ? `${mappings.length} de ${allMappings.length}`
      : `${allMappings.length} entidad${allMappings.length === 1 ? "" : "es"}`;
  }

  if (mappings.length === 0) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 8;
    td.className = "table-empty";
    td.textContent = allMappings.length
      ? "Sin coincidencias"
      : "Todavía no hay entidades. Creá una desde la pestaña Explorar.";
    tr.appendChild(td);
    tbody.appendChild(tr);
    return;
  }

  mappings.forEach((mapping) => {
    const hasValue = mapping.last_value !== null && mapping.last_value !== undefined;
    const isUnknown = mapping.last_value === "unknown";
    const statusHtml = mapping.last_error
      ? `<span class="cell-status error" title="${escapeHtml(mapping.last_error)}">error</span>`
      : isUnknown
      ? '<span class="cell-status waiting" title="Sin datos: broker desconectado o el topic dejó de publicar">desconocido</span>'
      : hasValue
      ? '<span class="cell-status ok">ok</span>'
      : '<span class="cell-status waiting">esperando dato</span>';

    const broker = brokers.find((b) => b.id === mapping.broker_id);
    const brokerLabel = broker
      ? escapeHtml(broker.label)
      : mapping.broker_id
      ? '<span class="broker-missing" title="El broker fue borrado">(borrado)</span>'
      : '<span class="broker-any" title="Mapeo previo a los multi-broker: escucha cualquier broker">(cualquiera)</span>';

    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${brokerLabel}</td>
      <td class="cell-topic">${escapeHtml(mapping.topic)}</td>
      <td>${escapeHtml(mapping.field_path)}</td>
      <td>${escapeHtml(mapping.entity_id)}</td>
      <td>${escapeHtml(mapping.domain)}</td>
      <td>${
        isUnknown
          ? '<span class="value-unknown">desconocido</span>'
          : hasValue
          ? escapeHtml(String(mapping.last_value))
          : "&mdash;"
      }</td>
      <td>${statusHtml}</td>
      <td>
        <button class="edit-btn" data-id="${mapping.id}">Editar</button>
        <button class="delete-btn" data-id="${mapping.id}">Borrar</button>
      </td>
    `;
    if (mapping.last_error) {
      const errRow = document.createElement("tr");
      errRow.className = "error-detail-row";
      errRow.innerHTML = `<td colspan="8">${escapeHtml(mapping.last_error)}</td>`;
      tbody.appendChild(tr);
      tbody.appendChild(errRow);
      return;
    }
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
      const mapping = allMappings.find((m) => m.id === btn.dataset.id);
      if (!mapping) return;
      currentTopic = mapping.topic;
      // openMappingModal resets the form (and clears editingId), so the edit
      // target is applied after it.
      openMappingModal(mapping.field_path);
      $("#mapping-modal-title").textContent = "Editar mapeo";
      $("#mapping-entity-id").value = mapping.entity_id;
      $("#mapping-domain").value = mapping.domain;
      updateDomainConfigVisibility();
      fillDomainConfig(mapping.domain, mapping.domain_config || {});
      $("#mapping-form").dataset.editingId = mapping.id;
    });
  });
}

function filterMappings(mappings, filter) {
  if (!filter) return mappings;
  const needle = filter.toLowerCase();
  return mappings.filter((m) =>
    [m.topic, m.field_path, m.entity_id, m.domain, String(m.last_value ?? "")]
      .join(" ")
      .toLowerCase()
      .includes(needle)
  );
}

function fillDomainConfig(domain, config) {
  setSelectedPrecision(config.precision ?? null);
  setSelectedStaleTimeout(config.stale_timeout ?? null);

  if (domain === "sensor") {
    setSelectedUnit(config.unit_of_measurement);
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
  connecting: "conectando",
  disconnected: "desconectado",
  error: "error",
};

let brokers = [];
let selectedBrokerId = null;
let editingBrokerId = null;

async function loadBrokers() {
  const res = await fetch(`${API_BASE}api/brokers`);
  brokers = await res.json();
  renderBrokers();
  syncBrokerSelect();
}

function statusPillHtml(status, lastError) {
  const cls =
    status === "connected"
      ? "connected"
      : status === "error"
      ? "error"
      : status === "connecting"
      ? "waiting"
      : "disconnected";
  const title = lastError ? ` title="${escapeHtml(lastError)}"` : "";
  return `<span class="status-pill ${cls}"${title}>${STATUS_LABELS[status] || status}</span>`;
}

function renderBrokers() {
  const tbody = document.querySelector("#brokers-table tbody");
  tbody.innerHTML = "";

  if (brokers.length === 0) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      '<td colspan="5" class="table-empty">Sin brokers. Agregá uno con el formulario de la izquierda.</td>';
    tbody.appendChild(tr);
    return;
  }

  brokers.forEach((broker) => {
    const tr = document.createElement("tr");
    if (broker.id === editingBrokerId) tr.classList.add("row-editing");
    tr.innerHTML = `
      <td>
        <div class="broker-label">${escapeHtml(broker.label)}</div>
        ${broker.name ? `<div class="broker-endpoint">${escapeHtml(broker.host)}:${broker.port}</div>` : ""}
      </td>
      <td>${statusPillHtml(broker.status, broker.last_error)}</td>
      <td>${broker.topic_count}${
        broker.evicted_topics
          ? ` <span class="broker-evicted" title="Se descartaron ${broker.evicted_topics} topics viejos del caché para acotar la memoria. No afecta a las entidades ya creadas.">(+${broker.evicted_topics} descartados)</span>`
          : ""
      }</td>
      <td>${broker.message_total}</td>
      <td class="broker-actions">
        <button class="broker-reconnect" data-id="${broker.id}">Reconectar</button>
        <button class="broker-edit" data-id="${broker.id}">Editar</button>
        <button class="broker-delete" data-id="${broker.id}">Borrar</button>
      </td>
    `;
    tbody.appendChild(tr);

    if (broker.last_error) {
      const errRow = document.createElement("tr");
      errRow.className = "error-detail-row";
      errRow.innerHTML = `<td colspan="5">${escapeHtml(broker.last_error)}</td>`;
      tbody.appendChild(errRow);
    }
  });

  tbody.querySelectorAll(".broker-reconnect").forEach((btn) => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      await fetch(`${API_BASE}api/brokers/${btn.dataset.id}/reconnect`, { method: "POST" });
      await loadBrokers();
      pollBrokersBriefly();
    });
  });

  tbody.querySelectorAll(".broker-edit").forEach((btn) => {
    btn.addEventListener("click", () => startEditBroker(btn.dataset.id));
  });

  tbody.querySelectorAll(".broker-delete").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const broker = brokers.find((b) => b.id === btn.dataset.id);
      const label = broker ? broker.label : "este broker";
      if (!window.confirm(`¿Borrar ${label}? Las entidades que dependen de él dejarán de actualizarse.`)) {
        return;
      }
      await fetch(`${API_BASE}api/brokers/${btn.dataset.id}`, { method: "DELETE" });
      if (editingBrokerId === btn.dataset.id) cancelEditBroker();
      if (selectedBrokerId === btn.dataset.id) selectedBrokerId = null;
      loadBrokers();
    });
  });
}

function startEditBroker(brokerId) {
  const broker = brokers.find((b) => b.id === brokerId);
  if (!broker) return;

  editingBrokerId = brokerId;
  $("#conn-name").value = broker.name || "";
  $("#conn-host").value = broker.host;
  $("#conn-port").value = broker.port;
  $("#conn-username").value = broker.username || "";
  // The API never returns stored passwords, so a blank field means "keep".
  $("#conn-password").value = "";
  $("#conn-password").placeholder = "(sin cambios)";
  $("#btn-add-broker").textContent = "Guardar cambios";
  $("#btn-cancel-edit").classList.remove("hidden");
  $("#conn-error").textContent = "";
  renderBrokers();
}

function cancelEditBroker() {
  editingBrokerId = null;
  $("#connection-form").reset();
  $("#conn-port").value = 1883;
  $("#conn-password").placeholder = "";
  $("#btn-add-broker").textContent = "Agregar a brokers";
  $("#btn-cancel-edit").classList.add("hidden");
  $("#conn-error").textContent = "";
  renderBrokers();
}

async function submitConnection(event) {
  event.preventDefault();

  const password = $("#conn-password").value;
  const body = {
    name: $("#conn-name").value.trim() || null,
    host: $("#conn-host").value.trim(),
    port: parseInt($("#conn-port").value, 10),
    username: $("#conn-username").value.trim() || null,
    password: password || null,
  };

  // Editing with an untouched password field keeps the stored one.
  if (editingBrokerId && !password) {
    const existing = brokers.find((b) => b.id === editingBrokerId);
    if (existing) delete body.password;
  }

  const url = editingBrokerId
    ? `${API_BASE}api/brokers/${editingBrokerId}`
    : `${API_BASE}api/brokers`;
  const res = await fetch(url, {
    method: editingBrokerId ? "PUT" : "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });

  if (!res.ok) {
    let detail = `Error HTTP ${res.status}`;
    try {
      const data = await res.json();
      if (data && data.detail) detail = data.detail;
    } catch (err) {
      void err;
    }
    $("#conn-error").textContent = detail;
    return;
  }

  cancelEditBroker();
  await loadBrokers();
  pollBrokersBriefly();
}

// connect_async returns before the handshake finishes, so poll briefly instead
// of waiting for the regular refresh to show the green state.
function pollBrokersBriefly(attempts = 6) {
  if (attempts <= 0) return;
  setTimeout(async () => {
    await loadBrokers();
    if (brokers.some((b) => b.status === "connecting" || b.status === "disconnected")) {
      pollBrokersBriefly(attempts - 1);
    }
  }, 500);
}

function syncBrokerSelect() {
  const select = $("#tree-broker");
  if (!select) return;

  const previous = selectedBrokerId;
  select.innerHTML = "";

  if (brokers.length === 0) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "(sin brokers)";
    select.appendChild(opt);
    selectedBrokerId = null;
    return;
  }

  brokers.forEach((broker) => {
    const opt = document.createElement("option");
    opt.value = broker.id;
    opt.textContent = `${broker.label} (${STATUS_LABELS[broker.status] || broker.status})`;
    select.appendChild(opt);
  });

  const stillExists = brokers.some((b) => b.id === previous);
  selectedBrokerId = stillExists ? previous : brokers[0].id;
  select.value = selectedBrokerId;
}

async function init() {
  initTabs();
  populateUnitSelect();
  populatePrecisionSelect();
  populateStaleTimeoutSelect();

  // Brokers first: the tree needs a selected broker before it can load.
  await loadBrokers();
  loadTree();
  setInterval(loadTree, 5000);
  setInterval(loadBrokers, 5000);

  $("#connection-form").addEventListener("submit", submitConnection);
  $("#btn-cancel-edit").addEventListener("click", cancelEditBroker);

  $("#tree-broker").addEventListener("change", (event) => {
    selectedBrokerId = event.target.value || null;
    // Different broker means a different topic namespace; reset the view.
    expandedPaths.clear();
    currentTopic = null;
    $("#topic-title").textContent = "";
    $("#topic-json").innerHTML = "";
    loadTree();
  });

  $("#cfg-precision").addEventListener("change", updatePrecisionExample);

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
  $("#mapping-domain").addEventListener("change", () => {
    updateDomainConfigVisibility();
    // Re-suggest only while creating; an existing mapping keeps its entity ID.
    if (!$("#mapping-form").dataset.editingId) {
      $("#mapping-entity-id").value = suggestEntityId(
        currentTopic,
        $("#mapping-field-path").value,
        $("#mapping-domain").value
      );
    }
  });
  $("#cfg-unit").addEventListener("change", updateUnitCustomVisibility);

  $("#mappings-search").addEventListener("input", (event) => {
    mappingsFilter = event.target.value.trim();
    renderMappings();
  });
}

document.addEventListener("DOMContentLoaded", init);
