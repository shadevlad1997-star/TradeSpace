(() => {
  "use strict";
  const root = document.querySelector("[data-merchant-rate]");
  if (!root) return;

  const form = root.querySelector("[data-rate-form]");
  const fields = root.querySelector("[data-rate-fields]");
  const widget = root.querySelector("[data-rate-widget]");
  const value = root.querySelector("[data-rate-widget-value]");
  const formValue = root.querySelector("[data-rate-form-value]");
  const maximum = root.querySelector("[data-rate-maximum]");
  const amount = form.querySelector('[name="amount_usdt"]');
  const time = root.querySelector("[data-rate-time]");
  const warning = root.querySelector("[data-rate-warning]");
  const loading = root.querySelector("[data-rate-loading]");
  const panel = root.querySelector("[data-rate-panel]");
  const interval = 20000;
  let validUntil = 0, nextRequestAt = 0, pollTimer, expiryTimer, controller;
  let inFlight = false, stopped = false, submitting = false;

  // Decimal strings come from the server. No currency arithmetic runs here.
  const decimal = (text, positive = false) =>
    typeof text === "string" && /^\d+(\.\d+)?$/.test(text) &&
    Number.isFinite(Number(text)) && (positive ? Number(text) > 0 : Number(text) >= 0);

  const unavailable = () => {
    validUntil = 0;
    window.clearTimeout(expiryTimer);
    value.textContent = "Курс временно недоступен";
    formValue.textContent = "Недоступен";
    maximum.textContent = "Недоступен";
    const dialog = form.querySelector("dialog");
    if (dialog?.open) dialog.close();
    fields.disabled = true;
    form.removeAttribute("action");
    warning.hidden = false;
    panel.classList.add("is-unavailable");
  };

  const setFreshness = (seconds) => {
    window.clearTimeout(expiryTimer);
    validUntil = Date.now() + seconds * 1000;
    expiryTimer = window.setTimeout(unavailable, seconds * 1000);
  };

  const showTime = (iso) => {
    time.setAttribute("datetime", iso);
    time.textContent = new Intl.DateTimeFormat("ru-RU", {
      day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
    }).format(new Date(iso));
  };

  const applyQuote = (quote, elapsedSeconds) => {
    const remaining = Number(quote.valid_for_seconds) - elapsedSeconds;
    if (quote.source !== "rapira_live" || quote.stale !== false ||
        !decimal(quote.rate_rub, true) || !decimal(quote.fee_usdt) ||
        !decimal(quote.available_rub) || !decimal(quote.max_request_usdt) ||
        !Number.isFinite(remaining) || remaining <= 0 || remaining > 3600 ||
        !Number.isFinite(Date.parse(quote.fetched_at))) {
      throw new Error("Unavailable quote");
    }
    value.textContent = `1 USDT = ${quote.rate_rub} RUB`;
    formValue.textContent = `${quote.rate_rub} RUB`;
    root.querySelector("[data-rate-fee]").textContent = `${quote.fee_usdt} USDT`;
    root.querySelector("[data-rate-balance] .ts-money__value").textContent = quote.available_rub;
    maximum.textContent = `${quote.max_request_usdt} USDT`;
    amount.setAttribute("max", quote.max_request_usdt);
    showTime(quote.fetched_at);
    setFreshness(remaining);
    warning.hidden = true;
    panel.classList.remove("is-unavailable");
    fields.disabled = false;
    form.setAttribute("action", form.dataset.submitUrl);
  };

  const refresh = async () => {
    window.clearTimeout(pollTimer);
    if (stopped || submitting || document.hidden || inFlight) return;
    if (Date.now() < nextRequestAt) {
      pollTimer = window.setTimeout(refresh, nextRequestAt - Date.now());
      return;
    }
    inFlight = true;
    controller = new AbortController();
    const started = Date.now();
    nextRequestAt = started + interval;
    const timeout = window.setTimeout(() => controller.abort(), 8000);
    widget.setAttribute("aria-busy", "true");
    loading.hidden = false;
    try {
      const response = await fetch(root.dataset.rateUrl, {
        method: "GET", credentials: "same-origin", cache: "no-store", redirect: "error",
        headers: {Accept: "application/json"}, signal: controller.signal,
      });
      if ([401, 403, 409].includes(response.status)) stopped = true;
      if (!response.ok) throw new Error("Unavailable quote");
      const quote = await response.json();
      if (!submitting) applyQuote(quote, (Date.now() - started) / 1000);
    } catch (_error) {
      if (!submitting) unavailable();
    } finally {
      window.clearTimeout(timeout);
      inFlight = false;
      widget.setAttribute("aria-busy", "false");
      loading.hidden = true;
      if (!stopped && !submitting && !document.hidden) pollTimer = window.setTimeout(refresh, interval);
    }
  };

  form.addEventListener("submit", (event) => {
    if (!validUntil || Date.now() >= validUntil) {
      event.preventDefault();
      unavailable();
      return;
    }
    submitting = true;
    window.clearTimeout(pollTimer);
    window.clearTimeout(expiryTimer);
  }, true);

  const initialLifetime = Number(root.dataset.rateValidFor);
  if (root.dataset.rateAvailable === "true" && root.dataset.rateSource === "rapira_live" &&
      decimal(root.dataset.rateValue, true) && initialLifetime > 0 && initialLifetime <= 3600 &&
      Number.isFinite(Date.parse(root.dataset.rateFetchedAt))) {
    setFreshness(initialLifetime);
    showTime(root.dataset.rateFetchedAt);
  } else {
    unavailable();
  }
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) window.clearTimeout(pollTimer);
    else {
      if (Date.now() >= validUntil) unavailable();
      refresh();
    }
  });
  window.addEventListener("pagehide", () => {
    stopped = true;
    controller?.abort();
    window.clearTimeout(pollTimer);
    window.clearTimeout(expiryTimer);
  });
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) {
      stopped = false;
      submitting = false;
      unavailable();
      refresh();
    }
  });
  refresh();
})();
