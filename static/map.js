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
      if (selected) drawPins(selected, false);
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
    const item = items.find((entry) => String(entry.id) === pin.dataset.item);
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
    else select(target.dataset.place === selected ? "" : target.dataset.place);
  });
  resetButton.addEventListener("click", () => {
    animateView(fullView);
    if (selected) select("");
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
