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
    input.addEventListener("change", () => {
      const previous = input.parentElement.querySelector(".image-preview, .upload-error");
      if (previous) previous.remove();

      const file = input.files && input.files[0];
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