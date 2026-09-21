document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".flash").forEach((flash) => {
    window.setTimeout(() => {
      flash.style.transition = "opacity .35s ease";
      flash.style.opacity = "0";
      window.setTimeout(() => flash.remove(), 400);
    }, 4500);
  });
});
