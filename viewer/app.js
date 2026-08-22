"use strict";

const DEFAULT_SCOPE = "all";
const TYPE_ORDER = [
  "security", "issuer", "sector", "factor", "macro",
  "event_type", "source", "thesis", "model",
];
const TYPE_COLORS = {
  security: "#64b5f6",
  issuer: "#81c784",
  sector: "#4dd0e1",
  factor: "#ffd54f",
  macro: "#ff8a65",
  event_type: "#f06292",
  source: "#b0bec5",
  thesis: "#ba68c8",
  model: "#7986cb",
};
const RELATION_ORDER = [
  "supports", "contradicts", "affected_by", "benefits_from",
  "hurt_by", "co_moves_with", "invalidates",
];
const SIGN_COLORS = {
  positive: "#66d9c3",
  negative: "#ff7f8d",
  neutral: "#8fa2b5",
  mixed: "#f4c96b",
};
const MIN_ALPHA = 0.004;
const $ = (id) => document.getElementById(id);

const state = {
  scope: DEFAULT_SCOPE,
  graph: {nodes: [], edges: []},
  byId: new Map(),
  selected: null,
  query: "",
  hiddenTypes: new Set(),
  hiddenRelations: new Set(),
  visibleIds: new Set(),
  visibleEdges: [],
  points: [],
  anchors: new Map(),
  alpha: 0,
  framePending: false,
  view: {x: 0, y: 0, k: 0.78},
  pointer: null,
};

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function stableHash(value) {
  let hash = 2166136261;
  for (let index = 0; index < value.length; index += 1) {
    hash ^= value.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function typeRank(type) {
  const rank = TYPE_ORDER.indexOf(type);
  return rank < 0 ? TYPE_ORDER.length : rank;
}

function nodeMatches(node) {
  const query = state.query.trim().toLowerCase();
  if (!query) return true;
  return [node.id, node.title, node.type, node.status, node.path, ...(node.aliases || [])]
    .join(" ")
    .toLowerCase()
    .includes(query);
}

function buildAnchors(nodes) {
  state.anchors = new Map();
  const types = [...new Set(nodes.map((node) => node.type))]
    .sort((a, b) => typeRank(a) - typeRank(b) || a.localeCompare(b));
  const radius = Math.min(275, 145 + Math.sqrt(nodes.length) * 13);
  types.forEach((type, index) => {
    const angle = -Math.PI / 2 + (index / Math.max(types.length, 1)) * Math.PI * 2;
    state.anchors.set(type, {x: Math.cos(angle) * radius, y: Math.sin(angle) * radius});
  });
}

function initialPoint(node, slot) {
  const anchor = state.anchors.get(node.type) || {x: 0, y: 0};
  const hash = stableHash(node.id);
  const angle = ((hash % 10000) / 10000) * Math.PI * 2 + slot * 2.399963;
  const radius = 18 + Math.sqrt(slot + 1) * 20 + ((hash >>> 16) % 17);
  return {
    id: node.id,
    x: anchor.x + Math.cos(angle) * radius,
    y: anchor.y + Math.sin(angle) * radius,
    vx: 0,
    vy: 0,
  };
}

function rebuild({resetPositions = false} = {}) {
  const visibleNodes = state.graph.nodes.filter(
    (node) => !state.hiddenTypes.has(node.type) && nodeMatches(node),
  );
  state.visibleIds = new Set(visibleNodes.map((node) => node.id));
  state.visibleEdges = state.graph.edges.filter(
    (edge) => state.visibleIds.has(edge.source)
      && state.visibleIds.has(edge.target)
      && !state.hiddenRelations.has(edge.relation),
  );

  const ordered = [...visibleNodes].sort(
    (a, b) => typeRank(a.type) - typeRank(b.type) || a.id.localeCompare(b.id),
  );
  buildAnchors(ordered);
  const previous = resetPositions
    ? new Map()
    : new Map(state.points.map((point) => [point.id, point]));
  const slots = new Map();
  state.points = ordered.map((node) => {
    const old = previous.get(node.id);
    if (old) return {...old, vx: 0, vy: 0};
    const slot = slots.get(node.type) || 0;
    slots.set(node.type, slot + 1);
    return initialPoint(node, slot);
  });
  state.alpha = state.points.length ? 0.9 : 0;
  $("layout-status").textContent = state.alpha ? "cooling" : "empty";
  renderControls();
  renderNodeList();
  updateCounts();
  scheduleFrame();
}

function updateCounts() {
  $("counts").textContent = `${state.graph.nodes.length} nodes · ${state.graph.edges.length} edges · showing ${state.visibleIds.size}`;
}

function renderControls() {
  const types = [...new Set(state.graph.nodes.map((node) => node.type))]
    .sort((a, b) => typeRank(a) - typeRank(b) || a.localeCompare(b));
  $("types").innerHTML = types.map((type) => {
    const off = state.hiddenTypes.has(type);
    return `<button type="button" class="chip ${off ? "off" : ""}" data-type="${escapeHtml(type)}" aria-pressed="${!off}"><span class="dot" style="background:${TYPE_COLORS[type] || "#b0bec5"}"></span>${escapeHtml(type)}</button>`;
  }).join("");

  const relations = [...new Set(state.graph.edges.map((edge) => edge.relation))]
    .sort((a, b) => RELATION_ORDER.indexOf(a) - RELATION_ORDER.indexOf(b));
  $("relations").innerHTML = relations.map((relation) => {
    const off = state.hiddenRelations.has(relation);
    return `<button type="button" class="chip ${off ? "off" : ""}" data-relation="${escapeHtml(relation)}" aria-pressed="${!off}">${escapeHtml(relation)}</button>`;
  }).join("");
}

function renderNodeList() {
  const nodes = state.graph.nodes
    .filter((node) => state.visibleIds.has(node.id))
    .sort((a, b) => a.id.localeCompare(b.id));
  $("list-count").textContent = `${nodes.length}/${state.graph.nodes.length}`;
  $("node-list").innerHTML = nodes.length
    ? nodes.map((node) => `<button type="button" class="node-row ${node.id === state.selected ? "on" : ""}" data-id="${escapeHtml(node.id)}"><span class="dot" style="background:${TYPE_COLORS[node.type] || "#b0bec5"}"></span>${escapeHtml(node.title)}<small>${escapeHtml(node.type)} · ${escapeHtml(node.id)}</small></button>`).join("")
    : '<p class="muted">No nodes match these filters.</p>';
}

function stepLayout() {
  if (state.alpha < MIN_ALPHA || !state.points.length) {
    state.alpha = 0;
    for (const point of state.points) {
      point.vx = 0;
      point.vy = 0;
    }
    $("layout-status").textContent = state.points.length ? "settled" : "empty";
    return;
  }

  const points = state.points;
  const byId = new Map(points.map((point) => [point.id, point]));
  const alpha = state.alpha;
  const repulsion = points.length > 150 ? 750 : 1450;
  for (let left = 0; left < points.length; left += 1) {
    for (let right = left + 1; right < points.length; right += 1) {
      const a = points[left];
      const b = points[right];
      const dx = a.x - b.x;
      const dy = a.y - b.y;
      const distanceSquared = dx * dx + dy * dy || 0.01;
      const distance = Math.sqrt(distanceSquared);
      const force = Math.min(4, repulsion / distanceSquared) * alpha;
      const fx = (dx / distance) * force;
      const fy = (dy / distance) * force;
      a.vx += fx;
      a.vy += fy;
      b.vx -= fx;
      b.vy -= fy;
    }
  }

  for (const edge of state.visibleEdges) {
    const a = byId.get(edge.source);
    const b = byId.get(edge.target);
    if (!a || !b) continue;
    const dx = b.x - a.x;
    const dy = b.y - a.y;
    const distance = Math.hypot(dx, dy) || 0.01;
    const force = (distance - 118) * 0.007 * alpha;
    const fx = (dx / distance) * force;
    const fy = (dy / distance) * force;
    a.vx += fx;
    a.vy += fy;
    b.vx -= fx;
    b.vy -= fy;
  }

  for (const point of points) {
    const node = state.byId.get(point.id);
    const anchor = state.anchors.get(node.type) || {x: 0, y: 0};
    point.vx += (anchor.x - point.x) * 0.035 * alpha;
    point.vy += (anchor.y - point.y) * 0.035 * alpha;
    point.vx *= 0.78;
    point.vy *= 0.78;
    const isDragged = state.pointer?.mode === "node" && state.pointer.id === point.id;
    if (!isDragged) {
      point.x += point.vx;
      point.y += point.vy;
    }
  }
  state.alpha *= points.length > 100 ? 0.97 : 0.94;
}

function canvasMetrics() {
  const canvas = $("canvas");
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.round(rect.width * ratio));
  const height = Math.max(1, Math.round(rect.height * ratio));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  return {canvas, rect, ratio};
}

function toScreen(point, rect) {
  return {
    x: rect.width / 2 + (point.x + state.view.x) * state.view.k,
    y: rect.height / 2 + (point.y + state.view.y) * state.view.k,
  };
}

function drawArrow(context, start, end, edge, showLabel) {
  const dx = end.x - start.x;
  const dy = end.y - start.y;
  const distance = Math.hypot(dx, dy) || 1;
  const ux = dx / distance;
  const uy = dy / distance;
  const from = {x: start.x + ux * 8, y: start.y + uy * 8};
  const to = {x: end.x - ux * 10, y: end.y - uy * 10};
  const color = SIGN_COLORS[edge.sign] || SIGN_COLORS.neutral;
  context.save();
  context.strokeStyle = color;
  context.fillStyle = color;
  context.globalAlpha = edge.causality === "hypothesis" ? 0.68 : 0.5;
  context.lineWidth = 1;
  context.setLineDash(edge.causality === "hypothesis" ? [5, 4] : []);
  context.beginPath();
  context.moveTo(from.x, from.y);
  context.lineTo(to.x, to.y);
  context.stroke();
  context.setLineDash([]);
  context.beginPath();
  context.moveTo(to.x, to.y);
  context.lineTo(to.x - ux * 7 - uy * 3.5, to.y - uy * 7 + ux * 3.5);
  context.lineTo(to.x - ux * 7 + uy * 3.5, to.y - uy * 7 - ux * 3.5);
  context.closePath();
  context.fill();
  if (showLabel) {
    const x = (from.x + to.x) / 2;
    const y = (from.y + to.y) / 2;
    context.globalAlpha = 0.85;
    context.font = "9px system-ui";
    context.textAlign = "center";
    context.fillText(edge.relation, x, y - 4);
  }
  context.restore();
}

function draw() {
  const {canvas, rect, ratio} = canvasMetrics();
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, rect.width, rect.height);
  const points = new Map(state.points.map((point) => [point.id, point]));
  const showEdgeLabels = state.visibleEdges.length <= 24 || state.view.k >= 1.15;
  for (const edge of state.visibleEdges) {
    const source = points.get(edge.source);
    const target = points.get(edge.target);
    if (!source || !target) continue;
    drawArrow(context, toScreen(source, rect), toScreen(target, rect), edge, showEdgeLabels);
  }

  context.font = "600 9px system-ui";
  context.textAlign = "center";
  for (const [type, anchor] of state.anchors) {
    const position = toScreen({x: anchor.x * 1.28, y: anchor.y * 1.28}, rect);
    context.fillStyle = TYPE_COLORS[type] || "#b0bec5";
    context.globalAlpha = 0.72;
    context.fillText(type.toUpperCase(), position.x, position.y);
  }
  context.globalAlpha = 1;

  const showNodeLabels = state.points.length <= 55 || state.view.k >= 1.05;
  context.textAlign = "left";
  for (const point of state.points) {
    const node = state.byId.get(point.id);
    const position = toScreen(point, rect);
    const selected = point.id === state.selected;
    context.beginPath();
    context.fillStyle = TYPE_COLORS[node.type] || "#b0bec5";
    context.arc(position.x, position.y, selected ? 8 : 5.5, 0, Math.PI * 2);
    context.fill();
    if (selected) {
      context.strokeStyle = "#ffffff";
      context.lineWidth = 2;
      context.stroke();
    }
    if (showNodeLabels || selected) {
      context.fillStyle = "#edf4fa";
      context.font = "10px system-ui";
      context.fillText(node.title, position.x + 9, position.y + 3.5);
    }
  }
}

function scheduleFrame() {
  if (state.framePending) return;
  state.framePending = true;
  requestAnimationFrame(() => {
    state.framePending = false;
    stepLayout();
    draw();
    if (state.alpha > 0 || state.pointer) scheduleFrame();
  });
}

function hitTest(x, y) {
  const rect = $("canvas").getBoundingClientRect();
  let winner = null;
  let best = 16;
  for (const point of state.points) {
    const position = toScreen(point, rect);
    const distance = Math.hypot(position.x - x, position.y - y);
    if (distance < best) {
      best = distance;
      winner = point;
    }
  }
  return winner;
}

function bindCanvas() {
  const canvas = $("canvas");
  canvas.addEventListener("pointerdown", (event) => {
    const rect = canvas.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const y = event.clientY - rect.top;
    const point = hitTest(x, y);
    canvas.setPointerCapture(event.pointerId);
    state.pointer = point
      ? {mode: "node", id: point.id, startX: x, startY: y, moved: false}
      : {
          mode: "pan", startX: x, startY: y,
          viewX: state.view.x, viewY: state.view.y, moved: false,
        };
    state.alpha = Math.max(state.alpha, point ? 0.24 : 0);
    scheduleFrame();
  });

  canvas.addEventListener("pointermove", (event) => {
    if (!state.pointer) return;
    const rect = canvas.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const y = event.clientY - rect.top;
    state.pointer.moved ||= Math.hypot(
      x - state.pointer.startX,
      y - state.pointer.startY,
    ) > 3;
    if (state.pointer.mode === "node") {
      const point = state.points.find((candidate) => candidate.id === state.pointer.id);
      if (point) {
        point.x = (x - rect.width / 2) / state.view.k - state.view.x;
        point.y = (y - rect.height / 2) / state.view.k - state.view.y;
        point.vx = 0;
        point.vy = 0;
      }
      state.alpha = Math.max(state.alpha, 0.18);
    } else {
      state.view.x = state.pointer.viewX + (x - state.pointer.startX) / state.view.k;
      state.view.y = state.pointer.viewY + (y - state.pointer.startY) / state.view.k;
    }
    scheduleFrame();
  });

  canvas.addEventListener("pointerup", () => {
    if (state.pointer?.mode === "node" && !state.pointer.moved) {
      selectNode(state.pointer.id);
    }
    state.pointer = null;
    state.alpha = Math.max(state.alpha, 0.12);
    scheduleFrame();
  });

  canvas.addEventListener("pointercancel", () => {
    state.pointer = null;
    scheduleFrame();
  });

  canvas.addEventListener("wheel", (event) => {
    event.preventDefault();
    state.view.k = Math.max(
      0.25,
      Math.min(3.5, state.view.k * (event.deltaY > 0 ? 0.91 : 1.1)),
    );
    scheduleFrame();
  }, {passive: false});

  canvas.addEventListener("dblclick", () => {
    state.view = {x: 0, y: 0, k: 0.78};
    scheduleFrame();
  });
}

function renderMarkdown(markdown) {
  const lines = markdown.replace(/\r/g, "").split("\n");
  let index = lines[0] === "---" ? lines.indexOf("---", 1) + 1 : 0;
  const output = [];
  let paragraph = [];
  const flush = () => {
    if (paragraph.length) {
      output.push(`<p>${escapeHtml(paragraph.join(" "))}</p>`);
      paragraph = [];
    }
  };
  for (; index < lines.length; index += 1) {
    const line = lines[index];
    if (!line.trim()) {
      flush();
      continue;
    }
    const heading = /^(#{1,3})\s+(.*)$/.exec(line);
    if (heading) {
      flush();
      output.push(`<h${heading[1].length}>${escapeHtml(heading[2])}</h${heading[1].length}>`);
      continue;
    }
    if (/^[-*]\s+/.test(line)) {
      flush();
      output.push(`<p>• ${escapeHtml(line.replace(/^[-*]\s+/, ""))}</p>`);
      continue;
    }
    paragraph.push(line.trim());
  }
  flush();
  return output.join("\n");
}

function edgeCard(edge, nodeId) {
  const outgoing = edge.source === nodeId;
  const otherId = outgoing ? edge.target : edge.source;
  const direction = outgoing ? "outgoing" : "incoming";
  const asOf = edge.as_of || "undated";
  return `<div class="edge-card"><button type="button" data-id="${escapeHtml(otherId)}">${escapeHtml(otherId)}</button><span class="relation">${escapeHtml(direction)} · ${escapeHtml(edge.relation)}</span><div class="edge-meta"><span class="badge ${edge.causality === "hypothesis" ? "hypothesis" : ""}">${escapeHtml(edge.causality)}</span>${escapeHtml(edge.sign)} · ${escapeHtml(edge.horizon)} · n=${escapeHtml(edge.observations)} · ${escapeHtml(edge.uncertainty)} uncertainty · as of ${escapeHtml(asOf)} · provenance ${escapeHtml(edge.provenance)}</div></div>`;
}

async function renderDetails(nodeId) {
  const node = state.byId.get(nodeId);
  if (!node) return;
  const selection = nodeId;
  let markdown = "";
  try {
    const response = await fetch(`/${node.path}`);
    if (response.ok) markdown = await response.text();
  } catch (error) {
    markdown = `Unable to load Markdown: ${error}`;
  }
  if (state.selected !== selection) return;
  const edges = state.graph.edges
    .filter((edge) => edge.source === nodeId || edge.target === nodeId)
    .sort((a, b) => a.id.localeCompare(b.id));
  $("details").innerHTML = `<div class="kicker">${escapeHtml(node.type)} · ${escapeHtml(node.status)}</div><h1>${escapeHtml(node.title)}</h1><p class="node-id">${escapeHtml(node.id)} · ${escapeHtml(node.path)}</p>${markdown ? renderMarkdown(markdown) : '<p class="muted">No Markdown body available.</p>'}<h2>Typed edges (${edges.length})</h2>${edges.length ? edges.map((edge) => edgeCard(edge, nodeId)).join("") : '<p class="muted">No edges.</p>'}`;
}

function selectNode(nodeId) {
  if (!state.byId.has(nodeId)) return;
  state.selected = nodeId;
  history.replaceState(null, "", `#${encodeURIComponent(nodeId)}`);
  renderNodeList();
  renderDetails(nodeId);
  scheduleFrame();
}

async function loadGraph() {
  const response = await fetch("/knowledge/graph.json", {cache: "no-store"});
  if (!response.ok) throw new Error(`graph request failed: HTTP ${response.status}`);
  const graph = await response.json();
  state.graph = graph;
  state.byId = new Map(graph.nodes.map((node) => [node.id, node]));
  const hashId = decodeURIComponent(location.hash.slice(1));
  state.selected = state.byId.has(hashId) ? hashId : graph.nodes[0]?.id || null;
  rebuild({resetPositions: true});
  if (state.selected) renderDetails(state.selected);
}

$("types").addEventListener("click", (event) => {
  const button = event.target.closest("[data-type]");
  if (!button) return;
  const type = button.dataset.type;
  if (state.hiddenTypes.has(type)) state.hiddenTypes.delete(type);
  else state.hiddenTypes.add(type);
  rebuild();
});

$("relations").addEventListener("click", (event) => {
  const button = event.target.closest("[data-relation]");
  if (!button) return;
  const relation = button.dataset.relation;
  if (state.hiddenRelations.has(relation)) state.hiddenRelations.delete(relation);
  else state.hiddenRelations.add(relation);
  rebuild();
});

$("node-list").addEventListener("click", (event) => {
  const row = event.target.closest("[data-id]");
  if (row) selectNode(row.dataset.id);
});

$("details").addEventListener("click", (event) => {
  const row = event.target.closest("[data-id]");
  if (row) selectNode(row.dataset.id);
});

$("search").addEventListener("input", (event) => {
  state.query = event.target.value;
  rebuild();
});

$("reset").addEventListener("click", () => {
  state.query = "";
  state.hiddenTypes.clear();
  state.hiddenRelations.clear();
  state.view = {x: 0, y: 0, k: 0.78};
  $("search").value = "";
  rebuild({resetPositions: true});
});

$("regen").addEventListener("click", async () => {
  const button = $("regen");
  button.disabled = true;
  button.textContent = "Regenerating…";
  try {
    const response = await fetch("/api/regenerate", {method: "POST"});
    if (!response.ok) throw new Error(await response.text());
    await loadGraph();
    button.textContent = "Regenerated";
  } catch (error) {
    button.textContent = "Failed";
    $("details").innerHTML = `<pre class="error">${escapeHtml(error)}</pre>`;
  } finally {
    button.disabled = false;
    setTimeout(() => { button.textContent = "Regenerate"; }, 1200);
  }
});

window.addEventListener("resize", scheduleFrame);
bindCanvas();
loadGraph().catch((error) => {
  $("details").innerHTML = `<pre class="error">${escapeHtml(error)}</pre>`;
  $("layout-status").textContent = "failed";
});
