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
    const chip = document.getElementById("headerAccountChip");
    const menu = document.getElementById("headerAccountMenu");
    if (!chip || !menu) return;
    menu.classList.add("hidden");
    chip.setAttribute("aria-expanded", "false");
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
    const signInLink = document.getElementById("headerSignInLink");
    const accountWrap = document.getElementById("headerAccountWrap");
    const accountEmail = document.getElementById("headerAccountEmail");
    const accountPlan = document.getElementById("headerAccountPlan");
    const billingBtn = document.getElementById("headerBillingBtn");

    document.documentElement.dataset.accountPlan = account.plan;
    document.documentElement.dataset.signedIn = account.signedIn ? "true" : "false";

    document.querySelectorAll("[data-upgrade-link]").forEach(function (el) {
      el.classList.toggle("hidden", account.plan === "pro");
    });

    if (!signInLink || !accountWrap || !accountEmail || !accountPlan) return;

    if (!account.signedIn) {
      signInLink.classList.remove("hidden");
      accountWrap.classList.add("hidden");
      closeHeaderAccountMenu();
      return;
    }

    signInLink.classList.add("hidden");
    accountWrap.classList.remove("hidden");
    accountEmail.textContent = account.email || "Signed in";
    accountPlan.textContent = "Plan: " + (account.plan === "pro" ? "Pro" : "Free");
    if (billingBtn) {
      billingBtn.classList.toggle("hidden", account.plan !== "pro");
    }
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
    dispatchAccountState(account);
    return account;
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
    if (window.__cvGlobalAccountDropdownInstalled) return;
    window.__cvGlobalAccountDropdownInstalled = true;

    document.addEventListener("click", function (event) {
      const chip = event.target.closest("#headerAccountChip");
      const wrap = document.getElementById("headerAccountWrap");
      const menu = document.getElementById("headerAccountMenu");
      if (chip && menu) {
        const shouldOpen = menu.classList.contains("hidden");
        menu.classList.toggle("hidden");
        chip.setAttribute("aria-expanded", shouldOpen ? "true" : "false");
        if (shouldOpen) {
          hideHeaderBillingNote();
        }
        return;
      }

      const billingBtn = event.target.closest("#headerBillingBtn");
      if (billingBtn) {
        event.preventDefault();
        handleHeaderBilling();
        return;
      }

      const signOutBtn = event.target.closest("#headerSignOutBtn");
      if (signOutBtn) {
        event.preventDefault();
        handleHeaderSignOut();
        return;
      }

      const accountLink = event.target.closest("#headerAccountLink");
      if (accountLink) {
        closeHeaderAccountMenu();
        if (window.location.pathname === "/") {
          const authCard = document.getElementById("authCard");
          if (authCard) {
            event.preventDefault();
            authCard.scrollIntoView({ behavior: "smooth", block: "start" });
          }
        }
        return;
      }

      if (wrap && menu && !wrap.contains(event.target)) {
        closeHeaderAccountMenu();
      }
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        closeHeaderAccountMenu();
      }
    });
  }

  async function bootstrapAccountUi() {
    installHeaderDropdownHandlers();
    await refreshGlobalAccountUi();
    const client = getSupabaseClient();
    if (client && !window.__cvGlobalAccountAuthListenerInstalled) {
      window.__cvGlobalAccountAuthListenerInstalled = true;
      client.auth.onAuthStateChange(function () {
        refreshGlobalAccountUi({ forceRefresh: true });
      });
    }
  }

  window.getAccountState = getAccountState;
  window.refreshGlobalAccountUi = refreshGlobalAccountUi;
  window.addEventListener("DOMContentLoaded", function () {
    bootstrapAccountUi();
  });
})();
