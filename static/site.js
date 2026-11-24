(() => {
  const naira = new Intl.NumberFormat("en-NG", {
    style: "currency",
    currency: "NGN",
    maximumFractionDigits: 0,
  });

  const termLabels = {
    monthly: { period: "/ month", note: "Billed monthly" },
    six_months: { period: "/ 6 months", note: "Billed once every 6 months" },
    yearly: { period: "/ year", note: "Billed once a year" },
  };

  const buttons = document.querySelectorAll("[data-term]");
  const plans = document.querySelectorAll(".plan-card");

  function selectTerm(term) {
    buttons.forEach((button) => {
      button.classList.toggle("active", button.dataset.term === term);
    });

    plans.forEach((card) => {
      const price = card.querySelector("[data-price-monthly]");
      if (!price) return;
      const kobo = Number(price.getAttribute("data-price-" + term) || 0);
      price.textContent = naira.format(kobo / 100);
      const period = card.querySelector("[data-period]");
      if (period) period.textContent = termLabels[term].period;
      const sub = card.querySelector("[data-price-sub]");
      if (sub) sub.textContent = termLabels[term].note;
      const input = card.querySelector("[data-term-input]");
      if (input) input.value = term;
    });
  }

  buttons.forEach((button) => {
    button.addEventListener("click", () => selectTerm(button.dataset.term));
  });

  document.querySelectorAll("[data-copy]").forEach((button) => {
    button.addEventListener("click", async () => {
      const target = document.querySelector(button.dataset.copy);
      if (!target) return;
      const value = target.textContent.trim();
      try {
        await navigator.clipboard.writeText(value);
        const original = button.textContent;
        button.textContent = "Copied";
        setTimeout(() => {
          button.textContent = original;
        }, 1600);
      } catch {
        button.textContent = "Select and copy";
      }
    });
  });
})();
