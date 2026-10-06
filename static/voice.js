// Speaking instead of typing, with the browser's own speech recognition.
// Chrome, Edge and Safari have it; elsewhere the mic buttons stay hidden and
// typing works as before. Browsers only allow the microphone over HTTPS.
(() => {
  const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Recognition || !window.isSecureContext) return;
  const csrf = (document.querySelector("meta[name='csrf-token']") || {}).content || "";

  function listen(button, lang, { onText, onDone }) {
    const recognition = new Recognition();
    recognition.lang = lang;
    recognition.interimResults = true;
    recognition.maxAlternatives = 1;
    let finalText = "";
    button.classList.add("listening");
    button.setAttribute("aria-pressed", "true");
    recognition.onresult = (event) => {
      let interim = "";
      for (let i = event.resultIndex; i < event.results.length; i += 1) {
        const piece = event.results[i][0].transcript;
        if (event.results[i].isFinal) finalText += piece;
        else interim += piece;
      }
      onText((finalText + interim).trim());
    };
    recognition.onerror = (event) => {
      onText(event.error === "not-allowed" ? "Allow the microphone to use this." : "Didn't catch that. Try again.");
    };
    recognition.onend = () => {
      button.classList.remove("listening");
      button.setAttribute("aria-pressed", "false");
      if (finalText.trim()) onDone(finalText.trim());
    };
    recognition.start();
    return recognition;
  }

  // Report form: dictate the one-sentence description.
  document.querySelectorAll(".compact-mic").forEach((button) => {
    const target = document.querySelector(button.dataset.target);
    if (!target) return;
    button.hidden = false;
    let active = null;
    button.addEventListener("click", () => {
      if (active) return active.stop();
      const before = target.value.trim();
      active = listen(button, "en-IN", {
        onText: (text) => { target.value = `${before} ${text}`.trim(); },
        onDone: () => { active = null; target.dispatchEvent(new Event("input", { bubbles: true })); },
      });
    });
  });

  // Home page: one sentence fills in Retrace.
  const box = document.querySelector(".voice-box");
  if (!box) return;
  box.hidden = false;
  const button = box.querySelector(".mic-button");
  const heard = box.querySelector(".voice-heard");
  const language = box.querySelector(".voice-lang");
  try {
    language.value = localStorage.getItem("classfind-voice-lang") || "en-IN";
  } catch {
    // Storage can be blocked; English is the default.
  }
  language.addEventListener("change", () => {
    try { localStorage.setItem("classfind-voice-lang", language.value); } catch { /* not needed */ }
  });
  let active = null;
  button.addEventListener("click", () => {
    if (active) return active.stop();
    heard.textContent = "Listening… e.g. “I lost my blue bottle, I was in P1, then the canteen, then workshops.”";
    active = listen(button, language.value, {
      onText: (text) => { heard.textContent = `“${text}”`; },
      onDone: async (text) => {
        active = null;
        heard.textContent = `“${text}” · working out your route…`;
        try {
          const response = await fetch(box.dataset.parse, {
            method: "POST",
            headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
            body: JSON.stringify({ text }),
          });
          const plan = await response.json();
          if (!response.ok) throw new Error(plan.error);
          heard.textContent = plan.stops.length
            ? `“${text}”`
            : `“${text}” · no campus places in that. Tap them on the map instead.`;
          document.dispatchEvent(new CustomEvent("classfind:plan", { detail: plan }));
        } catch (error) {
          heard.textContent = error.message || "Couldn't work that out. Tap the places on the map instead.";
        }
      },
    });
  });
})();
