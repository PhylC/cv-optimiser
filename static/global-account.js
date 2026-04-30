(function () {
  if (window.__cvGlobalAccountInstalled) return;
  window.__cvGlobalAccountInstalled = true;

  const supabaseUrl = window.CV_OPTIMISER_SUPABASE_URL || "";
  const supabaseAnonKey = window.CV_OPTIMISER_SUPABASE_ANON_KEY || "";
  const ACCOUNT_SNAPSHOT_KEY = "cv_account_snapshot";

  let supabaseClient = null;
  let inflightAuthState = null;
  let authState = {
    loading: true,
    user: null,
    plan: null,
    isPro: false,
    error: null,
    token: null
  };

  function getSupabaseClient() {
    if (supabaseClient) return supabaseClient;
    if (!window.supabase || !supabaseUrl || !supabaseAnonKey) return null;
    supabaseClient = window.supabase.createClient(supabaseUrl, supabaseAnonKey);
    return supabaseClient;
  }

  function clearAccountSnapshot() {
    try {
      window.localStorage.removeItem(ACCOUNT_SNAPSHOT_KEY);
    } catch (error) {}
  }

  function normalizePlanFromProfile(profile) {
    if (!profile) return null;
    const rawPlan = profile.plan || profile.plan_state;
    if (!rawPlan) return null;
    if (typeof rawPlan === "string") {
      const lowered = rawPlan.toLowerCase();
      if (lowered === "pro") return "pro";
      if (lowered === "free") return "free";
      return null;
    }
    if (typeof rawPlan === "object") {
      if (rawPlan.is_pro === true) return "pro";
      if (rawPlan.is_pro === false) return "free";
      if (typeof rawPlan.plan === "string") {
        const lowered = rawPlan.plan.toLowerCase();
        if (lowered === "pro") return "pro";
        if (lowered === "free") return "free";
      }
    }
    return null;
  }

  function accountForConsumers(state) {
    const user = state.user || null;
    return {
      loading: !!state.loading,
      signedIn: !!user,
      signed_in: !!user,
      email: user && user.email ? user.email : null,
      user: user,
      plan: state.plan,
      isPro: state.plan === "pro",
      is_pro: state.plan === "pro",
      planKnown: !!state.plan,
      status: state.loading ? "loading" : (!user ? "signed_out" : (state.plan || "unavailable")),
      error: state.error || null,
      token: state.token || null,
      profile: state.profile || null
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

  function setUpgradeVisibility(plan) {
    document.querySelectorAll("[data-upgrade-link]").forEach(function (el) {
      if (el.id === "upgradeLink") {
        el.classList.remove("hidden");
        el.style.display = "";
        el.style.visibility = plan === "pro" ? "hidden" : "";
        return;
      }
      el.classList.toggle("hidden", plan === "pro");
      el.style.display = plan === "pro" ? "none" : "";
    });
  }

  function accountInitial(email) {
    const value = (email || "").trim();
    if (!value) return "A";
    return value.charAt(0).toUpperCase();
  }

  function setHeaderCtaVisibility(plan) {
    document.querySelectorAll(".site-header-cta.header-cta").forEach(function (el) {
      el.classList.remove("hidden");
      el.style.display = "";
      el.style.visibility = plan === "pro" ? "hidden" : "";
    });
  }

  function applyHeaderAccountUi(state) {
    const body = document.body;
    const signInLink = document.getElementById("signInLink") || document.getElementById("headerSignInLink");
    const accountWrap = document.getElementById("accountMenuWrap");
    const accountEmail = document.getElementById("accountEmail");
    const accountAvatar = document.getElementById("accountAvatar");
    const accountPlan = document.getElementById("accountPlan") || document.getElementById("accountPlanText");
    const authLoadingText = document.getElementById("authLoadingText");
    const accountPillStatus = document.getElementById("accountPillStatus");
    const placeholder = document.getElementById("authLoadingPlaceholder");
    const user = state.user || null;
    const plan = state.plan || null;
    const stateName = state.loading ? "loading" : (!user ? "signed_out" : (plan || "unavailable"));

    if (body) {
      body.dataset.authState = stateName;
      body.dataset.authLoading = state.loading ? "true" : "false";
      body.dataset.authPlanPending = user && !plan ? "true" : "false";
      body.dataset.signedIn = user ? "true" : "false";
      body.dataset.accountPlan = plan || "";
    }
    document.documentElement.dataset.signedIn = user ? "true" : "false";
    document.documentElement.dataset.accountPlan = plan || "";

    if (placeholder) {
      placeholder.classList.add("hidden");
    }

    setUpgradeVisibility(plan);
    setHeaderCtaVisibility(plan);

    if (state.loading) {
      if (authLoadingText) {
        authLoadingText.textContent = "Checking account...";
        authLoadingText.classList.remove("hidden");
      }
      if (signInLink) {
        signInLink.classList.add("hidden");
        signInLink.style.display = "none";
      }
      if (accountWrap) {
        accountWrap.classList.add("hidden");
        accountWrap.style.display = "none";
      }
      closeHeaderAccountMenu();
      return;
    }

    if (!user) {
      if (authLoadingText) {
        authLoadingText.classList.add("hidden");
      }
      if (signInLink) {
        signInLink.classList.remove("hidden");
        signInLink.style.display = "";
      }
      if (accountWrap) {
        accountWrap.classList.add("hidden");
        accountWrap.style.display = "none";
      }
      closeHeaderAccountMenu();
      return;
    }

    if (signInLink) {
      signInLink.classList.add("hidden");
      signInLink.style.display = "none";
    }
    if (accountWrap) {
      accountWrap.classList.remove("hidden");
      accountWrap.style.display = "";
    }
    if (accountEmail) {
      accountEmail.textContent = user.email || "Signed in";
    }
    if (accountAvatar) {
      accountAvatar.textContent = accountInitial(user.email);
    }
    if (accountPillStatus) {
      accountPillStatus.textContent = "Signed in";
    }
    if (accountPlan) {
      accountPlan.textContent = plan === "pro" ? "PRO" : (plan === "free" ? "FREE" : "");
      accountPlan.classList.toggle("pro", plan === "pro");
      accountPlan.classList.toggle("free", plan === "free");
      accountPlan.classList.toggle("hidden", !plan);
    }
    if (authLoadingText) {
      authLoadingText.classList.add("hidden");
    }
    closeHeaderAccountMenu();
  }

  function setAuthState(nextState) {
    authState = Object.assign({
      loading: true,
      user: null,
      plan: null,
      isPro: false,
      error: null,
      token: null,
      profile: null
    }, nextState || {});
    authState.isPro = authState.plan === "pro";
    applyHeaderAccountUi(authState);
    document.dispatchEvent(new CustomEvent("cv-account-state-changed", {
      detail: {
        authState: authState,
        account: accountForConsumers(authState)
      }
    }));
    return authState;
  }

  async function resolveAuthState() {
    const client = getSupabaseClient();
    setAuthState({
      loading: true,
      user: null,
      plan: null,
      isPro: false,
      error: null,
      token: null,
      profile: null
    });

    if (!client) {
      console.log("auth user", undefined);
      console.log("profile response", null);
      console.log("resolved plan", null);
      return setAuthState({
        loading: false,
        user: null,
        plan: null,
        isPro: false,
        error: null,
        token: null,
        profile: null
      });
    }

    const sessionResult = await client.auth.getSession();
    const session = sessionResult && sessionResult.data ? sessionResult.data.session : null;
    const user = session && session.user ? session.user : null;
    const token = session && session.access_token ? session.access_token : null;
    console.log("auth user", user && user.email);

    if (!user || !token) {
      clearAccountSnapshot();
      console.log("profile response", null);
      console.log("resolved plan", null);
      return setAuthState({
        loading: false,
        user: null,
        plan: null,
        isPro: false,
        error: null,
        token: null,
        profile: null
      });
    }

    try {
      const response = await fetch("/api/me", {
        headers: {
          Authorization: "Bearer " + token
        }
      });
      const profile = await response.json();
      console.log("profile response", profile);

      if (!response.ok || profile.error) {
        console.log("resolved plan", null);
        return setAuthState({
          loading: false,
          user: { id: user.id || null, email: user.email || profile.email || null },
          plan: null,
          isPro: false,
          error: profile.error || profile.detail || "profile_unavailable",
          token: token,
          profile: profile
        });
      }

      const plan = normalizePlanFromProfile(profile);
      console.log("resolved plan", plan);
      return setAuthState({
        loading: false,
        user: { id: user.id || (profile.user && profile.user.id) || null, email: profile.email || user.email || null },
        plan: plan,
        isPro: plan === "pro",
        error: plan ? null : "profile_plan_unavailable",
        token: token,
        profile: profile
      });
    } catch (error) {
      console.error("global account state error:", error);
      console.log("profile response", null);
      console.log("resolved plan", null);
      return setAuthState({
        loading: false,
        user: { id: user.id || null, email: user.email || null },
        plan: null,
        isPro: false,
        error: error,
        token: token,
        profile: null
      });
    }
  }

  async function getAccountState(options) {
    const opts = options || {};
    if (inflightAuthState && !opts.forceRefresh) {
      return inflightAuthState;
    }

    inflightAuthState = resolveAuthState().then(function (state) {
      return accountForConsumers(state);
    }).finally(function () {
      inflightAuthState = null;
    });
    return inflightAuthState;
  }

  async function refreshGlobalAccountUi(options) {
    return getAccountState(Object.assign({}, options || {}, { forceRefresh: true }));
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
    if (account.plan !== "pro") {
      showHeaderBillingNote("Subscription management is available for Pro accounts.");
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
    clearAccountSnapshot();
    if (client) {
      await client.auth.signOut();
    }
    setAuthState({
      loading: false,
      user: null,
      plan: null,
      isPro: false,
      error: null,
      token: null,
      profile: null
    });
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

      if (els.dropdown && !event.target.closest("#accountDropdown")) {
        closeHeaderAccountMenu();
      }
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        closeHeaderAccountMenu();
      }
    });

    document.addEventListener("click", function (event) {
      if (event.target.closest("a")) {
        closeHeaderAccountMenu();
      }
    });
  }

  async function bootstrapAccountUi() {
    installHeaderDropdownHandlers();
    setAuthState({
      loading: true,
      user: null,
      plan: null,
      isPro: false,
      error: null,
      token: null,
      profile: null
    });
    closeHeaderAccountMenu();
    await refreshGlobalAccountState({ forceRefresh: true });
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
  window.getGlobalAuthState = function () { return authState; };
  window.clearCachedAccountSnapshot = clearAccountSnapshot;
  window.refreshGlobalAccountUi = refreshGlobalAccountUi;
  window.refreshGlobalAccountState = refreshGlobalAccountState;
  window.closeGlobalAccountDropdown = closeHeaderAccountMenu;

  setAuthState({
    loading: true,
    user: null,
    plan: null,
    isPro: false,
    error: null,
    token: null,
    profile: null
  });
  document.addEventListener("DOMContentLoaded", bootstrapAccountUi);
  window.addEventListener("pageshow", function () {
    refreshGlobalAccountState({ forceRefresh: true });
  });
})();
