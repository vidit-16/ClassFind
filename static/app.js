// Phone photos are often 4-12 MB, over the 5 MB upload limit. Large JPEG, PNG
// and WEBP images are redrawn at most 1600 px on the long side as JPEG before
// upload. Anything that fails, or would not come out smaller, is sent as it was.
const RESIZE_ABOVE_BYTES = 1024 * 1024;
const RESIZE_MAX_SIDE = 1600;
const RESIZE_QUALITY = 0.82;

async function shrinkImage(file) {
  // A GIF may be animated, and a canvas keeps only its first frame.
  if (!["image/jpeg", "image/png", "image/webp"].includes(file.type)) return file;
  if (file.size <= RESIZE_ABOVE_BYTES) return file;
  const url = URL.createObjectURL(file);
  try {
    const image = await new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = reject;
      img.src = url;
    });
    const scale = Math.min(1, RESIZE_MAX_SIDE / Math.max(image.naturalWidth, image.naturalHeight));
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(image.naturalWidth * scale);
    canvas.height = Math.round(image.naturalHeight * scale);
    const context = canvas.getContext("2d");
    // JPEG has no transparency, so a transparent PNG gets a white background, not black.
    context.fillStyle = "#fff";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", RESIZE_QUALITY));
    if (!blob || blob.size >= file.size) return file;
    const name = file.name.replace(/\.[^.]+$/, "") + ".jpg";
    return new File([blob], name, { type: "image/jpeg", lastModified: Date.now() });
  } catch {
    return file;
  } finally {
    URL.revokeObjectURL(url);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  // Chart bar widths come from data attributes, because the security policy
  // does not allow inline style attributes.
  document.querySelectorAll("[data-width]").forEach((bar) => {
    bar.style.width = `${Math.max(0, Math.min(100, Number(bar.dataset.width) || 0))}%`;
  });

  document.querySelectorAll(".flash").forEach((flash) => {
    window.setTimeout(() => {
      flash.style.transition = "opacity .35s ease";
      flash.style.opacity = "0";
      window.setTimeout(() => flash.remove(), 400);
    }, 4500);
  });

  const menuToggle = document.querySelector(".menu-toggle");
  const navLinks = document.querySelector(".nav-links");
  if (menuToggle && navLinks) {
    menuToggle.addEventListener("click", () => {
      const open = navLinks.classList.toggle("is-open");
      menuToggle.setAttribute("aria-expanded", String(open));
      menuToggle.setAttribute("aria-label", open ? "Close navigation" : "Open navigation");
    });

    navLinks.querySelectorAll("a").forEach((link) => {
      link.addEventListener("click", () => {
        navLinks.classList.remove("is-open");
        menuToggle.setAttribute("aria-expanded", "false");
        menuToggle.setAttribute("aria-label", "Open navigation");
      });
    });
  }

  document.querySelectorAll("textarea[maxlength]").forEach((textarea) => {
    const counter = document.createElement("span");
    counter.className = "field-count";
    textarea.insertAdjacentElement("afterend", counter);
    const updateCount = () => {
      counter.textContent = `${textarea.value.length}/${textarea.maxLength}`;
    };
    textarea.addEventListener("input", updateCount);
    updateCount();
  });

  document.querySelectorAll('input[type="file"][name="image_file"]').forEach((input) => {
    input.addEventListener("change", async () => {
      const previous = input.parentElement.querySelector(".image-preview, .upload-error");
      if (previous) previous.remove();

      let file = input.files && input.files[0];
      if (!file) return;

      const allowed = ["image/png", "image/jpeg", "image/gif", "image/webp"];
      if (!allowed.includes(file.type)) {
        const error = document.createElement("div");
        error.className = "upload-error";
        error.textContent = "Use a PNG, JPG, GIF or WEBP image.";
        input.insertAdjacentElement("afterend", error);
        input.value = "";
        return;
      }

      // Hold the form while the image is redrawn, so it is not sent half-way.
      const submit = input.form && input.form.querySelector('button[type="submit"]');
      if (submit) submit.disabled = true;
      const smaller = await shrinkImage(file);
      if (submit) submit.disabled = false;
      if (smaller !== file) {
        try {
          const transfer = new DataTransfer();
          transfer.items.add(smaller);
          input.files = transfer.files;
          file = smaller;
        } catch {
          // Browsers without DataTransfer upload the original, and the size check below applies.
        }
      }

      if (file.size > 5 * 1024 * 1024) {
        const error = document.createElement("div");
        error.className = "upload-error";
        error.textContent = "That image is larger than 5 MB.";
        input.insertAdjacentElement("afterend", error);
        input.value = "";
        return;
      }

      const preview = document.createElement("div");
      preview.className = "image-preview";
      const image = document.createElement("img");
      image.alt = "Selected image preview";
      const note = document.createElement("span");
      note.textContent = `${file.name} · ${(file.size / 1024 / 1024).toFixed(2)} MB`;
      preview.append(image, note);
      input.insertAdjacentElement("afterend", preview);

      const reader = new FileReader();
      reader.addEventListener("load", () => { image.src = reader.result; });
      reader.readAsDataURL(file);
    });
  });

  // Quick report: one sentence in, the form fields out, for the student to check.
  document.querySelectorAll(".quick-report").forEach((box) => {
    const form = box.closest("form");
    const text = box.querySelector(".quick-report-text");
    const button = box.querySelector(".quick-report-button");
    const status = box.querySelector(".quick-report-status");
    button.addEventListener("click", async () => {
      if (text.value.trim().length < 5) {
        status.textContent = "Write a sentence about the item first.";
        return;
      }
      button.disabled = true;
      status.textContent = "Reading your description…";
      try {
        const response = await fetch(box.dataset.parseUrl, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRF-Token": form.querySelector('input[name="csrf_token"]').value,
          },
          body: JSON.stringify({ text: text.value }),
        });
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || "Could not read that.");
        Object.entries(data.fields).forEach(([name, value]) => {
          const field = form.elements.namedItem(name);
          if (field && value) field.value = value;
        });
        form.querySelectorAll("textarea[maxlength]").forEach((area) => area.dispatchEvent(new Event("input")));
        status.textContent = "Filled in below. Check each field before publishing.";
      } catch (error) {
        status.textContent = error.message || "Could not read that. Fill the form below instead.";
      } finally {
        button.disabled = false;
      }
    });
  });

  // Print one tag: mark its card, print, then clear the mark.
  document.querySelectorAll(".print-button").forEach((button) => {
    button.addEventListener("click", () => {
      const card = button.closest(".tag-card");
      document.body.classList.add("printing-tag");
      card.classList.add("print-this");
      window.print();
      card.classList.remove("print-this");
      document.body.classList.remove("printing-tag");
    });
  });

  // Live search: results update as you type or change a filter, without a reload.
  const liveForm = document.querySelector(".live-filters");
  const results = document.getElementById("results");
  if (liveForm && results) {
    let timer;
    let latest = 0;
    const refresh = async () => {
      const params = new URLSearchParams(new FormData(liveForm));
      const url = `${liveForm.getAttribute("action") || window.location.pathname}?${params}`;
      const request = ++latest;
      try {
        const response = await fetch(url, { headers: { "X-Requested-With": "fetch" } });
        if (!response.ok || request !== latest) return;
        const page = new DOMParser().parseFromString(await response.text(), "text/html");
        const fresh = page.getElementById("results");
        if (fresh) {
          results.innerHTML = fresh.innerHTML;
          window.history.replaceState(null, "", url);
        }
      } catch {
        // Offline or the server is busy: the normal Search button still works.
      }
    };
    liveForm.querySelectorAll("input[name='q']").forEach((input) => {
      input.addEventListener("input", () => {
        window.clearTimeout(timer);
        timer = window.setTimeout(refresh, 250);
      });
    });
    liveForm.querySelectorAll("select").forEach((select) => select.addEventListener("change", refresh));
  }

  document.querySelectorAll("form").forEach((form) => {
    form.addEventListener("submit", () => {
      const submit = form.querySelector('button[type="submit"]');
      if (!submit || submit.disabled) return;
      submit.dataset.originalText = submit.innerHTML;
      submit.disabled = true;
      submit.innerHTML = "Working…";
      window.setTimeout(() => {
        submit.disabled = false;
        if (submit.dataset.originalText) submit.innerHTML = submit.dataset.originalText;
      }, 8000);
    });
  });
});