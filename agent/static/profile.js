// Progressive enhancement only: the form submits and saves correctly with JS
// disabled. This adds/removes contraindication rows and the select-all toggle.

(function () {
  "use strict";

  const rows = document.getElementById("ci-rows");
  const addButton = document.getElementById("add-ci");

  function buildRow() {
    const kinds = Array.from(
      document.querySelectorAll('#ci-rows select[name="ci_kind"]')
    );
    // Clone an existing row's option list so the kinds stay in sync with the
    // server-rendered enum rather than being duplicated here.
    const options = kinds.length
      ? kinds[0].innerHTML
      : '<option value="">—</option>';

    const tr = document.createElement("tr");
    tr.innerHTML =
      '<td><select name="ci_kind">' + options + "</select></td>" +
      '<td><input type="text" name="ci_value" list="ci-values"></td>' +
      '<td><input type="text" name="ci_reason" placeholder="e.g. shoulder impingement"></td>' +
      '<td><input type="date" name="ci_expires"></td>' +
      '<td><button type="button" class="remove-row" aria-label="Remove">×</button></td>';
    // A freshly added row has no selection yet.
    const select = tr.querySelector("select");
    if (select) select.value = "";
    return tr;
  }

  if (addButton && rows) {
    addButton.addEventListener("click", function () {
      const row = buildRow();
      rows.appendChild(row);
      const input = row.querySelector('input[name="ci_value"]');
      if (input) input.focus();
    });

    // Delegated so it covers rows added after load.
    rows.addEventListener("click", function (event) {
      const button = event.target.closest(".remove-row");
      if (!button) return;
      const tr = button.closest("tr");
      if (tr) tr.remove();
    });
  }

  // The server always renders one trailing blank row, so there is nothing to seed
  // here — and cloning from it is what makes the "+ Add" button work on a fresh
  // profile that has no saved restrictions.

  document.querySelectorAll("[data-toggle-all]").forEach(function (button) {
    button.addEventListener("click", function () {
      const group = button.getAttribute("data-toggle-all");
      const boxes = document.querySelectorAll(
        '[data-group="' + group + '"] input[type="checkbox"]'
      );
      // If anything is unchecked, check everything; otherwise clear it.
      const shouldCheck = Array.prototype.some.call(boxes, function (b) {
        return !b.checked;
      });
      boxes.forEach(function (b) {
        b.checked = shouldCheck;
      });
    });
  });

  // Ticking a goal shouldn't require also remembering to set its priority, but an
  // unticked goal's priority select is meaningless — dim it for clarity.
  function syncGoalRows() {
    document.querySelectorAll(".goal-row").forEach(function (row) {
      const box = row.querySelector('input[type="checkbox"]');
      const select = row.querySelector("select");
      if (!box || !select) return;
      select.disabled = !box.checked;
      row.classList.toggle("inactive", !box.checked);
    });
  }

  document.querySelectorAll('.goal-row input[type="checkbox"]').forEach(function (box) {
    box.addEventListener("change", syncGoalRows);
  });
  syncGoalRows();
})();
