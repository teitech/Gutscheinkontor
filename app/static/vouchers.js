// Code in die Zwischenablage kopieren. Funktioniert nur über HTTPS oder localhost.
document.addEventListener("DOMContentLoaded", function () {
  document.querySelectorAll("[data-copy]").forEach(function (button) {
    button.addEventListener("click", function () {
      var source = document.getElementById(button.getAttribute("data-copy"));
      if (!source || !navigator.clipboard || !navigator.clipboard.writeText) return;
      var original = button.textContent;
      navigator.clipboard.writeText(source.textContent.trim()).then(function () {
        button.textContent = button.getAttribute("data-done") || original;
        setTimeout(function () { button.textContent = original; }, 2000);
      });
    });
  });
});

// Formular: nur die Felder zur gewählten Art des Gutscheins zeigen.
document.addEventListener("DOMContentLoaded", function () {
  var select = document.getElementById("kind-select");
  if (!select) return;
  function update() {
    document.querySelectorAll("[data-kind]").forEach(function (el) {
      el.hidden = el.getAttribute("data-kind") !== select.value && el.getAttribute("data-kind2") !== select.value;
    });
  }
  select.addEventListener("change", update);
  update();
});
