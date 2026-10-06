// The map on the report form: tap where the item was, or let what you typed
// pick it. Without this script the server reads the place from the text.
(() => {
  const picker = document.querySelector(".place-picker");
  if (!picker) return;
  const form = picker.closest("form");
  const svg = picker.querySelector(".campus-svg");
  const placeInput = picker.querySelector("input[name='place']");
  const choice = picker.querySelector(".picker-choice");
  const sides = picker.querySelectorAll("input[name='place_side']");
  const location = form.elements.namedItem("location");
  // Once someone taps the map, typing no longer moves their choice.
  let tapped = Boolean(placeInput.value);
  let timer;

  const nameOf = (el) => (el.querySelector("title") || {}).textContent || "";
  const setSide = (side) => sides.forEach((radio) => { radio.checked = radio.value === side; });

  function clearMarks() {
    svg.querySelectorAll(".selected, .candidate").forEach((el) => el.classList.remove("selected", "candidate"));
  }

  function choose(el, side) {
    clearMarks();
    el.classList.add("selected");
    const isRoad = el.classList.contains("road-hit");
    placeInput.value = isRoad ? el.dataset.road : el.dataset.place;
    if (isRoad) setSide("outside");
    else if (side) setSide(side);
    choice.textContent = isRoad ? `On the ${nameOf(el).replace(/^./, (c) => c.toLowerCase())}` : nameOf(el);
  }

  svg.addEventListener("click", (event) => {
    const target = event.target.closest(".place, .road-hit");
    if (!target) return;
    tapped = true;
    choose(target);
  });
  svg.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" && event.key !== " ") return;
    const target = event.target.closest(".place");
    if (!target) return;
    event.preventDefault();
    tapped = true;
    choose(target);
  });

  async function guess() {
    if (tapped || !location.value.trim()) return;
    try {
      const url = `${picker.dataset.lookup}?q=${encodeURIComponent(location.value)}`;
      const found = await (await fetch(url)).json();
      if (tapped) return;
      clearMarks();
      placeInput.value = "";
      if (found.place) {
        const el = svg.querySelector(`.place[data-place="${found.place}"]`) ||
          svg.querySelector(`.road-hit[data-road="${found.place}"]`);
        if (el) choose(el, found.side);
        choice.textContent += " (from what you typed; tap to change)";
      } else if (found.candidates.length) {
        setSide(found.side);
        found.candidates.forEach((id) => {
          const el = svg.querySelector(`.place[data-place="${id}"]`);
          if (el) el.classList.add("candidate");
        });
        const names = found.candidates.map((id) => found.names[id]);
        const list = names.length > 1 ? `${names.slice(0, -1).join(", ")} or ${names.slice(-1)}` : names[0];
        choice.textContent = `That could be ${list}. Tap the right one.`;
      } else {
        choice.textContent = "Tap the place on the map.";
      }
    } catch {
      // The lookup is only a convenience.
    }
  }

  // A place that could be several, like "canteen", must be settled on the map
  // before the report is published.
  form.addEventListener("submit", (event) => {
    if (placeInput.value || !svg.querySelector(".candidate")) return;
    event.preventDefault();
    choice.textContent = `${choice.textContent.replace(/ Pick one to publish\.$/, "")} Pick one to publish.`;
    choice.classList.add("needs-pick");
    picker.scrollIntoView({ behavior: "smooth", block: "center" });
  });

  location.addEventListener("input", () => {
    window.clearTimeout(timer);
    timer = window.setTimeout(guess, 350);
  });

  const start = placeInput.value &&
    (svg.querySelector(`.place[data-place="${placeInput.value}"]`) ||
     svg.querySelector(`.road-hit[data-road="${placeInput.value}"]`));
  if (start) choose(start);
})();
