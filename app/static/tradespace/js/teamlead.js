// Presentation only: review user-entered values. The existing command owns quote and eligibility.
(() => {
  const formatUsdt = (raw) => {
    const decimal = /^(\d+)(?:\.(\d*))?$/.exec(raw);
    if (!decimal) return Number(raw).toFixed(2);
    const fraction = (decimal[2] || "").padEnd(3, "0");
    let cents = BigInt(decimal[1]) * 100n + BigInt(fraction.slice(0, 2));
    const remainder = fraction.slice(2);
    if (remainder[0] > "5" || (remainder[0] === "5" &&
        (/[1-9]/.test(remainder.slice(1)) || cents % 2n === 1n))) cents += 1n;
    return `${cents / 100n}.${String(cents % 100n).padStart(2, "0")}`;
  };
  const trigger = document.querySelector('[data-dialog-open="teamlead-settlement-confirm"]');
  if (!trigger) return;
  trigger.addEventListener("click", (event) => {
    const form = trigger.closest("form");
    if (!form.reportValidity()) { event.stopImmediatePropagation(); return; }
    const dialog = document.getElementById("teamlead-settlement-confirm");
    dialog.querySelector("[data-review-amount]").textContent = formatUsdt(form.elements.requested_usdt.value) + " USDT";
    dialog.querySelector("[data-review-wallet]").textContent = form.elements.wallet_address.value;
  }, true);
})();
