// The campus map on the home page. The page draws the map and the counts on
// the server; this adds what needs a browser: night mode, live counts, pins for
// the place you tap, and zooming on phones.
(() => {
  const section = document.querySelector(".campus");
  const holder = section && section.querySelector(".campus-map");
  if (!holder) return;

  const svg = holder.querySelector(".campus-svg");
  const pinLayer = svg.querySelector(".map-pins");
  const card = holder.querySelector(".map-card");
  const resetButton = holder.querySelector(".map-reset");
  const form = document.querySelector(".campus-search");
  const placeInput = form && form.querySelector("input[name='place']");
  const queryInput = form && form.querySelector("input[name='q']");
  const SVG_NS = "http://www.w3.org/2000/svg";
  const POLL_MS = 20000;
  const PHONE = window.matchMedia("(max-width: 900px)");
  const CALM = window.matchMedia("(prefers-reduced-motion: reduce)");
  const fullView = svg.dataset.viewbox.split(" ").map(Number);

  let campus = null;
  let items = [];
  let known = null;
  let selected = placeInput ? placeInput.value : "";
  let retracing = false;
  let retraceItems = [];

  const svgEl = (name, attrs = {}) => {
    const el = document.createElementNS(SVG_NS, name);
    Object.entries(attrs).forEach(([key, value]) => el.setAttribute(key, value));
    return el;
  };
  const placeById = (id) => campus && campus.places.find((place) => place.id === id);
  const boxOf = (outline) => {
    const xs = outline.map((p) => p[0]);
    const ys = outline.map((p) => p[1]);
    return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
  };

  // Dark map from 7 pm to 6 am, by the visitor's own clock.
  const setNight = () => {
    const hour = new Date().getHours();
    holder.classList.toggle("is-night", hour >= 19 || hour < 6);
  };

  function ago(iso) {
    const minutes = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
    if (minutes < 60) return minutes <= 1 ? "just now" : `${minutes} min ago`;
    const hours = Math.round(minutes / 60);
    if (hours < 24) return `${hours} h ago`;
    const days = Math.round(hours / 24);
    return days === 1 ? "yesterday" : `${days} days ago`;
  }

  // ---- Counts, hotspots and the desk ------------------------------------
  function applyCounts(data) {
    const counts = data.places || {};
    svg.querySelectorAll("[data-badge]").forEach((badge) => {
      const c = counts[badge.dataset.badge] || {};
      badge.querySelector(".badge-count").textContent = c.found || 0;
      badge.classList.toggle("empty", !c.found && !c.lost);
      badge.querySelector(".badge-lost").toggleAttribute("hidden", !c.lost);
    });
    document.querySelectorAll(".desk-count").forEach((el) => { el.textContent = data.desk; });
    const recent = Object.values(counts).map((c) => c.recent || 0);
    const most = Math.max(1, ...recent);
    svg.querySelectorAll(".place-heat").forEach((heat) => {
      const c = counts[heat.closest("[data-place]").dataset.place] || {};
      const share = data.hotspots ? (c.recent || 0) / most : 0;
      heat.style.opacity = share ? (0.1 + 0.4 * share).toFixed(2) : "0";
    });
  }

  function bump(placeId) {
    const badge = svg.querySelector(`[data-badge="${placeId}"]`);
    if (!badge || CALM.matches) return;
    badge.classList.remove("bump");
    void badge.getBBox();
    badge.classList.add("bump");
  }

  async function refresh() {
    try {
      const response = await fetch(section.dataset.mapFeed, { headers: { Accept: "application/json" } });
      if (!response.ok) return;
      const data = await response.json();
      items = data.items;
      const ids = new Set(items.map((item) => item.id));
      if (known) {
        items.filter((item) => !known.has(item.id)).forEach((item) => bump(item.place));
      }
      known = ids;
      applyCounts(data);
      if (selected && !retracing) drawPins(selected, false);
    } catch {
      // Offline for a moment: the next poll tries again.
    }
  }

  // ---- Pins for the selected place --------------------------------------
  function doorPoint(place) {
    const nodes = place.doors.map((index) => campus.nodes[index]);
    return nodes[0] || place.outline[0];
  }

  function slots(place, count, outside) {
    const [x0, y0, x1, y1] = boxOf(place.outline);
    const points = [];
    if (outside) {
      const [dx, dy] = doorPoint(place);
      for (let i = 0; i < count; i += 1) {
        const angle = (i / Math.max(count, 1)) * Math.PI * 2;
        const radius = 16 + 10 * Math.floor(i / 6);
        points.push([dx + Math.cos(angle) * radius, dy + Math.sin(angle) * radius]);
      }
      return points;
    }
    const width = x1 - x0;
    const height = y1 - y0;
    if (width < 50 || height < 50) {
      // Too small for a grid: a ring around it.
      const cx = (x0 + x1) / 2;
      const cy = (y0 + y1) / 2;
      const radius = Math.max(width, height) / 2 + 16;
      for (let i = 0; i < count; i += 1) {
        const angle = -Math.PI / 2 + (i / Math.max(count, 1)) * Math.PI * 2;
        points.push([cx + Math.cos(angle) * radius, cy + Math.sin(angle) * radius]);
      }
      return points;
    }
    const step = 26;
    const cols = Math.max(1, Math.floor((width - 16) / step));
    const rows = Math.max(1, Math.floor((height - 16) / step));
    const used = Math.min(count, cols * rows);
    const usedCols = Math.min(cols, used);
    const usedRows = Math.ceil(used / usedCols);
    const startX = (x0 + x1) / 2 - ((usedCols - 1) * step) / 2;
    // Below the building's name when it has room, so the pins don't cover it.
    const labelled = svg.querySelector(`.place[data-place="${place.id}"] .place-label`);
    const shift = labelled && height > usedRows * step + 40 ? 24 : 0;
    const startY = (y0 + y1) / 2 - ((usedRows - 1) * step) / 2 + shift;
    for (let i = 0; i < used; i += 1) {
      points.push([startX + (i % usedCols) * step, startY + Math.floor(i / usedCols) * step]);
    }
    return points;
  }

  function drawPins(placeId, animate = true) {
    pinLayer.replaceChildren();
    const place = placeById(placeId);
    if (!place) return;
    const here = items.filter((item) => item.place === placeId);
    ["inside", "outside"].forEach((side) => {
      const group = here.filter((item) => (item.side === "outside") === (side === "outside"));
      const spots = slots(place, group.length, side === "outside");
      group.forEach((item, i) => {
        const spot = spots[i];
        if (!spot) return;
        const pin = svgEl("g", {
          class: `pin ${item.status.toLowerCase()}${side === "outside" ? " outside" : ""}`,
          transform: `translate(${spot[0].toFixed(1)},${spot[1].toFixed(1)})`,
          tabindex: "0", role: "button", "data-item": item.id,
        });
        // The drop animation moves the inner group, so it never fights the pin's position.
        const body = svgEl("g", { class: "pin-body" });
        body.append(svgEl("circle", { r: 9 }));
        if (animate && !CALM.matches) body.style.animationDelay = `${i * 45}ms`;
        else body.style.animation = "none";
        const label = svgEl("title");
        label.textContent = `${item.title} (${item.status.toLowerCase()})`;
        pin.append(body, label);
        pinLayer.append(pin);
      });
      const hidden = group.length - spots.length;
      if (hidden > 0) {
        const [x0, , x1, y1] = boxOf(place.outline);
        const more = svgEl("text", { class: "pin-more", x: (x0 + x1) / 2, y: y1 - 6 });
        more.textContent = `+${hidden} more`;
        pinLayer.append(more);
      }
    });
  }

  function showCard(pin) {
    const item = [...retraceItems, ...items].find((entry) => String(entry.id) === pin.dataset.item);
    if (!item) return;
    pinLayer.querySelectorAll(".pin.active").forEach((el) => el.classList.remove("active"));
    pin.classList.add("active");
    card.replaceChildren();
    const close = Object.assign(document.createElement("button"), {
      type: "button", className: "map-card-close", textContent: "×",
    });
    close.setAttribute("aria-label", "Close");
    close.addEventListener("click", hideCard);
    if (item.image) {
      const img = Object.assign(document.createElement("img"), { src: item.image, alt: "" });
      card.append(img);
    }
    const body = document.createElement("div");
    body.className = "map-card-body";
    const pill = Object.assign(document.createElement("span"), {
      className: `status-pill status-${item.status.toLowerCase()}`, textContent: item.status,
    });
    const title = Object.assign(document.createElement("strong"), { textContent: item.title });
    const where = Object.assign(document.createElement("p"), {
      textContent: `${item.where} · ${ago(item.at)}${item.custody ? ` · ${item.custody}` : ""}`,
    });
    const link = Object.assign(document.createElement("a"), {
      className: "card-link", href: item.url, textContent: "View report →",
    });
    body.append(pill, title, where, link);
    card.append(close, body);
    card.hidden = false;

    const pinBox = pin.getBoundingClientRect();
    const frame = holder.getBoundingClientRect();
    const left = Math.min(Math.max(8, pinBox.left - frame.left - 125 + pinBox.width / 2), frame.width - 258);
    const below = pinBox.bottom - frame.top + 10;
    const top = below + card.offsetHeight > frame.height - 8 ? pinBox.top - frame.top - card.offsetHeight - 10 : below;
    card.style.left = `${left}px`;
    card.style.top = `${Math.max(8, top)}px`;
    document.querySelectorAll(".result-row.highlight").forEach((row) => row.classList.remove("highlight"));
    const row = document.querySelector(`.result-row[data-item="${item.id}"]`);
    if (row) row.classList.add("highlight");
  }

  function hideCard() {
    card.hidden = true;
    pinLayer.querySelectorAll(".pin.active").forEach((el) => el.classList.remove("active"));
  }

  // ---- Zoom on phones -----------------------------------------------------
  let currentView = fullView.slice();
  function animateView(target) {
    const from = currentView.slice();
    const start = performance.now();
    const duration = CALM.matches ? 0 : 380;
    const step = (now) => {
      const t = duration ? Math.min(1, (now - start) / duration) : 1;
      const ease = 1 - Math.pow(1 - t, 3);
      currentView = from.map((value, i) => value + (target[i] - value) * ease);
      svg.setAttribute("viewBox", currentView.map((v) => v.toFixed(1)).join(" "));
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
    resetButton.hidden = target.every((value, i) => value === fullView[i]);
  }

  function focusOn(place) {
    const [x0, y0, x1, y1] = boxOf(place.outline);
    const frame = holder.getBoundingClientRect();
    const ratio = frame.width / frame.height;
    let height = Math.max(y1 - y0, (x1 - x0) / ratio) + 260;
    height = Math.min(height, fullView[3]);
    const width = height * ratio;
    animateView([(x0 + x1) / 2 - width / 2, (y0 + y1) / 2 - height / 2, width, height]);
  }

  // ---- Selecting a place --------------------------------------------------
  function searchAgain() {
    if (queryInput) queryInput.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function select(placeId, { search = true } = {}) {
    selected = placeId;
    if (placeInput) placeInput.value = placeId;
    svg.querySelectorAll(".place.selected").forEach((el) => el.classList.remove("selected"));
    holder.classList.toggle("has-selection", Boolean(placeId));
    hideCard();
    if (placeId) {
      const shape = svg.querySelector(`.place[data-place="${placeId}"]`);
      if (shape) shape.classList.add("selected");
      drawPins(placeId);
      const place = placeById(placeId);
      if (place && PHONE.matches) focusOn(place);
    } else {
      pinLayer.replaceChildren();
      if (PHONE.matches) animateView(fullView);
    }
    if (search) searchAgain();
  }

  svg.addEventListener("click", (event) => {
    const pin = event.target.closest(".pin");
    if (pin) return showCard(pin);
    if (retracing) return addStop(event);
    const place = event.target.closest(".place");
    if (place) return select(place.dataset.place === selected ? "" : place.dataset.place);
    if (!card.hidden) return hideCard();
    if (selected) select("");
  });
  svg.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const target = event.target.closest(".pin, .place");
    if (!target) return;
    event.preventDefault();
    if (target.classList.contains("pin")) showCard(target);
    else if (retracing) addStop({ target, clientX: 0, clientY: 0 });
    else select(target.dataset.place === selected ? "" : target.dataset.place);
  });
  resetButton.addEventListener("click", () => {
    animateView(fullView);
    if (selected) select("");
  });

  // ---- Retrace: walk your route and see what was found along it -------------
  const retracePane = document.querySelector(".retrace-pane");
  const searchPane = document.querySelector(".search-pane");
  const stopList = retracePane && retracePane.querySelector(".retrace-stops");
  const retraceOut = retracePane && retracePane.querySelector(".retrace-results");
  const retraceWords = retracePane && retracePane.querySelector(".retrace-words");
  const routeLayer = svg.querySelector(".map-route");
  const markLayer = svg.querySelector(".map-route-marks");
  // Walking within this distance of a building counts as passing it.
  const PASS_DISTANCE = 30;
  const DOOR_REACH = 100;
  const hint = holder.querySelector(".map-hint");
  const hintText = hint ? hint.textContent : "";
  let stops = [];
  let graph = null;
  let retraceTimer;
  let scanFrame;

  // Cutting through a building or car park counts as this much longer than
  // walking the same distance on a path, so routes keep to paths when they can.
  const THROUGH_COST = 3;
  // Another way between two stops is searched too if it is at most this much longer.
  const ALT_STRETCH = 1.6;

  function buildGraph() {
    graph = campus.nodes.map(() => []);
    campus.edges.forEach(([a, b, length, via]) => {
      const cost = placeById(via) ? length * THROUGH_COST : length;
      graph[a].push([b, cost, via]);
      graph[b].push([a, cost, via]);
    });
  }

  const edgeKey = (a, b) => (a < b ? `${a}-${b}` : `${b}-${a}`);
  const pathCost = (path) => path.slice(1).reduce((sum, node, i) => (
    sum + graph[path[i]].find(([next]) => next === node)[1]), 0);

  // Shortest walk from any of `from` to any of `to`, as node indexes.
  // `avoid` makes some edges dearer, to find a second, different way.
  function shortest(from, to, avoid = null) {
    const dist = new Map(from.map((n) => [n, 0]));
    const prev = new Map();
    const done = new Set();
    const targets = new Set(to);
    while (true) {
      let node = null;
      let best = Infinity;
      dist.forEach((d, n) => { if (!done.has(n) && d < best) { best = d; node = n; } });
      if (node === null) return [];
      if (targets.has(node)) {
        const path = [node];
        while (prev.has(path[0])) path.unshift(prev.get(path[0]));
        return path;
      }
      done.add(node);
      graph[node].forEach(([next, length]) => {
        const d = best + length * (avoid && avoid.has(edgeKey(node, next)) ? 4 : 1);
        if (d < (dist.has(next) ? dist.get(next) : Infinity)) {
          dist.set(next, d);
          prev.set(next, node);
        }
      });
    }
  }

  function svgPoint(event) {
    const point = svg.createSVGPoint();
    point.x = event.clientX;
    point.y = event.clientY;
    return point.matrixTransform(svg.getScreenCTM().inverse());
  }

  // A stop on the route: a place (any of its doors) or a point on a road.
  function stopFor(id, tap) {
    const place = placeById(id);
    if (place) {
      const [x0, y0] = boxOf(place.outline);
      return { id, name: place.short, nodes: place.doors, at: [x0, y0], also: [] };
    }
    const road = campus.roads.find((r) => r.id === id);
    if (!road) return null;
    const onRoad = new Set();
    campus.edges.forEach(([a, b, , via]) => { if (via === road.id) { onRoad.add(a); onRoad.add(b); } });
    const [ax, ay] = road.points[0];
    const [bx, by] = road.points[road.points.length - 1];
    const aim = tap || { x: (ax + bx) / 2, y: (ay + by) / 2 };
    const nearest = [...onRoad].sort((m, n) => {
      const [mx, my] = campus.nodes[m];
      const [nx, ny] = campus.nodes[n];
      return Math.hypot(mx - aim.x, my - aim.y) - Math.hypot(nx - aim.x, ny - aim.y);
    })[0];
    return { id, name: road.name.replace(/^./, (c) => c.toLowerCase()), nodes: [nearest], at: campus.nodes[nearest], also: [] };
  }

  function addStop(event) {
    const placeEl = event.target.closest(".place");
    const roadEl = event.target.closest(".road-hit");
    let stop = null;
    if (placeEl) stop = stopFor(placeEl.dataset.place);
    else if (roadEl) stop = stopFor(roadEl.dataset.road, svgPoint(event));
    if (!stop || (stops.length && stops[stops.length - 1].id === stop.id)) return;
    stops.push(stop);
    showStops();
    drawRoute();
    queueRetrace();
  }

  function showStops() {
    stopList.replaceChildren();
    stops.forEach((stop, i) => {
      const li = document.createElement("li");
      li.textContent = stop.name;
      const remove = Object.assign(document.createElement("button"), { type: "button", textContent: "×" });
      remove.setAttribute("aria-label", `Remove ${stop.name}`);
      remove.addEventListener("click", () => {
        stops.splice(i, 1);
        showStops();
        drawRoute();
        queueRetrace();
      });
      li.append(remove);
      stopList.append(li);
    });
    if (hint) hint.textContent = stops.length ? "Keep tapping where you went next." : "Tap the first place you went today.";
  }

  // The route as node indexes, and every place and road it goes past.
  function walk() {
    const nodes = [];
    const alternates = [];
    for (let i = 1; i < stops.length; i += 1) {
      const from = nodes.length ? [nodes[nodes.length - 1], ...stops[i - 1].nodes] : stops[i - 1].nodes;
      const leg = shortest(from, stops[i].nodes);
      // The other likely way: the shortest walk that avoids this leg's edges where it can.
      const used = new Set(leg.slice(1).map((n, k) => edgeKey(leg[k], n)));
      const other = shortest(from, stops[i].nodes, used);
      const shared = other.slice(1).filter((n, k) => used.has(edgeKey(other[k], n))).length;
      if (other.length > 1 && shared < (other.length - 1) * 0.6 && pathCost(other) <= pathCost(leg) * ALT_STRETCH) {
        alternates.push(other);
      }
      if (nodes.length && leg[0] === nodes[nodes.length - 1]) leg.shift();
      nodes.push(...leg);
    }
    const passed = new Set();
    const onPath = new Set([...nodes, ...alternates.flat()]);
    const samples = [];
    [nodes, ...alternates].forEach((route) => {
      for (let i = 1; i < route.length; i += 1) {
        const [ax, ay] = campus.nodes[route[i - 1]];
        const [bx, by] = campus.nodes[route[i]];
        const steps = Math.max(1, Math.ceil(Math.hypot(bx - ax, by - ay) / 10));
        for (let k = 0; k <= steps; k += 1) samples.push([ax + ((bx - ax) * k) / steps, ay + ((by - ay) * k) / steps]);
      }
    });
    campus.places.forEach((place) => {
      const [x0, y0, x1, y1] = boxOf(place.outline);
      const near = samples.some(([x, y]) => Math.hypot(Math.max(x0 - x, 0, x - x1), Math.max(y0 - y, 0, y - y1)) <= PASS_DISTANCE);
      // Passing the end of a short road that leads to its door counts too.
      const atDoor = place.doors.some((d) => onPath.has(d) ||
        graph[d].some(([next, length]) => length <= DOOR_REACH && onPath.has(next)));
      if (near || atDoor) passed.add(place.id);
    });
    [nodes, ...alternates].forEach((route) => {
      for (let i = 1; i < route.length; i += 1) {
        const edge = graph[route[i - 1]].find(([next]) => next === route[i]);
        if (edge) passed.add(edge[2]);
      }
    });
    stops.forEach((stop) => passed.delete(stop.id));
    return { nodes, alternates, passed: [...passed] };
  }

  function drawRoute() {
    window.cancelAnimationFrame(scanFrame);
    routeLayer.replaceChildren();
    markLayer.replaceChildren();
    pinLayer.replaceChildren();
    hideCard();
    svg.querySelectorAll(".place.on-route").forEach((el) => el.classList.remove("on-route"));
    stops.forEach((stop) => {
      const el = svg.querySelector(`.place[data-place="${stop.id}"]`);
      if (el) el.classList.add("on-route");
    });
    const { nodes, alternates } = walk();
    alternates.forEach((route) => {
      const d = route.map((n, i) => `${i ? "L" : "M"}${campus.nodes[n].join(",")}`).join(" ");
      routeLayer.append(svgEl("path", { class: "route-alt", d }));
    });
    if (nodes.length > 1) {
      const d = nodes.map((n, i) => `${i ? "L" : "M"}${campus.nodes[n].join(",")}`).join(" ");
      const line = svgEl("path", { class: "route-line", d });
      routeLayer.append(line);
      const length = line.getTotalLength();
      line.style.strokeDasharray = length;
      line.style.strokeDashoffset = CALM.matches ? 0 : length;
      void line.getBoundingClientRect();
      line.style.strokeDashoffset = 0;
    }
    stops.forEach((stop, i) => {
      const mark = svgEl("g", { class: "route-stop", transform: `translate(${stop.at.join(",")})` });
      mark.append(svgEl("circle", { r: 13 }));
      const number = svgEl("text", { y: 4.5 });
      number.textContent = i + 1;
      mark.append(number);
      markLayer.append(mark);
    });
  }

  function since() {
    const choice = retracePane.querySelector("input[name='retrace-since']:checked").value;
    const start = new Date();
    if (choice === "week") return new Date(Date.now() - 7 * 86400000);
    start.setHours(0, 0, 0, 0);
    if (choice === "yesterday") start.setDate(start.getDate() - 1);
    return start;
  }

  function queueRetrace() {
    window.clearTimeout(retraceTimer);
    retraceTimer = window.setTimeout(runRetrace, 450);
  }

  async function runRetrace() {
    retraceOut.replaceChildren();
    retraceItems = [];
    if (!stops.length) return;
    const { passed } = walk();
    const params = new URLSearchParams({
      stops: stops.flatMap((s) => [s.id, ...s.also]).join(","),
      passed: passed.join(","),
      since: since().toISOString(),
      q: retraceWords.value.trim(),
    });
    let found = [];
    try {
      const response = await fetch(`${retracePane.dataset.retrace}?${params}`);
      found = (await response.json()).items;
    } catch {
      retraceOut.textContent = "Couldn't reach ClassFind. Try again in a moment.";
      return;
    }
    retraceItems = found;
    scan(found);
  }

  // A light runs along the route; each match drops in as it is passed.
  function scan(found) {
    const line = routeLayer.querySelector(".route-line");
    const anchors = found.map((item) => anchorFor(item));
    if (!line || CALM.matches) {
      anchors.forEach((anchor, i) => dropPin(found[i], anchor));
      listResults(found);
      return;
    }
    const length = line.getTotalLength();
    const samples = [];
    for (let at = 0; at <= length; at += 6) samples.push([at, line.getPointAtLength(at)]);
    const passAt = anchors.map(([x, y]) => samples.reduce((best, [at, p]) => (
      Math.hypot(p.x - x, p.y - y) < best[1] ? [at, Math.hypot(p.x - x, p.y - y)] : best), [0, Infinity])[0]);
    const dot = svgEl("circle", { class: "route-scan", r: 9 });
    markLayer.append(dot);
    const duration = Math.min(3200, 900 + length * 1.4);
    const start = performance.now();
    const dropped = new Set();
    const step = (now) => {
      const t = Math.min(1, (now - start) / duration);
      const at = t * length;
      const p = line.getPointAtLength(at);
      dot.setAttribute("cx", p.x);
      dot.setAttribute("cy", p.y);
      passAt.forEach((mark, i) => {
        if (mark <= at && !dropped.has(i)) {
          dropped.add(i);
          dropPin(found[i], anchors[i]);
        }
      });
      if (t < 1) {
        scanFrame = requestAnimationFrame(step);
      } else {
        dot.remove();
        found.forEach((item, i) => { if (!dropped.has(i)) dropPin(item, anchors[i]); });
        listResults(found);
      }
    };
    scanFrame = requestAnimationFrame(step);
  }

  function anchorFor(item) {
    const place = placeById(item.place);
    const jitter = () => (Math.random() - 0.5) * 22;
    if (place && item.side !== "outside") {
      const [x0, y0, x1, y1] = boxOf(place.outline);
      return [(x0 + x1) / 2 + jitter(), (y0 + y1) / 2 + jitter()];
    }
    if (place) {
      const [x, y] = doorPoint(place);
      return [x + jitter(), y + jitter()];
    }
    const road = campus.roads.find((r) => r.id === item.place);
    const [a, b] = road.points;
    const t = Math.random();
    return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t];
  }

  function dropPin(item, [x, y]) {
    const pin = svgEl("g", {
      class: `pin found${item.side === "outside" ? " outside" : ""}`,
      transform: `translate(${x.toFixed(1)},${y.toFixed(1)})`,
      tabindex: "0", role: "button", "data-item": item.id,
    });
    const body = svgEl("g", { class: "pin-body" });
    body.append(svgEl("circle", { r: 9 }));
    const label = svgEl("title");
    label.textContent = item.title;
    pin.append(body, label);
    pinLayer.append(pin);
  }

  function listResults(found) {
    retraceOut.replaceChildren();
    const heading = document.createElement("h2");
    heading.textContent = found.length
      ? `${found.length} found along your route`
      : "Nothing found along your route yet";
    retraceOut.append(heading);
    if (found.length) {
      const list = Object.assign(document.createElement("ul"), { className: "result-list" });
      found.forEach((item) => {
        const li = Object.assign(document.createElement("li"), { className: "result-row" });
        li.dataset.item = item.id;
        li.dataset.place = item.place;
        const link = Object.assign(document.createElement("a"), { href: item.url });
        const thumb = Object.assign(document.createElement("span"), { className: "result-thumb" });
        if (item.image) thumb.append(Object.assign(document.createElement("img"), { src: item.image, alt: "" }));
        else thumb.textContent = item.title.slice(0, 1).toUpperCase();
        const text = Object.assign(document.createElement("span"), { className: "result-text" });
        text.append(
          Object.assign(document.createElement("strong"), { textContent: item.title }),
          Object.assign(document.createElement("small"), { textContent: `${item.why} · ${ago(item.at)}` }),
        );
        const side = Object.assign(document.createElement("span"), { className: "result-side" });
        if (item.custody) side.append(Object.assign(document.createElement("span"), { className: "desk-chip", textContent: "At the desk" }));
        link.append(thumb, text, side);
        li.append(link);
        list.append(li);
      });
      retraceOut.append(list);
    }
    const note = document.createElement("p");
    note.className = "retrace-save";
    const params = new URLSearchParams({
      status: "Lost",
      title: retraceWords.value.trim(),
      location: stops.map((s) => s.name).join(" → ").slice(0, 120),
    });
    const save = Object.assign(document.createElement("a"), {
      className: "text-link", href: `${retracePane.dataset.report}?${params}`,
      textContent: "Save this as a lost report →",
    });
    note.append(found.length ? "Not there? " : "We'll email you if a match is handed in. ", save);
    retraceOut.append(note);
  }

  // The headline's second line follows the tab: searching or retracing.
  const headline = document.querySelector(".headline-line");
  function setHeadline(mode) {
    if (!headline) return;
    const text = headline.dataset[mode];
    if (!text || headline.textContent === text) return;
    if (CALM.matches) {
      headline.textContent = text;
      return;
    }
    headline.classList.add("swapping");
    window.setTimeout(() => {
      headline.textContent = text;
      headline.classList.remove("swapping");
    }, 160);
  }

  function setMode(mode) {
    retracing = mode === "retrace";
    setHeadline(mode);
    document.querySelectorAll(".mode-tab").forEach((tab) => {
      const on = tab.dataset.mode === mode;
      tab.classList.toggle("active", on);
      tab.setAttribute("aria-selected", on);
    });
    retracePane.hidden = !retracing;
    searchPane.hidden = retracing;
    holder.classList.toggle("retrace-mode", retracing);
    if (selected) select("", { search: false });
    if (!graph && campus) buildGraph();
    if (retracing) {
      showStops();
    } else {
      stops = [];
      retraceItems = [];
      routeLayer.replaceChildren();
      markLayer.replaceChildren();
      pinLayer.replaceChildren();
      svg.querySelectorAll(".place.on-route").forEach((el) => el.classList.remove("on-route"));
      if (hint) hint.textContent = hintText;
    }
  }

  if (retracePane) {
    document.querySelectorAll(".mode-tab").forEach((tab) => {
      tab.addEventListener("click", () => setMode(tab.dataset.mode));
    });
    retracePane.querySelectorAll("input[name='retrace-since']").forEach((radio) => {
      radio.addEventListener("change", queueRetrace);
    });
    retraceWords.addEventListener("input", queueRetrace);

    // A spoken sentence, read on the server, fills in the whole search.
    document.addEventListener("classfind:plan", ({ detail }) => {
      if (!campus) return;
      if (!retracing) setMode("retrace");
      stops = [];
      detail.stops.forEach(({ ids }) => {
        const stop = stopFor(ids[0]);
        if (!stop) return;
        // "Canteen" walks to the BIT Canteen but searches the puff shop and Nandini too.
        stop.also = ids.slice(1);
        if (ids.length > 1) stop.name = `${stop.name} (or nearby)`;
        stops.push(stop);
      });
      retraceWords.value = detail.item || "";
      const when = retracePane.querySelector(`input[name='retrace-since'][value='${detail.when}']`);
      if (when) when.checked = true;
      showStops();
      drawRoute();
      runRetrace();
    });
  }


  // A spoken search: the words go in the box and a named place filters the map.
  document.addEventListener("classfind:spoken-search", ({ detail }) => {
    if (retracing) setMode("search");
    if (queryInput) queryInput.value = detail.q || "";
    if (detail.place && placeById(detail.place)) {
      select(detail.place);
    } else {
      if (selected) select("", { search: false });
      if (placeInput) placeInput.value = detail.place || "";
      searchAgain();
    }
  });

  // A report in the list lights up its place on the map.
  document.addEventListener("mouseover", (event) => {
    const row = event.target.closest && event.target.closest(".result-row");
    svg.querySelectorAll(".place.hinted").forEach((el) => el.classList.remove("hinted"));
    if (!row || !row.dataset.place) return;
    const place = svg.querySelector(`.place[data-place="${row.dataset.place}"]`);
    if (place) place.classList.add("hinted");
  });

  setNight();
  window.setInterval(setNight, 60000);
  fetch(section.dataset.campus)
    .then((response) => response.json())
    .then((data) => {
      campus = data;
      return refresh();
    })
    .then(() => {
      if (selected) select(selected, { search: false });
    })
    .catch(() => {});
  window.setInterval(() => { if (!document.hidden) refresh(); }, POLL_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
})();
