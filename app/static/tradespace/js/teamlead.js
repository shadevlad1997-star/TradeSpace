// Presentation only: review user-entered values. The existing command owns quote and eligibility.
(() => {
  const trigger = document.querySelector('[data-dialog-open="teamlead-settlement-confirm"]');
  if (!trigger) return;
  trigger.addEventListener("click", (event) => {
    const form = trigger.closest("form");
    if (!form.reportValidity()) { event.stopImmediatePropagation(); return; }
    const dialog = document.getElementById("teamlead-settlement-confirm");
    dialog.querySelector("[data-review-amount]").textContent = form.elements.requested_usdt.value + " USDT";
    dialog.querySelector("[data-review-wallet]").textContent = form.elements.wallet_address.value;
  }, true);
})();
