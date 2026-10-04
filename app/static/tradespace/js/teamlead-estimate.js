// Read-only estimate. The settlement command obtains its own quote and balance.
(() => {
  "use strict";
  const display = document.querySelector("[data-teamlead-estimate]");
  if (!display) return;

  const interval = 20000;
  let timer, expiry, controller, inFlight = false, stopped = false;
  const unavailable = () => {
    display.textContent = "USDT: курс недоступен";
    window.clearTimeout(expiry);
  };
  const schedule = () => {
    window.clearTimeout(timer);
    if (!stopped && !document.hidden) timer = window.setTimeout(refresh, interval);
  };
  const refresh = async () => {
    if (stopped || document.hidden || inFlight) return;
    inFlight = true;
    controller = new AbortController();
    const started = Date.now();
    const timeout = window.setTimeout(() => controller.abort(), 8000);
    try {
      const response = await fetch(display.dataset.estimateUrl, {
        method: "GET", credentials: "same-origin", cache: "no-store", redirect: "error",
        headers: {Accept: "application/json"}, signal: controller.signal,
      });
      if ([401, 403].includes(response.status)) stopped = true;
      if (!response.ok) throw new Error("Estimate unavailable");
      const result = await response.json();
      const remaining = Number(result.valid_for_seconds) * 1000 - (Date.now() - started);
      if (result.source !== "rapira_live" || !/^\d+\.\d{2}$/.test(result.available_usdt) ||
          !Number.isFinite(remaining) || remaining <= 0 || remaining > 3600000) {
        throw new Error("Invalid estimate");
      }
      display.textContent = `≈ ${result.available_usdt} USDT после комиссии`;
      window.clearTimeout(expiry);
      expiry = window.setTimeout(unavailable, remaining);
    } catch (_error) {
      unavailable();
    } finally {
      window.clearTimeout(timeout);
      inFlight = false;
      schedule();
    }
  };
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) window.clearTimeout(timer);
    else refresh();
  });
  window.addEventListener("pagehide", () => {
    stopped = true;
    controller?.abort();
    window.clearTimeout(timer);
    window.clearTimeout(expiry);
  });
  refresh();
})();
