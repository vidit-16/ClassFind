// Speaking instead of typing. The mic records a short clip and the server has
// Gemini listen to it, so English, Hindi, Kannada and any mix of them work
// without choosing a language. Where the browser can caption speech, the words
// show while you talk and are sent too, as a fallback.
//
// Recording keeps going through pauses and stops after five seconds of
// silence, after half a minute, or when the mic is tapped again.
(() => {
  const mics = document.querySelectorAll("[data-voice]");
  if (!mics.length || !window.isSecureContext || !navigator.mediaDevices || !window.AudioContext) return;
  const csrf = (document.querySelector("meta[name='csrf-token']") || {}).content || "";
  const Captions = window.SpeechRecognition || window.webkitSpeechRecognition;
  const RATE = 16000;
  const QUIET_STOP_MS = 5000;
  const NO_SPEECH_STOP_MS = 8000;
  const MAX_MS = 30000;
  const SPEAKING_LEVEL = 0.008;

  // 16-bit mono WAV, which Gemini accepts and every browser can make.
  function wav(samples, inRate) {
    const step = inRate / RATE;
    const length = Math.floor(samples.length / step);
    const buffer = new ArrayBuffer(44 + length * 2);
    const view = new DataView(buffer);
    const text = (at, value) => [...value].forEach((c, i) => view.setUint8(at + i, c.charCodeAt(0)));
    text(0, "RIFF"); view.setUint32(4, 36 + length * 2, true); text(8, "WAVE"); text(12, "fmt ");
    view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
    view.setUint32(24, RATE, true); view.setUint32(28, RATE * 2, true); view.setUint16(32, 2, true);
    view.setUint16(34, 16, true); text(36, "data"); view.setUint32(40, length * 2, true);
    for (let i = 0; i < length; i += 1) {
      const s = Math.max(-1, Math.min(1, samples[Math.floor(i * step)]));
      view.setInt16(44 + i * 2, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    }
    return new Blob([view], { type: "audio/wav" });
  }

  async function record(button, onCaption) {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
    const context = new AudioContext();
    const source = context.createMediaStreamSource(stream);
    const node = context.createScriptProcessor(4096, 1, 1);
    const chunks = [];
    const started = performance.now();
    let lastVoice = 0;
    let captions = "";
    let caption = null;
    let finish;
    const done = new Promise((resolve) => { finish = resolve; });

    if (Captions) {
      caption = new Captions();
      caption.lang = "en-IN";
      caption.continuous = true;
      caption.interimResults = true;
      caption.onresult = (event) => {
        let interim = "";
        for (let i = event.resultIndex; i < event.results.length; i += 1) {
          if (event.results[i].isFinal) captions += `${event.results[i][0].transcript} `;
          else interim += event.results[i][0].transcript;
        }
        onCaption(`${captions}${interim}`.trim());
      };
      caption.onerror = () => {};
      try { caption.start(); } catch { caption = null; }
    }

    const stop = () => {
      if (!node.onaudioprocess) return;
      node.onaudioprocess = null;
      source.disconnect();
      node.disconnect();
      stream.getTracks().forEach((track) => track.stop());
      if (caption) { try { caption.stop(); } catch { /* already stopped */ } }
      const total = chunks.reduce((sum, c) => sum + c.length, 0);
      const all = new Float32Array(total);
      let at = 0;
      chunks.forEach((c) => { all.set(c, at); at += c.length; });
      const rate = context.sampleRate;
      context.close();
      // Captions can arrive a moment after the audio stops.
      const seconds = total / rate;
      window.setTimeout(() => finish({ audio: lastVoice ? wav(all, rate) : null, captions: captions.trim(), seconds }), 400);
    };

    node.onaudioprocess = (event) => {
      const input = event.inputBuffer.getChannelData(0);
      chunks.push(new Float32Array(input));
      const level = Math.sqrt(input.reduce((sum, v) => sum + v * v, 0) / input.length);
      const now = performance.now();
      if (level > SPEAKING_LEVEL) lastVoice = now;
      const quietFor = now - (lastVoice || started);
      if (now - started > MAX_MS || (lastVoice && quietFor > QUIET_STOP_MS) || (!lastVoice && quietFor > NO_SPEECH_STOP_MS)) {
        stop();
      }
    };
    source.connect(node);
    node.connect(context.destination);
    return { stop, done };
  }

  async function send(url, mode, clip) {
    const form = new FormData();
    form.append("mode", mode);
    form.append("transcript", clip.captions);
    if (clip.audio) form.append("audio", clip.audio, "clip.wav");
    const response = await fetch(url, { method: "POST", headers: { "X-CSRF-Token": csrf }, body: form });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Couldn't make that out.");
    return data;
  }

  // What each mic does with what it heard.
  const handlers = {
    retrace: (data) => document.dispatchEvent(new CustomEvent("classfind:plan", { detail: data })),
    search: (data) => document.dispatchEvent(new CustomEvent("classfind:spoken-search", { detail: data })),
    report: (data, button) => {
      const form = button.closest("form");
      const box = form.querySelector(".quick-report-text");
      if (box) box.value = data.heard;
      Object.entries(data.fields || {}).forEach(([name, value]) => {
        const field = form.elements.namedItem(name);
        if (field && value) {
          field.value = value;
          field.dispatchEvent(new Event("input", { bubbles: true }));
        }
      });
      form.querySelectorAll("textarea[maxlength]").forEach((area) => area.dispatchEvent(new Event("input")));
    },
  };

  mics.forEach((button) => {
    const mode = button.dataset.voice;
    const url = button.dataset.voiceUrl || (document.querySelector("[data-voice-url]") || {}).dataset?.voiceUrl;
    const area = button.closest(".voice-box, form, .campus-panel") || document;
    const say = (text) => {
      const out = area.querySelector(".voice-heard, .quick-report-status");
      if (out) out.textContent = text;
    };
    if (!url) return;
    button.hidden = false;
    let active = null;
    button.addEventListener("click", async () => {
      if (active) return active.stop();
      try {
        active = await record(button, (text) => say(`“${text}”`));
      } catch {
        say("Allow the microphone to use this, or type instead.");
        return;
      }
      button.classList.add("listening");
      button.setAttribute("aria-pressed", "true");
      say("Listening… take your time. It stops when you go quiet, or tap again.");
      const clip = await active.done;
      active = null;
      button.classList.remove("listening");
      button.setAttribute("aria-pressed", "false");
      if (!clip.audio && !clip.captions) {
        say("Didn't hear anything. Tap the mic and try again.");
        return;
      }
      button.classList.add("working");
      say(clip.captions ? `“${clip.captions}” · working it out…` : "Working out what you said…");
      try {
        const data = await send(url, mode, clip);
        // The recording's length shows whether a short transcript was cut off by the mic.
        say(data.heard ? `“${data.heard}” (${clip.seconds.toFixed(1)} s)` : "");
        handlers[mode](data, button);
      } catch (error) {
        say(error.message || "Couldn't make that out. Try again, or type it.");
      } finally {
        button.classList.remove("working");
      }
    });
  });
})();
