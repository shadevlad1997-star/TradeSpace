(() => {
  "use strict";

  // Presentation only. The server remains authoritative for method validation.
  document.querySelectorAll(".ts-requisite-form, [data-requisite-form]").forEach((form) => {
    const method = form.querySelector("select[name='method']");
    if (!method) return;
    const groups = {
      value: ["sbp", "c2c", "mobile_commerce"],
      qr_mode: ["sbp_qr"], qr_image: ["sbp_qr"], bank_code: ["sbp", "c2c", "sbp_qr"],
      operator_code: ["mobile_commerce"], last4: ["c2c"],
      automation_id: ["sbp", "c2c", "mobile_commerce"]
    };
    const update = () => {
      const qr = method.value === "sbp_qr";
      const saved = form.querySelector("[data-qr-existing]")?.dataset.qrExisting === "true"
        || form.querySelector("input[name='qr_existing']")?.value === "true";
      Object.entries(groups).forEach(([name, methods]) => {
        const field = form.elements.namedItem(name);
        if (!field) return;
        const visible = methods.includes(method.value);
        field.closest("label").hidden = !visible;
        field.disabled = !visible;
        field.required = visible && (["value", "bank_code", "operator_code"].includes(name)
          || (name === "qr_image" && !saved));
      });
      const owner = form.elements.namedItem("full_name");
      if (owner) owner.required = qr;
    };
    const preview = form.querySelector('[data-qr-preview]');
    const image = form.elements.namedItem('qr_image');
    let generation = 0;
    image?.addEventListener('change', async () => {
      const current = ++generation;
      if (!preview) return;
      preview.hidden = !image.files.length;
      if (preview.hidden) return;
      preview.textContent = 'Проверяем QR…';
      const body = new FormData(form);
      try {
        const prefix = new URL(form.action).pathname.split('/requisites')[0];
        const response = await fetch(prefix + '/requisites/qr-preview', {method: 'POST', body, credentials: 'same-origin'});
        const result = await response.json();
        if (current === generation) preview.textContent = result.message || 'Не удалось проверить QR.';
      } catch (_) {
        if (current === generation) preview.textContent = 'Не удалось проверить QR. Проверка повторится при сохранении.';
      }
    });
    method.addEventListener("change", () => {generation++; if (preview) preview.hidden = true; update();});
    update();
  });

  const mobileSheet = document.getElementById("mobile-more-sheet");
  const mobileToggle = document.querySelector("[data-mobile-more]");
  const closeMobileSheet = () => {
    if (!mobileSheet || !mobileToggle) return;
    const wasOpen = !mobileSheet.hidden;
    mobileSheet.hidden = true;
    mobileToggle.setAttribute("aria-expanded", "false");
    document.body.style.overflow = "";
    if (wasOpen && window.matchMedia("(max-width: 767px)").matches) mobileToggle.focus();
  };
  if (mobileSheet && mobileToggle) {
    mobileToggle.addEventListener("click", () => {
      mobileSheet.hidden = false;
      mobileToggle.setAttribute("aria-expanded", "true");
      document.body.style.overflow = "hidden";
      const first = mobileSheet.querySelector("a, button:not(.ts-mobile-sheet__backdrop)");
      if (first) first.focus();
    });
    mobileSheet.addEventListener("keydown", (event) => {
      if (event.key !== "Tab") return;
      const controls = [...mobileSheet.querySelectorAll("a, button:not(.ts-mobile-sheet__backdrop)")]
        .filter((item) => !item.disabled);
      if (!controls.length) return;
      if (event.shiftKey && document.activeElement === controls[0]) {
        event.preventDefault(); controls[controls.length - 1].focus();
      } else if (!event.shiftKey && document.activeElement === controls[controls.length - 1]) {
        event.preventDefault(); controls[0].focus();
      }
    });
    window.matchMedia("(min-width: 768px)").addEventListener("change", (event) => {
      if (event.matches) closeMobileSheet();
    });
    mobileSheet.querySelectorAll("[data-mobile-more-close]").forEach((button) => {
      button.addEventListener("click", closeMobileSheet);
    });
  }

  const labelScrollableTables = () => {
    document.querySelectorAll(".ts-data-table-wrap").forEach((table) => {
      const scrollable = getComputedStyle(table).display !== "none" && table.scrollWidth > table.clientWidth + 2;
      if (scrollable) {
        table.tabIndex = 0;
        table.setAttribute("role", "region");
        const title = table.closest(".ts-section-block")?.querySelector("h2")?.textContent?.trim();
        table.setAttribute("aria-label", title ? `Таблица: ${title}` : "Таблица данных");
      } else {
        table.removeAttribute("tabindex");
        table.removeAttribute("role");
        table.removeAttribute("aria-label");
      }
    });
  };
  labelScrollableTables();
  window.addEventListener("resize", labelScrollableTables);

  document.querySelectorAll("[data-copy-target]").forEach((button) => {
    button.addEventListener("click", async () => {
      const id = button.getAttribute("data-copy-target");
      const value = id ? document.getElementById(id) : null;
      const result = button.closest("[data-copy-control]")?.querySelector(".ts-copy__result");
      if (!value) return;
      try {
        await navigator.clipboard.writeText(value.textContent.trim());
        if (result) result.textContent = "Скопировано";
      } catch (_error) {
        if (result) result.textContent = "Не удалось скопировать автоматически";
      }
    });
  });

  document.querySelectorAll("[data-reveal-target]").forEach((button) => {
    button.addEventListener("click", () => {
      const id = button.getAttribute("data-reveal-target");
      const value = id ? document.getElementById(id) : null;
      if (!value) return;
      const masked = value.classList.toggle("is-masked");
      button.textContent = masked ? "Показать" : "Скрыть";
      button.setAttribute("aria-pressed", String(!masked));
    });
  });

  document.querySelectorAll("[data-dialog-close]").forEach((button) => {
    button.addEventListener("click", () => button.closest("dialog")?.close());
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeMobileSheet();
  });

  // Native summary keeps mouse, touch, Enter and Space activation.
  const accountMenus = [...document.querySelectorAll(".ts-account, .ts-more-menu")];
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    accountMenus.filter((menu) => menu.open).forEach((menu) => {
      const returnFocus = menu.contains(document.activeElement);
      menu.open = false;
      if (returnFocus) menu.querySelector("summary")?.focus();
    });
  });
  document.addEventListener("click", (event) => {
    accountMenus.forEach((menu) => {
      if (menu.open && !menu.contains(event.target)) menu.open = false;
    });
  });
})();

(() => {
  "use strict";

  document.querySelectorAll("[data-dialog-open]").forEach((button) => {
    button.addEventListener("click", () => {
      const id = button.getAttribute("data-dialog-open");
      const dialog = id ? document.getElementById(id) : null;
      if (dialog && typeof dialog.showModal === "function") dialog.showModal();
    });
  });

  document.querySelectorAll("[data-copy-value]").forEach((button) => {
    button.addEventListener("click", async () => {
      const value = button.getAttribute("data-copy-value") || "";
      try {
        await navigator.clipboard.writeText(value);
        const old = button.textContent;
        button.textContent = "Скопировано";
        window.setTimeout(() => { button.textContent = old; }, 1400);
      } catch (_error) {
        button.textContent = "Не скопировано";
      }
    });
  });

  const formatCountdown = (seconds) => {
    const value = Math.max(0, Number(seconds) || 0);
    const minutes = Math.floor(value / 60);
    const rest = value % 60;
    if (minutes >= 60) {
      const hours = Math.floor(minutes / 60);
      return String(hours) + " ч " + String(minutes % 60) + " мин";
    }
    return String(minutes) + ":" + String(rest).padStart(2, "0");
  };

  document.querySelectorAll("[data-countdown]").forEach((element) => {
    let remaining = Number(element.getAttribute("data-countdown")) || 0;
    const render = () => {
      element.textContent = remaining > 0 ? formatCountdown(remaining) : "Срок истёк";
      element.classList.toggle("is-urgent", remaining > 0 && remaining <= 300);
    };
    render();
    if (remaining > 0) {
      window.setInterval(() => {
        remaining = Math.max(0, remaining - 1);
        render();
      }, 1000);
    }
  });

  document.querySelectorAll("[data-submit-once]").forEach((form) => {
    form.addEventListener("submit", () => {
      form.querySelectorAll("button[type='submit']").forEach((button) => {
        button.disabled = true;
        button.setAttribute("aria-busy", "true");
      });
    });
  });
})();
