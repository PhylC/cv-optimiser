(function () {
  if (window.__cvGlobalAccountInstalled) return;
  window.__cvGlobalAccountInstalled = true;

  const supabaseUrl = window.CV_OPTIMISER_SUPABASE_URL || "";
  const supabaseAnonKey = window.CV_OPTIMISER_SUPABASE_ANON_KEY || "";
  let supabaseClient = null;
  let cachedAccountState = {
    signedIn: false,
    email: null,
    plan: "free",
    token: null
  };
  let inflightAccountState = null;

  function getSupabaseClient() {
    if (supabaseClient) return supabaseClient;
    if (!window.supabase || !supabaseUrl || !supabaseAnonKey) return null;
    supabaseClient = window.supabase.createClient(supabaseUrl, supabaseAnonKey);
    return supabaseClient;
  }

  function normalizePlan(plan) {
    if (!plan) return "free";
    if (typeof plan === "string") {
      return plan.toLowerCase() === "pro" ? "pro" : "free";
    }
    if (typeof plan === "object") {
      if (plan.is_pro) return "pro";
      if (typeof plan.plan === "string") {
        return plan.plan.toLowerCase() === "pro" ? "pro" : "free";
      }
    }
    return "free";
  }

  function signedOutState() {
    return {
      signedIn: false,
      email: null,
      plan: "free",
      token: null
    };
  }

  function closeHeaderAccountMenu() {
    const chip = document.getElementById("accountMenuButton");
    const menu = document.getElementById("accountDropdown");
    if (menu) {
      menu.classList.add("hidden");
      menu.setAttribute("aria-hidden", "true");
    }
    if (chip) {
      chip.setAttribute("aria-expanded", "false");
    }
  }

  function showHeaderBillingNote(message) {
    const note = document.getElementById("headerBillingNote");
    if (!note) return;
    note.textContent = message;
    note.classList.remove("hidden");
  }

  function hideHeaderBillingNote() {
    const note = document.getElementById("headerBillingNote");
    if (!note) return;
    note.classList.add("hidden");
  }

  function applyHeaderAccountUi(account) {
    const signInLink = document.getElementById("signInLink") || document.getElementById("headerSignInLink");
    const accountWrap = document.getElementById("accountMenuWrap");
    const accountEmail = document.getElementById("accountEmail");
    const accountPlan = document.getElementById("accountPlan") || document.getElementById("accountPlanText");
    const billingBtn = document.getElementById("menuManageSubBtn");
    const dropdown = document.getElementById("accountDropdown");

    document.documentElement.dataset.accountPlan = account.plan;
    document.documentElement.dataset.signedIn = account.signedIn ? "true" : "false";

    document.querySelectorAll("[data-upgrade-link]").forEach(function (el) {
      el.classList.toggle("hidden", account.plan === "pro");
    });

    if (!signInLink || !accountWrap || !accountEmail || !accountPlan) return;

    if (!account.signedIn) {
      signInLink.classList.remove("hidden");
      signInLink.style.display = "";
      accountWrap.classList.add("hidden");
      accountWrap.style.display = "none";
      closeHeaderAccountMenu();
      document.body.dataset.accountPlan = "free";
      document.body.dataset.signedIn = "false";
      return;
    }

    signInLink.classList.add("hidden");
    signInLink.style.display = "none";
    accountWrap.classList.remove("hidden");
    accountWrap.style.display = "";
    accountEmail.textContent = account.email || "Signed in";
    accountPlan.textContent = "Plan: " + (account.plan === "pro" ? "Pro" : "Free");
    if (dropdown) {
      dropdown.classList.add("hidden");
      dropdown.setAttribute("aria-hidden", "true");
    }
    const button = document.getElementById("accountMenuButton");
    if (button) {
      button.setAttribute("aria-expanded", "false");
    }
    if (billingBtn) {
      billingBtn.classList.toggle("hidden", account.plan !== "pro");
    }
    document.body.dataset.accountPlan = account.plan;
    document.body.dataset.signedIn = "true";
  }

  function dispatchAccountState(account) {
    document.dispatchEvent(
      new CustomEvent("cv-account-state-changed", {
        detail: { account: account }
      })
    );
  }

  async function getAccountState(options) {
    const opts = options || {};
    if (inflightAccountState && !opts.forceRefresh) {
      return inflightAccountState;
    }

    inflightAccountState = (async function () {
      const client = getSupabaseClient();
      if (!client) {
        cachedAccountState = signedOutState();
        return cachedAccountState;
      }

      const sessionResult = await client.auth.getSession();
      const session = sessionResult && sessionResult.data ? sessionResult.data.session : null;
      if (!session || !session.access_token) {
        cachedAccountState = signedOutState();
        return cachedAccountState;
      }

      const token = session.access_token;
      let account = {
        signedIn: true,
        email: session.user && session.user.email ? session.user.email : null,
        plan: "free",
        token: token
      };

      try {
        const response = await fetch("/api/me", {
          headers: {
            Authorization: "Bearer " + token
          }
        });
        const data = await response.json();
        if (response.ok && !data.error) {
          account = {
            signedIn: !!data.signed_in,
            email: data.email || account.email,
            plan: normalizePlan(data.plan || data.plan_state),
            token: data.signed_in ? token : null
          };
        }
      } catch (error) {
        console.error("global account state error:", error);
      }

      if (!account.signedIn) {
        cachedAccountState = signedOutState();
        return cachedAccountState;
      }

      cachedAccountState = account;
      return cachedAccountState;
    })();

    try {
      return await inflightAccountState;
    } finally {
      inflightAccountState = null;
    }
  }

  async function refreshGlobalAccountUi(options) {
    const account = await getAccountState(options);
    applyHeaderAccountUi(account);
    console.log("GLOBAL_ACCOUNT_STATE", account);
    dispatchAccountState(account);
    return account;
  }

  async function refreshGlobalAccountState(options) {
    return refreshGlobalAccountUi(options);
  }

  async function handleHeaderBilling() {
    const account = await getAccountState({ forceRefresh: true });
    closeHeaderAccountMenu();
    if (!account.signedIn || !account.token) {
      showHeaderBillingNote("Please sign in to manage your subscription.");
      return;
    }

    hideHeaderBillingNote();

    try {
      const response = await fetch("/api/create-billing-portal-session", {
        method: "POST",
        headers: {
          Authorization: "Bearer " + account.token
        }
      });
      const data = await response.json();
      if (response.ok && data.url) {
        window.location.href = data.url;
        return;
      }
      showHeaderBillingNote(data.detail || data.error || "Billing management is not available yet.");
    } catch (error) {
      console.error("billing portal error:", error);
      showHeaderBillingNote("Billing management is not available yet.");
    }
  }

  async function handleHeaderSignOut() {
    const client = getSupabaseClient();
    closeHeaderAccountMenu();
    if (!client) {
      window.location.href = "/";
      return;
    }

    await client.auth.signOut();
    cachedAccountState = signedOutState();
    applyHeaderAccountUi(cachedAccountState);
    dispatchAccountState(cachedAccountState);
    if (window.location.pathname === "/") {
      window.location.reload();
      return;
    }
    window.location.href = "/";
  }

  function installHeaderDropdownHandlers() {
    if (window.__accountDropdownInstalled) return;
    window.__accountDropdownInstalled = true;

    function getEls() {
      return {
        button: document.getElementById("accountMenuButton"),
        dropdown: document.getElementById("accountDropdown")
      };
    }

    function openDropdown() {
      const els = getEls();
      if (!els.dropdown) return;
      els.dropdown.classList.remove("hidden");
      els.dropdown.setAttribute("aria-hidden", "false");
      if (els.button) {
        els.button.setAttribute("aria-expanded", "true");
      }
    }

    function toggleDropdown(event) {
      event.preventDefault();
      event.stopPropagation();
      const els = getEls();
      if (!els.dropdown) return;
      if (els.dropdown.classList.contains("hidden")) {
        openDropdown();
      } else {
        closeHeaderAccountMenu();
      }
    }

    document.addEventListener("DOMContentLoaded", closeHeaderAccountMenu);
    window.addEventListener("load", closeHeaderAccountMenu);
    window.addEventListener("pageshow", closeHeaderAccountMenu);
    window.addEventListener("beforeunload", closeHeaderAccountMenu);

    document.addEventListener("click", function (event) {
      const els = getEls();
      if (!els.button || !els.dropdown) return;

      if (event.target.closest("#accountMenuButton")) {
        hideHeaderBillingNote();
        toggleDropdown(event);
        return;
      }

      const action = event.target.closest("[data-account-action]");
      if (action) {
        event.preventDefault();
        closeHeaderAccountMenu();
        const actionType = action.getAttribute("data-account-action");
        if (actionType === "account") {
          if (window.location.pathname === "/") {
            const authCard = document.getElementById("authCard");
            if (authCard) {
              authCard.scrollIntoView({ behavior: "smooth", block: "start" });
              return;
            }
          }
          window.location.href = "/#authCard";
          return;
        }
        if (actionType === "billing") {
          handleHeaderBilling();
          return;
        }
        if (actionType === "signout") {
          handleHeaderSignOut();
          return;
        }
      }

      if (!event.target.closest("#accountDropdown")) {
        closeHeaderAccountMenu();
      }
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        closeHeaderAccountMenu();
      }
    });

    document.addEventListener("click", function (event) {
      const link = event.target.closest("a");
      if (link) {
        closeHeaderAccountMenu();
      }
    });
  }

  async function bootstrapAccountUi() {
    installHeaderDropdownHandlers();
    closeHeaderAccountMenu();
    await refreshGlobalAccountState();
    const client = getSupabaseClient();
    if (client && !window.__cvGlobalAccountAuthListenerInstalled) {
      window.__cvGlobalAccountAuthListenerInstalled = true;
      client.auth.onAuthStateChange(function () {
        refreshGlobalAccountState({ forceRefresh: true });
      });
    }
  }

  window.getAccountState = getAccountState;
  window.refreshGlobalAccountUi = refreshGlobalAccountUi;
  window.refreshGlobalAccountState = refreshGlobalAccountState;
  window.closeGlobalAccountDropdown = closeHeaderAccountMenu;
  window.addEventListener("DOMContentLoaded", function () {
    bootstrapAccountUi();
  });
  window.addEventListener("pageshow", function () {
    refreshGlobalAccountState({ forceRefresh: true });
  });
})();
