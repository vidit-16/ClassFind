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