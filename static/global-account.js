(function () {
  if (window.__cvGlobalAccountInstalled) return;
  window.__cvGlobalAccountInstalled = true;
  if (document.body) {
    document.body.dataset.authState = "loading";
    document.body.dataset.authLoading = "true";
  }

  const supabaseUrl = window.CV_OPTIMISER_SUPABASE_URL || "";
  const supabaseAnonKey = window.CV_OPTIMISER_SUPABASE_ANON_KEY || "";
  const ACCOUNT_SNAPSHOT_KEY = "cv_account_snapshot";
  const ACCOUNT_SNAPSHOT_MAX_AGE_MS = 24 * 60 * 60 * 1000;
  let supabaseClient = null;
  let cachedAccountState = {
    signedIn: null,
    email: null,
    plan: null,
    token: null,
    planKnown: false,
    status: "loading"
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

  function accountFromSnapshot(snapshot) {
    if (!snapshot || typeof snapshot !== "object") return null;
    if (!snapshot.cached_at || Date.now() - Number(snapshot.cached_at) > ACCOUNT_SNAPSHOT_MAX_AGE_MS) return null;
    const signedIn = !!snapshot.signed_in;
    const plan = normalizePlan(snapshot.plan);
    return {
      signedIn: signedIn,
      email: snapshot.email || null,
      plan: signedIn ? plan : "free",
      token: null,
      planKnown: true,
      status: signedIn ? plan : "signed_out",
      fromSnapshot: true
    };
  }

  function readAccountSnapshot() {
    try {
      return accountFromSnapshot(JSON.parse(window.localStorage.getItem(ACCOUNT_SNAPSHOT_KEY) || "null"));
    } catch (error) {
      return null;
    }
  }

  function writeAccountSnapshot(account) {
    try {
      window.localStorage.setItem(ACCOUNT_SNAPSHOT_KEY, JSON.stringify({
        signed_in: !!account.signedIn,
        email: account.email || null,
        plan: normalizePlan(account.plan),
        cached_at: Date.now()
      }));
    } catch (error) {}
  }

  function clearAccountSnapshot() {
    try {
      window.localStorage.removeItem(ACCOUNT_SNAPSHOT_KEY);
    } catch (error) {}
  }

  function accountsDiffer(first, second) {
    if (!first || !second) return true;
    return !!first.signedIn !== !!second.signedIn ||
      (first.email || "") !== (second.email || "") ||
      normalizePlan(first.plan) !== normalizePlan(second.plan);
  }

  function signedOutState() {
    return {
      signedIn: false,
      email: null,
      plan: "free",
      token: null,
      planKnown: true,
      status: "signed_out"
    };
  }

  function loadingState() {
    return {
      signedIn: null,
      email: null,
      plan: null,
      token: null,
      planKnown: false,
      status: "loading"
    };
  }

  function signedInPlanPendingState(session, token) {
    return {
      signedIn: true,
      email: session && session.user && session.user.email ? session.user.email : null,
      plan: null,
      token: token || null,
      planKnown: false,
      status: "loading"
    };
  }

  function setInitialAuthLoadingUi() {
    const body = document.body;
    if (!body) return;
    const signInLink = document.getElementById("signInLink") || document.getElementById("headerSignInLink");
    const accountWrap = document.getElementById("accountMenuWrap");
    const upgradeLink = document.getElementById("upgradeLink");
    const placeholder = document.getElementById("authLoadingPlaceholder");

    body.dataset.authState = "loading";
    body.dataset.authLoading = "true";
    body.dataset.signedIn = "";
    body.dataset.accountPlan = "";
    body.dataset.authPlanPending = "false";

    if (signInLink) signInLink.classList.add("hidden");
    if (upgradeLink) {
      upgradeLink.classList.remove("hidden");
      upgradeLink.style.display = "";
      upgradeLink.style.visibility = "";
    }
    document.querySelectorAll("[data-upgrade-link]").forEach(function (el) {
      if (el.id === "upgradeLink") {
        el.classList.remove("hidden");
        el.style.display = "";
        el.style.visibility = "";
        return;
      }
      el.classList.add("hidden");
    });
    if (accountWrap) accountWrap.classList.add("hidden");
    if (placeholder) placeholder.classList.remove("hidden");
    closeHeaderAccountMenu();
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
    account = account || loadingState();
    const signInLink = document.getElementById("signInLink") || document.getElementById("headerSignInLink");
    const accountWrap = document.getElementById("accountMenuWrap");
    const accountEmail = document.getElementById("accountEmail");
    const accountPlan = document.getElementById("accountPlan") || document.getElementById("accountPlanText");
    const billingBtn = document.getElementById("menuManageSubBtn");
    const dropdown = document.getElementById("accountDropdown");
    const upgradeLink = document.getElementById("upgradeLink");
    const placeholder = document.getElementById("authLoadingPlaceholder");
    const planKnown = account.planKnown !== false && !!account.plan;
    const authState = account.signedIn
      ? (planKnown && account.plan === "pro" ? "pro" : (planKnown ? "free" : "loading"))
      : (account.signedIn === false ? "signed_out" : "loading");

    document.documentElement.dataset.accountPlan = account.plan || "";
    document.documentElement.dataset.signedIn = account.signedIn ? "true" : "false";
    document.body.dataset.authState = authState;
    document.body.dataset.accountPlan = account.plan || "";
    document.body.dataset.signedIn = account.signedIn ? "true" : "false";
    document.body.dataset.authLoading = authState === "loading" ? "true" : "false";
    document.body.dataset.authPlanPending = account.signedIn && !planKnown ? "true" : "false";

    document.querySelectorAll("[data-upgrade-link]").forEach(function (el) {
      if (el.id === "upgradeLink" && !planKnown) {
        el.classList.remove("hidden");
        el.style.display = "";
        el.style.visibility = "hidden";
        return;
      }
      if (el.id === "upgradeLink") {
        el.classList.remove("hidden");
        el.style.display = "";
        el.style.visibility = account.plan === "pro" ? "hidden" : "";
        return;
      }
      el.classList.toggle("hidden", account.plan === "pro" || !planKnown);
    });
    if (upgradeLink) {
      upgradeLink.classList.remove("hidden");
      upgradeLink.style.display = "";
      upgradeLink.style.visibility = account.plan === "pro" || !planKnown ? "hidden" : "";
    }
    if (placeholder) {
      placeholder.classList.toggle("hidden", authState !== "loading" || (account.signedIn && !planKnown));
    }

    if (!signInLink || !accountWrap || !accountEmail || !accountPlan) return;

    if (account.signedIn === false) {
      signInLink.classList.remove("hidden");
      signInLink.style.display = "";
      accountWrap.classList.add("hidden");
      accountWrap.style.display = "none";
      closeHeaderAccountMenu();
      return;
    }

    if (account.signedIn && !planKnown) {
      signInLink.classList.add("hidden");
      signInLink.style.display = "none";
      accountWrap.classList.remove("hidden");
      accountWrap.style.display = "";
      accountEmail.textContent = "Account";
      accountPlan.textContent = "Checking plan...";
      closeHeaderAccountMenu();
      if (billingBtn) billingBtn.classList.add("hidden");
      return;
    }

    signInLink.classList.add("hidden");
    signInLink.style.display = "none";
    accountWrap.classList.remove("hidden");
    accountWrap.style.display = "";
    accountEmail.textContent = account.email || "Signed in";
    accountPlan.textContent = account.plan === "pro" ? "Pro" : "Free";
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
      let account = signedInPlanPendingState(session, token);
      const snapshotFallback = cachedAccountState && cachedAccountState.fromSnapshot && cachedAccountState.signedIn
        ? Object.assign({}, cachedAccountState, { token: token })
        : null;

      try {
        const response = await fetch("/api/me", {
          headers: {
            Authorization: "Bearer " + token
          }
        });
        const data = await response.json();
        if (response.ok && !data.error) {
          const resolvedPlan = normalizePlan(data.plan || data.plan_state);
          account = {
            signedIn: !!data.signed_in,
            email: data.email || account.email,
            plan: resolvedPlan,
            token: data.signed_in ? token : null,
            planKnown: true,
            status: data.signed_in ? resolvedPlan : "signed_out"
          };
          writeAccountSnapshot(account);
        } else {
          account = snapshotFallback || signedInPlanPendingState(session, token);
        }
      } catch (error) {
        console.error("global account state error:", error);
        account = snapshotFallback || signedInPlanPendingState(session, token);
      }

      if (!account.signedIn) {
        cachedAccountState = signedOutState();
        clearAccountSnapshot();
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
    const opts = options || {};
    try {
      if (!opts.skipLoadingReset) {
        setInitialAuthLoadingUi();
      }
      const previousAccount = cachedAccountState;
      const account = await getAccountState(options);
      if (accountsDiffer(previousAccount, account) || !previousAccount.fromSnapshot) {
        applyHeaderAccountUi(account);
        dispatchAccountState(account);
      }
      console.log("GLOBAL_ACCOUNT_STATE", account);
      return account;
    } catch (error) {
      console.error("refreshGlobalAccountUi error:", error);
      const fallbackAccount = cachedAccountState && cachedAccountState.signedIn ? cachedAccountState : signedOutState();
      applyHeaderAccountUi(fallbackAccount);
      console.log("GLOBAL_ACCOUNT_STATE", fallbackAccount);
      dispatchAccountState(fallbackAccount);
      return fallbackAccount;
    }
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
    clearAccountSnapshot();
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
    const snapshotAccount = readAccountSnapshot();
    if (snapshotAccount) {
      cachedAccountState = snapshotAccount;
      applyHeaderAccountUi(snapshotAccount);
      dispatchAccountState(snapshotAccount);
    } else {
      setInitialAuthLoadingUi();
    }
    closeHeaderAccountMenu();
    await refreshGlobalAccountState({ forceRefresh: true, skipLoadingReset: !!snapshotAccount });
    const client = getSupabaseClient();
    if (client && !window.__cvGlobalAccountAuthListenerInstalled) {
      window.__cvGlobalAccountAuthListenerInstalled = true;
      client.auth.onAuthStateChange(function (event) {
        if (event === "SIGNED_OUT") {
          clearAccountSnapshot();
        }
        refreshGlobalAccountState({ forceRefresh: true });
      });
    }
  }

  window.getAccountState = getAccountState;
  window.getCachedAccountSnapshot = readAccountSnapshot;
  window.clearCachedAccountSnapshot = clearAccountSnapshot;
  window.refreshGlobalAccountUi = refreshGlobalAccountUi;
  window.refreshGlobalAccountState = refreshGlobalAccountState;
  window.closeGlobalAccountDropdown = closeHeaderAccountMenu;
  setInitialAuthLoadingUi();
  document.addEventListener("DOMContentLoaded", function () {
    bootstrapAccountUi();
  });
  window.addEventListener("pageshow", function () {
    const snapshotAccount = readAccountSnapshot();
    if (snapshotAccount) {
      cachedAccountState = snapshotAccount;
      applyHeaderAccountUi(snapshotAccount);
      dispatchAccountState(snapshotAccount);
    }
    refreshGlobalAccountState({ forceRefresh: true, skipLoadingReset: !!snapshotAccount });
  });
})();
