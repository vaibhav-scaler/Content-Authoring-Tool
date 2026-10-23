// ==============================================================================
// SCALER AI SCRIPT STUDIO - COMPLETE CLIENT LOGIC (ES6 - ZERO EMOJIS)
// ==============================================================================

// Every request to this app's API carries who is signed in, so the server can credit each action to the right person
(function () {
  const nativeFetch = window.fetch.bind(window);
  window.fetch = function (input, init) {
    try {
      const url = typeof input === "string" ? input : (input && input.url) || "";
      if (url.startsWith("/api/") || url.includes("/api/")) {
        const who = (typeof currentUser !== "undefined" && currentUser) ? currentUser.id : null;
        if (who !== null && who !== undefined) {
          init = init || {};
          const headers = new Headers(init.headers || (typeof input !== "string" && input.headers) || {});
          if (!headers.has("X-Actor-Id")) headers.set("X-Actor-Id", String(who));
          init.headers = headers;
        }
      }
    } catch (e) { /* never block a request over tracking */ }
    return nativeFetch(input, init);
  };
})();

const API_BASE = (typeof window !== "undefined" && window.location.origin && window.location.origin.startsWith("http"))
  ? `${window.location.origin}/api`
  : "http://127.0.0.1:8002/api";

// State
let currentScriptId = null;
let currentShareableUuid = null;
let currentOutlineCards = [];
let currentLoadedCards = [];
let currentLoadedScript = null;

// Users and Roles Governance State (PRD Users & Roles)
let allUsers = [];
let currentUser = null;
let allSystemPrograms = [];

// Script endpoints check who is acting (program membership, read-only roles), so every call to them carries the
// signed-in user. Done once here rather than at each of the call sites.
(function addUserToScriptCalls() {
  const nativeFetch = window.fetch.bind(window);
  window.fetch = (input, init) => {
    if (typeof input === "string" && currentUser && input.startsWith(`${API_BASE}/scripts`) && !/[?&](user_id|actor_id)=/.test(input)) {
      input += (input.includes("?") ? "&" : "?") + `user_id=${currentUser.id}`;
    }
    return nativeFetch(input, init);
  };
})();

// Records what a signed-in person touches on the screens: buttons and links they click, filters they change, searches they type. Sent in small
// batches. The server turns the user id into the name and role, and accepts only these three kinds of entry.
(function trackInteractions() {
  const queue = [];
  let timer = null, lastKey = "", lastAt = 0;
  const SCREENS = [["adminDashboardView", "Admin"], ["moDashboardView", "Module Owner"], ["pmDashboardView", "Program Manager"], ["smeDashboardView", "SME"], ["homeDashboardView", "Home"]];
  const ADMIN_TABS = { adminTabUsers: "Users", adminTabCurriculum: "Curriculum", adminTabScripts: "Generated scripts", adminTabActivities: "User activity" };
  const SME_VIEWS = [["studioView", "script studio"], ["smeInputsView", "script inputs"], ["smeScriptsDedicatedView", "scripts list"], ["smeCurriculumDedicatedView", "curriculum"], ["smeProgramsLandingView", "programs"]];

  const visible = (el) => el && !el.classList.contains("hidden") && el.offsetParent !== null;
  function where() {
    const screen = SCREENS.find(([id]) => visible(document.getElementById(id)));
    if (!screen) return "";
    if (screen[0] === "adminDashboardView") {
      const tab = Object.keys(ADMIN_TABS).find(id => visible(document.getElementById(id)));
      return tab ? `Admin, ${ADMIN_TABS[tab]}` : "Admin";
    }
    if (screen[0] === "smeDashboardView") {
      const v = SME_VIEWS.find(([id]) => visible(document.getElementById(id)));
      return v ? `SME, ${v[1]}` : "SME";
    }
    return screen[1];
  }
  function label(el) {
    const raw = el.getAttribute("aria-label") || el.getAttribute("title") || el.innerText || el.textContent || el.value || "";
    return raw.replace(/\s+/g, " ").trim().slice(0, 60);
  }
  function push(type, text) {
    if (!currentUser) return;
    const key = type + text, now = Date.now();
    if (key === lastKey && now - lastAt < 1500) return;
    lastKey = key; lastAt = now;
    localStorage.setItem("scaler_last_seen", String(now));              // being active keeps the session "current", so only a real break counts as coming back
    queue.push({ action_type: type, description: text.slice(0, 280) });
    if (!timer) timer = setTimeout(flush, 2500);
  }
  function flush() {
    timer = null;
    if (!queue.length || !currentUser) { queue.length = 0; return; }
    const items = queue.splice(0, 50);
    fetch(`${API_BASE}/activities/batch`, { method: "POST", headers: { "Content-Type": "application/json" }, keepalive: true,
      body: JSON.stringify({ actor_id: currentUser.id, items }) }).catch(() => {});
    if (queue.length) timer = setTimeout(flush, 500);
  }
  window.addEventListener("pagehide", () => {
    if (queue.length && currentUser && navigator.sendBeacon) {
      navigator.sendBeacon(`${API_BASE}/activities/batch`, new Blob([JSON.stringify({ actor_id: currentUser.id, items: queue.splice(0, 50) })], { type: "application/json" }));
    }
  });

  // The activity tab's own controls are left out, so looking at the log does not fill the log
  const ignore = (el) => !!el.closest("#adminTabActivities, .kmenu-list .kmenu-item.nolog");
  document.addEventListener("click", (e) => {
    if (!e.isTrusted) return;                              // a click made by the page itself (a download link, for example) is not the person
    const el = e.target.closest && e.target.closest("button, a[href], [role='tab'], .admin-tab-btn, .program-subtab-btn, .track-subtab-btn, summary, label.btn, input[type='checkbox'], input[type='radio']");
    if (!el || ignore(el) || el.disabled) return;
    const name = label(el) || el.id || "a control";
    const place = where();
    push("Click", `Clicked "${name}"${place ? ` (on ${place})` : ""}`);
  }, true);
  document.addEventListener("change", (e) => {
    const el = e.target;
    if (!el || ignore(el)) return;
    if (el.tagName === "SELECT") {
      const own = el.closest("label") ? [...el.closest("label").childNodes].filter(n => n.nodeType === 3).map(n => n.textContent).join(" ").trim() : "";
      const byId = el.id.replace(/^admin(Scripts|Activity)?/, "").replace(/Filter$/, "").replace(/([a-z])([A-Z])/g, "$1 $2").trim();      // adminScriptsMoFilter -> "Mo"
      const field = own || (document.querySelector(`label[for="${el.id}"]`)?.innerText) || ({ "Mo": "Module Owner", "Period": "Period", "Type": "Type" })[byId] || byId || el.getAttribute("title") || "a filter";
      const chosen = el.options && el.options[el.selectedIndex] ? el.options[el.selectedIndex].text.trim() : el.value;
      const place = where();
      push("Filter Changed", `Set ${field.replace(/\s+/g, " ").trim().slice(0, 40)} to "${chosen.slice(0, 60)}"${place ? ` (on ${place})` : ""}`);
    } else if (el.type === "date") {
      push("Filter Changed", `Picked the date ${el.value || "(cleared)"}${where() ? ` (on ${where()})` : ""}`);
    }
  }, true);
  const typed = new WeakMap();
  document.addEventListener("input", (e) => {
    const el = e.target;
    if (!el || el.tagName !== "INPUT" || el.type === "password" || ignore(el)) return;
    if (!/search|filter/i.test(`${el.id} ${el.placeholder || ""} ${el.className}`)) return;
    clearTimeout(typed.get(el));
    typed.set(el, setTimeout(() => { if (el.value.trim()) push("Search", `Searched for "${el.value.trim().slice(0, 60)}"${where() ? ` (on ${where()})` : ""}`); }, 1500));
  }, true);
})();

// The admin lists stay current without a manual refresh: every minute while the tab is open and the window is showing, and straight away when the
// window is brought back. "Today" is worked out each time, so a page left open overnight moves on to the new day by itself.
(function keepAdminDataFresh() {
  // Every 15 seconds the page peeks at the newest activity entry. A new one means someone just did something, so the open list is re-read at once.
  // With nothing new it still refreshes about once a minute, so a missed signal never leaves the page stale for long.
  let lastRun = 0, lastActivityId = 0, busy = false;
  async function tick(force) {
    if (busy || document.hidden || !currentUser || currentUser.role !== "admin") return;
    const admin = document.getElementById("adminDashboardView");
    if (!admin || admin.classList.contains("hidden")) return;
    const open = (id) => { const el = document.getElementById(id); return el && !el.classList.contains("hidden"); };
    const onActivity = open("adminTabActivities"), onScripts = open("adminTabScripts");
    if (!onActivity && !onScripts) return;
    busy = true;
    try {
      let changed = !!force;
      try {
        const r = await fetch(`${API_BASE}/activities?limit=1`);
        const rows = r.ok ? await r.json() : [];
        const id = rows.length ? rows[0].id : 0;
        if (lastActivityId && id !== lastActivityId) changed = true;
        lastActivityId = id;
      } catch (e) { /* fall back to the once-a-minute refresh */ }
      if (changed || Date.now() - lastRun > 60000) {
        lastRun = Date.now();
        if (onActivity) await loadUserActivities(true); else await loadAdminScriptsTable(true);
      }
    } finally { busy = false; }
  }
  setInterval(() => tick(false), 15000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden && Date.now() - lastRun > 15000) tick(true); });
  window.addEventListener("focus", () => { if (Date.now() - lastRun > 15000) tick(true); });
})();

// ---- one watcher for every signed-in screen: every 15 seconds it checks the newest activity entry, and when someone has done something it tells the
// screens that are open what kind of thing happened, so each one re-reads only its own data (quietly, with no loading overlay)
const liveHub = (() => {
  let last = null, busy = false;
  const subs = [];
  async function tick() {
    if (busy || document.hidden || !currentUser) return;
    busy = true;
    try {
      const head = await fetch(`${API_BASE}/activities?limit=1`);
      if (!head.ok) return;
      const rows = await head.json();
      const newest = rows.length ? rows[0].id : 0;
      if (last === null) { last = newest; return; }
      if (newest === last) return;
      const res = await fetch(`${API_BASE}/activities?since_id=${last}&limit=300`);
      const arrived = res.ok ? await res.json() : [];
      last = newest;
      subs.forEach(fn => { try { fn(arrived); } catch (e) { /* one screen failing must not stop the others */ } });
    } catch (e) { /* the next check tries again */ } finally { busy = false; }
  }
  fetch(`${API_BASE}/activities?limit=1`).then(r => r.ok ? r.json() : []).then(rows => { if (last === null) last = rows.length ? rows[0].id : 0; }).catch(() => {});     // the starting point is the moment the page loaded
  setInterval(tick, 15000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });
  return { on: (fn) => subs.push(fn), tick };
})();

liveHub.on(async (arrived) => {
  const hit = (re) => arrived.some(a => re.test(a.action_type || ""));
  const open = (id) => { const el = document.getElementById(id); return !!el && !el.classList.contains("hidden"); };
  const CURRICULUM = /^(Class|Module|Track|Program|Syllabus)/i;
  const SCRIPTS = /script|GTG|sign-off|outline|cards|card|quiz|review|changes|submitted|visual|setup|assignment/i;
  if (hit(/outline|cards|ai review|card revised|quiz regenerated|visual regenerated|setup checklist|details filled|assignments|script initialized|chat/i)) loadApiUsageData();     // the cost badge
  if (hit(/GTG|sign-off|Approved Script Edited/i)) {                     // an approval changes the turnaround figures
    if (open("adminTabTat")) renderTatPanel("tatAdmin", true);
    if (open("pmPane-turnaround")) renderTatPanel("tatPm", true);
    if (open("moPane-turnaround")) renderTatPanel("tatMo", true);
  }
  if (currentUser.role === "admin" && open("adminDashboardView")) {
    if (open("adminTabUsers") && hit(/^(User|Program|Track|Module|Class)/i)) renderAdminUsersTable();
    if (open("adminTabCurriculum") && hit(CURRICULUM)) loadAdminCurriculumArchitecture(true);
  }
  if (currentUser.role === "sme" && open("smeDashboardView")) {
    if (open("smeProgramsLandingView") && (hit(SCRIPTS) || hit(CURRICULUM))) renderSmeProgramCards(currentUser, true);
    if (open("smeCurriculumDedicatedView") && hit(CURRICULUM)) loadSmeCurriculum(true, true);
  }
});

// Initialize App
document.addEventListener("DOMContentLoaded", async () => {
  initTheme();
  setupEventListeners();
  await initUsersAndRoles();
});

// Theme Management (Light by Default, Switchable to Dark)
function initTheme() {
  const savedTheme = localStorage.getItem("scaler_theme_preference") || "light";
  applyTheme(savedTheme);
}

function applyTheme(theme) {
  const themeBtn = document.getElementById("themeToggleBtn");
  if (theme === "dark") {
    document.body.classList.remove("light-theme");
    document.body.classList.add("dark-theme");
    if (themeBtn) themeBtn.textContent = "Switch to Light";
    if (window.mermaid) {
      mermaid.initialize({
        startOnLoad: false,
        theme: "dark",
        securityLevel: "strict",
        themeVariables: { fontSize: "18px" },
        flowchart: { useMaxWidth: true, nodeSpacing: 50, rankSpacing: 60, padding: 16 }
      });
    }
  } else {
    document.body.classList.remove("dark-theme");
    document.body.classList.add("light-theme");
    if (themeBtn) themeBtn.textContent = "Switch to Dark";
    if (window.mermaid) {
      mermaid.initialize({
        startOnLoad: false,
        theme: "default",
        securityLevel: "strict",
        themeVariables: { fontSize: "18px" },
        flowchart: { useMaxWidth: true, nodeSpacing: 50, rankSpacing: 60, padding: 16 }
      });
    }
  }
  localStorage.setItem("scaler_theme_preference", theme);
}

function toggleTheme() {
  const isDark = document.body.classList.contains("dark-theme");
  applyTheme(isDark ? "light" : "dark");
}

// Universal Loader Helper
function showLoader(message = "AI Multi-Agent Collaboration in Progress...") {
  const loader = document.getElementById("universalLoader");
  const msg = document.getElementById("loaderMessage");
  if (msg) msg.textContent = message;
  if (loader) loader.classList.remove("hidden");
}

function hideLoader() {
  const loader = document.getElementById("universalLoader");
  if (loader) loader.classList.add("hidden");
}

// Event Listeners
function setupEventListeners() {
  // Theme Toggle (Upper One Working for All)
  const themeBtn = document.getElementById("themeToggleBtn");
  if (themeBtn) {
    themeBtn.onclick = toggleTheme;
  }

  // Cascading Selectors
  document.getElementById("programSelect").addEventListener("change", onProgramChange);
  document.getElementById("trackSelect").addEventListener("change", onTrackChange);
  document.getElementById("moduleSelect").addEventListener("change", onModuleChange);
  document.getElementById("classSelect").addEventListener("change", onClassChange);

  // Sliders
  const mixSlider = document.getElementById("mixSlider");
  const mixVal = document.getElementById("mixVal");
  mixSlider.addEventListener("input", (e) => {
    mixVal.textContent = `${e.target.value}% Beg / ${100 - e.target.value}% Adv`;
    e.target.classList.remove("gen-unset"); document.getElementById("mixError")?.classList.add("hidden");
  });

  const codeSlider = document.getElementById("codeRatioSlider");
  const codeVal = document.getElementById("codeRatioVal");
  codeSlider.addEventListener("input", (e) => {
    codeVal.textContent = `${e.target.value}% Code / ${100 - e.target.value}% Theory`;
    e.target.classList.remove("gen-unset"); document.getElementById("codeError")?.classList.add("hidden");
  });

  const durationSelect = document.getElementById("durationSelect");
  const customDurationInput = document.getElementById("customDurationInput");
  const durationVal = document.getElementById("durationVal");

  if (durationSelect) {
    durationSelect.addEventListener("change", (e) => {
      const val = e.target.value;
      if (val) {
        if (durationVal) durationVal.textContent = `${val} mins`;
        if (customDurationInput) customDurationInput.value = val;
      } else {
        if (durationVal) durationVal.textContent = "Select duration";
        if (customDurationInput) customDurationInput.value = "";
      }
    });
  }

  // Prev Script File Upload
  const prevFileInput = document.getElementById("prevScriptFileInput");
  if (prevFileInput) {
    prevFileInput.addEventListener("change", (e) => {
      const file = e.target.files[0];
      if (file) {
        const reader = new FileReader();
        reader.onload = (evt) => {
          const up = document.getElementById("prevScriptUpload");
          if (up) up.value = evt.target.result;
        };
        reader.readAsText(file);
      }
    });
  }

  // Action Buttons
  document.getElementById("initScriptBtn")?.addEventListener("click", onInitScript);
  document.getElementById("outlineGtgBtn")?.addEventListener("click", onOutlineGtg);
  document.getElementById("outlineRethinkBtn")?.addEventListener("click", onOutlineRethink);
  document.getElementById("draftAllBtn")?.addEventListener("click", onDraftAllRemaining);
  document.getElementById("exportMdBtn")?.addEventListener("click", onExportMarkdown);
  document.getElementById("assignmentsBtn")?.addEventListener("click", onGenerateAssignments);
  document.getElementById("runReviewBtn")?.addEventListener("click", onRunReview);
  document.getElementById("moGtgBtn")?.addEventListener("click", onMoSignOff);
  document.getElementById("shareLinkBtn")?.addEventListener("click", onShareLink);

  // Modal Closers
  document.getElementById("closeEditModalBtn").addEventListener("click", () => {
    document.getElementById("cardEditModal").classList.add("hidden");
  });
  document.getElementById("closeChatModalBtn").addEventListener("click", () => {
    document.getElementById("chatAssistModal").classList.add("hidden");
  });
  document.getElementById("closeAssignmentsModalBtn").addEventListener("click", () => {
    document.getElementById("assignmentsModal").classList.add("hidden");
  });

  // Role Workspace Switcher (PRD Users & Roles)
  const roleSelect = document.getElementById("activeRoleSelector");
  if (roleSelect) roleSelect.addEventListener("change", onRoleChange);

  // MO Dashboard Listeners
  const moScopeFilter = document.getElementById("moScopeProgramFilter");
  if (moScopeFilter) moScopeFilter.addEventListener("change", loadMoDashboardData);
  const refreshMoDashBtn = document.getElementById("refreshMoDashboardBtn");
  if (refreshMoDashBtn) refreshMoDashBtn.addEventListener("click", loadMoDashboardData);

  // PM Dashboard Listeners
  const pmProgFilter = document.getElementById("pmProgramFilterSelect");
  if (pmProgFilter) pmProgFilter.addEventListener("change", () => filterPmCoverageTable());
  const pmSearchInput = document.getElementById("pmClassSearchInput");
  if (pmSearchInput) pmSearchInput.addEventListener("input", filterPmCoverageTable);
  const refreshPmDashBtn = document.getElementById("refreshPmDashboardBtn");
  if (refreshPmDashBtn) refreshPmDashBtn.addEventListener("click", loadPmDashboardData);

  // Admin Dashboard Listeners
  const refreshAdminDashBtn = document.getElementById("refreshAdminDashboardBtn");
  if (refreshAdminDashBtn) refreshAdminDashBtn.addEventListener("click", loadAdminDashboardData);

  // Modal Submissions
  document.getElementById("saveCardEditBtn").addEventListener("click", onSaveCardEdit);
  document.getElementById("submitChatAssistBtn").addEventListener("click", onSubmitChatAssist);

  // MO Curriculum Modal Listeners
  const saveMoClassBtn = document.getElementById("saveMoClassBtn");
  if (saveMoClassBtn) saveMoClassBtn.addEventListener("click", onSaveMoClass);
  const deleteMoClassBtn = document.getElementById("deleteMoClassBtn");
  if (deleteMoClassBtn) deleteMoClassBtn.addEventListener("click", onDeleteMoClass);
  const closeMoEditClassBtn = document.getElementById("closeMoEditClassBtn");
  if (closeMoEditClassBtn) closeMoEditClassBtn.addEventListener("click", () => {
    document.getElementById("moEditClassModal").classList.add("hidden");
  });
  const closeMoModalBtn = document.getElementById("closeMoModalBtn");
  if (closeMoModalBtn) closeMoModalBtn.addEventListener("click", () => {
    document.getElementById("moCurriculumModal").classList.add("hidden");
  });

  // User Management Modal Listeners
  const saveUserBtn = document.getElementById("saveUserBtn");
  if (saveUserBtn) saveUserBtn.addEventListener("click", onSaveUser);
  const closeUserModalBtn = document.getElementById("closeUserManageModalBtn");
  if (closeUserModalBtn) closeUserModalBtn.addEventListener("click", () => {
    document.getElementById("userManageModal").classList.add("hidden");
  });
  const cancelUserBtn = document.getElementById("cancelUserManageBtn");
  if (cancelUserBtn) cancelUserBtn.addEventListener("click", () => {
    document.getElementById("userManageModal").classList.add("hidden");
  });

  // Close profile dropdown on outside click
  document.addEventListener("click", (e) => {
    const menu = document.getElementById("userProfileDropdown");
    const btn = document.getElementById("userProfileBtn");
    if (menu && !menu.classList.contains("hidden")) {
      if (!menu.contains(e.target) && !btn?.contains(e.target)) {
        menu.classList.add("hidden");
      }
    }
  });
}

// ==============================================================================
// 0. USERS AND ROLES GOVERNANCE ENGINE (PRD USERS & ROLES)
// ==============================================================================

function getRoleLabel(role) {
  switch (role) {
    case "sme": return "SME / Author";
    case "mo": return "Module Owner (MO)";
    case "pm": return "Program Manager (PM)";
    case "admin": return "Admin";
    default: return role;
  }
}

function getInitials(name) {
  if (!name) return "U";
  const parts = name.trim().split(/\s+/);
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase();
}

function renderUserProfileMenu(user) {
  if (!user) return;

  const initials = getInitials(user.name);
  const roleLabel = getRoleLabel(user.role);

  // Update Navbar profile pill
  const avatarCircle = document.getElementById("userAvatarCircle");
  if (avatarCircle) avatarCircle.textContent = initials;

  const displayName = document.getElementById("userDisplayName");
  if (displayName) displayName.textContent = user.name;

  const roleBadge = document.getElementById("userRoleBadge");
  if (roleBadge) roleBadge.textContent = roleLabel;

  // Update Dropdown header
  const dropAvatar = document.getElementById("dropdownAvatarCircle");
  if (dropAvatar) dropAvatar.textContent = initials;

  const dropName = document.getElementById("dropdownUserName");
  if (dropName) dropName.textContent = user.name;

  const dropEmail = document.getElementById("dropdownUserEmail");
  if (dropEmail) dropEmail.textContent = user.email || "";

  const dropRole = document.getElementById("dropdownRoleBadge");
  if (dropRole) dropRole.textContent = roleLabel;

  // Render profiles switch list
  const list = document.getElementById("profileSelectList");
  if (list && allUsers.length > 0) {
    list.innerHTML = "";
    allUsers.forEach(u => {
      const isActive = u.id === user.id;
      const uInitials = getInitials(u.name);
      const uRole = getRoleLabel(u.role);
      list.innerHTML += `
        <div class="profile-select-item ${isActive ? 'active' : ''}" onclick="selectUserProfile(${u.id})">
          <div class="user-avatar-circle small">${uInitials}</div>
          <div class="profile-item-text">
            <div class="profile-item-name">${escapeHtml(u.name)}</div>
            <div class="profile-item-role">${uRole}</div>
          </div>
        </div>
      `;
    });
  }
}

function toggleUserProfileMenu() {
  const menu = document.getElementById("userProfileDropdown");
  if (menu) {
    menu.classList.toggle("hidden");
  }
}

window.toggleUserProfileMenu = toggleUserProfileMenu;

async function selectUserProfile(userId) {
  const menu = document.getElementById("userProfileDropdown");
  if (menu) menu.classList.add("hidden");
  await switchActiveUser(userId, true);
}

window.selectUserProfile = selectUserProfile;

function switchWorkspaceRole(role) {
  const menu = document.getElementById("userProfileDropdown");
  if (menu) menu.classList.add("hidden");
  const roleSelect = document.getElementById("activeRoleSelector");
  if (roleSelect) roleSelect.value = role;
  onRoleChange({ target: { value: role }, announce: true });
}

window.switchWorkspaceRole = switchWorkspaceRole;

function signOutCurrentUser() {
  if (typeof confirmLeaveGenerator === "function" && !confirmLeaveGenerator()) return;
  recordClientActivity("User Sign-Out", "Signed out");
  localStorage.removeItem("scaler_last_seen");
  localStorage.removeItem("scaler_active_user_id");
  try { sessionStorage.removeItem("sme_generator_draft"); localStorage.removeItem("admin_dashboard_tab"); } catch (e) { /* nothing stored */ }
  currentUser = null;
  currentScriptId = null;
  currentShareableUuid = null;
  currentSelectedProgramId = null;
  pendingHash = "";
  const input = document.getElementById("companyMailInput");
  if (input) input.value = "";
  const errorBox = document.getElementById("companyMailError");
  if (errorBox) errorBox.classList.add("hidden");
  showHomeDashboard();
}

window.signOutCurrentUser = signOutCurrentUser;

const ROLE_NAMES = { admin: "Admin", mo: "Module Owner", pm: "Program Manager", sme: "Author" };
function syncHomeAuthState() {
  const signedIn = !!currentUser;
  document.getElementById("hpSignInState")?.classList.toggle("hidden", signedIn);
  document.getElementById("hpSignedInState")?.classList.toggle("hidden", !signedIn);
  const who = document.getElementById("hpWho");
  if (who) who.textContent = signedIn ? `${currentUser.name || currentUser.email} \u00b7 ${ROLE_NAMES[currentUser.role] || currentUser.role}` : "";
  document.getElementById("signOutBtn")?.classList.toggle("hidden", !signedIn);          // the top bar offers Sign Out whenever someone is signed in
}
window.syncHomeAuthState = syncHomeAuthState;

function showHomeDashboard() {
  // Hide all other dashboard views
  document.querySelectorAll(".dashboard-view").forEach(v => {
    v.classList.remove("active");
    v.classList.add("hidden");
  });

  const homeView = document.getElementById("homeDashboardView");
  if (homeView) {
    homeView.classList.remove("hidden");
    homeView.classList.add("active");
  }

  // Someone is signed in: the home screen offers "Open my workspace" and "Sign out", never the sign-in form. After signing out it is the sign-in form again.
  syncHomeAuthState();
  setUrl("", false);                                              // Home has no screen in its address: nothing left over from the screen just left

  // Ensure header is visible
  const header = document.querySelector(".app-header");
  if (header) header.classList.remove("hidden");

  window.scrollTo(0, 0);
}

window.showHomeDashboard = showHomeDashboard;

function returnToActiveWorkspace() {
  if (currentUser) {
    const homeView = document.getElementById("homeDashboardView");
    if (homeView) {
      homeView.classList.add("hidden");
      homeView.classList.remove("active");
    }
    const signOutBtn = document.getElementById("signOutBtn");
    if (signOutBtn) signOutBtn.classList.remove("hidden");

    onRoleChange({ target: { value: currentUser.role } });
    if (currentUser.role === "sme") {
      showSmeLandingView();
    }
  } else {
    showHomeDashboard();
  }
}

window.returnToActiveWorkspace = returnToActiveWorkspace;

async function submitScalerCompanyLogin() {
  const input = document.getElementById("companyMailInput");
  const errorBox = document.getElementById("companyMailError");
  const button = document.getElementById("companyMailSubmitBtn");
  if (!input) return;
  const email = input.value.trim().toLowerCase();
  const fail = (msg) => {                                   // the message is shown under the field; no pop-up
    if (errorBox) { errorBox.textContent = msg; errorBox.classList.remove("hidden"); }
    input.setAttribute("aria-invalid", "true");
    input.focus();
  };
  input.removeAttribute("aria-invalid");
  if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) { fail("Enter your Scaler email address, for example name@scaler.com."); return; }

  if (button) { button.disabled = true; button.textContent = "Signing in..."; }
  try {
    const res = await fetch(`${API_BASE}/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email })
    });
    if (!res.ok && res.status !== 404) throw new Error(`The server answered ${res.status}.`);
    const targetUser = res.ok ? await res.json() : null;
    allUsers = targetUser ? [targetUser] : [];

    if (targetUser) {
      if (errorBox) errorBox.classList.add("hidden");
      showLoader("Opening your workspace...");
      localStorage.setItem("scaler_active_user_id", targetUser.id);
      currentUser = targetUser;
      const homeView = document.getElementById("homeDashboardView");
      if (homeView) { homeView.classList.add("hidden"); homeView.classList.remove("active"); }
      await switchActiveUser(targetUser.id, true);
      if (pendingHash) { const wanted = pendingHash; pendingHash = ""; await restoreScreenAfterReload(wanted); }      // the link they came with, if they may open it
      routeBootDone();
      localStorage.setItem("scaler_last_seen", String(Date.now()));       // a fresh sign-in is not a "return": a reload right after it records nothing
      recordClientActivity("User Sign-In", `Signed into ${targetUser.role.toUpperCase()} portal`);
    } else {
      fail(`We could not find an account for ${email}. Check the address, or ask your administrator to add it.`);
    }
  } catch (err) {
    fail("We could not reach the server. Check your connection and try again.");
  } finally {
    hideLoader();
    if (button) { button.disabled = false; button.textContent = "Continue"; }
  }
}

window.submitScalerCompanyLogin = submitScalerCompanyLogin;

function quickFillScalerLogin(email) {
  const input = document.getElementById("companyMailInput");
  if (input) input.value = email;
  submitScalerCompanyLogin();
}

window.quickFillScalerLogin = quickFillScalerLogin;

async function initUsersAndRoles() {
  try {
    const pRes = await fetch(`${API_BASE}/programs`);
    allSystemPrograms = await pRes.json();

    const savedId = localStorage.getItem("scaler_active_user_id");
    let savedUser = null;
    if (savedId) {
      // Only the signed-in user's own profile is fetched; the user directory is Admin-only
      const meRes = await fetch(`${API_BASE}/users/${parseInt(savedId, 10)}?actor_id=${parseInt(savedId, 10)}`);
      savedUser = meRes.ok ? await meRes.json() : null;
    }
    allUsers = savedUser ? [savedUser] : [];

    const userSelect = document.getElementById("activeUserSelector");
    if (userSelect) {
      userSelect.innerHTML = "";
      allUsers.forEach(u => {
        userSelect.innerHTML += `<option value="${u.id}">${escapeHtml(u.name)} (${getRoleLabel(u.role)})</option>`;
      });
      userSelect.addEventListener("change", (e) => {
        switchActiveUser(parseInt(e.target.value, 10));
      });
    }

    if (savedId) {
      const savedIdInt = parseInt(savedId, 10);
      // Verify the saved user still exists in the DB (admin may have deleted them)
      const stillExists = allUsers.find(u => u.id === savedIdInt);
      if (stillExists) {
        await switchActiveUser(savedIdInt, true);
        const last = Number(localStorage.getItem("scaler_last_seen") || 0);               // coming back after a break counts as a visit; a page reload does not
        if (Date.now() - last > 30 * 60 * 1000) recordClientActivity("Session Resumed", "Came back to the tool (still signed in)");
        localStorage.setItem("scaler_last_seen", String(Date.now()));
        const wanted = pendingHash; pendingHash = "";
        await restoreScreenAfterReload(wanted);
        routeBootDone();
      } else {
        // User was deleted — clear stale session and show login
        localStorage.removeItem("scaler_active_user_id");
        routeBootDone();
        showHomeDashboard();
      }
    } else {
      routeBootDone();
      showHomeDashboard();
    }
  } catch (err) {
    console.error("Error initializing users:", err);
    routeBootDone();
    showHomeDashboard();
  }
}

async function switchActiveUser(userId, autoSwitchWorkspace = true) {
  const user = allUsers.find(u => u.id === userId) || allUsers[0];
  if (!user) return;
  currentUser = user;
  localStorage.setItem("scaler_active_user_id", user.id);
  { const h = location.hash || "";                                 // an address left by another role does not belong to this one
    if ((/^#program\//.test(h) && user.role !== "sme") || (/^#\/(admin|mo|pm)\//.test(h) && !h.startsWith(`#/${user.role}/`))) setUrl("", false); }

  const userSelect = document.getElementById("activeUserSelector");
  if (userSelect) userSelect.value = user.id;

  // Update Scope Badge
  const scopeBadge = document.getElementById("userScopeBadge");
  if (scopeBadge) {
    scopeBadge.className = `scope-badge scope-${user.role}`;
    if (user.role === "sme") {
      scopeBadge.textContent = `Scope: Programs Joined (${user.program_names.length})`;
      scopeBadge.title = `Joined Programs: ${user.program_names.join(", ")}`;
    } else if (user.role === "mo") {
      scopeBadge.textContent = `Scope: Programs Part Of (${user.program_names.length})`;
      scopeBadge.title = `Assigned Programs: ${user.program_names.join(", ")}`;
    } else if (user.role === "pm") {
      scopeBadge.textContent = "Scope: All 5 Programs (LO Team - Read-Only)";
      scopeBadge.title = "Views status, content, module names, class names and agendas with no edit access across all programs";
    } else if (user.role === "admin") {
      scopeBadge.textContent = "Scope: All Programs (Full Control)";
      scopeBadge.title = "Global management of programs, tracks, levels, users and module structure";
    }
  }

  // Update Production Profile Menu in Navbar
  renderUserProfileMenu(user);

  const signOutBtn = document.getElementById("signOutBtn");
  if (signOutBtn) signOutBtn.classList.remove("hidden");

  // Always hide home portal when user is activated
  const homeView = document.getElementById("homeDashboardView");
  if (homeView) {
    homeView.classList.add("hidden");
    homeView.classList.remove("active");
  }

  // Automatically switch workspace to the user's role
  const roleSelect = document.getElementById("activeRoleSelector");
  if (roleSelect && autoSwitchWorkspace) {
    roleSelect.value = user.role;
    onRoleChange({ target: { value: user.role } });
  }

  // If user is SME, ALWAYS present the Enrolled Programs Card landing screen as the FIRST screen
  // with production navbar showing their profile only
  const appHeader = document.querySelector(".app-header");
  if (user.role === "sme") {
    if (appHeader) appHeader.classList.remove("hidden");
    showSmeLandingView();
    await renderSmeProgramCards(user);
  } else {
    if (appHeader) appHeader.classList.remove("hidden");
    const landing = document.getElementById("smeProgramsLandingView");
    const studio = document.getElementById("smeAuthoringStudioLayout");
    if (landing) landing.classList.add("hidden");
    if (studio) studio.classList.remove("hidden");
  }

  // Reload cascading programs for SME (filtered to user's joined programs)
  await loadPrograms(user.id);
}

// Render SME Enrolled Programs as Cards (First Screen After Login)
async function renderSmeProgramCards(user = null, silent = false) {
  const targetUser = user || currentUser;
  const grid = document.getElementById("smeEnrolledProgramsGrid");
  if (!grid) return;

  if (!targetUser) {
    grid.innerHTML = '<div style="color:var(--text-secondary); padding:20px;">No user logged in.</div>';
    return;
  }

  try {
    if (!silent) grid.innerHTML = '<div style="color:var(--text-secondary); padding:20px;">Loading your enrolled programs...</div>';

    // Fetch programs and authored scripts scoped strictly to this SME
    const [progRes, scriptRes] = await Promise.all([
      fetch(`${API_BASE}/programs?user_id=${targetUser.id}`),
      fetch(`${API_BASE}/dashboard/scripts?user_id=${targetUser.id}`)
    ]);
    const enrolledPrograms = await progRes.json();
    const allScripts = await scriptRes.json();

    if (!enrolledPrograms || enrolledPrograms.length === 0) {
      grid.innerHTML = `
        <div style="grid-column: 1 / -1; background: var(--bg-card); padding: 32px; border-radius: 8px; border: 1px dashed var(--border-subtle); text-align: center;">
          <h3 style="font-size: 1.1rem; color: var(--text-primary); margin-bottom: 8px;">No Programs Enrolled</h3>
          <p style="font-size: 0.88rem; color: var(--text-secondary); max-width: 500px; margin: 0 auto 16px auto;">
            You are not currently enrolled in any curriculum programs. Please ask your Administrator to enroll your email (${escapeHtml(targetUser.email)}) in curriculum programs.
          </p>
        </div>
      `;
      return;
    }

    const kinds = (list) => ({
      total: list.length,
      approved: list.filter(x => x.status === "GTG").length,
      pending: list.filter(x => x.status === "MO Approval").length,
      review: list.filter(x => x.status === "SME Review" && !x.revision_note).length,
      revise: list.filter(x => x.status !== "GTG" && x.status !== "MO Approval" && x.revision_note).length,
    });
    const mineAll = kinds(allScripts || []);
    const draftsAll = mineAll.total - mineAll.approved - mineAll.pending - mineAll.revise - mineAll.review;
    const sumEl = document.getElementById("smeSummary");
    if (sumEl) sumEl.innerHTML = [
      [enrolledPrograms.length, "Enrolled programs", ""], [mineAll.total, "My scripts", ""], [draftsAll, "Drafts", ""],
      [mineAll.pending, "Pending review", ""], [mineAll.revise, "Changes requested", mineAll.revise ? "pm-stat-warn" : ""], [mineAll.approved, "Approved", ""],
    ].map(([n, label, cls]) => `<div><span class="${cls}">${n}</span><label>${label}</label></div>`).join("");

    let cardsHtml = "";
    enrolledPrograms.forEach(prog => {
      const k = kinds((allScripts || []).filter(sc => sc.program_id === prog.id));
      const drafts = k.total - k.approved - k.pending - k.revise - k.review;
      const pct = k.total ? Math.round(k.approved * 100 / k.total) : 0;
      const chip = (n, label, cls = "") => n ? `<span class="spc-chip ${cls}"><b>${n}</b> ${label}</span>` : "";
      cardsHtml += `
        <div class="spc" role="button" tabindex="0" data-name="${escapeHtml(prog.name)}" onclick="selectSmeProgramCard(${prog.id}, this.dataset.name)" onkeydown="if(event.key==='Enter')this.click()">
          <div class="spc-head">
            <h2 class="spc-name">${escapeHtml(prog.name)}</h2>
            <span class="spc-go">Open &rarr;</span>
          </div>
          ${k.total ? `
            <div class="spc-bar" title="${k.approved} of ${k.total} scripts approved"><span style="width:${pct}%"></span></div>
            <div class="spc-line">${k.approved} of ${k.total} ${k.total === 1 ? "script" : "scripts"} approved</div>
            <div class="spc-chips">${chip(k.revise, "changes requested", "spc-warn")}${chip(k.review, "in SME review")}${chip(k.pending, "pending review")}${chip(drafts, drafts === 1 ? "draft" : "drafts")}</div>`
          : `<div class="spc-line">No scripts yet. Open the program to write your first one.</div>`}
        </div>`;
    });

    grid.innerHTML = cardsHtml;
  } catch (err) {
    grid.innerHTML = `<div style="color:var(--danger-color); padding:20px;">Error loading enrolled programs: ${err.message}</div>`;
  }
}

function showSmeLandingView() {
  if (!confirmLeaveGenerator()) return;
  const landing = document.getElementById("smeProgramsLandingView");
  const studio = document.getElementById("smeAuthoringStudioLayout");
  const header = document.querySelector(".app-header");
  if (landing) landing.classList.remove("hidden");
  if (studio) studio.classList.add("hidden");
  if (header) header.classList.remove("hidden");
  document.getElementById("outlineGate")?.classList.add("hidden");
  document.getElementById("studioView")?.classList.add("hidden");
  if (inFullScreen()) exitFullScreen();
  smePushHistory({ sme: 0 }, "#programs");
  renderSmeProgramCards(currentUser);
}

window.showSmeLandingView = showSmeLandingView;

// Stub for SME authored scripts refresh — called on workspace switch and after GTG approval
async function loadSmeAuthoredScripts() {
  if (!currentUser || currentUser.role !== "sme") return;
  if (currentSelectedProgramId) {
    const progSel = document.getElementById("programSelect");
    const pName = progSel?.selectedOptions[0]?.text || "";
    await renderProgramScriptsTable(currentSelectedProgramId, pName);
  }
}
window.loadSmeAuthoredScripts = loadSmeAuthoredScripts;

// Track active program in SME Studio
let currentSelectedProgramId = null;

// View switcher for SME Studio: 'generator' | 'scripts' | 'outline' | 'script'
window.switchSmeStudioView = function (view) {
  const inputsView = document.getElementById("smeInputsView");
  const scriptsView = document.getElementById("smeScriptsDedicatedView");
  const outlineGate = document.getElementById("outlineGate");
  const studioView = document.getElementById("studioView");
  const tabScriptsBtn = document.getElementById("smeTabScriptsBtn");
  const curriculumView = document.getElementById("smeCurriculumDedicatedView");
  const tabCurriculumBtn = document.getElementById("smeTabCurriculumBtn");
  if (curriculumView) curriculumView.classList.add("hidden");
  if (tabCurriculumBtn) tabCurriculumBtn.classList.remove("active");

  if (view === "curriculum") {
    if (inputsView) inputsView.classList.add("hidden");
    if (scriptsView) scriptsView.classList.add("hidden");
    if (outlineGate) outlineGate.classList.add("hidden");
    if (studioView) studioView.classList.add("hidden");
    if (curriculumView) curriculumView.classList.remove("hidden");
    if (tabCurriculumBtn) tabCurriculumBtn.classList.add("active");
    if (tabScriptsBtn) tabScriptsBtn.classList.remove("active");
    loadSmeCurriculum();
    recordClientActivity("Tab Switched", "Opened SME Program Curriculum view");
    window.scrollTo({ top: 0, behavior: "smooth" });
  } else if (view === "scripts") {
    // Show only Program Scripts in full screen
    if (inputsView) inputsView.classList.add("hidden");
    if (outlineGate) outlineGate.classList.add("hidden");
    if (studioView) studioView.classList.add("hidden");
    if (scriptsView) scriptsView.classList.remove("hidden");
    if (tabScriptsBtn) tabScriptsBtn.classList.add("active");
    refreshCurrentProgramScripts();
    recordClientActivity("Tab Switched", "Opened SME Program Scripts view");
    window.scrollTo({ top: 0, behavior: "smooth" });
  } else if (view === "generator") {
    // Show only generator inputs in full screen
    if (scriptsView) scriptsView.classList.add("hidden");
    if (outlineGate) outlineGate.classList.add("hidden");
    if (studioView) studioView.classList.add("hidden");
    if (inputsView) inputsView.classList.remove("hidden");
    if (tabScriptsBtn) tabScriptsBtn.classList.remove("active");
    recordClientActivity("Tab Switched", "Switched to SME Script Generator Input view");
    window.scrollTo({ top: 0, behavior: "smooth" });
  } else if (view === "outline") {
    // Show only outline gate in full screen
    if (inputsView) inputsView.classList.add("hidden");
    if (scriptsView) scriptsView.classList.add("hidden");
    if (studioView) studioView.classList.add("hidden");
    if (outlineGate) outlineGate.classList.remove("hidden");
    if (tabScriptsBtn) tabScriptsBtn.classList.remove("active");
    recordClientActivity("Tab Switched", "Opened Outline Review Gate");
    window.scrollTo({ top: 0, behavior: "smooth" });
  } else if (view === "script") {
    // Show only script studio teleprompter deck in full screen
    if (inputsView) inputsView.classList.add("hidden");
    if (scriptsView) scriptsView.classList.add("hidden");
    if (outlineGate) outlineGate.classList.add("hidden");
    if (studioView) studioView.classList.remove("hidden");
    if (tabScriptsBtn) tabScriptsBtn.classList.remove("active");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }
};

// ---- one navigation bar for the author workspace: breadcrumb, tabs, script progress, and the browser's Back button ----
let smeView = "generator", smeOpenedExisting = false, smePopping = false;
const SME_PAGE_NAMES = { generator: "Create script", outline: "Outline review", script: "Script", scripts: "Scripts", curriculum: "Curriculum" };

// Where the Back button leads from each screen
const SME_BACK = { generator: "landing", outline: "generator", scripts: "landing", curriculum: "landing", script: "scripts" };
let smeBackTarget = null;
window.smeGoBack = function () {
  if (smeBackTarget === "landing") showSmeLandingView();
  else if (smeBackTarget) window.switchSmeStudioView(smeBackTarget);
};

function smeSetStep(n, complete = false, note = null) {
  const steps = document.getElementById("smeSteps");
  if (!steps) return;
  steps.querySelectorAll("li").forEach(li => {
    const k = Number(li.dataset.step);
    li.classList.toggle("done", k < n || (complete && k === n));
    li.classList.toggle("active", k === n && !complete);
    li.querySelector(".sme-step-dot").textContent = (k < n || (complete && k === n)) ? "\u2713" : String(k);
    if (k === 3) li.querySelector(".sme-step-label").textContent = note || "Script draft";
  });
  document.getElementById("smeStepsFill").style.width = `${Math.round(((complete ? n : n - 1) / 6) * 100)}%`;
  steps.setAttribute("aria-valuenow", String(n));
}

function smePushHistory(state, hash) {
  if (smePopping || !currentUser || currentUser.role !== "sme") return;
  const last = history.state;
  if (last && JSON.stringify(last) === JSON.stringify(state)) return;
  history.pushState(state, "", hash);
}

function smeSyncNav(view) {
  smeView = view;
  if (view === "generator" || view === "outline") smeOpenedExisting = false;
  const create = view === "generator" || view === "outline" || (view === "script" && !smeOpenedExisting);
  const scripts = view === "scripts" || (view === "script" && smeOpenedExisting);
  document.getElementById("smeTabCreateBtn")?.classList.toggle("active", create);
  document.getElementById("smeTabScriptsBtn")?.classList.toggle("active", scripts);
  document.getElementById("smeTabCurriculumBtn")?.classList.toggle("active", view === "curriculum");
  const page = document.getElementById("smeCrumbPage");
  if (page) page.textContent = SME_PAGE_NAMES[view] || "";
  smeBackTarget = SME_BACK[view] || null;
  const backLabel = { landing: "My Programs", scripts: "Scripts", generator: "Class inputs" }[smeBackTarget];
  document.getElementById("smeBackLabel").textContent = backLabel || "Back";
  document.getElementById("smeBackBtn")?.classList.toggle("hidden", !smeBackTarget);
  const steps = document.getElementById("smeSteps");
  const inFlow = view === "generator" || view === "outline" || view === "script";
  steps?.classList.toggle("hidden", !inFlow);
  if (view === "generator") smeSetStep(1);
  else if (view === "outline") smeSetStep(2);
  else if (view === "script") smeSetStep(3);                              // the status badge then moves it on
  if (view !== "script" && inFullScreen()) exitFullScreen();
  const withId = (view === "script" || view === "outline") && currentScriptId;                                // a refresh can reopen this very script or outline
  smePushHistory({ sme: 1, prog: currentSelectedProgramId, view, script: withId ? currentScriptId : null },
    `#program/${currentSelectedProgramId}/${view}${withId ? "/" + currentScriptId : ""}`);
}
const _smeSwitchRaw = window.switchSmeStudioView;
window.switchSmeStudioView = function (view) {
  if (!confirmLeaveCreation(view)) return;
  _smeSwitchRaw(view); smeSyncNav(view);
};

window.addEventListener("popstate", async (e) => {                       // the browser's Back and Forward buttons move between these screens
  if (!currentUser || currentUser.role !== "sme" || document.getElementById("smeDashboardView")?.classList.contains("hidden")) return;
  const st = e.state;
  const target = (!st || !st.sme) ? "landing" : st.view;
  if (!confirmLeaveCreation(target)) {                                                          // the author chose to stay: put the page back in the history
    smePopping = true;
    const here = outlineIsOpen() ? "outline" : "generator";
    history.pushState({ sme: 1, prog: currentSelectedProgramId, view: here, script: null }, "", `#program/${currentSelectedProgramId}/${here}`);
    smePopping = false;
    return;
  }
  smePopping = true;
  try {
    if (!st || !st.sme) { showSmeLandingView(); return; }
    if (!document.getElementById("smeProgramsLandingView")?.classList.contains("hidden") || currentSelectedProgramId !== st.prog) await selectSmeProgramCard(st.prog);
    if (st.view === "script" && st.script) await loadExistingScriptIntoStudio(st.script);
    else window.switchSmeStudioView(st.view === "outline" ? "generator" : st.view);          // an outline in progress cannot be rebuilt, so it returns to the first step
  } finally { smePopping = false; }
});

// Back/Forward (and an address edited by hand) for Home and the Admin, Module Owner and Program Manager screens; Author screens have their own handler above
window.addEventListener("popstate", async () => {
  if (!currentUser) return;
  const h = location.hash || "";
  const home = document.getElementById("homeDashboardView");
  const onHome = !!home && !home.classList.contains("hidden");
  if (!h || h === "#/") { if (currentUser.role !== "sme" && !onHome) showHomeDashboard(); return; }
  const roleRoute = /^#\/(admin|mo|pm)\//.test(h), authorRoute = /^#program\//.test(h);
  if (!(roleRoute || (authorRoute && onHome))) return;
  routePopping = true; smePopping = true;
  try { if (onHome) returnToActiveWorkspace(); } finally { routePopping = false; smePopping = false; }
  await restoreScreenAfterReload(h);
});

// Share script link helper with clipboard copying
window.shareScriptLink = function (scriptId, evt) {
  if (evt) evt.stopPropagation();
  const url = window.location.origin + '/?script=' + scriptId;
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(url).then(() => {
      showNotice("Share link copied.");
    }).catch(() => {
      prompt("Copy script link:", url);
    });
  } else {
    prompt("Copy script link:", url);
  }
};

// ---- full screen: a script opens full screen, and the Full screen button (or the F key) toggles it ----------------------------------------------
// A browser only allows full screen after a click or a key press, so it is requested from the click that opens the script.
function fullScreenSupported() {
  return !!(document.documentElement.requestFullscreen || document.documentElement.webkitRequestFullscreen);
}

function inFullScreen() {
  return !!(document.fullscreenElement || document.webkitFullscreenElement);
}

function enterFullScreen() {
  if (!fullScreenSupported() || inFullScreen()) return;
  const root = document.documentElement;
  try {
    const result = (root.requestFullscreen || root.webkitRequestFullscreen).call(root);
    if (result && result.catch) result.catch(() => {});          // refused (no click behind it): the button still works
  } catch (e) { /* not available here */ }
}

function exitFullScreen() {
  try {
    const result = (document.exitFullscreen || document.webkitExitFullscreen).call(document);
    if (result && result.catch) result.catch(() => {});
  } catch (e) { /* not in full screen */ }
}

window.toggleFullScreen = function () { inFullScreen() ? exitFullScreen() : enterFullScreen(); };

function syncFullScreenButton() {
  const btn = document.getElementById("fullScreenBtn");
  if (btn) {
    btn.textContent = inFullScreen() ? "Exit full screen" : "Full screen";
    btn.style.display = fullScreenSupported() ? "" : "none";
  }
}
document.addEventListener("fullscreenchange", syncFullScreenButton);
document.addEventListener("webkitfullscreenchange", syncFullScreenButton);

// Open existing script directly in SME studio (ONLY shows the script, hides inputs)
window.loadExistingScriptIntoStudio = async function (scriptId) {
  smeOpenedExisting = true;
  currentScriptId = scriptId;
  const landing = document.getElementById("smeProgramsLandingView");
  const studio = document.getElementById("smeAuthoringStudioLayout");
  const modal = document.getElementById("assignmentsModal");
  if (modal) modal.classList.add("hidden");
  if (landing) landing.classList.add("hidden");
  if (studio) studio.classList.remove("hidden");

  // Switch to dedicated full-width script view - hiding generator inputs!
  switchSmeStudioView("script");

  document.getElementById("emptyState")?.classList.add("hidden");
  document.getElementById("outlineGate")?.classList.add("hidden");

  await loadScriptStudioView();
  const studioView = document.getElementById("studioView");
  if (studioView) {
    studioView.classList.remove("hidden");
    window.scrollTo({ top: 0, behavior: "instant" });
  }
};

// The Module Owner's "changes requested" note, shown above the script it is about
function applyRevisionBanner(script) {
  const banner = document.getElementById("smeRevisionBanner");
  if (!banner) return;
  const show = !!script.revision_note && script.status === "Script WIP";
  banner.classList.toggle("hidden", !show);
  if (show) {
    const mineScript = !!currentUser && smePlain(script.author_name).toLowerCase() === smePlain(currentUser.name).toLowerCase();
    document.getElementById("smeReviseBtn")?.classList.add("hidden");              // the Revise option is switched off for now
    const withdrawn = script.revision_by === "Approval withdrawn";
    const rt = document.querySelector("#smeRevisionBanner .rev-banner-title"); if (rt) rt.textContent = withdrawn ? "Needs approval again" : "Changes requested by the Module Owner";
    document.getElementById("smeRevisionNote").textContent = script.revision_note;
    document.getElementById("smeRevisionMeta").textContent = `${script.revision_by || "Module Owner"}${script.revision_at ? " \u00b7 " + parseServerTime(script.revision_at).toLocaleString() : ""}`;
  }
}

function showNotice(text) {
  let box = document.getElementById("liveNotice");
  if (!box) { box = document.createElement("div"); box.id = "liveNotice"; box.className = "live-notice"; document.body.appendChild(box); }
  const item = document.createElement("div");
  item.textContent = text;
  box.appendChild(item);
  setTimeout(() => item.remove(), 9000);
}

// The author's own screens follow the Module Owner: a script sent back or approved shows up without a reload
let smeLastActivityId = null, smeSeen = {}, smeLiveBusy = false;
async function smeLivePoll() {
  const visible = !document.getElementById("smeDashboardView")?.classList.contains("hidden");
  if (!visible || document.hidden || !currentUser || currentUser.role !== "sme" || smeLiveBusy) return;
  smeLiveBusy = true;
  try {
    const head = await fetch(`${API_BASE}/activities?limit=1`);
    if (!head.ok) return;
    const rows = await head.json();
    const newest = rows.length ? rows[0].id : 0;
    const first = smeLastActivityId === null;
    if (!first && newest === smeLastActivityId) return;
    smeLastActivityId = newest;
    const res = await fetch(`${API_BASE}/dashboard/scripts?user_id=${currentUser.id}`);
    if (!res.ok) return;
    const mine = await res.json();
    mine.forEach(sc => {
      const key = `${sc.status}|${sc.revision_at || ""}`, before = smeSeen[sc.id];
      if (before !== undefined && before !== key) {
        if (sc.revision_note && sc.status === "Script WIP") showNotice(`${sc.revision_by || "The Module Owner"} did not approve "${sc.class_title}". Open it to see what to change.`);
        else if (sc.status === "GTG") showNotice(`"${sc.class_title}" was approved by ${sc.approved_by_name || "the Module Owner"}.`);
        if (currentScriptId === sc.id) {                                          // the script open right now: update only its banner
          fetch(`${API_BASE}/scripts/${sc.id}?user_id=${currentUser.id}`).then(r => r.ok ? r.json() : null).then(d => { if (d) applyRevisionBanner(d); });
        }
      }
      smeSeen[sc.id] = key;
    });
    if (!first && currentSelectedProgramId) window.refreshCurrentProgramScripts();
  } catch (e) { /* the next check tries again */ } finally { smeLiveBusy = false; }
}
setInterval(smeLivePoll, 15000);
document.addEventListener("visibilitychange", () => { if (!document.hidden) smeLivePoll(); });

// ---- the Scripts screen of a program: summary, filters, and one row per script with the actions that fit its stage
let smeScripts = [], smeScriptsProgram = "", smeScriptsVisible = 50;
const smeStage = (s) => s.status === "GTG" ? { key: "approved", text: "Approved", dot: "sa-ok" }
  : s.status === "MO Approval" ? { key: "pending", text: "Pending review", dot: "sa-wait" }
  : s.status === "SME Review" ? { key: "review", text: "SME review", dot: "sa-wait" }
  : s.revision_note ? { key: "changes", text: s.revision_by === "Approval withdrawn" ? "Needs approval again" : "Changes requested", dot: "sa-warn" }
  : { key: "draft", text: "Draft", dot: "" };
const smePlain = (n) => String(n || "").replace(/\s*\((?:SME|MO|PM|Admin)\)\s*$/i, "").trim();
const smeIsMine = (s) => !!currentUser && smePlain(s.sme_name).toLowerCase() === smePlain(currentUser.name).toLowerCase();
function smeAgo(v) {
  const t = parseServerTime(v).getTime();
  if (isNaN(t)) return "-";
  const m = Math.max(0, Math.round((Date.now() - t) / 60000));
  return m < 1 ? "just now" : m < 60 ? `${m} min ago` : m < 48 * 60 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} days ago`;
}

async function renderProgramScriptsTable(progId, progName = "") {
  const tbody = document.getElementById("smeProgramScriptsTableBody");
  if (!tbody) return;
  smeScriptsProgram = progName || (allSystemPrograms.find(p => p.id === parseInt(progId, 10))?.name) || `Program #${progId}`;
  const sub = document.getElementById("smeProgramScriptsSubtitle");
  if (sub) sub.textContent = `All scripts written for ${smeScriptsProgram}.`;
  try {
    const userParam = (currentUser && currentUser.role === "sme") ? `&user_id=${currentUser.id}` : "";
    const res = await fetch(`${API_BASE}/dashboard/scripts?program_id=${progId}${userParam}`);
    if (!res.ok) return;
    smeScripts = await res.json() || [];
    const total = smeScripts.length, approved = smeScripts.filter(s => s.status === "GTG").length;
    ["smeStudioTabScriptsBadge", "smeProgramScriptsNavBadge", "headerScriptsBadge", "emptyStateScriptsBadge"].forEach(id => { const el = document.getElementById(id); if (el) el.textContent = total; });
    const badge = document.getElementById("smeProgramScriptsCountBadge"); if (badge) badge.textContent = `${approved} GTG Approved / ${total} Total`;
    const trackSel = document.getElementById("scTrack");
    if (trackSel) {                                                       // the tracks that have scripts, keeping the current choice
      const keep = trackSel.value, tracks = [...new Set(smeScripts.map(s => s.track_name).filter(Boolean))].sort();
      trackSel.innerHTML = '<option value="">All tracks</option>' + tracks.map(t => `<option value="${escapeHtml(t)}">${escapeHtml(t)}</option>`).join("");
      trackSel.value = tracks.includes(keep) ? keep : "";
    }
    renderSmeScripts();
  } catch (err) {
    console.error("Error loading program scripts:", err);
    tbody.innerHTML = '<div class="sa-empty" style="color:var(--danger-color);">Could not load the scripts. Check your connection and try again.</div>';
  }
}

function renderSmeScripts() {
  const list = document.getElementById("smeProgramScriptsTableBody");
  if (!list) return;
  const val = (id) => document.getElementById(id)?.value || "";
  const q = val("scSearch").toLowerCase().trim(), status = val("scStatus"), track = val("scTrack"), author = val("scAuthor"), sort = val("scSort") || "attention";
  // the summary counts the whole program; clicking a tile filters by it
  const count = (k) => smeScripts.filter(s => smeStage(s).key === k).length;
  const tiles = [["", smeScripts.length, "Total scripts"], ["draft", count("draft"), "Drafts"], ["review", count("review"), "SME review"], ["changes", count("changes"), "Changes requested"], ["pending", count("pending"), "Pending review"], ["approved", count("approved"), "Approved"]];
  const stats = document.getElementById("scStats");
  if (stats) stats.innerHTML = tiles.map(([k, n, label]) =>
    `<button type="button" class="sc-tile${status === k ? " on" : ""}" onclick="setSmeScriptStatus('${k}')"><span class="${k === "changes" && n ? "pm-stat-warn" : ""}">${n}</span><label>${label}</label></button>`).join("");
  const order = { changes: 0, review: 1, draft: 2, pending: 3, approved: 4 };
  const rows = smeScripts.filter(s =>
    (!q || [s.class_title, s.module_name, s.track_name, s.sme_name].some(t => (t || "").toLowerCase().includes(q))) &&
    (!status || smeStage(s).key === status) && (!track || s.track_name === track) && (author !== "mine" || smeIsMine(s)))
    .sort((a, b) => sort === "name" ? (a.class_title || "").localeCompare(b.class_title || "")
      : sort === "recent" ? parseServerTime(b.last_activity_at) - parseServerTime(a.last_activity_at)
      : (order[smeStage(a).key] - order[smeStage(b).key]) || (parseServerTime(b.last_activity_at) - parseServerTime(a.last_activity_at)));
  if (!smeScripts.length) { list.innerHTML = `<div class="sa-empty">No scripts yet for ${escapeHtml(smeScriptsProgram)}. Open Create script to write the first one.</div>`; return; }
  if (!rows.length) { list.innerHTML = '<div class="sa-empty">No scripts match the selected filters.</div>'; return; }
  list.innerHTML = rows.slice(0, smeScriptsVisible).map(s => {
    const st = smeStage(s), mine = smeIsMine(s);
    const canSubmit = mine && (st.key === "review" || st.key === "changes") && (s.cards_count || 0) > 0;          // only after the AI review (or when sent back)
    const canDelete = mine && (st.key === "draft" || st.key === "review" || st.key === "changes");
    return `
      <div class="sc-row" id="sme-prog-script-row-${s.id}">
        <div>
          <div class="sa-class-title">${escapeHtml(s.class_title)}</div>
          <div class="sa-sub">${escapeHtml(s.module_name)} &middot; ${escapeHtml(s.track_name || "Standard")} &middot; ${s.duration_minutes} min</div>
          ${st.key === "changes" ? `<div class="mr-note"><b>${escapeHtml(s.revision_by || "Module Owner")}</b>: ${escapeHtml(s.revision_note)}</div>` : ""}
          ${st.key === "draft" && (s.cards_count || 0) > 0 && s.review_passed === null ? `<div class="sa-sub">AI review not run yet</div>` : ""}
        </div>
        <div>${escapeHtml(smePlain(s.sme_name) || "Unknown")}${mine ? ' <span class="sa-sub">You</span>' : ""}</div>
        <div><div class="sa-state ${st.dot}">${st.text}</div>${st.key === "pending" && s.submitted_at ? `<div class="sa-sub">sent ${smeAgo(s.submitted_at)}</div>` : ""}${st.key === "approved" && s.approved_by_name ? `<div class="sa-sub">by ${escapeHtml(s.approved_by_name)}</div>` : ""}</div>
        <div>${s.cards_count || 0}${s.cards_planned ? ` <span class="sa-sub">of ${s.cards_planned}</span>` : ""}</div>
        <div class="sa-sub" style="margin:0;">${smeAgo(s.last_activity_at)}</div>
        <div class="sa-actions">
          <button class="btn btn-primary" onclick="loadExistingScriptIntoStudio(${s.id})">Open</button>
          ${canSubmit ? `<button class="btn btn-secondary" onclick="smeSubmitFromList(${s.id})">Submit to MO</button>` : ""}
          <button class="btn btn-secondary" onclick="shareScriptLink(${s.id}, event)">Share</button>
          ${canDelete ? `<button class="btn btn-secondary mr-reject" data-title="${escapeHtml(s.class_title)}" onclick="deleteScriptDirectly(${s.id}, this.dataset.title)">Delete</button>` : ""}
        </div>
      </div>`;
  }).join("") + (rows.length > smeScriptsVisible ? `<div class="sa-more"><span>Showing ${smeScriptsVisible} of ${rows.length}</span><button class="btn btn-secondary" onclick="smeScriptsVisible += 50; renderSmeScripts()">Show 50 more</button></div>` : "");
}
window.renderSmeScripts = renderSmeScripts;
window.setSmeScriptStatus = function (k) { const el = document.getElementById("scStatus"); if (el) { el.value = (el.value === k ? "" : k); renderSmeScripts(); } };
window.clearSmeScriptFilters = function () {
  ["scSearch", "scStatus", "scTrack", "scAuthor"].forEach(id => { const el = document.getElementById(id); if (el) el.value = ""; });
  const sort = document.getElementById("scSort"); if (sort) sort.value = "attention";
  renderSmeScripts();
};
window.smeSubmitFromList = async function (id) {
  const s = smeScripts.find(x => x.id === id);
  if (!confirm(`Submit "${s ? s.class_title : "this script"}" to the Module Owner for review?\n\nYou can't edit it while it is waiting for their decision.`)) return;
  try {
    const res = await fetch(`${API_BASE}/scripts/${id}/submit-mo?user_id=${currentUser.id}`, { method: "POST" });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof d.detail === "string" ? d.detail : "Could not submit the script.");
    showNotice(`"${s ? s.class_title : "The script"}" was sent to the Module Owner.`);
    window.refreshCurrentProgramScripts();
  } catch (e) { alert(e.message); }
};

window.renderProgramScriptsTable = renderProgramScriptsTable;

window.refreshCurrentProgramScripts = function () {
  if (currentSelectedProgramId) {
    const progSel = document.getElementById("programSelect");
    const pName = progSel?.selectedOptions[0]?.text || "Selected Program";
    renderProgramScriptsTable(currentSelectedProgramId, pName);
  }
};

// The generator starts empty, and the two sliders start unset: the author has to choose both before an outline can be made
function resetGeneratorForm() {
  if (smeRevising) exitRevisionMode();
  clearGeneratorDraft();
  ["classAgendasInput", "learningOutcomesInput", "referenceUrls", "prevScriptUpload", "classTitleInput"].forEach(id => { const el = document.getElementById(id); if (el) el.value = ""; });
  const file = document.getElementById("prevScriptFileInput"); if (file) file.value = "";
  const dur = document.getElementById("durationSelect"); if (dur) dur.value = "";
  const customDur = document.getElementById("customDurationInput"); if (customDur) customDur.value = "";
  const durVal = document.getElementById("durationVal"); if (durVal) durVal.textContent = "Select duration";
  const set = (id, valId, errId, def) => {
    const sl = document.getElementById(id); if (!sl) return;
    sl.value = def; sl.classList.add("gen-unset");
    document.getElementById(valId).textContent = "Not set"; document.getElementById(errId)?.classList.add("hidden");
  };
  set("mixSlider", "mixVal", "mixError", 70); set("codeRatioSlider", "codeRatioVal", "codeError", 20);
}

// Anything entered on the generator page that has not been turned into an outline yet
function generatorHasUnsavedInput() {
  if (document.getElementById("smeInputsView")?.classList.contains("hidden")) return false;
  const v = (id) => (document.getElementById(id)?.value || "").trim();
  return !!(v("trackSelect") || v("classAgendasInput") || v("learningOutcomesInput") || v("referenceUrls") || v("prevScriptUpload") || v("durationSelect")
    || !document.getElementById("mixSlider")?.classList.contains("gen-unset") || !document.getElementById("codeRatioSlider")?.classList.contains("gen-unset"));
}

// ---- a refresh keeps the author where they were: the screen comes back from the address, and what was typed on the generator comes back from the browser
const GEN_DRAFT_KEY = "sme_generator_draft";
const BOOT_HASH = location.hash;                                  // where the address pointed when the page loaded, before the landing screen rewrote it

// ---- addresses. Home is the bare address; Authors use #program/<id>/<screen>[/<script>]; the other roles use #/admin/<tab>, #/mo/<tab>, #/pm/<tab>.
// A link someone opens while signed out is kept (pendingHash) and opened after they sign in, if their role and programs allow it.
let routePopping = false, routeBooting = true, pendingHash = BOOT_HASH;
function setUrl(hash, push) {
  if (routePopping) return;                                       // the address is already where Back/Forward or a restore put it
  const want = hash || "";
  if ((location.hash || "") === want) return;
  history[push && !routeBooting ? "pushState" : "replaceState"]({ route: want }, "", want || (location.pathname + location.search));   // first screens replace, later ones add a step for Back
}
function routeBootDone() { routeBooting = false; }
function saveGeneratorDraft() {
  if (!currentUser || currentUser.role !== "sme" || smeRevising) return;
  const v = (id) => document.getElementById(id)?.value || "";
  const set = (id) => !document.getElementById(id)?.classList.contains("gen-unset");
  const d = { prog: currentSelectedProgramId, track: v("trackSelect"), module: v("moduleSelect"), cls: v("classSelect"), agendas: v("classAgendasInput"), outcomes: v("learningOutcomesInput"),
              duration: v("durationSelect"), mix: set("mixSlider") ? v("mixSlider") : null, code: set("codeRatioSlider") ? v("codeRatioSlider") : null, refs: v("referenceUrls") };
  try { sessionStorage.setItem(GEN_DRAFT_KEY, JSON.stringify(d)); } catch (e) { /* storage may be off */ }
}
function readGeneratorDraft() { try { return JSON.parse(sessionStorage.getItem(GEN_DRAFT_KEY) || "null"); } catch (e) { return null; } }
function clearGeneratorDraft() { try { sessionStorage.removeItem(GEN_DRAFT_KEY); } catch (e) { /* nothing to clear */ } }
["input", "change"].forEach(t => document.addEventListener(t, (e) => { if (e.isTrusted && e.target.closest && e.target.closest("#smeInputsContainer")) saveGeneratorDraft(); }));

async function restoreGeneratorDraft(d, progId) {
  if (!d || Number(d.prog) !== Number(progId) || !d.track) return;
  const trackSel = document.getElementById("trackSelect"), modSel = document.getElementById("moduleSelect"), classSel = document.getElementById("classSelect");
  trackSel.value = d.track; await onTrackChange({ target: trackSel });
  if (d.module) { modSel.value = d.module; await onModuleChange({ target: modSel }); }
  if (d.cls) { classSel.value = d.cls; await onClassChange({ target: classSel }); }
  document.getElementById("classAgendasInput").value = d.agendas || "";
  document.getElementById("learningOutcomesInput").value = d.outcomes || "";
  if (d.duration) { const dur = document.getElementById("durationSelect"); dur.value = d.duration; dur.dispatchEvent(new Event("change")); }
  if (d.mix !== null && d.mix !== undefined) { const m = document.getElementById("mixSlider"); m.value = d.mix; m.dispatchEvent(new Event("input")); }
  if (d.code !== null && d.code !== undefined) { const c = document.getElementById("codeRatioSlider"); c.value = d.code; c.dispatchEvent(new Event("input")); }
  document.getElementById("referenceUrls").value = d.refs || "";
  saveGeneratorDraft();                                            // so a second refresh still finds it
}

async function restoreScreenAfterReload(hash = BOOT_HASH) {
  if (!currentUser) return;
  const r = /^#\/(admin|mo|pm)\/(\w+)$/.exec(hash || "");
  if (r) {                                                         // an address for the Admin, Module Owner or Program Manager screens
    if (r[1] !== currentUser.role) { showNotice("That link is for a different role, so it was not opened."); setUrl("", false); return; }
    routePopping = true;
    try { ({ admin: window.switchAdminTab, mo: window.showMoPane, pm: window.showPmPane })[r[1]](r[2]); }
    finally { routePopping = false; }
    return;
  }
  if (currentUser.role === "admin") {
    let tab = null; try { tab = localStorage.getItem("admin_dashboard_tab"); } catch (e) { /* default */ }
    if (tab && tab !== "users") window.switchAdminTab(tab);
    else setUrl("#/admin/users", false);
    return;
  }
  if (currentUser.role !== "sme") return;                         // the Module Owner and Program Manager screens remember their own tabs
  const m = /^#program\/(\d+)\/(generator|outline|scripts|curriculum|script)(?:\/(\d+))?$/.exec(hash || "");
  if (!m) { if (hash) setUrl("", false); return; }
  const progId = Number(m[1]), view = m[2], sid = m[3] ? Number(m[3]) : null;
  if (!(currentUser.program_ids || []).map(Number).includes(progId)) { showNotice("You have not joined that program, so the link could not be opened."); setUrl("", false); return; }
  const draft = readGeneratorDraft();                              // read before the program opens, because opening it starts a clean form
  smePopping = true;
  try {
    await selectSmeProgramCard(progId);
    if (view === "script" && sid) {
      smeOpenedExisting = true;
      await loadExistingScriptIntoStudio(sid);
    } else if (view === "outline" && sid) {
      const res = await fetch(`${API_BASE}/scripts/${sid}/outline?user_id=${currentUser.id}`);
      if (res.ok) { currentScriptId = sid; renderOutlineGate(await res.json()); }
      else await restoreGeneratorDraft(draft, progId);
    } else if (view === "scripts" || view === "curriculum") {
      window.switchSmeStudioView(view);
    } else {
      await restoreGeneratorDraft(draft, progId);
    }
  } catch (e) { console.error("Could not restore the screen:", e); showNotice("That link could not be opened."); }
  finally { smePopping = false; }
  history.replaceState({ sme: 1, prog: progId, view: smeView, script: (smeView === "script" || smeView === "outline") ? currentScriptId : null }, "", `#program/${progId}/${smeView}${(smeView === "script" || smeView === "outline") && currentScriptId ? "/" + currentScriptId : ""}`);
}

// ---- revising a script the Module Owner sent back: the generator opens with the script's own inputs
let smeRevising = null, smeRevisionScriptId = null;
function exitRevisionMode() {
  smeRevising = null;
  document.getElementById("smeReviseBox")?.classList.add("hidden");
  ["trackSelect", "moduleSelect", "classSelect"].forEach(id => { const el = document.getElementById(id); if (el) el.disabled = false; });
  const t = document.querySelector("#smeInputsContainer .sme-card-title"); if (t) t.textContent = "Create a class script";
  const b = document.getElementById("initScriptBtn"); if (b) b.textContent = "Generate Lesson Outline";
}
window.smeCancelRevision = function () {
  if (generatorHasUnsavedInput() && !confirm("Cancel the revision?\n\nYour changes to these inputs will be discarded. The script stays as it is.")) return;
  exitRevisionMode(); resetGeneratorForm();
  if (currentSelectedProgramId) onProgramChange({ target: { value: currentSelectedProgramId } });
  window.switchSmeStudioView("scripts");
};
window.smeReviseScript = async function (id) {
  if (!currentUser || !id) return;
  if (!confirmLeaveCreation("generator")) return;
  try {
    showLoader("Loading the script's inputs...");
    const res = await fetch(`${API_BASE}/scripts/${id}/inputs?user_id=${currentUser.id}`);
    const d = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof d.detail === "string" ? d.detail : "Could not load this script's inputs.");
    if (!d.revision_note) throw new Error("This script has no changes requested, so there is nothing to revise.");
    exitRevisionMode(); resetGeneratorForm();
    if (inFullScreen()) exitFullScreen();
    currentScriptId = null;
    window.switchSmeStudioView("generator");
    // the class is fixed: choose its track, module and class the way the author would, then put the script's own inputs on top
    const trackSel = document.getElementById("trackSelect"), modSel = document.getElementById("moduleSelect"), classSel = document.getElementById("classSelect");
    trackSel.value = String(d.track_id); await onTrackChange({ target: trackSel });
    modSel.value = String(d.module_id); await onModuleChange({ target: modSel });
    classSel.value = String(d.class_id); await onClassChange({ target: classSel });
    document.getElementById("classAgendasInput").value = (d.agendas || []).join("\n");
    document.getElementById("learningOutcomesInput").value = d.learning_outcomes || document.getElementById("learningOutcomesInput").value;
    const dur = document.getElementById("durationSelect"); dur.value = String(d.duration_minutes); dur.dispatchEvent(new Event("change"));
    const mix = document.getElementById("mixSlider"), code = document.getElementById("codeRatioSlider");
    mix.value = d.content_mix_beginner; mix.dispatchEvent(new Event("input"));
    code.value = d.code_ratio; code.dispatchEvent(new Event("input"));
    document.getElementById("referenceUrls").value = d.reference_urls || "";
    document.getElementById("apiDocs").value = d.api_docs || "";
    [trackSel, modSel, classSel].forEach(el => { el.disabled = true; });
    smeRevising = d;
    document.getElementById("smeReviseTitle").textContent = d.class_title;
    document.getElementById("smeReviseMeta").textContent = `Changes requested by ${d.revision_by || "the Module Owner"}${d.revision_at ? " \u00b7 " + parseServerTime(d.revision_at).toLocaleString() : ""}`;
    document.getElementById("smeReviseNote").textContent = d.revision_note;
    document.getElementById("smeReviseBox").classList.remove("hidden");
    document.querySelector("#smeInputsContainer .sme-card-title").textContent = "Revise class script";
    document.getElementById("initScriptBtn").textContent = "Regenerate Lesson Outline";
    window.scrollTo({ top: 0, behavior: "instant" });
  } catch (e) { alert(e.message); }
  finally { hideLoader(); }
};

// An outline on screen that has not been approved yet: nothing is kept of it, and the empty draft behind it is removed
function outlineIsOpen() {
  return !!currentScriptId && document.getElementById("outlineGate") && !document.getElementById("outlineGate").classList.contains("hidden");
}
async function discardOutlineDraft() {
  const id = currentScriptId;
  currentScriptId = null; currentOutlineCards = [];
  if (!id || !currentUser) return;
  try {
    const d = await fetch(`${API_BASE}/scripts/${id}?user_id=${currentUser.id}`).then(r => r.ok ? r.json() : null);
    if (d && d.cards_count > 0) return;                                  // it has a written script, so it is a real draft and stays
    await fetch(`${API_BASE}/scripts/${id}?user_id=${currentUser.id}`, { method: "DELETE" });
  } catch (e) { /* the draft is hidden from every list until it has cards anyway */ }
}

// Leaving the generator or an unapproved outline loses it, so the author is asked first. True means "go ahead".
function confirmLeaveCreation(target) {
  if (outlineIsOpen() && target !== "outline" && smeRevisionScriptId === currentScriptId) {
    if (!confirm("Leave without approving the new outline?\n\nThe new outline will be discarded. The script as it is now stays in your list.")) return false;
    smeRevisionScriptId = null; currentScriptId = null; currentOutlineCards = [];
    return true;
  }
  if (outlineIsOpen() && target !== "outline") {
    const msg = target === "generator"
      ? "Go back to the class inputs?\n\nThis outline has not been approved. If you go back it will be discarded, and no draft will be saved."
      : "Leave without approving the outline?\n\nIf you leave now the outline will be discarded, and no draft will be saved.";
    if (!confirm(msg)) return false;
    discardOutlineDraft();
    return true;
  }
  if (target === "generator" || target === "outline") return true;
  if (!generatorHasUnsavedInput()) return true;
  if (!confirm("Leave without generating?\n\nWhat you entered on this page has not been saved. If you leave now it will be discarded.")) return false;
  resetGeneratorForm();
  if (currentSelectedProgramId) onProgramChange({ target: { value: currentSelectedProgramId } });          // the track, module and class lists start again too
  return true;
}
const confirmLeaveGenerator = () => confirmLeaveCreation("landing");

async function selectSmeProgramCard(progId, progName = "") {
  currentSelectedProgramId = progId;
  const landing = document.getElementById("smeProgramsLandingView");
  const studio = document.getElementById("smeAuthoringStudioLayout");
  const header = document.querySelector(".app-header");
  if (landing) landing.classList.add("hidden");
  if (studio) studio.classList.remove("hidden");
  if (header) header.classList.remove("hidden");

  resetGeneratorForm();
  // Activate the clean generator view
  switchSmeStudioView("generator");
  document.getElementById("smeInputsContainer")?.classList.remove("hidden");
  document.getElementById("emptyState")?.classList.add("hidden");
  
  // Reset any active gates or studio views from previously opened scripts
  document.getElementById("outlineGate")?.classList.add("hidden");
  document.getElementById("studioView")?.classList.add("hidden");
  window.scrollTo({ top: 0, behavior: "instant" });

  // Update breadcrumb
  const pName = progName || (allSystemPrograms.find(p => p.id === progId)?.name) || "Program #" + progId;
  const breadcrumb = document.getElementById("smeSelectedProgramBreadcrumb");
  if (breadcrumb) {
    breadcrumb.textContent = pName;
  }

  // Pre-select program dropdown and auto-cascade inputs
  const progSel = document.getElementById("programSelect");
  if (progSel) {
    progSel.value = progId;
    await onProgramChange({ target: { value: progId } });
  }

  // Populate program scripts count in badge
  await renderProgramScriptsTable(progId, pName);
}

window.selectSmeProgramCard = selectSmeProgramCard;


// ==============================================================================
// CLASS TYPE BADGES + PROGRAM CURRICULUM VIEW
// ==============================================================================

const CLASS_TYPE_META = {
  "Live": { cls: "ct-live", hint: "Taught live" },
  "Inverted": { cls: "ct-inverted", hint: "Inverted class: learners prepare before the session" },
  "Lab": { cls: "ct-lab", hint: "Hands-on lab session" },
  "Recorded": { cls: "ct-recorded", hint: "Recorded class" },
  "To be planned": { cls: "ct-planned", hint: "Not planned yet" },
  "Unspecified": { cls: "ct-unspecified", hint: "The curriculum source does not say what type this class is" }
};

function classTypeBadge(type, countPrefix = "") {
  const t = type || "Live";
  const meta = CLASS_TYPE_META[t] || { cls: "ct-unspecified", hint: t };
  return `<span class="ct-badge ${meta.cls}" title="${escapeHtml(meta.hint)}">${countPrefix}${escapeHtml(t)}</span>`;
}

function contentChangeChip(c) {
  const pct = c && c.content_change_pct;
  if (!pct) return "";
  const why = c.content_change_description ? `${pct}% changed: ${c.content_change_description}` : `${pct}% changed`;
  return `<span class="cur-change" title="${escapeHtml(why)}">&Delta; ${pct}%</span>`;
}

// Classes parked past the official sequence (not in the curriculum list, but kept because they have a script)
function classSeqLabel(c) {
  if ((c.sequence_number || 0) >= 1000) {
    return '<span class="cur-extra" title="Not in the official curriculum list. Kept because it already has a script.">extra</span>';
  }
  return `#${c.sequence_number}`;
}

function classTypeCountsHtml(counts) {
  const entries = Object.entries(counts || {}).filter(([, n]) => n > 0);
  if (!entries.length) return '<span style="color: var(--text-muted);">-</span>';
  const order = Object.keys(CLASS_TYPE_META);
  const rank = t => (order.indexOf(t) < 0 ? order.length : order.indexOf(t));
  entries.sort((a, b) => rank(a[0]) - rank(b[0]));
  return entries.map(([t, n]) => classTypeBadge(t, `${n}&nbsp;`)).join(" ");
}

// Fills the class dropdown. The visible text carries the class type, so the plain title is kept in
// data-title for anything that needs it (the title field is pre-filled from there, not from the text).
function renderClassOptions(classSel, classes, selectedId = "") {
  classSel.innerHTML = '<option value="" selected>Choose a class</option>';
  classes.forEach(c => {
    const opt = document.createElement("option");
    opt.value = c.id;
    opt.dataset.title = c.title;
    opt.dataset.classType = c.class_type || "Live";
    opt.dataset.junction = c.junction_number == null ? "" : c.junction_number;
    opt.textContent = `${c.title}  ·  ${c.class_type || "Live"}`;
    classSel.appendChild(opt);
  });
  if (selectedId) classSel.value = selectedId;
}

function setClassTypeHint(option) {
  const hint = document.getElementById("classTypeHint");
  if (!hint) return;
  if (!option || !option.value) {
    hint.classList.add("hidden");
    hint.innerHTML = "";
    return;
  }
  const type = option.dataset.classType || "Live";
  hint.innerHTML = `<span>Class type:</span> ${classTypeBadge(type)}`;
  hint.classList.remove("hidden");
}

const smeCurriculum = { progId: null, programName: "", tracks: [], trackId: null, type: "All", query: "", handlersBound: false };

function bindSmeCurriculumHandlers() {
  if (smeCurriculum.handlersBound) return;
  smeCurriculum.handlersBound = true;
  document.getElementById("smeCurTracks")?.addEventListener("click", e => {
    const trackBtn = e.target.closest("[data-cur-track]");
    if (!trackBtn) return;
    smeCurriculum.trackId = parseInt(trackBtn.dataset.curTrack, 10);
    smeCurriculum.type = "All";
    renderSmeCurriculum();
  });
  document.getElementById("smeCurTypeSelect")?.addEventListener("change", e => { smeCurriculum.type = e.target.value; renderSmeCurriculumList(); });
  document.getElementById("smeCurSearch")?.addEventListener("input", e => { smeCurriculum.query = e.target.value; renderSmeCurriculumList(); });
}

async function loadSmeCurriculum(force = false, silent = false) {
  const progId = currentSelectedProgramId;
  const body = document.getElementById("smeCurriculumBody");
  if (!body) return;
  bindSmeCurriculumHandlers();
  if (!progId) {
    body.innerHTML = '<div class="cur-state">Select an enrolled program first.</div>';
    return;
  }
  if (!force && smeCurriculum.progId == progId && smeCurriculum.tracks.length) {
    renderSmeCurriculum();
    return;
  }

  if (!silent) body.innerHTML = '<div class="cur-state">Loading curriculum...</div>';
  try {
    if (!silent) showLoader("Loading program curriculum...");
    const res = await fetch(`${API_BASE}/curriculum/full-tree?program_id=${progId}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const tree = await res.json();
    const prog = (tree || [])[0];
    if (!prog) {
      smeCurriculum.tracks = [];
      body.innerHTML = '<div class="cur-state">No curriculum found for this program.</div>';
      return;
    }
    smeCurriculum.progId = progId;
    smeCurriculum.programName = prog.name;
    // Same track list the generator offers: specialisation pseudo-tracks are not shown
    smeCurriculum.tracks = (prog.tracks || []).filter(t => !t.name.toLowerCase().includes("specialisation"));
    const picked = parseInt(document.getElementById("trackSelect")?.value, 10);
    const enrolledTrack = smeCurriculum.tracks.find(t => ((currentUser && currentUser.track_ids) || []).includes(t.id));
    smeCurriculum.trackId = smeCurriculum.tracks.some(t => t.id === picked) ? picked : (enrolledTrack || smeCurriculum.tracks[0] || {}).id;
    smeCurriculum.type = "All";
    smeCurriculum.query = "";
    const search = document.getElementById("smeCurSearch");
    if (search) search.value = "";
    renderSmeCurriculum();
  } catch (err) {
    console.error("Error loading curriculum:", err);
    body.innerHTML = '<div class="cur-state">Could not load the curriculum. Try again.</div>';
  } finally {
    if (!silent) hideLoader();
  }
}

function currentCurriculumTrack() {
  return smeCurriculum.tracks.find(t => t.id === smeCurriculum.trackId) || smeCurriculum.tracks[0];
}

function renderSmeCurriculum() {
  const track = currentCurriculumTrack();
  const title = document.getElementById("smeCurriculumTitle");
  if (title) title.textContent = `${smeCurriculum.programName} Curriculum`;
  const tracksEl = document.getElementById("smeCurTracks");
  if (tracksEl) {                                                          // the same track tabs as the administrator's screen
    tracksEl.innerHTML = smeCurriculum.tracks.length ? '<div class="cur-tabs-label">Tracks</div>' + smeCurriculum.tracks.map(t => {
      const enrolled = smeCurriculum.tracks.filter(x => ((currentUser && currentUser.track_ids) || []).includes(x.id)).length;
      const mine = ((currentUser && currentUser.track_ids) || []).includes(t.id) && enrolled > 0 && enrolled < smeCurriculum.tracks.length;     // a star only helps when it sets some tracks apart
      const n = (t.modules || []).length;
      return `<button type="button" role="tab" class="cur-tab${track && t.id === track.id ? " active" : ""}" data-cur-track="${t.id}"><span>${escapeHtml(t.name)}${mine ? ' <span title="You are enrolled in this track">&#9733;</span>' : ""}</span><span class="cur-tab-count">${n} ${n === 1 ? "module" : "modules"}</span></button>`;
    }).join("") : "";
  }
  // The class types that exist in this track, for the filter
  const typeCounts = {};
  ((track && track.modules) || []).forEach(m => (m.classes || []).forEach(c => { const t = c.class_type || "Live"; typeCounts[t] = (typeCounts[t] || 0) + 1; }));
  const order = Object.keys(CLASS_TYPE_META);
  const types = Object.keys(typeCounts).sort((a, b) => order.indexOf(a) - order.indexOf(b));
  if (smeCurriculum.type !== "All" && !typeCounts[smeCurriculum.type]) smeCurriculum.type = "All";
  const sel = document.getElementById("smeCurTypeSelect");
  if (sel) {
    sel.innerHTML = '<option value="All">All class types</option>' + types.map(t => `<option value="${escapeHtml(t)}">${escapeHtml(t)} (${typeCounts[t]})</option>`).join("");
    sel.value = smeCurriculum.type;
  }
  renderSmeCurriculumList();
}

function highlightMatch(text, query) {
  const safe = escapeHtml(text);
  if (!query) return safe;
  const idx = text.toLowerCase().indexOf(query.toLowerCase());
  if (idx < 0) return safe;
  return escapeHtml(text.slice(0, idx)) + `<mark class="cur-mark">${escapeHtml(text.slice(idx, idx + query.length))}</mark>` + escapeHtml(text.slice(idx + query.length));
}

function renderSmeCurriculumList() {
  const body = document.getElementById("smeCurriculumBody");
  const track = currentCurriculumTrack();
  if (!body) return;
  if (!track) { body.innerHTML = '<div class="sa-empty">This program has no tracks yet.</div>'; return; }
  // the selected track, with only the classes of the chosen type; drawn with the same tree the administrator uses, read-only for authors
  const type = smeCurriculum.type;
  const modules = (track.modules || []).map(m => {
    const classes = (m.classes || []).filter(c => type === "All" || (c.class_type || "Live") === type);
    return { ...m, classes, class_count: classes.length };
  }).filter(m => type === "All" || m.classes.length);
  const shown = { ...track, modules, module_count: modules.length };
  const prog = { id: smeCurriculum.progId, name: smeCurriculum.programName, tracks: [shown] };
  renderCurriculumTree([prog], body);
  flattenAdminTrackView(body, prog, shown);
  if (smeCurriculum.query.trim()) filterAdminTree(smeCurriculum.query);
}


window.loadSmeCurriculum = loadSmeCurriculum;

// ==============================================================================
// 1. CASCADING SELECTION LOGIC
// ==============================================================================

async function loadPrograms(userId = null) {
  try {
    const targetUserId = userId || (currentUser ? currentUser.id : null);
    const url = targetUserId ? `${API_BASE}/programs?user_id=${targetUserId}` : `${API_BASE}/programs`;
    const res = await fetch(url);
    const programs = await res.json();
    const sel = document.getElementById("programSelect");
    sel.innerHTML = '<option value="">Select Program...</option>';
    programs.forEach(p => {
      sel.innerHTML += `<option value="${p.id}">${escapeHtml(p.name)}</option>`;
    });

    // Reset cascading children
    const trackSel = document.getElementById("trackSelect");
    const modSel = document.getElementById("moduleSelect");
    const classSel = document.getElementById("classSelect");
    if (trackSel) { trackSel.innerHTML = '<option value="">Choose a track</option>'; trackSel.disabled = true; }
    if (modSel) { modSel.innerHTML = '<option value="">Choose a module</option>'; modSel.disabled = true; }
    if (classSel) { classSel.innerHTML = '<option value="">Choose a class</option>'; classSel.disabled = true; }
    const classTitleInput = document.getElementById("classTitleInput");
    if (classTitleInput) classTitleInput.value = "";
  } catch (err) {
    console.error("Error loading programs:", err);
  }
}

async function onProgramChange(e) {
  const progId = e.target.value;
  const trackSel = document.getElementById("trackSelect");
  const modSel = document.getElementById("moduleSelect");
  const classSel = document.getElementById("classSelect");
  const classTitleInput = document.getElementById("classTitleInput");
  const classAgendasInput = document.getElementById("classAgendasInput");

  if (trackSel) {
    trackSel.innerHTML = '<option value="" selected>Choose a track</option>';
    trackSel.disabled = true;
  }
  if (modSel) {
    modSel.innerHTML = '<option value="" selected>Choose a module</option>';
    modSel.disabled = true;
  }
  if (classSel) {
    classSel.innerHTML = '<option value="" selected>Choose a class</option>';
    classSel.disabled = true;
  }
  if (classTitleInput) classTitleInput.value = "";
  if (classAgendasInput) classAgendasInput.value = "";
  setClassTypeHint(null);

  const outlineBanner = document.getElementById("levelOutlineSummaryBanner");
  if (outlineBanner) {
    outlineBanner.classList.add("hidden");
    outlineBanner.innerHTML = "";
  }

  if (!progId) return;

  currentSelectedProgramId = progId;
  const progSel = document.getElementById("programSelect");
  const pName = progSel?.selectedOptions[0]?.text || "";
  renderProgramScriptsTable(progId, pName);

  try {
    showLoader("Loading Tracks for Enrolled Program...");
    const res = await fetch(`${API_BASE}/tracks?program_id=${progId}`);
    const tracks = await res.json();
    if (trackSel) {
      trackSel.innerHTML = '<option value="" selected>Choose a track</option>';

      // Remove Specialisations options: only keep official core program tracks
      const filteredTracks = tracks.filter(t => 
        !t.name.toLowerCase().includes("specialisation") && 
        t.id !== 135 && 
        t.id !== 136
      );

      const enrolled = (currentUser && currentUser.track_ids) || [];
      filteredTracks.sort((a, b) => (enrolled.includes(b.id) ? 1 : 0) - (enrolled.includes(a.id) ? 1 : 0));
      filteredTracks.forEach(t => {
        const opt = document.createElement("option");
        opt.value = t.id;
        opt.dataset.name = t.name;
        opt.textContent = enrolled.includes(t.id) && filteredTracks.length > 1 ? `${t.name}  (your track)` : t.name;
        trackSel.appendChild(opt);
      });

      trackSel.disabled = false;
      // Do NOT auto-select the first track: "Choose Track" by default
    }
  } finally {
    hideLoader();
  }
}

let currentLevelModules = [];

async function onTrackChange(e) {
  const trackId = e.target.value;
  const modSel = document.getElementById("moduleSelect");
  const classSel = document.getElementById("classSelect");
  const classTitleInput = document.getElementById("classTitleInput");
  const classAgendasInput = document.getElementById("classAgendasInput");

  if (modSel) {
    modSel.innerHTML = '<option value="" selected>Choose a module</option>';
    modSel.disabled = true;
  }
  if (classSel) {
    classSel.innerHTML = '<option value="" selected>Choose a class</option>';
    classSel.disabled = true;
  }
  if (classTitleInput) classTitleInput.value = "";
  if (classAgendasInput) classAgendasInput.value = "";
  setClassTypeHint(null);

  const outlineBanner = document.getElementById("levelOutlineSummaryBanner");
  if (outlineBanner) {
    outlineBanner.classList.add("hidden");
    outlineBanner.innerHTML = "";
  }

  currentLevelModules = [];
  if (!trackId) return;

  try {
    showLoader("Loading Modules for Selected Track...");
    const res = await fetch(`${API_BASE}/modules?track_id=${trackId}`);
    const modules = await res.json();
    currentLevelModules = modules || [];

    if (modSel) {
      modSel.innerHTML = '<option value="" selected>Choose a module</option>';

      // Group modules by curriculum segment
      const segmentMap = new Map();
      modules.forEach(m => {
        const seg = m.segment || "Core Curriculum";
        if (!segmentMap.has(seg)) {
          segmentMap.set(seg, []);
        }
        segmentMap.get(seg).push(m);
      });

      if (segmentMap.size > 1) {
        segmentMap.forEach((modList, segName) => {
          const optGroup = document.createElement("optgroup");
          optGroup.label = segName;
          modList.forEach(m => {
            const opt = document.createElement("option");
            opt.value = m.id;
            const dur = m.duration_label ? ` (${m.duration_label})` : "";
            const modType = (m.module_type && m.module_type !== "Core") ? ` [${m.module_type}]` : "";
            opt.textContent = `${m.name}${modType}${dur}`;
            optGroup.appendChild(opt);
          });
          modSel.appendChild(optGroup);
        });
      } else {
        modules.forEach(m => {
          const opt = document.createElement("option");
          opt.value = m.id;
          const dur = m.duration_label ? ` (${m.duration_label})` : "";
          const modType = (m.module_type && m.module_type !== "Core") ? ` [${m.module_type}]` : "";
          opt.textContent = `${m.name}${modType}${dur}`;
          modSel.appendChild(opt);
        });
      }
      modSel.disabled = false;
      // Do NOT auto-select the first module: "Choose Module" by default
    }

  } finally {
    hideLoader();
  }
}

function openLevelOutlineModal() {
  const modal = document.getElementById("courseOutlineModal");
  const trackSel = document.getElementById("trackSelect");
  const levelName = trackSel?.selectedOptions[0]?.dataset?.name || trackSel?.selectedOptions[0]?.text || "Selected Level";
  const title = document.getElementById("courseOutlineModalTitle");
  const subtitle = document.getElementById("courseOutlineModalSubtitle");
  const tbody = document.getElementById("courseOutlineModalTableBody");

  if (!modal || !tbody) return;

  if (title) title.textContent = `Course Outline for ${levelName}`;
  if (subtitle) {
    subtitle.textContent = `Showing all ${currentLevelModules.length} modules configured for this level, organized by curriculum segment. Selecting a module from the dropdown allows authoring scripts for any of its classes.`;
  }

  let rowsHtml = "";
  currentLevelModules.forEach((m, idx) => {
    const seg = m.segment || "Core";
    const modType = m.module_type || "Core";
    const dur = m.duration_label || "1 month";
    const cCount = m.class_count || 0;
    const typeColor = modType === "Specialisation" ? "var(--accent-primary, #3b82f6)" : (modType === "Elective" ? "#9333ea" : "var(--accent-success, #10b981)");

    rowsHtml += `
      <tr style="border-bottom: 1px solid var(--border-subtle);">
        <td style="padding: 10px 14px; color: var(--text-secondary); font-weight: 600;">${idx + 1}</td>
        <td style="padding: 10px 14px; font-weight: 600; color: var(--text-primary);">${escapeHtml(seg)}</td>
        <td style="padding: 10px 14px;"><span style="font-size: 0.75rem; padding: 2px 8px; border-radius: 4px; background: rgba(59,130,246,0.1); color: ${typeColor}; font-weight: 600;">${escapeHtml(modType)}</span></td>
        <td style="padding: 10px 14px; font-weight: 600; color: var(--text-primary);">${escapeHtml(m.name)}</td>
        <td style="padding: 10px 14px; color: var(--text-secondary);">${escapeHtml(dur)}</td>
        <td style="padding: 10px 14px; text-align: center; font-weight: 600; color: var(--text-secondary);">${cCount}</td>
        <td style="padding: 10px 14px;"><div style="display: flex; gap: 6px; flex-wrap: wrap;">${classTypeCountsHtml(m.class_type_counts)}</div></td>
      </tr>
    `;
  });

  tbody.innerHTML = rowsHtml || '<tr><td colspan="7" style="padding: 20px; text-align: center; color: var(--text-secondary);">No modules found for this level.</td></tr>';
  modal.classList.remove("hidden");
}

function closeLevelOutlineModal() {
  const modal = document.getElementById("courseOutlineModal");
  if (modal) modal.classList.add("hidden");
}

window.openLevelOutlineModal = openLevelOutlineModal;
window.closeLevelOutlineModal = closeLevelOutlineModal;

async function onModuleChange(e) {
  const modId = e.target.value;
  const classSel = document.getElementById("classSelect");
  const classTitleInput = document.getElementById("classTitleInput");
  const classAgendasInput = document.getElementById("classAgendasInput");

  if (classSel) {
    classSel.innerHTML = '<option value="" selected>Choose a class</option>';
    classSel.disabled = true;
  }
  if (classTitleInput) classTitleInput.value = "";
  if (classAgendasInput) classAgendasInput.value = "";
  setClassTypeHint(null);

  if (!modId) return;

  try {
    showLoader("Loading Classes for Selected Module...");
    const res = await fetch(`${API_BASE}/classes?module_id=${modId}`);
    const classes = await res.json();
    if (classSel) {
      renderClassOptions(classSel, classes);
      classSel.disabled = false;
      // Do NOT auto-select the first class, per user requirement: "Choose a class" by default
    }
  } finally {
    hideLoader();
  }
}

async function onClassChange(e) {
  const classId = e.target.value;
  const classSel = document.getElementById("classSelect");
  const classTitleInput = document.getElementById("classTitleInput");
  const agendasInput = document.getElementById("classAgendasInput");

  if (!classId) {
    if (classTitleInput) classTitleInput.value = "";
    if (agendasInput) agendasInput.value = "";
    setClassTypeHint(null);
    return;
  }

  // Pre-fill selected title into editable input
  if (classTitleInput && classSel) {
    const selectedOpt = classSel.selectedOptions[0];
    const selectedText = selectedOpt?.dataset?.title || selectedOpt?.text || "";
    if (selectedText && selectedText !== "Choose a class") {
      classTitleInput.value = selectedText;
    }
    setClassTypeHint(selectedOpt);
  }

  try {
    showLoader("Loading Class Agendas...");
    const res = await fetch(`${API_BASE}/class/${classId}`);
    const data = await res.json();

    if (classTitleInput) classTitleInput.value = data.title || "";
    if (agendasInput) {
      // Pre-fill agendas, SME can edit them manually
      agendasInput.value = (data.agendas || []).join("\n");
    }
    const refNote = document.getElementById("prevScriptFileInput")?.closest(".form-group");
    document.getElementById("ownerRefNote")?.remove();
    if (refNote && data.has_reference_script) refNote.insertAdjacentHTML("beforeend", `<div id="ownerRefNote" class="gen-hint" style="margin-top:4px;color:#15803d;">The Module Owner attached a previous script${data.reference_script_name ? " (" + escapeHtml(data.reference_script_name) + ")" : ""} for this class. It is used as reference.</div>`);
    const outcomesInput = document.getElementById("learningOutcomesInput");
    if (outcomesInput) {                                                   // the Module Owner's outcomes for this class, unless it only has the generic placeholder text
      const mo = (data.learning_outcomes || "").trim();
      outcomesInput.value = mo && !mo.startsWith("Master foundational concepts and industry best practices for") ? mo : "";
    }

    const rawDur = data.duration_minutes || 90;
    const durMins = Math.round(rawDur / 10) * 10 || 90;
    const durSelect = document.getElementById("durationSelect");
    const customDurInput = document.getElementById("customDurationInput");
    const durVal = document.getElementById("durationVal");
    if (customDurInput) customDurInput.value = durMins;
    if (durVal) durVal.textContent = `${durMins} mins`;
    if (durSelect) durSelect.value = String(durMins);

    const mem = data.context_memory_preview || {};
    const memAnalogy = document.getElementById("memoryAnalogy");
    if (memAnalogy) memAnalogy.innerHTML = `<strong>Anchor Analogy:</strong> ${mem.anchor_analogy || 'Real-Time System'}`;
    const cliff = mem.cliffhanger ? `<br><strong>Cliffhanger Bridge:</strong> ${mem.cliffhanger}` : '';
    const memBridge = document.getElementById("memoryBridge");
    if (memBridge) memBridge.innerHTML = `<strong>Continuity:</strong> ${mem.is_first_class ? 'Inaugural Class: Foundational mental model setup.' : `Continuing Class: Bridges from ${mem.previous_class_title || 'prior class'}.${cliff}`}`;

    // Remove any existing script notice banner if previously rendered
    const existingNotice = document.getElementById("existingClassScriptNotice");
    if (existingNotice) existingNotice.remove();
  } finally {
    hideLoader();
  }
}

// ==============================================================================
// 2. SCRIPT INITIALIZATION & OUTLINE GENERATION
// ==============================================================================

async function onInitScript() {
  const classSel = document.getElementById("classSelect");
  const classId = classSel?.value;
  const customTitle = (classSel?.selectedOptions[0]?.text && classSel.value) ? classSel.selectedOptions[0].text : null;
  const rawAgendas = document.getElementById("classAgendasInput")?.value.trim() || "";
  const customAgendas = rawAgendas ? rawAgendas.split("\n").map(s => s.trim()).filter(Boolean) : null;

  const mixBeg = parseInt(document.getElementById("mixSlider").value);
  const codeRatio = parseInt(document.getElementById("codeRatioSlider").value);
  const directives = document.getElementById("additionalDirectives")?.value || "";
  const apiDocs = document.getElementById("apiDocs")?.value || "";
  const refUrls = document.getElementById("referenceUrls")?.value || null;
  const prevScript = document.getElementById("prevScriptUpload")?.value || "";

  const trackSel = document.getElementById("trackSelect");
  if (!trackSel?.value) {
    alert("Please select a Track before generating the outline.");
    trackSel?.focus();
    return;
  }

  const modSel = document.getElementById("moduleSelect");
  if (!modSel?.value) {
    alert("Please select a Module before generating the outline.");
    modSel?.focus();
    return;
  }

  if (!classId) {
    alert("Please select a Class Title before generating the outline.");
    classSel?.focus();
    return;
  }

  if (!rawAgendas) {
    alert("Class Agendas is required. Please enter at least one agenda for this class.");
    document.getElementById("classAgendasInput")?.focus();
    return;
  }

  const learningOutcomes = document.getElementById("learningOutcomesInput")?.value.trim() || "";

  const durSelElem = document.getElementById("durationSelect");
  if (!durSelElem?.value) {
    alert("Please choose a Class Duration before generating the outline.");
    durSelElem?.focus();
    return;
  }
  const duration = parseInt(durSelElem.value, 10);

  for (const [sliderId, errId] of [["mixSlider", "mixError"], ["codeRatioSlider", "codeError"]]) {       // both are choices the author must make
    const sl = document.getElementById(sliderId);
    if (sl?.classList.contains("gen-unset")) {
      document.getElementById(errId)?.classList.remove("hidden");
      sl.scrollIntoView({ block: "center", behavior: "smooth" }); sl.focus();
      return;
    }
  }


  try {
    showLoader("Initializing Script & Querying Memory Ledger...");

    const initUrl = smeRevising
      ? `${API_BASE}/scripts/${smeRevising.script_id}/revise?user_id=${currentUser.id}`        // the same script, started again
      : (currentUser ? `${API_BASE}/scripts/init?user_id=${currentUser.id}` : `${API_BASE}/scripts/init`);
    const initRes = await fetch(initUrl, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        class_id: parseInt(classId),
        custom_class_title: customTitle,
        custom_agendas: customAgendas,
        duration_minutes: duration,
        content_mix_beginner: mixBeg,
        content_mix_advanced: 100 - mixBeg,
        code_ratio: codeRatio,
        additional_resources: (refUrls || directives || ""),
        api_docs: apiDocs,
        reference_urls: refUrls,
        learning_outcomes: learningOutcomes,
        // review_lenses is not sent: the server picks the reviewers from the track (Beginner, Intermediate, Advanced) and the program
        previous_script_context: prevScript
      })
    });

    if (!initRes.ok) {
      let errDetail = "Script initialization failed";
      try { const e = await initRes.json(); errDetail = e.detail || errDetail; } catch {}
      throw new Error(errDetail);
    }

    const initData = await initRes.json();
    currentScriptId = initData.script_id;
    currentShareableUuid = initData.shareable_uuid;
    if (smeRevising) { smeRevisionScriptId = initData.script_id; exitRevisionMode(); }
    clearGeneratorDraft();                                                  // the outline exists now; the typed inputs belong to it

    updateScriptStatusBadge("Script WIP", "status-wip");

    showLoader("Claude Sonnet Architecting 14-18 Card Outline...");
    const outlineRes = await fetch(`${API_BASE}/scripts/${currentScriptId}/outline`, {
      method: "POST"
    });
    if (!outlineRes.ok) {
      let errDetail = "Outline generation failed";
      try { const e = await outlineRes.json(); errDetail = e.detail || errDetail; } catch {}
      throw new Error(errDetail);
    }
    const outlineData = await outlineRes.json();
    currentOutlineCards = outlineData.cards;

    renderOutlineGate(outlineData);
  } catch (err) {
    alert("Error initializing script: " + err);
  } finally {
    hideLoader();
  }
}

function renderOutlineGate(outlineData) {
  switchSmeStudioView("outline");

  if (outlineData && outlineData.cards) {
    currentOutlineCards = outlineData.cards;
  }

  const gate = document.getElementById("outlineGate");
  if (gate) {
    gate.scrollIntoView({ behavior: "smooth" });
  }

  const minsElem = document.getElementById("outlineTotalMins");
  if (minsElem) minsElem.textContent = outlineData.total_duration_mins ? outlineData.total_duration_mins.toFixed(1) : "90.0";
  const countElem = document.getElementById("outlineCardCount");
  if (countElem) countElem.textContent = outlineData.total_cards || (outlineData.cards ? outlineData.cards.length : 0);

  const grid = document.getElementById("outlineCardsList");
  grid.innerHTML = "";

  // Which visual cards belong to which card: the outline's cards carry ids, the flat list only positions.
  const visualCards = Array.isArray(outlineData.visual_cards) ? outlineData.visual_cards : [];
  const cardIdByIndex = {};
  let runningIndex = 1;
  (outlineData.sections || []).forEach(sec => (sec.cards || []).forEach(card => { cardIdByIndex[runningIndex++] = card.card_id; }));
  const KIND_LABEL = { diagram: "Diagram", animation: "Animation", image: "Image" };
  const VISUAL_MAKER = { diagram: "made by Claude", image: "made by Gemini", animation: "made by Claude" };

  // The SME's plan, in one line: the beginner/advanced split and the code ratio, as it was planned onto the cards
  document.getElementById("outlinePlanSummary")?.remove();
  const plan = outlineData.plan_summary;
  if (plan) {
    const mins = plan.level_minutes || {};
    const arc = plan.beginner_pct === null || plan.beginner_pct === undefined ? "" :
      (plan.beginner_pct >= 100 ? "All beginner level." : plan.beginner_pct <= 0 ? "All advanced level." :
       `Starts at beginner level (${mins.beginner || 0} min), steps up${mins.bridge ? ` at one bridge card (${mins.bridge} min)` : ""}, then goes advanced (${mins.advanced || 0} min): ${plan.beginner_pct}% beginner, ${plan.advanced_pct}% advanced.`);
    const box = document.createElement("div");
    box.id = "outlinePlanSummary";
    box.style.cssText = "margin: 10px 0; padding: 10px 14px; border: 1px solid var(--border-color, #cbd5e1); border-radius: 8px; font-size: 0.88rem;";
    box.innerHTML = `<strong>Your plan:</strong> ${escapeHtml(arc)} ${plan.code_ratio !== null && plan.code_ratio !== undefined ? `${plan.coded_cards} of ${plan.cards} cards carry code (about ${Math.round(100 * plan.coded_cards / Math.max(1, plan.cards))}%, you asked for ${plan.code_ratio}%).` : ""}`;
    grid.parentNode.insertBefore(box, grid);
  }

  // The classes around this one: what it builds on, and what it must not teach
  document.getElementById("outlineContinuity")?.remove();
  const cont = outlineData.continuity;
  if (cont) {
    const prev = cont.previous;
    const upcoming = cont.upcoming || [];
    const prevLine = !prev
      ? "This is the first class of its module: nothing has been taught before it."
      : (!prev.approved && prev.draft
          ? `Continues from Class ${prev.number} "${escapeHtml(prev.title)}". Its script is not approved yet, so this class follows its newest draft. If that draft changes, check the opening of this script again.`
      : prev.approved && prev.has_record
          ? `Continues from Class ${prev.number} "${escapeHtml(prev.title)}" (script approved${prev.approved_by ? " by " + escapeHtml(prev.approved_by) : ""}${prev.approved_at ? " on " + escapeHtml(prev.approved_at) : ""}). Learners already know what it taught, and this class starts where it stopped.`
          : `Class ${prev.number} "${escapeHtml(prev.title)}" has no approved script yet, so there is no record of what it taught. Ask the Module Owner to approve it first, or this class can only assume its agenda was covered.`);
    const warn = prev && !(prev.approved && prev.has_record);
    const box2 = document.createElement("div");
    box2.id = "outlineContinuity";
    box2.style.cssText = `margin: 10px 0; padding: 10px 14px; border: 1px solid ${warn ? "#f59e0b" : "var(--border-color, #cbd5e1)"}; border-radius: 8px; font-size: 0.88rem; ${warn ? "background: rgba(245, 158, 11, 0.08);" : ""}`;
    box2.innerHTML = `<div>${prevLine}</div>` + (upcoming.length
      ? `<div style="margin-top: 4px; color: var(--text-muted);">Not taught here (they come later): ${upcoming.slice(0, 5).map(u => `Class ${u.number} "${escapeHtml(u.title)}"`).join(", ")}${upcoming.length > 5 ? ` and ${upcoming.length - 5} more` : ""}.</div>`
      : `<div style="margin-top: 4px; color: var(--text-muted);">This is the last class of the module.</div>`);
    grid.parentNode.insertBefore(box2, grid);
  }

  // Overlap with the other classes of the module, shown before the SME approves the outline
  document.getElementById("outlineOverlap")?.remove();
  const flags = Array.isArray(outlineData.overlap_flags) ? outlineData.overlap_flags : [];
  const ob = document.createElement("div");
  ob.id = "outlineOverlap";
  ob.className = "ol-overlap" + (flags.length ? " warn" : "");
  ob.innerHTML = flags.length
    ? `<div class="ol-overlap-head">Overlap with other classes (${flags.length})</div><div class="ol-overlap-sub">Check these before you approve the outline. Edit the card, or approve it if the overlap is intended.</div><ul>${flags.map(f => `<li>${f.card_index ? `<a href="javascript:void(0)" onclick="document.getElementById('outlineCardsList')?.children[${Number(f.card_index) - 1}]?.scrollIntoView({behavior:'smooth',block:'center'})">Card ${Number(f.card_index)}${f.card_title ? ` &ldquo;${escapeHtml(f.card_title)}&rdquo;` : ""}</a>: ` : ""}${escapeHtml(f.note || "")}${f.topic ? ` <span class="ol-topic">(${escapeHtml(f.topic)})</span>` : ""}${f.kind === "upcoming" && f.card_index ? ` <button type="button" class="btn btn-secondary ol-fix" onclick="fixOverlap(${Number(f.card_index)}, ${JSON.stringify(f.topic || "").replace(/"/g, "&quot;")}, ${JSON.stringify(f.class_title || "").replace(/"/g, "&quot;")})">Fix this</button>` : ""}</li>`).join("")}</ul>`
    : `<div class="ol-overlap-head ok">No overlap found with the classes before or after this one.</div>`;
  grid.parentNode.insertBefore(ob, grid);

  // Render cards with Edit and Chat buttons on each card
  outlineData.cards.forEach((c) => {
    const mine = visualCards.filter(v => v.parent_card_id && v.parent_card_id === cardIdByIndex[c.card_index]);
    const visTag = mine.length ? mine.map(v => `[${KIND_LABEL[v.kind] || "Visual"} ${escapeHtml(v.visual_id)}]`).join(" ")
                 : c.visual_type === "mermaid" ? "[Diagram]"
                 : c.visual_type === "image"   ? "[Image]"
                 : c.visual_type === "animation" ? "[Animation]"
                 : "[Card]";
    grid.innerHTML += `
      <div class="outline-card" id="outlineCardNode_${c.card_index}">
        <div class="outline-card-top">
          <span class="card-num-badge">Card ${c.card_index}</span>
          <span class="card-time-badge">${c.duration_mins}m</span>
        </div>
        <div class="outline-card-title">${escapeHtml(c.title)}</div>
        <div class="outline-card-desc" id="outlineCardDesc_${c.card_index}">${escapeHtml(c.key_concept || c.pedagogical_phase)}</div>
        <div class="outline-card-bottom">
          <span class="phase-tag">${escapeHtml(c.pedagogical_phase)}</span>
          ${c.level ? `<span class="level-chip level-${escapeHtml(c.level)}" title="Planned from the beginner/advanced split">${escapeHtml(c.level)}</span>` : ""}
          ${c.has_code ? `<span class="level-chip code-chip" title="Planned from the code ratio: this card carries code">Code</span>` : ""}
          <span class="visual-indicator">${visTag}</span>
        </div>
        <div class="outline-card-actions">
          <button type="button" class="btn btn-edit" onclick="openSingleOutlineCardEdit(${c.card_index})" title="Edit Card ${c.card_index}">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="margin-right:3px;"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path><path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path></svg>
            Edit
          </button>
          <button type="button" class="btn btn-chat" onclick="openSingleOutlineCardChat(${c.card_index})" title="Chat / Revise Card ${c.card_index} with AI">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="margin-right:3px;"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path></svg>
            Chat
          </button>
        </div>
      </div>
    `;
    // The visual cards planned for this card sit right after it, in the order they will be made.
    mine.forEach(v => {
      grid.innerHTML += `
        <div class="outline-card outline-visual-card" id="outlineVisualNode_${escapeHtml(v.visual_id)}" style="border-style: dashed;">
          <div class="outline-card-top">
            <span class="card-num-badge">${escapeHtml(v.visual_id)}</span>
            <span class="card-time-badge">${escapeHtml(VISUAL_MAKER[v.kind] || "")}</span>
          </div>
          <div class="outline-card-title">${escapeHtml(KIND_LABEL[v.kind] || "Visual")}: ${escapeHtml(v.title)}</div>
          <div class="outline-card-desc"><strong>Prompt:</strong> ${escapeHtml(v.prompt || "")}</div>
          <div class="outline-card-desc"><strong>Serves:</strong> ${escapeHtml([].concat(v.serves_agenda_items || [], v.serves_outcomes || []).join("; ") || "-")}</div>
          <div class="outline-card-bottom"><span class="phase-tag">After card ${c.card_index}</span><span class="visual-indicator">${(v.elements || []).length} elements</span></div>
          <div class="outline-card-actions"><button type="button" class="btn btn-edit" onclick="openVisualEditor('${escapeHtml(v.visual_id)}')">Edit prompt</button></div>
        </div>`;
    });
  });

  renderOutlineVisualCards(grid, visualCards, cardIdByIndex, outlineData);
}

// The visual cards planned in the outline (each with its prompt and the agenda item it serves) and the checks the
// outline failed. Shown under the card list so the SME can approve them with the outline.
function renderOutlineVisualCards(grid, visualCards, cardIdByIndex, outlineData) {
  const old = document.getElementById("outlineVisualCards");
  if (old) old.remove();
  const checks = Array.isArray(outlineData.checks) ? outlineData.checks : [];
  if (!visualCards.length && !checks.length) return;
  const checkHtml = checks.length ? `
    <div class="outline-checks" style="margin: 10px 0; padding: 10px 14px; border: 1px solid var(--border-color, #f59e0b); border-radius: 8px;">
      <strong>Outline checks (${checks.length})</strong>
      <ul style="margin: 6px 0 0 18px;">${checks.map(c => `<li><em>${escapeHtml(c.level || "")}</em>: ${escapeHtml(c.problem || "")}</li>`).join("")}</ul>
    </div>` : `<div class="outline-checks" style="margin: 10px 0; color: var(--text-muted);">Outline checks: all agenda items and outcomes are mapped, and the minutes add up. ${visualCards.length} visual cards are planned, each shown after the card it sits on.</div>`;
  const box = document.createElement("div");
  box.id = "outlineVisualCards";
  box.innerHTML = checkHtml;
  grid.parentNode.insertBefore(box, grid.nextSibling);
}

// Single Card Edit Modal Handler
function openSingleOutlineCardEdit(cardIndex) {
  const card = (currentOutlineCards || []).find(c => c.card_index === cardIndex);
  if (!card) {
    alert("Card not found");
    return;
  }

  document.getElementById("singleCardEditIndex").value = cardIndex;
  document.getElementById("singleCardEditModalTitle").textContent = `Edit Card ${cardIndex}: ${card.title}`;
  document.getElementById("singleCardEditTitle").value = card.title || "";
  document.getElementById("singleCardEditDuration").value = card.duration_mins || 6;
  
  const phaseSelect = document.getElementById("singleCardEditPhase");
  if (phaseSelect) {
    let matched = false;
    for (let opt of phaseSelect.options) {
      if (opt.value === card.pedagogical_phase) {
        opt.selected = true;
        matched = true;
        break;
      }
    }
    if (!matched) {
      phaseSelect.value = "Concept";
    }
  }

  document.getElementById("singleCardEditConcept").value = card.key_concept || "";
  document.getElementById("singleCardEditModal").classList.remove("hidden");
}

window.openSingleOutlineCardEdit = openSingleOutlineCardEdit;

// Save Single Card Edit
async function saveSingleCardEdit() {
  const cardIndex = parseInt(document.getElementById("singleCardEditIndex").value);
  const card = (currentOutlineCards || []).find(c => c.card_index === cardIndex);
  if (!card) return;

  const newTitle = document.getElementById("singleCardEditTitle").value.trim();
  const newDuration = parseFloat(document.getElementById("singleCardEditDuration").value);
  const newPhase = document.getElementById("singleCardEditPhase").value;
  const newConcept = document.getElementById("singleCardEditConcept").value.trim();

  if (!newTitle) {
    alert("Please provide a title for the card.");
    return;
  }

  if (!confirm(`Are you sure you want to save changes to Card ${cardIndex}?`)) {
    return;
  }

  card.title = newTitle;
  card.duration_mins = !isNaN(newDuration) && newDuration > 0 ? newDuration : card.duration_mins;
  card.pedagogical_phase = newPhase;
  card.key_concept = newConcept;

  document.getElementById("singleCardEditModal").classList.add("hidden");

  try {
    showLoader(`Saving updates to Card ${cardIndex}...`);
    const res = await fetch(`${API_BASE}/scripts/${currentScriptId}/outline/action`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "EDIT",
        modified_cards: currentOutlineCards
      })
    });
    if (!res.ok) throw new Error("Failed to save outline card edit");
    
    renderOutlineGate({
      total_duration_mins: currentOutlineCards.reduce((acc, c) => acc + (c.duration_mins || 0), 0),
      total_cards: currentOutlineCards.length,
      cards: currentOutlineCards
    });
  } catch (err) {
    alert("Error updating card: " + err.message);
  } finally {
    hideLoader();
  }
}

window.saveSingleCardEdit = saveSingleCardEdit;

// Single Card Chat Modal Handler
function openSingleOutlineCardChat(cardIndex) {
  const card = (currentOutlineCards || []).find(c => c.card_index === cardIndex);
  if (!card) {
    alert("Card not found");
    return;
  }

  document.getElementById("singleCardChatIndex").value = cardIndex;
  const titleElem = document.getElementById("singleCardChatModalTitle");
  if (titleElem) titleElem.textContent = `Chat — Card ${cardIndex}`;
  
  const promptInput = document.getElementById("singleCardChatPrompt");
  if (promptInput) promptInput.value = "";
  
  document.getElementById("singleCardChatModal").classList.remove("hidden");
  setTimeout(() => {
    if (promptInput) promptInput.focus();
  }, 100);
}

window.openSingleOutlineCardChat = openSingleOutlineCardChat;

function applyQuickPromptChip(text) {
  const promptArea = document.getElementById("singleCardChatPrompt");
  if (!promptArea) return;
  if (promptArea.value.trim()) {
    promptArea.value += "; " + text;
  } else {
    promptArea.value = text;
  }
  promptArea.focus();
}

window.applyQuickPromptChip = applyQuickPromptChip;

// Submit Single Card Chat Assist
async function submitSingleCardChat() {
  const cardIndex = parseInt(document.getElementById("singleCardChatIndex").value);
  const promptText = document.getElementById("singleCardChatPrompt").value.trim();

  if (!promptText) {
    alert("Please enter instructions for the card revision.");
    return;
  }

  if (!confirm(`Are you sure you want to revise Card ${cardIndex} with this AI prompt?`)) {
    return;
  }

  document.getElementById("singleCardChatModal").classList.add("hidden");

  try {
    showLoader(`Revising Card ${cardIndex} with Claude Sonnet...`);
    const res = await fetch(`${API_BASE}/scripts/${currentScriptId}/outline/card-assist`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        card_index: cardIndex,
        user_prompt: promptText
      })
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "Could not update card");
    }

    // Fetch the updated outline from backend
    const outlineRes = await fetch(`${API_BASE}/scripts/${currentScriptId}/outline`);
    if (outlineRes.ok) {
      const outlineData = await outlineRes.json();
      currentOutlineCards = outlineData.cards || [];
      renderOutlineGate(outlineData);
    }
  } catch (err) {
    alert("Error revising card: " + err.message);
  } finally {
    hideLoader();
  }
}

window.submitSingleCardChat = submitSingleCardChat;

// One click: the card is revised to drop the later class's topic. The wording avoids the words that would let the revision change the agenda, outcomes or time.
async function fixOverlap(cardIndex, topic, classTitle) {
  if (!confirm(`Revise Card ${cardIndex} so it no longer teaches "${topic}"? The later class "${classTitle}" covers it. The rest of the card stays as it is.`)) return;
  try {
    showLoader(`Fixing the overlap on Card ${cardIndex}...`);
    const res = await fetch(`${API_BASE}/scripts/${currentScriptId}/outline/card-assist`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ card_index: cardIndex, user_prompt: `Remove everything from this card that teaches "${topic}" (the later class "${classTitle}" covers it). Keep only what the card's own listed items need. In the plan note write one sentence: "Do not teach ${topic}; a later class covers it." Keep everything else as it is.` })
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || "Could not update the card");
    const outlineRes = await fetch(`${API_BASE}/scripts/${currentScriptId}/outline`);
    if (outlineRes.ok) { const data = await outlineRes.json(); currentOutlineCards = data.cards || []; renderOutlineGate(data); }
  } catch (err) {
    alert("Could not fix the overlap: " + (err.message || err));
  } finally {
    hideLoader();
  }
}
window.fixOverlap = fixOverlap;

async function onOutlineRethink() {
  const feedback = prompt("Enter ReThink Feedback for the Outliner (e.g. 'Make section 2 more hands-on with BookMyShow analogy'):");
  if (feedback === null) return;

  try {
    showLoader("ReThinking & Re-architecting Outline with Claude Sonnet...");
    const res = await fetch(`${API_BASE}/scripts/${currentScriptId}/outline/action`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "RETHINK",
        revision_feedback: feedback.trim() || null
      })
    });
    const data = await res.json();
    currentOutlineCards = data.cards || [];
    renderOutlineGate(data);
  } finally {
    hideLoader();
  }
}

// Current Section Drafting State
let currentDraftSectionIndex = 1;
let totalSectionsCount = 4;

async function onOutlineGtg() {
  if (!confirm("Are you sure you want to approve the Outline (GTG) and start drafting Card 1?")) return;
  try {
    showLoader("Locking Outline [GTG] & Drafting Card 1 with Claude Sonnet...");

    await fetch(`${API_BASE}/scripts/${currentScriptId}/outline/action`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "GTG" })
    });

    currentDraftSectionIndex = 1;
    totalSectionsCount = (currentOutlineCards && currentOutlineCards.length) ? currentOutlineCards.length : 14;

    // Draft Card 1 first (Single-card sequential progression)
    const first = await fetch(`${API_BASE}/scripts/${currentScriptId}/generate-section?start_card=1&end_card=1&section_id=sec_1`, {
      method: "POST"
    });
    await loadScriptStudioView();
    updateSectionGateState();
    if (!first.ok) alert("The outline is approved, but Card 1 could not be written.\n\n" + await responseProblem(first) + "\n\nWhen that is sorted, press \"Draft Card 1\" to try again. Nothing is lost.");
  } catch (err) {
    console.error("Error drafting Card 1:", err);
    await loadScriptStudioView();
  } finally {
    hideLoader();
  }
}

function updateSectionGateState() {
  const gate = document.getElementById("sectionGateContainer");
  const title = document.getElementById("sectionGateTitle");
  const subtitle = document.getElementById("sectionGateSubtitle");
  const gtgBtn = document.getElementById("sectionGtgBtn");
  if (!gate) return;

  const totalCards = (currentOutlineCards && currentOutlineCards.length) ? currentOutlineCards.length : (currentLoadedCards ? currentLoadedCards.length : 14);
  const draftedCards = currentLoadedCards || [];
  const latestCardIndex = draftedCards.length;
  const isLatestApproved = latestCardIndex > 0 && !!draftedCards[latestCardIndex - 1]?.is_approved;

  if (latestCardIndex < totalCards) {
    gate.classList.remove("hidden");
    if (title) title.textContent = `Card ${latestCardIndex} of ${totalCards} Drafted`;
    if (subtitle) {
      if (isLatestApproved) {
        subtitle.textContent = `Card ${latestCardIndex} is approved (GTG). Ready to draft Card ${latestCardIndex + 1}.`;
      } else {
        subtitle.textContent = `Review Card ${latestCardIndex} above. Once you mark it [GTG (Approve)], Card ${latestCardIndex + 1} will generate.`;
      }
    }
    if (gtgBtn) {
      gtgBtn.textContent = `Draft Card ${latestCardIndex + 1} \u2192`;
      gtgBtn.onclick = () => advanceNextCard(latestCardIndex + 1);
    }
  } else {
    // All cards drafted
    gate.classList.remove("hidden");
    if (title) title.textContent = `All ${totalCards} Cards Drafted!`;
    const isSubmitted = currentLoadedScript && currentLoadedScript.status === "MO Approval";
    const isApproved = currentLoadedScript && currentLoadedScript.status === "GTG";
    if (isApproved) {
      if (subtitle) subtitle.textContent = "Production deck is officially GTG approved and sealed by the Module Owner.";
      if (gtgBtn) {
        gtgBtn.textContent = "[Official GTG Approved]";
        gtgBtn.className = "btn btn-success";
        gtgBtn.onclick = null;
      }
    } else if (isSubmitted) {
      if (subtitle) subtitle.textContent = "Script is submitted and awaiting Module Owner review and sign-off.";
      if (gtgBtn) {
        gtgBtn.textContent = "[Awaiting MO Review]";
        gtgBtn.className = "btn btn-secondary";
        gtgBtn.onclick = null;
      }
    } else {
      if (subtitle) subtitle.textContent = "All cards are drafted. Review each card above or submit for Module Owner review.";
      if (gtgBtn) {
        gtgBtn.textContent = "[Submit Script for MO Review]";
        gtgBtn.className = "btn btn-warning";
        gtgBtn.onclick = submitScriptToMo;
      }
    }
  }
}

// What went wrong with a request, in the server's own words
async function responseProblem(res) {
  const data = await res.json().catch(() => ({}));
  return (typeof data.detail === "string" ? data.detail : data.detail ? JSON.stringify(data.detail) : `The server answered ${res.status}.`);
}

async function advanceNextCard(targetCardIndex) {
  const draftedCards = currentLoadedCards || [];
  const prevCard = draftedCards.find(c => c.card_index === targetCardIndex - 1);
  if (prevCard && !prevCard.is_approved) {
    alert(`Please approve Card ${targetCardIndex - 1} with [GTG (Approve)] before generating Card ${targetCardIndex}.`);
    return;
  }

  if (!confirm(`Are you sure you want to generate Card ${targetCardIndex}?`)) return;

  try {
    showLoader(`Drafting Card ${targetCardIndex} with Claude Sonnet...`);
    const secId = `sec_${Math.max(1, Math.ceil(targetCardIndex / 4))}`;
    const res = await fetch(`${API_BASE}/scripts/${currentScriptId}/generate-section?start_card=${targetCardIndex}&end_card=${targetCardIndex}&section_id=${secId}`, {
      method: "POST"
    });
    await loadScriptStudioView();
    updateSectionGateState();
    if (!res.ok) alert(`Card ${targetCardIndex} could not be written.\n\n` + await responseProblem(res));
  } catch (err) {
    alert("Error drafting card: " + err.message);
  } finally {
    hideLoader();
  }
}

window.advanceNextCard = advanceNextCard;

async function promptCurrentSectionAssist() {
  const promptText = prompt(`Enter prompt instruction for Section ${currentDraftSectionIndex} (e.g. "Add everyday UPI payment failure example" or "Simplify the code trace"):`);
  if (!promptText || !promptText.trim()) return;

  try {
    showLoader(`Refining Section ${currentDraftSectionIndex} with Claude Sonnet...`);
    // Re-draft section with SME prompt guidance
    const start = (currentDraftSectionIndex - 1) * 4 + 1;
    const end = currentDraftSectionIndex * 4;
    const q = encodeURIComponent(promptText.trim());
    await fetch(`${API_BASE}/scripts/${currentScriptId}/generate-section?start_card=${start}&end_card=${end}&section_id=sec_${currentDraftSectionIndex}&user_prompt=${q}`, {
      method: "POST"
    });
    await loadScriptStudioView();
    updateSectionGateState();
  } catch (err) {
    alert("Error assisting section: " + err.message);
  } finally {
    hideLoader();
  }
}

async function rethinkCurrentSection() {
  if (!confirm(`ReThink Section ${currentDraftSectionIndex} and regenerate it from scratch?`)) return;
  const start = (currentDraftSectionIndex - 1) * 4 + 1;
  const end = currentDraftSectionIndex * 4;

  try {
    showLoader(`ReThinking Section ${currentDraftSectionIndex}...`);
    await fetch(`${API_BASE}/scripts/${currentScriptId}/generate-section?start_card=${start}&end_card=${end}&section_id=sec_${currentDraftSectionIndex}`, {
      method: "POST"
    });
    await loadScriptStudioView();
    updateSectionGateState();
  } finally {
    hideLoader();
  }
}

async function runFinalScriptReview() {
  await onRunReview();
}

// Drafting moved from sections to single cards; the old name stays as an alias for the Draft Next button.
window.advanceNextSection = () => advanceNextCard((currentLoadedCards || []).length + 1);
window.promptCurrentSectionAssist = promptCurrentSectionAssist;
window.rethinkCurrentSection = rethinkCurrentSection;
window.runFinalScriptReview = runFinalScriptReview;

// ==============================================================================
// 3. SCRIPT STUDIO, TELEPROMPTER & INLINE EDITING
// ==============================================================================

// The status of the open script: the banner, the pill and which of Submit / Approve is offered
function applyScriptStatus(script) {
  const isApproved = script.status === "GTG";
  const isMoApproval = script.status === "MO Approval";
  const inReview = script.status === "SME Review";
  applyRevisionBanner(script);
  const gtgBanner = document.getElementById("smeGtgApprovedBanner");
  const moGtgBtn = document.getElementById("moGtgBtn");
  const submitToMoBtn = document.getElementById("submitToMoBtn");
  const isOwner = !!currentUser && (currentUser.role === "mo" || currentUser.role === "admin");
  const submitIcon = `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path><polyline points="22 4 12 14.01 9 11.01"></polyline></svg>`;

  if (isApproved) {
    const apText = document.getElementById("smeGtgApprovedText");
    if (apText) apText.textContent = `by ${script.approved_by_name || "the Module Owner"}${script.approved_at ? " on " + parseServerTime(script.approved_at).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }) : ""}. This is the final version.`;
    updateScriptStatusBadge("Official GTG Approved", "status-gtg");
    if (gtgBanner) gtgBanner.classList.remove("hidden");
    if (moGtgBtn) moGtgBtn.classList.add("hidden");
    if (submitToMoBtn) submitToMoBtn.classList.add("hidden");
  } else if (isMoApproval) {
    updateScriptStatusBadge("Pending MO Review", "status-mo-approval");
    if (gtgBanner) gtgBanner.classList.add("hidden");
    if (submitToMoBtn) {
      submitToMoBtn.classList.remove("hidden");
      submitToMoBtn.disabled = true;
      submitToMoBtn.innerHTML = `<span>Submitted to MO</span>`;
      submitToMoBtn.style.opacity = "0.75";
    }
    if (moGtgBtn) moGtgBtn.classList.toggle("hidden", !isOwner);
  } else {
    // a draft, or a script in SME review: it can go to the Module Owner only after the AI review (or when it was already reviewed and sent back)
    const reviewed = inReview || !!script.revision_note;
    updateScriptStatusBadge(inReview ? "SME Review" : (script.status || "Script WIP"), inReview ? "status-mo-approval" : "status-wip");
    if (gtgBanner) gtgBanner.classList.add("hidden");
    if (submitToMoBtn) {
      submitToMoBtn.classList.remove("hidden");
      submitToMoBtn.disabled = !reviewed;
      submitToMoBtn.innerHTML = `${submitIcon}<span>Submit to MO</span>`;
      submitToMoBtn.style.opacity = reviewed ? "1" : "0.6";
      submitToMoBtn.title = reviewed ? "Send the script to the Module Owner for approval" : "Run the AI review first. A script goes to the Module Owner after the AI review.";
    }
    if (moGtgBtn) moGtgBtn.classList.add("hidden");
  }
}

// ---- the AI review: who reviewed, what they found and what the AI fixed. The author runs it; the author and the Module Owner both read it.
const AIR_SEVERITY = { blocker: "Blocker", major: "Major", minor: "Minor", info: "Note" };
function aiFindingsHtml(r) {
  const items = (r.findings && r.findings.length) ? r.findings : (r.detailed_findings || []).map(t => ({ problem: t }));
  if (!items.length) return `<div class="air-none">${r.ran === false ? "This reviewer could not run." : "Nothing to fix."}</div>`;
  const row = (f) => `<li class="air-f air-${escapeHtml(f.severity || "info")}">
      <span class="air-sev">${AIR_SEVERITY[f.severity] || "Note"}</span>
      ${f.card_id && f.card_id !== "all" ? `<a href="javascript:void(0)" class="air-card" onclick="scrollToCard(${Number(f.card_id) || 0})">Card ${escapeHtml(String(f.card_id))}</a>` : ""}
      <span class="air-text">${escapeHtml(f.problem || "")}${f.suggested_fix ? ` <span class="air-fix"><b>Fix:</b> ${escapeHtml(f.suggested_fix)}</span>` : ""}</span></li>`;
  const first = items.slice(0, 4).map(row).join(""), rest = items.slice(4).map(row).join("");
  return `<ul class="air-list">${first}</ul>` + (rest ? `<details class="air-more"><summary>Show ${items.length - 4} more</summary><ul class="air-list">${rest}</ul></details>` : "");
}
// The fix checklist. After a review it lists every problem the AI will fix (blocker, major, minor). While the fixes are made each item is ticked as its card is done, and after
// them the list says which are fixed, which are still open and which are new. A problem about the whole script has no card to rewrite, so it is marked as needing you.
const AIC_STATE = { todo: ["&#9744;", "To fix"], applied: ["&#10003;", "Fix made, checking"], fixed: ["&#10003;", "Fixed"], open: ["&#9675;", "Still open"], new: ["+", "New"], manual: ["!", "Needs you"] };
const AIC_SEV = { blocker: "Blocker", major: "Major", minor: "Minor" };
function aiChecklistItems(review, live) { return (live && live.length ? live : (review && review.checklist)) || []; }
function aiChecklistHtml(review, live) {
  const items = aiChecklistItems(review, live);
  if (!items.length) return "";
  const n = (s) => items.filter(i => i.status === s).length;
  const parts = ["todo", "applied", "fixed", "open", "new", "manual"].filter(s => n(s)).map(s => `${n(s)} ${AIC_STATE[s][1].toLowerCase()}`);
  const row = (i) => `<li class="aic-${i.status}"><span class="aic-mark">${AIC_STATE[i.status] ? AIC_STATE[i.status][0] : ""}</span>
      <span class="aic-body"><b>${i.card_id && String(i.card_id) !== "all" ? `Card ${escapeHtml(String(i.card_id))}` : "Whole script"}</b>
        <span class="aic-state">${AIC_STATE[i.status] ? AIC_STATE[i.status][1] : ""}</span>${i.rule ? `<span class="aic-rule">${escapeHtml(String(i.rule))}</span>` : ""}
        <span class="aic-text">${escapeHtml(i.problem || "")}</span>
        ${i.status === "todo" && i.fix ? `<span class="aic-did">Fix: ${escapeHtml(i.fix)}</span>` : ""}${i.action ? `<span class="aic-did">Done: ${escapeHtml(i.action)}</span>` : ""}</span></li>`;
  const sorted = (list) => [...list].sort((a, b) => Number(a.card_id || 0) - Number(b.card_id || 0));
  const group = (title, list) => list.length ? `<div class="aic-group">${title} (${list.length})</div><ul>${sorted(list).map(row).join("")}</ul>` : "";
  const bySev = (s) => items.filter(i => i.status !== "manual" && i.severity === s);
  return `<details class="aic" open><summary><b>Fix checklist</b> &middot; ${parts.join(" &middot; ")}</summary>
    ${group("Blockers", bySev("blocker"))}${group("Major problems", bySev("major"))}${group("Minor problems", bySev("minor"))}${group("Needs you", items.filter(i => i.status === "manual"))}</details>`;
}
function aiReviewBodyHtml(review) {
  if (!review || !review.ran) return "";
  const lenses = (review.reviews || []).map(r => `
    <div class="air-lens">
      <div class="air-lens-head"><span class="air-lens-name">${escapeHtml(r.lens_name || "")}</span>
        <span class="air-score ${r.ran === false ? "off" : r.passed ? "ok" : "warn"}">${r.ran === false ? "did not run" : `${r.passed ? "Passed" : "Findings"} &middot; ${Number(r.score).toFixed(1)}/10`}</span></div>
      <div class="air-persona">${escapeHtml(r.reviewer_persona || "")}</div>
      ${r.summary ? `<div class="air-summary">${escapeHtml(r.summary)}</div>` : ""}
      ${aiFindingsHtml(r)}
    </div>`).join("");
  return `<div class="air-grid">${lenses}</div>`;
}
const AIR_LENS_LABEL = { technical_accuracy: "Technical accuracy", learner_clarity: "Beginner reviewer", intermediate_clarity: "Intermediate reviewer", advanced_depth: "Advanced reviewer", syllabus_and_rules: "Syllabus and script rules" };
function renderAiReviewPanel(script, review) {
  const panel = document.getElementById("aiReviewPanel");
  if (!panel) return;
  if (!script || !(script.cards || []).length) { panel.classList.add("hidden"); return; }
  panel.classList.remove("hidden");
  const sealed = ["MO Approval", "GTG"].includes(script.status);                       // a submitted script keeps the review it was sent with
  const outOfDate = !!(review && review.ran && review.stale && !sealed);                 // a review of an earlier version is not shown: only the current one is
  if (outOfDate) review = { ran: false, lenses: review.lenses };
  const ran = !!(review && review.ran);
  const missing = (review && review.reviews ? review.reviews.filter(r => r.ran === false).length : 0) || ((review && review.incomplete) || []).length;     // reviewers that could not run
  const state = document.getElementById("airState"), sub = document.getElementById("airSub"), btn = document.getElementById("airRunBtn"), body = document.getElementById("airBody");
  const canRun = !!currentUser && currentUser.role === "sme" && script.status !== "MO Approval" && script.status !== "GTG";   // also decides who may apply fixes
  if (btn) { btn.classList.toggle("hidden", !canRun); btn.textContent = ran ? "Run AI review again" : "Run AI review"; btn.disabled = false; }
  const applyBtn = document.getElementById("airApplyBtn");           // offered only for a finished review of this version of the script that still has problems
  const waiting = ran ? aiChecklistItems(review).filter(i => ["todo", "open", "new"].includes(i.status)).length : 0;          // problems with a card that can still be fixed
  if (applyBtn) { applyBtn.classList.toggle("hidden", !(canRun && ran && !missing && waiting && review.cards_hash)); applyBtn.disabled = false; applyBtn.textContent = waiting ? `Apply AI fixes (${waiting})` : "Apply AI fixes"; }
  if (state) { state.textContent = ran ? (missing ? "Review incomplete" : review.passed ? "Passed" : "Needs your attention") : "Not run yet"; state.className = "air-state " + (ran ? (missing ? "warn" : review.passed ? "ok" : "warn") : ""); }
  if (sub) {
    sub.textContent = ran
      ? (missing
          ? `${missing} of ${review.reviews.length - 1} reviewers could not run, so there is no score. Run the review again. \u00b7 ${parseServerTime(review.at).toLocaleString()}`
          : `${Number(review.score).toFixed(1)}/10 \u00b7 ${review.since_last ? `since the last review: ${review.since_last.fixed} fixed, ${review.since_last.still_open} still open, ${review.since_last.new} new \u00b7 ` : ""}${review.repairs ? `the AI has fixed the script ${review.repairs} ${review.repairs === 1 ? "time" : "times"}` : "your script was not changed"} \u00b7 ${parseServerTime(review.at).toLocaleString()}`)
      : (outOfDate ? "The last review was of an earlier version of the script, so it is not shown. Run the review again to see the current findings."
                   : "Run it before you submit to the Module Owner. The reviewers read the whole script and list what needs fixing. Nothing in your script is changed.");
  }
  window.__lastReview = review;
  const refs = referencesToCheck();
  let refRow = document.getElementById("airRefs");
  if (!refRow) { refRow = document.createElement("div"); refRow.id = "airRefs"; panel.insertBefore(refRow, document.getElementById("airProgress")); }
  refRow.className = "air-refs" + (refs.length ? " warn" : "");
  refRow.style.display = refs.length ? "" : "none";         // nothing to show once every reference is checked
  refRow.innerHTML = refs.length
    ? `<span><b>${refs.length} ${refs.length === 1 ? "reference" : "references"} to check.</b> The AI found ${refs.length === 1 ? "it" : "them"} outside the module's approved list. Open each card's References and accept or remove ${refs.length === 1 ? "it" : "them"} before you submit.</span>${canRun ? `<button type="button" class="btn btn-secondary" onclick="acceptAllReferences()">Accept all</button>` : ""}`
    : `<span>&#10003; Every reference from outside the approved list has been checked.</span>`;
  if (body) {
    const chosen = (review && review.lenses) || [];
    body.innerHTML = ran ? (aiChecklistHtml(review) ? aiChecklistHtml(review) + `<details class="air-details"><summary>Show what each reviewer found</summary>${aiReviewBodyHtml(review)}</details>` : aiReviewBodyHtml(review))
      : `<div class="air-chips">${(chosen.length ? chosen : Object.keys(AIR_LENS_LABEL)).map(k => `<span class="air-chip">${escapeHtml(AIR_LENS_LABEL[k] || k)}</span>`).join("")}</div>`;
  }
}
async function loadAiReview(script) {
  const sc = script || currentLoadedScript;
  if (!sc) return;
  let review = null;
  try { const r = await fetch(`${API_BASE}/scripts/${sc.id}/review/latest`); review = r.ok ? await r.json() : null; } catch (e) { /* shown as not run */ }
  renderAiReviewPanel(sc, review);
}
window.scrollToCard = function (index) {
  const card = (currentLoadedCards || []).find(c => Number(c.card_index) === Number(index));
  const el = card && document.getElementById(`section-block-${card.id}`);
  if (el) el.scrollIntoView({ behavior: "smooth", block: "start" });
};
window.runAiReview = function () { return aiReviewCall("find"); };
window.applyAiFixes = function () { return aiReviewCall("apply"); };
// "find": the reviewers read the script and list the problems; nothing is changed. "apply": the AI makes the fixes the author agreed to.
async function aiReviewCall(kind) {
  const id = currentScriptId;
  if (!id) return;
  const apply = kind === "apply";
  const chosen = apply ? [...document.querySelectorAll(".air-prop-box:checked")].map(b => Number(b.value)) : [];
  if (apply) {
    const all = document.querySelectorAll(".air-prop-box");
    if (all.length && !chosen.length) { alert("Tick at least one card to fix."); return; }
    if (!confirm(`Apply the AI's fixes to ${chosen.length ? chosen.length + (chosen.length === 1 ? " card" : " cards") : "every card the review flagged"}?\n\nThe AI will fix every problem on the checklist (blocker, major and minor), one card at a time, and tick each off. It rewrites the text of the flagged cards and makes flagged diagrams, animations and quizzes again. It then reads ${chosen.length === 1 ? "it" : "them"} again, up to 2 times. You can still edit everything afterwards. It can take several minutes.`)) return;
  } else if (!confirm("Run the AI review now?\n\nThe reviewers read the whole script and list what needs fixing. Nothing in your script is changed. It can take several minutes.")) return;
  const btn = document.getElementById("airRunBtn"), prog = document.getElementById("airProgress"), applyBtn = document.getElementById("airApplyBtn");
  if (btn) { btn.disabled = true; btn.textContent = apply ? "Applying..." : "Reviewing..."; }
  if (applyBtn) { applyBtn.disabled = true; applyBtn.textContent = "Applying..."; }
  prog.classList.remove("hidden"); prog.innerHTML = `<div class="air-step now">${apply ? "Starting the fixes..." : "Starting the review..."}</div>`;
  const poll = setInterval(async () => {                                   // the server reports each step as it happens; they are shown here as they come
    try {
      const p = await (await fetch(`${API_BASE}/scripts/${id}/review/progress`)).json();
      const steps = p.steps || [];
      if (steps.length) prog.innerHTML = steps.map((st, i) => `<div class="air-step ${i === steps.length - 1 && !p.done ? "now" : "done"}">${escapeHtml(st.message)}</div>`).join("");
      const live = document.getElementById("airBody");
      if (apply && live && (p.checklist || []).length) live.innerHTML = aiChecklistHtml(null, p.checklist);          // ticked card by card as the fixes are made
      if (!apply && live && (p.partial || []).length) live.innerHTML = `<div class="air-live">Each reviewer's findings appear here as it finishes (${p.partial.length} so far).</div>` + aiReviewBodyHtml({ ran: true, reviews: p.partial });
    } catch (e) { /* the list is only for display */ }
  }, 2000);
  try {
    const res = await fetch(`${API_BASE}/scripts/${id}/review${apply ? "/apply" : ""}`, apply
      ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ cards: chosen.length === document.querySelectorAll(".air-prop-box").length ? null : chosen }) }
      : { method: "POST" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : `${apply ? "The fixes" : "The review"} failed (HTTP ${res.status}).`);
    const sres = await fetch(`${API_BASE}/scripts/${id}`);                  // after an apply the AI has rewritten cards; the status moved to SME review
    if (sres.ok) {
      const script = await sres.json();
      currentLoadedScript = script; currentLoadedCards = script.cards;
      renderCardsStream(script.cards); applyScriptStatus(script);
      await loadAiReview(script);
    }
    showNotice(apply
      ? (data.is_gtg ? "The fixes are applied and the script now passes the review." : "The fixes are applied. Some findings are left for you. Read the changes, then submit to the Module Owner.")
      : (data.is_gtg ? "The AI review passed. Nothing needs fixing. You can submit to the Module Owner." : "The review is done. Nothing in your script was changed. Read the findings and fix what you agree with."));
  } catch (err) {
    alert((apply ? "The fixes did not finish: " : "The AI review did not finish: ") + (err.message || err));
    await loadAiReview();
  } finally {
    clearInterval(poll);
    prog.classList.add("hidden");
    if (btn) btn.disabled = false;
  }
};

async function loadScriptStudioView() {
  syncFullScreenButton();
  document.getElementById("outlineGate")?.classList.add("hidden");           // the outline was approved, so leaving it needs no question
  switchSmeStudioView("script");
  const studio = document.getElementById("studioView");
  if (studio) studio.classList.remove("hidden");

  try {
    showLoader("Loading Spoken Dialogue & Diagrams...");
    const res = await fetch(`${API_BASE}/scripts/${currentScriptId}`);
    const script = await res.json();
    currentLoadedScript = script;
    currentShareableUuid = script.shareable_uuid || script.uuid;
    updateSmeCount();
    loadClassSetup();
    renderClassTimeline(script.cards || [], script);

    document.getElementById("studioClassTitle").textContent = script.class_title;
    const progTrackElem = document.getElementById("studioProgTrack");
    if (progTrackElem) {
      const levelLabel = script.track_name ? ` • Level: ${script.track_name}` : '';
      progTrackElem.textContent = `${script.program_name}${levelLabel} • Module: ${script.module_name}`;
    }
    document.getElementById("studioCardProgress").textContent = `${script.cards.length} Sections Drafted (${script.duration_minutes}m)`;
    const meta = document.getElementById("svMeta");
    if (meta) meta.textContent = [script.program_name, script.track_name, script.module_name, `${script.duration_minutes} min`, `${script.cards.length} ${script.cards.length === 1 ? "card" : "cards"}`].filter(Boolean).join("  \u00b7  ");

    // Populate defined script document header
    const docTitle = document.getElementById("scriptDocClassTitle");
    if (docTitle) docTitle.textContent = script.class_title;
    const docHierarchy = document.getElementById("scriptDocHierarchy");
    if (docHierarchy) {
      docHierarchy.innerHTML = `
        <span>Target Duration: ${script.duration_minutes} Mins</span> &bull; 
        <span>${script.cards.length} Sections</span>
      `;
    }

    // Keep draftAllBtn permanently hidden per user preference
    const draftAllBtn = document.getElementById("draftAllBtn");
    if (draftAllBtn) {
      draftAllBtn.style.display = "none";
    }

    document.getElementById("shareLinkBtn")?.classList.remove("hidden");
    document.getElementById("revokeLinkBtn")?.classList.remove("hidden");
    document.getElementById("renewLinkBtn")?.classList.remove("hidden");

    applyScriptStatus(script);

    currentLoadedCards = script.cards;
    renderCardsStream(script.cards);
    renderDynamicDownloadMenu();
    loadAiReview(script);
  } finally {
    hideLoader();
  }
}

// A card's text as readable HTML: headings, bold, bullet and numbered lists, callout boxes and stage cues instead of the raw
// '#', '-', '>' and '**' symbols. Mirrors agents/safe_html.format_card_text; everything is escaped before any markup is added.
function normUrl(url) {
  const u = String(url || "").trim().split("#")[0];
  const m = u.match(/^(https?):\/\/([^\/?]+)(.*)$/i);
  return m ? `${m[1].toLowerCase()}://${m[2].toLowerCase()}${m[3].replace(/\/+$/, "")}` : u;
}

function hostOf(url) {
  const m = String(url || "").match(/^https?:\/\/([^\/?#:]+)/i);
  return m ? m[1].toLowerCase().replace(/^www\./, "") : "";
}

// A clickable link with its source written beside it (mirrors agents/safe_html._link_html).
function linkHtml(label, url, index) {
  const bare = url.replace(/^https?:\/\/(www\.)?/, "");
  const shown = label || (bare.replace(/\/$/, "").slice(0, 55) + (bare.length > 56 ? "..." : ""));
  // where the link comes from and whether it was checked is in the card's References list, not in the middle of what the instructor reads
  return `<a class="card-link" href="${escapeHtml(url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(shown)}</a>`;
}

function formatCardText(text, sources) {
  const linkIndex = {};
  (Array.isArray(sources) ? sources : []).forEach(x => { if (x && x.url) linkIndex[normUrl(x.url)] = x; });
  const inline = raw => {
    const holders = [];
    raw = String(raw).replace(/\[([^\]\n]{1,120})\]\((https?:\/\/[^)\s`]+)\)|(https?:\/\/[^\s<>)\]"'`]+)/g, (m, label, mdUrl, bareUrl) => {
      let url = mdUrl || bareUrl, tail = "";
      if (bareUrl) { const clean = url.replace(/[.,;:!?]+$/, ""); tail = url.slice(clean.length); url = clean; }
      holders.push(linkHtml(label || "", url, linkIndex));
      return `\u0000${holders.length - 1}\u0000${tail}`;
    });
    let out = escapeHtml(raw);
    out = out.replace(/\[\[VISUAL:\s*([^\]|]+?)\s*(?:\|[^\]]*)?\]\]/g, "");           // the visual sits in its place on the page; the marker is only for the writer
    out = out.replace(/\[\[FROM_SME:?\s*([^\]]*)\]\]/g, (m, need) => `<mark class="sme-gap">needed from the SME: ${need.trim() || "a fact"}</mark>`);
    out = out.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    out = out.replace(/(^|[^\w*])\*([^*\n]+)\*(?![\w*])/g, "$1<em>$2</em>");
    out = out.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    return out.replace(/\u0000(\d+)\u0000/g, (m, i) => holders[Number(i)]);
  };
  const kinds = { "key": "key", "mistake": "mistake", "remember": "remember", "real-world": "realworld", "real world": "realworld", "predict": "predict" };
  const html = [];
  let para = [], quote = [], items = [], listTag = null;
  const closePara = () => { if (para.length) { html.push(`<p class="card-para">${para.map(inline).join("<br>")}</p>`); para = []; } };
  const closeList = () => { if (items.length) { html.push(`<${listTag} class="card-list">${items.map(i => `<li>${inline(i)}</li>`).join("")}</${listTag}>`); items = []; } listTag = null; };
  const closeQuote = () => {
    if (!quote.length) return;
    const body = quote.join(" ").trim();
    const m = body.match(/^\*\*\s*([A-Za-z][A-Za-z -]{1,18}?)\s*:?\s*\*\*\s*:?\s*([\s\S]*)$/);
    const kind = m ? kinds[m[1].trim().toLowerCase()] : null;
    html.push(kind ? `<div class="card-callout callout-${kind}"><span class="callout-label">${escapeHtml(m[1].trim())}</span> ${inline(m[2])}</div>`
                   : `<div class="card-callout callout-note">${inline(body)}</div>`);
    quote = [];
  };
  const closeAll = () => { closePara(); closeList(); closeQuote(); };
  let fence = [], inFence = false;
  for (const line of String(text || "").replace(/\r\n/g, "\n").split("\n")) {
    let m;
    if (/^\s*```/.test(line)) {                                  // a fenced code block: shown as code, line breaks and indents kept
      if (inFence) { html.push(`<pre class="card-code"><code>${escapeHtml(fence.join("\n"))}</code></pre>`); fence = []; inFence = false; }
      else { closeAll(); inFence = true; }
      continue;
    }
    if (inFence) { fence.push(line); continue; }
    if (!line.trim() || /^\s*([-*_])\1{2,}\s*$/.test(line)) { closeAll(); continue; }
    if ((m = line.match(/^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$/))) { closeAll(); html.push(`<h4 class="card-heading">${inline(m[1])}</h4>`); continue; }
    if ((m = line.match(/^\s*>\s?(.*)$/))) { closePara(); closeList(); quote.push(m[1]); continue; }
    closeQuote();
    if ((m = line.match(/^\s*[-*\u2022]\s+(.+)$/))) { closePara(); if (listTag !== "ul") { closeList(); listTag = "ul"; } items.push(m[1]); continue; }
    if ((m = line.match(/^\s*\d{1,2}[.)]\s+(.+)$/))) { closePara(); if (listTag !== "ol") { closeList(); listTag = "ol"; } items.push(m[1]); continue; }
    closeList();
    if (/^\s*\[(?!\[)[^\[\]]{1,160}\]\s*$/.test(line)) { closePara(); html.push(`<p class="stage-cue">${escapeHtml(line.trim())}</p>`); continue; }
    if (/^\s*\**reveal\**\s*:/i.test(line)) { closePara(); html.push(`<p class="card-para reveal">${inline(line.trim())}</p>`); continue; }
    para.push(line.trim());
  }
  if (inFence && fence.length) html.push(`<pre class="card-code"><code>${escapeHtml(fence.join("\n"))}</code></pre>`);
  closeAll();
  return html.join("");
}

// ---- a quiz, shown the same way everywhere (mirrors agents/quiz_format.quiz_html) ----
function quizView(q) {
  if (!q || typeof q !== "object" || !(q.question || (q.options || []).length)) return null;
  const clean = t => String(t == null ? "" : t).replace(/ \u2014 /g, ", ").replace(/\u2014/g, ", ").trim();
  const strip = t => String(t || "").replace(/^\s*\(?([A-Ha-h])[.):\-]\s+/, "").trim();
  const raw = (q.options || []).map(String);
  const right = Number.isInteger(Number(q.correct_option_index)) ? Number(q.correct_option_index) : -1;
  const reasons = {};
  Object.entries(q.distractor_explanations || {}).forEach(([key, value]) => {
    const letters = String(key).replace(/[^A-Za-z]/g, "");
    if (letters.length === 1) { reasons[letters.toUpperCase()] = clean(value); return; }
    raw.forEach((opt, i) => { if (strip(opt).toLowerCase() === strip(String(key)).toLowerCase()) reasons[String.fromCharCode(65 + i)] = clean(value); });
  });
  const options = raw.map((opt, i) => ({ letter: String.fromCharCode(65 + i), text: strip(clean(opt)), correct: i === right, why: i === right ? "" : (reasons[String.fromCharCode(65 + i)] || "") }));
  return { question: clean(q.question), options, answerLetter: right >= 0 && right < raw.length ? String.fromCharCode(65 + right) : "", explanation: clean(q.explanation) };
}

function formatQuiz(q, cardIndex) {
  const v = quizView(q);
  if (!v) return "";
  // with a card number the quiz also shows its state and the Edit, Regenerate and Approve buttons (the shared page shows the quiz alone)
  const approved = !!(q && q.approved);
  const actions = cardIndex === undefined || cardIndex === null ? "" :
    `<span class="quiz2-state ${approved ? "ok" : ""}">${approved ? "Approved" : "Not approved yet"}</span>` +
    `<button type="button" class="quiz2-toggle" onclick="openQuizEditor(${Number(cardIndex)})">Edit</button>` +
    `<button type="button" class="quiz2-toggle" onclick="regenerateQuiz(${Number(cardIndex)})">Regenerate</button>` +
    `<button type="button" class="quiz2-toggle" onclick="approveQuiz(${Number(cardIndex)}, ${approved ? "false" : "true"})">${approved ? "Undo approval" : "Approve quiz"}</button>`;
  const opts = v.options.map(o => {
    const answer = o.correct ? `<div class="quiz2-ans"><span class="quiz2-tag">&#10003; Correct answer</span></div>`
      : (o.why ? `<div class="quiz2-ans quiz2-why"><b>Why not:</b> ${escapeHtml(o.why)}</div>` : "");
    return `<div class="quiz2-opt${o.correct ? " right" : ""}"><span class="quiz2-letter">${escapeHtml(o.letter)}</span>` +
           `<div class="quiz2-body"><div class="quiz2-text">${escapeHtml(o.text)}</div>${answer}</div></div>`;
  }).join("");
  const expl = v.explanation ? `<div class="quiz2-ans quiz2-expl"><b>Why ${escapeHtml(v.answerLetter || "this")} is right:</b> ${escapeHtml(v.explanation)}</div>` : "";
  return `<div class="quiz2"><div class="quiz2-head"><span class="quiz2-label">Quiz</span><span class="quiz2-actions">${actions}` +
         `<button type="button" class="quiz2-toggle" onclick="toggleQuizAnswers(this)">Hide answers</button></span></div>` +
         `<div class="quiz2-q">${formatCardText(v.question)}</div><div class="quiz2-opts">${opts}</div>${expl}</div>`;
}


// A card's text as clean PLAIN text (for Copy and the .txt download): no #, *, >, backticks or --- ; links as "text (address)"; code kept as it was typed.
function markdownToPlain(text) {
  const out = [];
  let inFence = false;
  const inline = s => String(s)
    .replace(/\[\[VISUAL:\s*([^\]|]+?)\s*(?:\|[^\]]*)?\]\]/g, "")
    .replace(/\[\[FROM_SME:?\s*([^\]]*)\]\]/g, (m, need) => `[needed from the SME: ${need.trim() || "a fact"}]`)
    .replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, "$1 ($2)")
    .replace(/\*\*([^*\n]+)\*\*/g, "$1").replace(/(^|[^\w*])\*([^*\n]+)\*(?![\w*])/g, "$1$2").replace(/`([^`\n]+)`/g, "$1");
  for (const line of String(text || "").replace(/\r\n/g, "\n").split("\n")) {
    if (/^\s*```/.test(line)) { inFence = !inFence; out.push(""); continue; }
    if (inFence) { out.push("    " + line); continue; }
    if (/^\s*([-*_])\1{2,}\s*$/.test(line)) { out.push(""); continue; }
    let m;
    if ((m = line.match(/^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$/))) { out.push("", inline(m[1]).toUpperCase(), ""); continue; }
    if ((m = line.match(/^\s*>\s?(.*)$/))) { out.push(inline(m[1]).replace(/^([A-Za-z][A-Za-z -]{1,18}?):\s*/, (x, label) => label.toUpperCase() + ": ")); continue; }
    out.push(inline(line.replace(/^(\s*)[*\u2022]\s+/, "$1- ")));
  }
  return out.join("\n").replace(/\n{3,}/g, "\n\n").trim();
}

// A quiz as plain text: question, A to D, the right one marked, and why each wrong one is wrong.
function quizPlain(q) {
  const v = quizView(q);
  if (!v) return "";
  const lines = [markdownToPlain(v.question), ""];
  v.options.forEach(o => {
    lines.push(`  ${o.letter}. ${o.text}${o.correct ? "   <-- correct answer" : ""}`);
    if (!o.correct && o.why) lines.push(`       Why not: ${o.why}`);
  });
  if (v.explanation) lines.push("", `Why ${v.answerLetter || "this"} is right: ${v.explanation}`);
  return lines.join("\n");
}

// A quiz for the Word download: plain paragraphs (Word does not follow our screen layout).
function quizDocHtml(q) {
  const v = quizView(q);
  if (!v) return "";
  const opts = v.options.map(o => `<p style="margin:4px 0 4px 18px;${o.correct ? "font-weight:bold;" : ""}">${escapeHtml(o.letter)}. ${escapeHtml(o.text)}${o.correct ? " &#10003; correct answer" : ""}</p>` +
    (!o.correct && o.why ? `<p style="margin:0 0 6px 36px;color:#404040;font-size:10pt;"><i>Why not:</i> ${escapeHtml(o.why)}</p>` : "")).join("");
  return `<div class="quiz"><p><b>Quiz</b></p>${formatCardText(v.question, [])}${opts}` +
         (v.explanation ? `<p style="margin-top:8px;"><b>Why ${escapeHtml(v.answerLetter || "this")} is right:</b> ${escapeHtml(v.explanation)}</p>` : "") + `</div>`;
}

function toggleQuizAnswers(btn) {
  const box = btn.closest(".quiz2");
  const hidden = box.classList.toggle("hide-answers");
  btn.textContent = hidden ? "Show answers" : "Hide answers";
}
window.toggleQuizAnswers = toggleQuizAnswers;

// ---- quizzes: edit, regenerate and approve one quiz at a time ----------------------------------------------------------------------
async function quizApi(cardIndex, path, method, body) {
  const res = await fetch(`${API_BASE}/scripts/${currentScriptId}/cards/${cardIndex}/quiz${path}`, {
    method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `the server answered ${res.status}`);
  return data;
}

async function approveQuiz(cardIndex, approved) {
  try {
    await quizApi(cardIndex, `/approve?approved=${approved}`, "POST");
    await reloadCards();
  } catch (err) { alert(err.message); }
}

async function regenerateQuiz(cardIndex) {
  const instruction = prompt("Make a new quiz for this card.\nAnything it should do differently? (optional, for example: test the difference between X and Y)", "");
  if (instruction === null) return;
  try {
    showLoader("Making a new quiz...");
    await quizApi(cardIndex, "/regenerate", "POST", { instruction: instruction.trim() || null });
    await reloadCards();
  } catch (err) {
    alert("The quiz was not changed. " + err.message);
  } finally { hideLoader(); }
}

function openQuizEditor(cardIndex) {
  const card = (currentLoadedCards || []).find(c => c.card_index === cardIndex);
  const v = card && quizView(card.quiz);
  if (!v) { alert("This card has no quiz."); return; }
  document.getElementById("quizEditorModal")?.remove();
  const wrap = document.createElement("div");
  wrap.id = "quizEditorModal";
  wrap.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,0.55);z-index:9999;display:flex;align-items:center;justify-content:center;";
  const inputStyle = "width:100%;padding:7px;margin:3px 0;box-sizing:border-box;";
  wrap.innerHTML = `
    <div style="background:var(--bg-card,#fff);color:var(--text-primary,#111);border-radius:12px;padding:20px 22px;width:min(680px,94vw);max-height:92vh;overflow:auto;">
      <h3 style="margin:0 0 4px;">Edit the quiz on card ${Number(cardIndex)}</h3>
      <div style="font-size:0.82rem;color:var(--text-muted);margin-bottom:12px;">Pick the right answer with the round button. Every wrong option needs a reason: it is shown under that option.</div>
      <div id="qedError" style="display:none;background:#fef2f2;border:1px solid #fecaca;color:#b91c1c;border-radius:8px;padding:8px 12px;margin-bottom:10px;font-size:0.85rem;"></div>
      <label style="font-weight:600;font-size:0.85rem;">Question (code goes between three backticks)</label>
      <textarea id="qedQuestion" rows="3" style="${inputStyle}"></textarea>
      <div style="font-weight:600;font-size:0.85rem;margin-top:8px;">Options</div>
      <div id="qedOptions"></div>
      <button type="button" class="btn btn-secondary" id="qedAdd" style="margin:6px 0 10px;padding:3px 10px;font-size:0.8rem;">Add an option</button>
      <label style="font-weight:600;font-size:0.85rem;display:block;">Why the right answer is right</label>
      <textarea id="qedExplanation" rows="2" style="${inputStyle}"></textarea>
      <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px;">
        <button type="button" class="btn btn-secondary" id="qedCancel">Cancel</button>
        <button type="button" class="btn btn-secondary" id="qedSave">Save</button>
        <button type="button" class="btn btn-primary" id="qedSaveApprove">Save and approve</button>
      </div>
    </div>`;
  document.body.appendChild(wrap);
  document.getElementById("qedQuestion").value = v.question;
  document.getElementById("qedExplanation").value = v.explanation;
  const list = document.getElementById("qedOptions");
  const addRow = (text, why, correct) => {
    const row = document.createElement("div");
    row.className = "qed-row";
    row.style.cssText = "border:1px solid var(--border-color,#e2e8f0);border-radius:8px;padding:8px;margin:6px 0;";
    row.innerHTML = `<div style="display:flex;gap:8px;align-items:center;">
        <input type="radio" name="qedCorrect" title="This is the right answer">
        <strong class="qed-letter" style="width:1.4em;"></strong>
        <input class="qed-text" style="flex:1;padding:6px;" placeholder="The option">
        <button type="button" class="btn btn-secondary qed-remove" style="padding:2px 8px;font-size:0.75rem;">Remove</button>
      </div>
      <input class="qed-why" style="${inputStyle}" placeholder="Why this option is wrong (not needed for the right answer)">`;
    row.querySelector(".qed-text").value = text;
    row.querySelector(".qed-why").value = why;
    row.querySelector("input[type=radio]").checked = !!correct;
    row.querySelector(".qed-remove").onclick = () => { if (list.children.length > 2) { row.remove(); relabel(); } };
    list.appendChild(row);
    relabel();
  };
  const relabel = () => {
    [...list.children].forEach((row, i) => {
      row.querySelector(".qed-letter").textContent = String.fromCharCode(65 + i);
      const isRight = row.querySelector("input[type=radio]").checked;
      row.querySelector(".qed-why").style.display = isRight ? "none" : "block";
    });
  };
  v.options.forEach(o => addRow(o.text, o.why, o.correct));
  if (!v.options.some(o => o.correct)) list.querySelector("input[type=radio]").checked = true;
  list.addEventListener("change", relabel);
  relabel();
  document.getElementById("qedAdd").onclick = () => { if (list.children.length < 6) addRow("", "", false); };
  const showError = msg => { const e = document.getElementById("qedError"); e.textContent = msg; e.style.display = msg ? "block" : "none"; };
  const collect = () => {
    const rows = [...list.children];
    return {
      question: document.getElementById("qedQuestion").value.trim(),
      options: rows.map(r => r.querySelector(".qed-text").value.trim()),
      correct_option_index: rows.findIndex(r => r.querySelector("input[type=radio]").checked),
      explanation: document.getElementById("qedExplanation").value.trim(),
      reasons: rows.map(r => r.querySelector(".qed-why").value.trim()),
    };
  };
  const save = async (andApprove) => {
    try {
      showError("");
      const body = collect();
      if (!body.question) { showError("Write the question."); return; }
      if (body.options.some(o => !o)) { showError("Fill in every option, or remove the empty one."); return; }
      const saved = await quizApi(cardIndex, "", "PUT", body);
      if (andApprove) {
        if (saved.problems && saved.problems.length) { showError("Saved, but it cannot be approved yet. " + saved.problems.join(" ")); await reloadCards(); return; }
        await quizApi(cardIndex, "/approve?approved=true", "POST");
      }
      wrap.remove();
      await reloadCards();
    } catch (err) { showError(err.message); }
  };
  document.getElementById("qedCancel").onclick = () => wrap.remove();
  document.getElementById("qedSave").onclick = () => save(false);
  document.getElementById("qedSaveApprove").onclick = () => save(true);
}

window.openQuizEditor = openQuizEditor;
window.regenerateQuiz = regenerateQuiz;
window.approveQuiz = approveQuiz;

// ---- the class clock: where every card sits on the clock, and whether its words fit its time -----------------------------------------------------
const SPEAKING_WORDS_PER_MINUTE = 90;

function fmtClock(sec) {
  const s = Math.max(0, Math.round(sec || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), r = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}` : `${m}:${String(r).padStart(2, "0")}`;
}

// the chips in a card's header: its place on the class clock, its length, and whether the words fit
function timingChips(c) {
  let html = "";
  if (c.starts_at_sec !== null && c.starts_at_sec !== undefined) {
    html += `<span class="script-meta-badge timing-badge" title="Where this card sits on the class clock, from the first word of the class">${fmtClock(c.starts_at_sec)} &ndash; ${fmtClock(c.ends_at_sec)}</span>`;
  }
  html += `<span class="script-meta-badge timing-badge" title="Planned length of this card">${c.duration_mins}m</span>`;
  if (c.spoken_words !== null && c.spoken_words !== undefined && c.words_max) {
    const reads = c.spoken_words / SPEAKING_WORDS_PER_MINUTE;
    html += c.spoken_words > c.words_max
      ? `<span class="script-meta-badge" style="background:#fee2e2;color:#991b1b;" title="${c.spoken_words} words; this card's time holds about ${c.word_budget}">Over time: reads in ~${reads.toFixed(1)} min</span>`
      : `<span class="script-meta-badge" style="background:#dcfce7;color:#166534;" title="${c.spoken_words} words; this card's time holds about ${c.word_budget}">Fits: ~${reads.toFixed(1)} min</span>`;
  }
  return html;
}

// the whole class on one line and one bar: planned minutes against the class length, how long the words take to say, and every card in order
function renderClassTimeline() {
  // the class agenda is part of the welcome card, so there is no separate box
  const panel = document.getElementById("classTimelinePanel");
  if (panel) panel.innerHTML = "";
}

// ---- the before-the-class setup checklist of a programming class -------------------------------------------------------------------------
async function loadClassSetup() {
  const panel = document.getElementById("classSetupPanel");
  if (!panel || !currentScriptId) return;
  try {
    const res = await fetch(`${API_BASE}/scripts/${currentScriptId}/setup`);
    if (!res.ok) { panel.innerHTML = ""; return; }
    const data = await res.json();
    if (!data.is_programming && !data.setup) { panel.innerHTML = ""; return; }
    const button = (label, primary) => `<button type="button" class="btn ${primary ? "btn-primary" : "btn-secondary"}" style="padding:4px 12px;font-size:0.8rem;" onclick="makeClassSetup()">${label}</button>`;
    panel.innerHTML = data.setup
      ? `<details class="setup-fold"><summary>Before the class: mandatory setup <span>Show the checklist</span></summary>${data.html}<div style="padding:0 16px 14px;display:flex;gap:8px;align-items:center;">${button("Make the checklist again", false)}<span style="font-size:0.78rem;color:var(--text-muted);">It is also in the shared page and in the exports.</span></div></details>`
      : `<div class="setup2"><div class="setup2-head">Before the class: mandatory setup</div>
           <div class="setup2-lead">This class has code, so the instructor and the learners need tools installed first. Make the checklist: the tools, their versions, how to check each one works, and a checked source link for each.</div>
           ${button("Make the setup checklist", true)}</div>`;
  } catch (e) { panel.innerHTML = ""; }
}

async function makeClassSetup() {
  try {
    showLoader("Making the setup checklist and checking its links...");
    const res = await fetch(`${API_BASE}/scripts/${currentScriptId}/setup/generate`, { method: "POST" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `the server answered ${res.status}`);
    await loadClassSetup();
  } catch (err) {
    alert("The checklist was not made. " + (err.message || err));
  } finally { hideLoader(); }
}
window.makeClassSetup = makeClassSetup;

// What happened when the card's code was run: a status, the output or the error, and a button to run it again.
// ---- the code editor: type in the code, run what you typed, see the output underneath, save or put it back -------------------------------
const codeDrafts = {};            // card_index -> text typed but not saved, so a redraw of the page does not lose it

function renderCodeEditor(c) {
  const saved = c.code_snippet || "";
  const text = Object.prototype.hasOwnProperty.call(codeDrafts, c.card_index) ? codeDrafts[c.card_index] : saved;
  const lines = text.split("\n").length;
  const dirty = text !== saved;
  const lang = escapeHtml(c.code_language || "python");
  const tool = "padding: 2px 10px; font-size: 0.72rem; background: #1e293b; color: #e2e8f0; border: 1px solid #475569; border-radius: 6px; cursor: pointer;";
  return `
    <div class="code-container ce" style="margin: 12px 0;" data-card-index="${Number(c.card_index)}" data-card-id="${Number(c.id)}" data-lang="${lang}">
      <div class="code-header">
        <span>Live Code (${lang}) <span class="ce-state" style="margin-left: 8px; color: #fbbf24;">${dirty ? "Edited, not saved" : ""}</span></span>
        <span style="display: flex; gap: 6px; flex-wrap: wrap;">
          <button type="button" class="ce-run" style="${tool} background: #16a34a; border-color: #16a34a; font-weight: 700;" onclick="ceRun(this)" title="Run what is in the editor (Ctrl or Cmd + Enter)">&#9654; Run</button>
          <button type="button" class="ce-save" style="${tool}" onclick="ceSave(this)" ${dirty ? "" : "disabled"} title="Keep your changes (Ctrl or Cmd + S)">Save changes</button>
          <button type="button" class="ce-revert" style="${tool}" onclick="ceRevert(this)" ${dirty ? "" : "disabled"}>Undo changes</button>
          <button type="button" style="${tool}" onclick="copyCodeFromBlock(this)">Copy Code</button>
        </span>
      </div>
      <div class="ce-wrap">
        <pre class="ce-nums" aria-hidden="true">${Array.from({ length: lines }, (_, i) => i + 1).join("\n")}</pre>
        <textarea class="ce-area" spellcheck="false" autocapitalize="off" autocomplete="off" autocorrect="off" wrap="off" aria-label="Code editor"
          rows="${Math.min(Math.max(lines, 4), 26)}" data-saved="${escapeHtml(saved)}">${escapeHtml(text)}</textarea>
      </div>
      <div class="ce-output">${codeRunHtml(c.code_run, c.card_index, true)}</div>
      <div class="ce-trace">${renderCodeTrace(c)}</div>
    </div>`;
}

function ceParts(el) {
  const root = el.closest(".ce");
  return { root, area: root.querySelector(".ce-area"), nums: root.querySelector(".ce-nums"), state: root.querySelector(".ce-state"),
           run: root.querySelector(".ce-run"), save: root.querySelector(".ce-save"), revert: root.querySelector(".ce-revert"),
           output: root.querySelector(".ce-output"), trace: root.querySelector(".ce-trace"), index: Number(root.dataset.cardIndex), id: Number(root.dataset.cardId), lang: root.dataset.lang };
}

function ceRefresh(area) {
  const p = ceParts(area);
  const lines = area.value.split("\n").length;
  p.nums.textContent = Array.from({ length: lines }, (_, i) => i + 1).join("\n");
  area.rows = Math.min(Math.max(lines, 4), 26);
  const dirty = area.value !== area.dataset.saved;
  p.save.disabled = p.revert.disabled = !dirty;
  p.state.textContent = dirty ? "Edited, not saved" : "";
  if (dirty) codeDrafts[p.index] = area.value; else delete codeDrafts[p.index];
}

async function ceRun(btn) {
  const p = ceParts(btn);
  const code = p.area.value;
  if (!code.trim()) { p.output.innerHTML = codeRunHtml({ status: "skipped", reason: "There is no code to run." }, p.index, false); return; }
  const label = p.run.innerHTML;
  p.run.disabled = true; p.run.textContent = "Running...";
  p.output.innerHTML = `<div class="code-run" style="padding: 8px 14px; font-size: 0.8rem; color: #94a3b8;">Running your code...</div>`;
  try {
    const res = await fetch(`${API_BASE}/scripts/${currentScriptId}/cards/${p.index}/run-code`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code, language: p.lang })
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `the server answered ${res.status}`);
    const edited = code !== p.area.dataset.saved;
    p.output.innerHTML = codeRunHtml(data.code_run, p.index, false) +
      (edited ? `<div style="padding: 0 14px 8px; font-size: 0.74rem; color: #fbbf24;">This is the result of the code in the editor. Press "Save changes" to keep it.</div>` : "");
    const run = data.code_run || {};
    p.trace.innerHTML = run.trace ? `<div class="code-trace" style="border-top: 1px solid #334155;" data-trace="${escapeHtml(JSON.stringify(Object.assign({}, run.trace, { stdout: run.stdout || "", status: run.status })))}"></div>` : "";
    if (window.initCodeTrace) initCodeTrace(p.trace);
  } catch (err) {
    p.output.innerHTML = `<div class="code-run" style="padding: 8px 14px; font-size: 0.8rem; color: #fca5a5;">Could not run it: ${escapeHtml(err.message || String(err))}</div>`;
  } finally {
    p.run.disabled = false; p.run.innerHTML = label;
  }
}

async function ceSave(btn) {
  const p = ceParts(btn);
  if (!p.area.value.trim()) { alert("The code is empty. Undo the changes, or write some code first."); return; }
  try {
    showLoader("Saving the code and running it...");
    const res = await fetch(`${API_BASE}/scripts/${currentScriptId}/cards/${p.id}`, {
      method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ code_snippet: p.area.value })
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `the server answered ${res.status}`);
    delete codeDrafts[p.index];
    await reloadCards();
  } catch (err) {
    alert("The code was not saved. " + (err.message || err));
  } finally { hideLoader(); }
}

function ceRevert(btn) {
  const p = ceParts(btn);
  p.area.value = p.area.dataset.saved;
  ceRefresh(p.area);
}

// typing like in an editor: Tab indents, Enter keeps the indent (and adds one after { : or (), Ctrl or Cmd + Enter runs, Ctrl or Cmd + S saves
document.addEventListener("input", ev => { if (ev.target.classList && ev.target.classList.contains("ce-area")) ceRefresh(ev.target); });
document.addEventListener("scroll", ev => {
  if (ev.target.classList && ev.target.classList.contains("ce-area")) ceParts(ev.target).nums.scrollTop = ev.target.scrollTop;
}, true);
document.addEventListener("keydown", ev => {
  const area = ev.target;
  if (!area.classList || !area.classList.contains("ce-area")) return;
  const unit = /^(python|py)$/i.test(area.closest(".ce").dataset.lang) ? "    " : "  ";
  const mod = ev.metaKey || ev.ctrlKey;
  if (mod && ev.key === "Enter") { ev.preventDefault(); ceRun(area); return; }
  if (mod && ev.key.toLowerCase() === "s") { ev.preventDefault(); const s = ceParts(area).save; if (!s.disabled) ceSave(s); return; }
  const { selectionStart: a, selectionEnd: b, value } = area;
  const edit = (from, to, text, caretStart, caretEnd) => {
    area.setRangeText(text, from, to, "end");
    area.setSelectionRange(caretStart, caretEnd === undefined ? caretStart : caretEnd);
    ceRefresh(area);
  };
  if (ev.key === "Tab") {
    ev.preventDefault();
    const startLine = value.lastIndexOf("\n", a - 1) + 1;
    if (a === b && !ev.shiftKey) { edit(a, b, unit, a + unit.length); return; }
    const endLine = (value.indexOf("\n", b) === -1 ? value.length : value.indexOf("\n", b));
    const block = value.slice(startLine, endLine).split("\n");
    const changed = block.map(l => ev.shiftKey ? l.replace(new RegExp("^( {1," + unit.length + "}|\\t)"), "") : unit + l).join("\n");
    edit(startLine, endLine, changed, startLine, startLine + changed.length);
    return;
  }
  if (ev.key === "Enter" && !ev.shiftKey && !mod) {
    ev.preventDefault();
    const lineStart = value.lastIndexOf("\n", a - 1) + 1;
    const before = value.slice(lineStart, a);
    const indent = (before.match(/^[ \t]*/) || [""])[0];
    const extra = /[{:(\[]\s*$/.test(before) ? unit : "";
    const insert = "\n" + indent + extra;
    edit(a, b, insert, a + insert.length);
  }
});

// The step-by-step walkthrough under the code (frontend/code_trace.js builds it from this data).
function renderCodeTrace(c) {
  const run = c.code_run;
  if (!run || !run.trace) {
    return "";
  }
  const payload = Object.assign({}, run.trace, { stdout: run.stdout || "", status: run.status });
  return `<div class="code-trace" style="border-top: 1px solid #334155;" data-trace="${escapeHtml(JSON.stringify(payload))}"></div>`;
}

function renderCodeRun(c) {
  return codeRunHtml(c.code_run, c.card_index, true);
}

// The result of running code: a status, how long it took, the output or the error and the reason. `withButtons` adds the buttons that
// act on the SAVED code (the editor above has its own Run button for what is typed in it).
function codeRunHtml(run, cardIndex, withButtons) {
  const btn = "padding: 2px 8px; font-size: 0.72rem; background: #1e293b; color: #e2e8f0; border: 1px solid #475569;";
  if (!run) {
    return `<div class="code-run" style="padding: 8px 14px; font-size: 0.8rem; color: #94a3b8;">Not run yet. Press Run to see the output.</div>`;
  }
  const looks = {
    passed:  { label: "Ran successfully", color: "#10b981" },
    failed:  { label: "Failed to run", color: "#ef4444" },
    timeout: { label: "Took too long", color: "#f59e0b" },
    skipped: { label: "Not run", color: "#94a3b8" },
  }[run.status] || { label: run.status, color: "#94a3b8" };
  const out = (run.stdout || "").trim();
  const err = (run.stderr || "").trim();
  const reason = run.reason ? `<div style="margin-top: 4px; color: #94a3b8;">${escapeHtml(run.reason)}</div>` : "";
  const fixBtn = withButtons && run.status === "failed"
    ? `<button type="button" class="btn btn-secondary" style="${btn}" onclick="runCardCode(${Number(cardIndex)}, true)">Run and let AI fix it</button>` : "";
  const box = (label, text, color) => `<div style="margin-top: 6px;"><div style="font-size: 0.68rem; letter-spacing: 0.5px; text-transform: uppercase; color: #94a3b8;">${label}</div>` +
    `<pre style="margin: 2px 0 0; padding: 8px; background: rgba(255,255,255,0.06); color: ${color}; border-radius: 6px; white-space: pre-wrap; max-height: 220px; overflow: auto;">${escapeHtml(text.slice(0, 4000))}</pre></div>`;
  return `
    <div class="code-run" style="padding: 8px 14px; font-size: 0.8rem; color: #cbd5e1; border-top: 1px solid #334155;">
      <div style="display: flex; gap: 10px; align-items: center; flex-wrap: wrap;">
        <strong style="color: ${looks.color};">${looks.label}${run.repaired ? " (fixed by AI)" : ""}</strong>
        <span style="color: #94a3b8;">${escapeHtml(run.runner || run.language || "")}${run.seconds ? `, ${run.seconds}s` : ""}</span>
        ${fixBtn}
      </div>
      ${out ? box("Output", out, "#e2e8f0") : (run.status === "passed" ? `<div style="margin-top: 6px; color: #94a3b8;">The code ran and printed nothing.</div>` : "")}
      ${err ? box("Error", err, "#fca5a5") : ""}
      ${reason}
    </div>`;
}

async function runCardCode(cardIndex, repair) {
  try {
    showLoader(repair ? "Running the code and fixing it if needed..." : "Running the code...");
    const res = await fetch(`${API_BASE}/scripts/${currentScriptId}/cards/${cardIndex}/run-code?repair=${repair}`, { method: "POST" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `The run failed (HTTP ${res.status}).`);
    await reloadCards();
  } catch (err) {
    alert("Could not run the code: " + err.message);
  } finally {
    hideLoader();
  }
}
window.runCardCode = runCardCode;

// ---- references the AI found outside the module's approved list: the SME accepts or removes each one before the script goes on (FR-14)
const REF_OUTSIDE = ["fetched", "search", "link"];
function referencesToCheck() {
  const out = [];
  (currentLoadedCards || []).forEach(c => (c.sources || []).forEach(src => { if (REF_OUTSIDE.includes(src.url_kind) && !src.validated) out.push({ card: c.card_index, src }); }));
  return out;
}
window.reviewReference = async function (cardIndex, encUrl, decision) {
  try {
    const res = await fetch(`${API_BASE}/scripts/${currentScriptId}/cards/${cardIndex}/sources/review?user_id=${currentUser.id}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: decodeURIComponent(encUrl), decision })
    });
    const d = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof d.detail === "string" ? d.detail : "Could not save that.");
    await reloadCards();
    if (d.still_in_text) showNotice("That link is also written in the card text, so it is marked rejected. Edit the card to take it out.");
    renderAiReviewPanel(currentLoadedScript, window.__lastReview || null);
  } catch (e) { alert(e.message); }
};
window.acceptAllReferences = async function () {
  const list = referencesToCheck();
  if (!list.length) return;
  if (!confirm(`Accept all ${list.length} references?\n\nDo this only after you have opened them and they are right. Each one will be marked as checked by you.`)) return;
  try {
    for (const { card, src } of list) {
      const res = await fetch(`${API_BASE}/scripts/${currentScriptId}/cards/${card}/sources/review?user_id=${currentUser.id}`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ url: src.url, decision: "accept" }) });
      if (!res.ok) throw new Error("Could not save one of them.");
    }
    await reloadCards();
    renderAiReviewPanel(currentLoadedScript, window.__lastReview || null);
  } catch (e) { alert(e.message); }
};

function renderCardsStream(cards) {
  const stream = document.getElementById("cardsStream");
  if (!stream) return;
  stream.innerHTML = "";

  cards.forEach(c => {
    let stageDirHtml = c.stage_directions ? `
      <div class="script-stage-direction-cue">
        <span class="stage-cue-tag">Instructor Staging Cue</span>
        <p class="stage-cue-content">${escapeHtml(c.stage_directions)}</p>
      </div>
    ` : '';

    const codeHtml = c.code_snippet ? renderCodeEditor(c) : '';

    let visualHtml = '';
    if (c.visual_content) {
      if (c.visual_type === 'mermaid') {
        visualHtml = `
          <div class="visual-container" style="margin: 16px 0; text-align: center;">
            <div class="mermaid">${escapeHtml(c.visual_content)}</div>
            <div class="visual-caption" style="margin-top: 6px; font-size: 0.82rem; color: var(--text-muted);">${escapeHtml(c.visual_caption || 'Architecture Diagram')}</div>
          </div>
        `;
      } else if (c.visual_type === 'animation') {
        // Model-written HTML runs only inside a sandboxed frame with no network, and reports its own height.
        visualHtml = !c.visual_srcdoc ? `
          <div class="visual-container" style="margin: 16px 0;">
            <div style="padding: 18px; border: 1px dashed #f59e0b; border-radius: 10px; color: #b45309; background: rgba(245, 158, 11, 0.08); font-size: 0.9rem;">
              ${escapeHtml(c.visual_note || "This animation could not be shown.")}
            </div>
          </div>` : `
          <div class="visual-container" style="margin: 16px 0;">
            <iframe class="animation-frame" sandbox="allow-scripts" referrerpolicy="no-referrer" title="${escapeHtml(c.visual_caption || 'Animation')}"
              style="width: 100%; height: 480px; border: 0; border-radius: 10px;" srcdoc="${escapeHtml(c.visual_srcdoc || '')}"></iframe>
            <div class="visual-caption" style="margin-top: 6px; font-size: 0.82rem; color: var(--text-muted);">${escapeHtml(c.visual_caption || 'Interactive Simulation')}</div>
          </div>
        `;
      } else if (c.visual_type === 'image') {
        const isSvg = (c.visual_content || '').trim().startsWith('<');
        const src = isSvg ? 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(c.visual_content) : safeUrl(c.visual_content);
        visualHtml = `
          <div class="visual-container" style="margin: 16px 0; text-align: center;">
            <img src="${escapeHtml(src)}" alt="${escapeHtml(c.visual_caption || '')}" style="max-width:100%; border-radius:8px;">
            <div class="visual-caption" style="margin-top: 6px; font-size: 0.82rem; color: var(--text-muted);">${escapeHtml(c.visual_caption || 'Conceptual Infographic')}</div>
          </div>
        `;
      }
    }

    if (visualHtml && c.visual_id) {
      const vid = escapeHtml(c.visual_id);
      const badge = c.visual_approved ? '<span style="color:#10b981;font-weight:700;">Approved</span>'
        : c.visual_stale ? '<span style="color:#f59e0b;font-weight:700;">Prompt changed: regenerate</span>'
        : '<span style="color:var(--text-muted);">Not approved yet</span>';
      visualHtml += `<div class="visual-actions" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:-4px 0 12px;font-size:0.8rem;">
        ${badge}
        <button type="button" class="btn btn-secondary" style="padding:3px 10px;font-size:0.76rem;" onclick="openVisualEditor('${vid}')">Edit prompt</button>
        <button type="button" class="btn btn-secondary" style="padding:3px 10px;font-size:0.76rem;" onclick="regenerateVisual('${vid}')">${c.visual_source === 'web_search' ? 'Different image' : 'Regenerate'}</button>
        ${c.visual_source === 'web_search' ? `<button type="button" class="btn btn-secondary" style="padding:3px 10px;font-size:0.76rem;" onclick="regenerateVisual('${vid}', null, true)">Not from the web</button>` : ''}
        <button type="button" class="btn btn-secondary" style="padding:3px 10px;font-size:0.76rem;" onclick="approveVisual('${vid}', ${c.visual_approved ? 'false' : 'true'})">${c.visual_approved ? 'Undo approval' : 'Approve visual'}</button>
      </div>`;
    }
    if (visualHtml && c.visual_source === 'placeholder') {
      visualHtml += `<div style="margin: -8px 0 12px; font-size: 0.78rem; color: var(--text-muted); text-align: center;"><em>Placeholder visual. ${escapeHtml(c.visual_note || '')}</em></div>`;
    } else if (visualHtml && c.visual_source) {
      visualHtml += `<div style="margin: -8px 0 12px; font-size: 0.72rem; color: var(--text-muted); text-align: center;">${c.visual_source === 'web_search' ? 'web picture from Wikimedia Commons. Check there is no watermark or logo before approving' : 'made by ' + (c.visual_source === 'claude' ? 'Claude' : 'Gemini')}</div>`;
    }

    const quizHtml = c.quiz ? formatQuiz(c.quiz, c.card_index) : '';

    const formattedDialogue = formatCardText(c.content_script || "", c.sources);

    let sourcesHtml = '';
    if (Array.isArray(c.sources) && c.sources.length) {
      sourcesHtml = `
        <div class="card-sources" style="margin: 12px 0; padding: 8px 12px; border-left: 3px solid var(--border-color, #94a3b8); font-size: 0.85rem;">
          <strong>Sources</strong>
          <ul style="margin: 4px 0 0 18px;">
            ${c.sources.map(src => {
              const href = safeUrl(src.url);
              const checked = !!src.verified;
              const isKit = src.url_kind === 'kit';
              const label = checked ? "open page (checked)" : (src.url_kind === 'search' ? "no page found; search link only (not checked)" : (src.url_kind === 'provided' ? "open link (provided by the SME)" : (src.url_kind === 'link' ? "open link (written by the AI, not checked)" : "open page (claim not confirmed)")));
              const detail = checked && src.evidence ? `<div style="color: var(--text-muted); font-size: 0.78rem;">The page says: &ldquo;${escapeHtml(src.evidence)}&rdquo;${src.checked_at ? ` (checked ${escapeHtml(src.checked_at)})` : ""}</div>`
                           : (src.note ? `<div style="color: var(--text-muted); font-size: 0.78rem;">${escapeHtml(src.note)}</div>` : "");
              const outside = REF_OUTSIDE.includes(src.url_kind);                     // found by the AI outside the module's approved list: the SME checks it
              const state = !outside ? "" : src.validated ? `<span class="ref-ok">&#10003; ${src.verified ? "Checked" : "Accepted"} by ${escapeHtml(src.validated_by || "you")}${src.verified ? "" : " (the page does not confirm the claim)"}</span>`
                : src.rejected ? `<span class="ref-bad">Rejected. Take this link out of the card text.</span>` : `<span class="ref-check">Needs your check</span>`;
              const act = outside && !src.validated ? `<span class="ref-actions"><button type="button" onclick="reviewReference(${c.card_index}, '${encodeURIComponent(src.url || "")}', 'accept')">Accept</button><button type="button" onclick="reviewReference(${c.card_index}, '${encodeURIComponent(src.url || "")}', 'remove')">Remove</button></span>` : "";
              return `<li>${checked ? "&#10003; " : ""}${escapeHtml(src.source || "")}${src.claim ? `: ${escapeHtml(src.claim)}` : ""}
                ${isKit ? `<span style="color: var(--text-muted); font-size: 0.78rem;">(not an outside source)</span>` : ""}
                ${!isKit && href !== "#" ? `<a href="${escapeHtml(href)}" target="_blank" rel="noopener noreferrer">${label}</a>` : ""} ${state} ${act}${detail}</li>`;
            }).join("")}
          </ul>
        </div>`;
    }

    const isApproved = !!c.is_approved;
    const scriptApproved = !!currentLoadedScript && currentLoadedScript.status === "GTG";          // an approved script keeps Edit and Chat; changing it sends it back for approval
    const approvalBadgeHtml = scriptApproved ? "" : isApproved
      ? `<span class="script-meta-badge approved-badge" id="card-badge-${c.id}">Approved</span>`
      : `<span class="script-meta-badge" id="card-badge-${c.id}" style="color: var(--text-muted);">Draft</span>`;

    const gtgBtnClass = isApproved ? "script-card-action-btn script-card-gtg-btn is-approved" : "script-card-action-btn script-card-gtg-btn";
    const gtgBtnText = isApproved ? "Approved (GTG)" : "GTG (Approve)";
    const over = c.spoken_words !== null && c.spoken_words !== undefined && c.words_max && c.spoken_words > c.words_max;
    const timeText = (c.starts_at_sec !== null && c.starts_at_sec !== undefined ? `${fmtClock(c.starts_at_sec)} &ndash; ${fmtClock(c.ends_at_sec)} &middot; ` : "") + `${c.duration_mins} min`;

    stream.innerHTML += `
      <section class="script-section-block" id="section-block-${c.id}">
        <div class="script-section-header">
          <div class="sh-left"><span class="sh-num">${c.card_index}</span><h3 class="script-section-heading">${escapeHtml(c.title)}</h3></div>
          <div class="script-section-meta-tags">
            <span class="sh-phase">${escapeHtml(c.pedagogical_phase)}</span>
            <span class="sh-time" title="Where this card sits on the class clock, and its planned length">${timeText}</span>
            ${over ? `<span class="sh-over" title="${c.spoken_words} words; this card's time holds about ${c.word_budget}">Reads in ~${(c.spoken_words / SPEAKING_WORDS_PER_MINUTE).toFixed(1)} min, over its time</span>` : ""}
            ${approvalBadgeHtml}
          </div>
        </div>

        ${stageDirHtml}

        <div class="spoken-script-text">
          ${formattedDialogue}
        </div>

        ${visualHtml}
        ${codeHtml}
        ${quizHtml}
        ${sourcesHtml}

        <div class="script-card-actions" id="card-actions-${c.id}">
          ${scriptApproved ? "" : `<button type="button" class="${gtgBtnClass}" id="card-gtg-btn-${c.id}" onclick="toggleCardApproval(${c.id}, ${c.card_index})">
            ${gtgBtnText}
          </button>`}
          <button type="button" class="script-card-action-btn script-card-edit-btn" onclick="openCardEditModal(${c.id})">
            Edit
          </button>
          <button type="button" class="script-card-action-btn script-card-chat-btn" onclick="openChatAssistModal(${c.card_index}, '${escapeHtml(c.title).replace(/'/g, "\\'")}')">
            Chat
          </button>
        </div>
      </section>
    `;
  });

  setTimeout(() => {
    if (window.initCodeTrace) initCodeTrace(stream);
    if (window.mermaid) mermaid.run();
  }, 100);

  // Sync markdown to the Sandbox Editor textarea
  syncSandboxEditorFromCards(cards);
}


// Convert current cards to clean HackMD Markdown string
function generateScriptMarkdownString(cards) {
  const title = (currentLoadedScript?.class_title) || document.getElementById("studioClassTitle")?.textContent || "Lecture Script";
  const duration = (currentLoadedScript?.duration_minutes) || "90";

  let md = `# ${title}\n\n`;
  md += `> Target Duration: ${duration} Minutes | Total Sections: ${cards.length}\n\n---\n\n`;

  cards.forEach(c => {
    md += `## ${c.card_index}. ${c.title}\n`;
    md += `*Timing: ${fmtClock(c.starts_at_sec)} - ${fmtClock(c.ends_at_sec)} on the class clock (${c.duration_mins}m) | Phase: ${c.pedagogical_phase}*\n\n`;

    if (c.stage_directions) {
      md += `> **Instructor Note:** ${c.stage_directions}\n\n`;
    }

    if (c.content_script) {
      md += `${c.content_script}\n\n`;
    }

    if (c.visual_content) {
      if (c.visual_type === 'mermaid') {
        md += `\`\`\`mermaid\n${c.visual_content}\n\`\`\`\n\n`;
      } else if (c.visual_type === 'image') {
        md += `![${c.visual_caption || ''}](${c.visual_content})\n\n`;
      }
    }

    if (c.code_snippet) {
      md += `\`\`\`${c.code_language || 'python'}\n${c.code_snippet}\n\`\`\`\n\n`;
    }

    if (c.quiz) {
      md += `### Checkpoint Quiz: ${c.quiz.question}\n`;
      (c.quiz.options || []).forEach(opt => {
        md += `- ${opt}\n`;
      });
      md += `\n*Explanation:* ${c.quiz.explanation}\n\n`;
    }

    md += `---\n\n`;
  });

  return md;
}

// Populate the Sandbox Editor textarea from structured cards
function syncSandboxEditorFromCards(cards) {
  const textarea = document.getElementById("sandboxMarkdownTextarea");
  if (!textarea) return;
  const md = generateScriptMarkdownString(cards);
  textarea.value = md;
  updateSandboxWordCount(md);
}

function updateSandboxWordCount(text) {
  const counter = document.getElementById("sandboxWordCount");
  if (!counter) return;
  const words = text.trim() ? text.trim().split(/\s+/).length : 0;
  counter.textContent = `${words} words`;
}

// Real-time live input handler for the Sandbox Editor
window.onSandboxEditorInput = function () {
  const textarea = document.getElementById("sandboxMarkdownTextarea");
  if (!textarea) return;
  const content = textarea.value;
  updateSandboxWordCount(content);

  // If marked.js is available, re-render the preview pane
  const stream = document.getElementById("cardsStream");
  if (stream && typeof marked !== "undefined") {
    // Render markdown directly inside the stream container
    stream.innerHTML = marked.parse(content);
    setTimeout(() => {
      if (window.mermaid) mermaid.run();
    }, 150);
  }
};

// Sandbox View Mode Switcher: split | preview | editor
window.setSandboxView = function (mode) {
  const wrapper = document.getElementById("sandboxPanesWrapper");
  if (!wrapper) return;

  wrapper.className = `sandbox-panes-wrapper ${mode}-mode`;

  document.querySelectorAll(".sandbox-tab-btn").forEach(btn => btn.classList.remove("active"));
  if (mode === "split") document.getElementById("sandboxViewSplit")?.classList.add("active");
  if (mode === "preview") document.getElementById("sandboxViewPreview")?.classList.add("active");
  if (mode === "editor") document.getElementById("sandboxViewEditor")?.classList.add("active");
};

// 1-Click Copy Sandbox Markdown
window.copySandboxMarkdown = function () {
  const textarea = document.getElementById("sandboxMarkdownTextarea");
  const content = textarea ? textarea.value : generateScriptMarkdownString(currentLoadedCards || []);
  if (content) {
    navigator.clipboard.writeText(content);
    alert("Script Markdown copied to clipboard!");
  }
};

// Copy Code Button Helper
window.copyCodeFromBlock = function (btn) {
  const box = btn.closest(".code-container");
  const code = box?.querySelector(".ce-area")?.value || box?.querySelector("code")?.innerText || "";
  if (code) {
    navigator.clipboard.writeText(code);
    const orig = btn.textContent;
    btn.textContent = "Copied!";
    setTimeout(() => { btn.textContent = orig; }, 1500);
  }
};

// ==============================================================================
// SCRIPT DOWNLOAD & EXPORT UTILITIES (MARKDOWN, PDF, WORD, TXT, COPY)
// ==============================================================================

window.toggleShareMenu = function (e) {
  if (e) e.stopPropagation();
  document.getElementById("shareMenu")?.classList.toggle("hidden");
  document.getElementById("downloadDropdownMenu")?.classList.add("hidden");
};
window.toggleDownloadDropdown = function (e) {
  document.getElementById("shareMenu")?.classList.add("hidden");
  if (e) e.stopPropagation();
  const menu = document.getElementById("downloadDropdownMenu");
  if (menu) menu.classList.toggle("hidden");
};

// Global click listener to close download dropdown
document.addEventListener("click", (e) => {
  if (!e.target.closest(".download-dropdown-wrap")) {
    document.getElementById("downloadDropdownMenu")?.classList.add("hidden");
    document.getElementById("shareMenu")?.classList.add("hidden");
  }
});

function getCleanScriptFilename(ext) {
  const clsTitle = (currentLoadedScript?.class_title) || document.getElementById("studioClassTitle")?.textContent || "Lecture_Script";
  const sanitized = clsTitle.replace(/[^a-zA-Z0-9_\-]/g, "_").replace(/_+/g, "_").slice(0, 60);
  return `${sanitized}_Script.${ext}`;
}

function buildScriptPlainText(setupMarkdown = "") {
  const title = (currentLoadedScript?.class_title) || document.getElementById("studioClassTitle")?.textContent || "Lecture Script";
  const hierarchy = document.getElementById("studioProgTrack")?.textContent || "";
  const duration = (currentLoadedScript?.duration_minutes) || document.getElementById("durationSelect")?.value || "90";

  let out = "================================================================================\n";
  out += "SCALER MASTERCLASS LECTURE SCRIPT\n";
  out += `Title: ${title}\n`;
  out += `Context: ${hierarchy}\n`;
  out += `Target Duration: ${duration} Minutes\n`;
  out += "================================================================================\n\n";

  if (setupMarkdown) out += `BEFORE THE CLASS: MANDATORY SETUP\n${setupMarkdown.replace(/[*_`#>]/g, "")}\n\n`;
  (currentLoadedCards || []).forEach(c => {
    out += `--------------------------------------------------------------------------------\n`;
    out += `SECTION ${c.card_index}: ${c.title.toUpperCase()}\n`;
    out += `[Timing: ${fmtClock(c.starts_at_sec)} - ${fmtClock(c.ends_at_sec)} on the class clock | ${c.duration_mins} mins | ${c.pedagogical_phase}]\n`;
    out += `--------------------------------------------------------------------------------\n\n`;

    if (c.stage_directions) {
      out += `[INSTRUCTOR STAGING CUE]:\n${c.stage_directions}\n\n`;
    }

    out += `[SPOKEN LECTURE DIALOGUE]:\n${markdownToPlain(c.content_script)}\n\n`;

    const note = dlVisualNote(c);
    if (note) out += `[${note.toUpperCase()}]\n${c.visual_type === "mermaid" ? c.visual_content + "\n" : ""}\n`;

    if (c.code_snippet) {
      out += `[CODE IMPLEMENTATION (${c.code_language || 'python'})]:\n${c.code_snippet}\n\n`;
      const run = dlRunText(c);
      if (run) out += `[${run.label.toUpperCase()}]:\n${run.text}\n\n`;
    }

    if (c.quiz) {
      out += `[CHECKPOINT QUIZ]:\n${quizPlain(c.quiz)}\n\n`;
    }
    out += "\n";
  });

  out += "================================================================================\n";
  out += "END OF LECTURE SCRIPT\n";
  out += "================================================================================\n";
  return out;
}

function detectTechnicalContext() {
  const prog = (currentLoadedScript?.program_name || document.getElementById("programSelect")?.selectedOptions?.[0]?.text || "").toLowerCase();
  const track = (currentLoadedScript?.track_name || document.getElementById("trackSelect")?.selectedOptions?.[0]?.dataset?.name || document.getElementById("trackSelect")?.selectedOptions?.[0]?.text || "").toLowerCase();
  const mod = (currentLoadedScript?.module_name || document.getElementById("moduleSelect")?.selectedOptions?.[0]?.text || "").toLowerCase();
  const title = (currentLoadedScript?.class_title || document.getElementById("studioClassTitle")?.textContent || "").toLowerCase();

  const isDataScienceOrAI = prog.includes("dsml") || prog.includes("data science") || prog.includes("machine learning") ||
                            prog.includes("ai") || prog.includes("artificial") || track.includes("dsml") ||
                            track.includes("ml") || track.includes("ai") || mod.includes("python") ||
                            mod.includes("pandas") || mod.includes("numpy") || mod.includes("deep learning") ||
                            mod.includes("machine learning") || title.includes("model") || title.includes("neural") ||
                            title.includes("regression") || title.includes("classification") || title.includes("dataset") ||
                            title.includes("pytorch") || title.includes("tensorflow") || title.includes("clustering") ||
                            title.includes("llm") || title.includes("nlp") || title.includes("computer vision");

  const isSoftwareEngineering = prog.includes("software") || prog.includes("backend") || prog.includes("system design") ||
                                prog.includes("dsa") || track.includes("backend") || track.includes("system design") ||
                                track.includes("dsa") || mod.includes("system design") || mod.includes("architecture") ||
                                mod.includes("microservices") || title.includes("architecture") || title.includes("concurrency") ||
                                title.includes("distributed") || title.includes("caching") || title.includes("database");

  return {
    isDataScienceOrAI,
    isSoftwareEngineering,
    programName: currentLoadedScript?.program_name || "Engineering",
    moduleName: currentLoadedScript?.module_name || "Module"
  };
}

function renderDynamicDownloadMenu() {
  const menu = document.getElementById("downloadDropdownMenu");
  if (!menu) return;
  const ctx = detectTechnicalContext();
  const item = (fmt, label, ext, hint = "") => `
    <button type="button" class="download-item-btn" onclick="downloadScript('${fmt}')">
      <span>${label}${hint ? `<small class="dl-hint">${hint}</small>` : ""}</span>
      <span class="download-item-ext">${ext}</span>
    </button>`;
  let html = item("pdf", "PDF document", ".PDF", "Diagrams, code and quizzes");
  html += item("doc", "Word document", ".DOC", "Diagrams, code and quizzes");
  html += item("html", "Web page", ".HTML", "Everything, and the animations play");
  html += item("markdown", "Markdown", ".MD", "Text, diagrams as code");
  html += item("txt", "Plain text for the teleprompter", ".TXT");
  if ((currentLoadedCards || []).some(c => !!c.code_snippet)) html += item("code", "Code only", ".PY");
  if (ctx.isDataScienceOrAI) html += item("colab", "Jupyter / Colab notebook", ".IPYNB");
  menu.innerHTML = html;
}

// ---- what every format carries: the cards, the diagrams, a note where an animation plays, the code with what it printed, the quizzes, and the setup checklist
const dlSleep = (ms) => new Promise(r => setTimeout(r, ms));
function dlSave(blob, name) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}
async function dlSetupMarkdown() {
  try {
    const r = await fetch(`${API_BASE}/scripts/${currentScriptId}/setup`);
    return r.ok ? ((await r.json()).markdown || "") : "";
  } catch (e) { return ""; }
}
function dlRunText(c) {
  const run = c.code_run;
  if (!run || typeof run !== "object") return null;
  const label = { passed: "Output", failed: "Error when run", timeout: "Stopped after the time limit", skipped: "Not run" }[run.status] || "Result";
  const text = ((run.stdout || "").trim() || (run.stderr || "").trim() || run.reason || "").slice(0, 3000);
  return text ? { label, text } : null;
}
function dlVisualNote(c) {
  if (!c.visual_content) return "";
  const cap = c.visual_caption || "";
  if (c.visual_type === "animation") return `Interactive animation${cap ? ": " + cap : ""}. It plays in the Web page download.`;
  if (c.visual_type === "mermaid") return `Diagram${cap ? ": " + cap : ""}`;
  if (c.visual_type === "image") return `Picture${cap ? ": " + cap : ""}`;
  return "";
}
// The diagrams already drawn on the page, as pictures, keyed by card index. A diagram that cannot be turned into a picture is left out and its source is used instead.
async function dlDiagramPictures() {
  const out = {};
  const cards = (currentLoadedCards || []).filter(c => c.visual_type === "mermaid" && c.visual_content);
  const svgs = Array.from(document.querySelectorAll("#cardsStream .visual-container .mermaid svg"));
  for (let i = 0; i < cards.length && i < svgs.length; i++) {
    try {
      const svg = svgs[i], rect = svg.getBoundingClientRect();
      const w = Math.max(200, Math.ceil(rect.width)), h = Math.max(120, Math.ceil(rect.height));
      const clone = svg.cloneNode(true);
      clone.setAttribute("xmlns", "http://www.w3.org/2000/svg"); clone.setAttribute("width", w); clone.setAttribute("height", h);
      const img = new Image();
      await new Promise((res, rej) => { img.onload = res; img.onerror = rej; img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(new XMLSerializer().serializeToString(clone)); });
      const canvas = document.createElement("canvas");
      canvas.width = w * 2; canvas.height = h * 2;
      const ctx = canvas.getContext("2d");
      ctx.fillStyle = "#ffffff"; ctx.fillRect(0, 0, canvas.width, canvas.height); ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      out[cards[i].card_index] = { data: canvas.toDataURL("image/png"), w, h };
    } catch (e) { /* this diagram falls back to its source */ }
  }
  return out;
}
// A Word file that carries its pictures inside it (a "web archive" Word opens as an ordinary document)
function dlWordFile(html, images) {
  const b = "----=_ScalerPart_" + Date.now().toString(36);
  const wrap = (t) => t.replace(/(.{76})/g, "$1\r\n");
  let out = `MIME-Version: 1.0\r\nContent-Type: multipart/related; boundary="${b}"; type="text/html"\r\n\r\n`;
  out += `--${b}\r\nContent-Type: text/html; charset="utf-8"\r\nContent-Transfer-Encoding: base64\r\nContent-Location: file:///C:/script.htm\r\n\r\n${wrap(btoa(unescape(encodeURIComponent(html))))}\r\n\r\n`;
  images.forEach((im, i) => {
    out += `--${b}\r\nContent-Type: image/png\r\nContent-Transfer-Encoding: base64\r\nContent-Location: file:///C:/diagram${i + 1}.png\r\n\r\n${wrap(im.data.split(",")[1])}\r\n\r\n`;
  });
  return out + `--${b}--`;
}
async function dlPrintPdf() {
  try {
    showLoader("Preparing the PDF...");
    const r = await fetch(`${API_BASE}/scripts/${currentScriptId}/export/html?print=true`);
    if (!r.ok) throw new Error(`the server answered ${r.status}`);
    const data = await r.json();
    const frame = document.createElement("iframe");
    frame.style.cssText = "position:fixed;right:0;bottom:0;width:0;height:0;border:0;";
    document.body.appendChild(frame);
    await new Promise(res => { frame.onload = res; frame.srcdoc = data.html; });
    const doc = frame.contentDocument, t0 = Date.now();
    while (Date.now() - t0 < 10000 && doc.querySelectorAll(".mermaid svg").length < doc.querySelectorAll(".mermaid").length) await dlSleep(300);   // wait for the diagrams to draw
    await dlSleep(1500);                                                                                                                         // and for the animations to show their first picture
    hideLoader();
    frame.contentWindow.focus(); frame.contentWindow.print();
    setTimeout(() => frame.remove(), 120000);
  } catch (err) { hideLoader(); alert("Could not prepare the PDF: " + (err.message || err)); }
}

window.downloadScript = async function (format) {
  if (format !== "markdown") recordClientActivity("Script Downloaded", `Downloaded '${scriptLabelNow()}' as ${String(format).toUpperCase()}`);   // the Markdown export is recorded by the server
  document.getElementById("downloadDropdownMenu")?.classList.add("hidden");

  if (!currentLoadedCards || currentLoadedCards.length === 0) {
    alert("No script sections available to download.");
    return;
  }

  try {
    if (format === "markdown") {
      await onExportMarkdown();
    } else if (format === "pdf") {
      await dlPrintPdf();
    } else if (format === "html") {
      showLoader("Packaging the web page...");
      const r = await fetch(`${API_BASE}/scripts/${currentScriptId}/export/html`);
      if (!r.ok) throw new Error(`the server answered ${r.status}`);
      const data = await r.json();
      dlSave(new Blob([data.html], { type: "text/html;charset=utf-8" }), data.filename || getCleanScriptFilename("html"));
    } else if (format === "txt") {
      const setup = await dlSetupMarkdown();
      dlSave(new Blob([buildScriptPlainText(setup)], { type: "text/plain;charset=utf-8" }), getCleanScriptFilename("txt"));
    } else if (format === "colab") {
      const setup = await dlSetupMarkdown();
      dlSave(new Blob([JSON.stringify(generateJupyterNotebook(setup), null, 2)], { type: "application/x-ipynb+json;charset=utf-8" }), getCleanScriptFilename("ipynb"));
    } else if (format === "code") {
      const codeBlocks = (currentLoadedCards || []).filter(c => !!c.code_snippet);
      let text = `# ==============================================================================\n# SCALER LIVE CODE IMPLEMENTATION\n# Class: ${(currentLoadedScript?.class_title) || "Lecture"}\n# ==============================================================================\n\n`;
      codeBlocks.forEach(c => { text += `# --- Card ${c.card_index}: ${c.title} ---\n${c.code_snippet}\n\n`; });
      dlSave(new Blob([text], { type: "text/x-python;charset=utf-8" }), getCleanScriptFilename("py"));
    } else if (format === "doc") {
      showLoader("Building the Word document...");
      await dlWord();
    }
  } catch (err) {
    alert("The download did not finish: " + (err.message || err));
  } finally {
    hideLoader();
  }
};

async function dlWord() {
  const title = (currentLoadedScript?.class_title) || document.getElementById("studioClassTitle")?.textContent || "Lecture Script";
  const hierarchy = [currentLoadedScript?.program_name, currentLoadedScript?.track_name, currentLoadedScript?.module_name].filter(Boolean).join(" \u00b7 ");
  const duration = (currentLoadedScript?.duration_minutes) || "90";
  const setup = await dlSetupMarkdown();
  const pictures = await dlDiagramPictures();
  const images = [];

  let html = `<!DOCTYPE html>
<html xmlns:o='urn:schemas-microsoft-com:office:office' xmlns:w='urn:schemas-microsoft-com:office:word' xmlns='http://www.w3.org/TR/REC-html40'>
<head><meta charset='utf-8'><title>${escapeHtml(title)}</title>
<style>
  body { font-family: Calibri, Arial, sans-serif; font-size: 11pt; line-height: 1.5; color: #000000; margin: 36px; }
  h1 { font-size: 22pt; color: #000000; margin-bottom: 4px; font-weight: bold; }
  h2 { font-size: 14pt; color: #000000; border-bottom: 1px solid #808080; padding-bottom: 3px; margin-top: 26px; }
  .meta-banner { font-size: 10pt; color: #404040; margin-bottom: 20px; }
  .cue { border-left: 3px solid #808080; padding: 6px 12px; margin: 10px 0; font-style: italic; }
  .cue-tag { font-size: 9pt; font-weight: bold; color: #404040; text-transform: uppercase; margin-bottom: 2px; font-style: normal; }
  .dialogue-title { font-size: 9pt; font-weight: bold; color: #404040; text-transform: uppercase; margin-top: 10px; }
  pre { background: #ffffff; border: 1px solid #a0a0a0; padding: 10px; font-family: Consolas, 'Courier New', monospace; font-size: 10pt; margin: 10px 0; white-space: pre-wrap; }
  .card-para { font-size: 11pt; line-height: 1.6; color: #000000; margin: 8px 0; }
  .card-heading { font-size: 12pt; font-weight: bold; color: #000000; margin: 16px 0 6px; }
  .card-list { margin: 6px 0 10px 24px; font-size: 11pt; line-height: 1.5; }
  .card-callout { border-left: 3px solid #808080; padding: 4px 12px; margin: 10px 0; font-size: 11pt; }
  .callout-label { font-weight: bold; text-transform: uppercase; font-size: 9pt; margin-right: 6px; }
  .card-code { background: #ffffff; border: 1px solid #a0a0a0; padding: 10px; font-family: Consolas, 'Courier New', monospace; font-size: 10pt; margin: 10px 0; white-space: pre-wrap; }
  .stage-cue { font-style: italic; color: #404040; margin: 8px 0; }
  .card-link { color: #000000; text-decoration: underline; } .link-source { font-size: 9pt; color: #404040; } .sme-gap { font-weight: bold; }
  .quiz { border: 1px solid #808080; padding: 10px 14px; margin: 12px 0; }
  .visual { text-align: center; margin: 14px 0; } .visual-cap { font-size: 9.5pt; color: #404040; margin-top: 4px; }
  .note { border: 1px dashed #808080; padding: 8px 12px; margin: 12px 0; color: #404040; }
  .run { border-left: 3px solid #808080; padding: 4px 12px; margin: 8px 0; font-size: 10pt; } .run pre { margin: 4px 0 0; }
  hr { border: none; border-top: 1px solid #a0a0a0; margin: 26px 0; }
  .footer { text-align: center; font-size: 9pt; color: #404040; margin-top: 36px; }
</style>
</head>
<body>
  <h1>${escapeHtml(title)}</h1>
  <div class="meta-banner">${escapeHtml(hierarchy)} &bull; ${duration} Minutes &bull; ${currentLoadedCards.length} Sections</div>
  <hr/>
`;
  if (setup) html += `<h2>Before the class: mandatory setup</h2><pre>${escapeHtml(setup.replace(/[*_`#>]/g, ""))}</pre>`;

  currentLoadedCards.forEach(c => {
    html += `<h2>Section ${c.card_index}: ${escapeHtml(c.title)} (${fmtClock(c.starts_at_sec)} &ndash; ${fmtClock(c.ends_at_sec)} &bull; ${c.duration_mins}m &bull; ${escapeHtml(c.pedagogical_phase)})</h2>`;
    if (c.stage_directions) html += `<div class="cue"><div class="cue-tag">Instructor Staging Cue</div><div>${escapeHtml(c.stage_directions)}</div></div>`;
    html += `<div class="dialogue-title">Spoken Lecture Dialogue</div>`;
    html += formatCardText(c.content_script || "", c.sources);
    const pic = pictures[c.card_index];
    if (c.visual_type === "mermaid" && c.visual_content) {
      if (pic) {
        images.push(pic);
        const w = Math.min(pic.w, 600), h = Math.round(pic.h * w / pic.w);
        html += `<div class="visual"><img src="file:///C:/diagram${images.length}.png" width="${w}" height="${h}" alt="${escapeHtml(c.visual_caption || "Diagram")}"><div class="visual-cap">${escapeHtml(c.visual_caption || "")}</div></div>`;
      } else {
        html += `<div class="note"><b>${escapeHtml(dlVisualNote(c))}</b><pre>${escapeHtml(c.visual_content)}</pre></div>`;
      }
    } else if (c.visual_type === "image" && c.visual_content && /^https?:/i.test(c.visual_content)) {
      html += `<div class="visual"><img src="${escapeHtml(c.visual_content)}" width="560" alt="${escapeHtml(c.visual_caption || "")}"><div class="visual-cap">${escapeHtml(c.visual_caption || "")}</div></div>`;
    } else if (c.visual_content) {
      html += `<div class="note">${escapeHtml(dlVisualNote(c))}</div>`;
    }
    if (c.code_snippet) {
      html += `<pre><code>${escapeHtml(c.code_snippet)}</code></pre>`;
      const run = dlRunText(c);
      if (run) html += `<div class="run"><b>${escapeHtml(run.label)}</b><pre>${escapeHtml(run.text)}</pre></div>`;
    }
    if (c.quiz) html += quizDocHtml(c.quiz);
  });
  html += `<hr/><div class="footer">&mdash; End of Masterclass Lecture Script &mdash;</div></body></html>`;
  dlSave(new Blob([dlWordFile(html, images)], { type: "application/msword" }), getCleanScriptFilename("doc"));
}

function generateJupyterNotebook(setupMarkdown = "") {
  const title = (currentLoadedScript?.class_title) || document.getElementById("studioClassTitle")?.textContent || "Lecture Notebook";
  const hierarchy = document.getElementById("studioProgTrack")?.textContent || "";
  const duration = (currentLoadedScript?.duration_minutes) || "90";

  const cells = [
    {
      cell_type: "markdown",
      metadata: { colab_type: "text" },
      source: [
        `# ${title}\n`,
        `**Curriculum Track:** ${hierarchy}  \n`,
        `**Target Duration:** ${duration} Minutes  \n`,
        `*Auto-generated Scaler Masterclass Interactive Google Colab Notebook*\n\n`,
        `---\n`
      ]
    }
  ];

  if (setupMarkdown) cells.push({ cell_type: "markdown", metadata: { colab_type: "text" }, source: ("## Before the class: mandatory setup\n\n" + setupMarkdown).split("\n").map(l => l + "\n") });
  (currentLoadedCards || []).forEach(c => {
    let mdContent = `## Card ${c.card_index}: ${c.title} (${c.duration_mins}m - ${c.pedagogical_phase})\n\n`;
    if (c.stage_directions) {
      mdContent += `> **Instructor Cue:** ${c.stage_directions}\n\n`;
    }
    mdContent += `### Spoken Lecture Concept\n${c.content_script}\n\n`;

    const vnote = dlVisualNote(c);
    if (vnote) mdContent += `*${vnote}*\n\n` + (c.visual_type === "mermaid" ? "```mermaid\n" + c.visual_content + "\n```\n\n" : "");
    cells.push({
      cell_type: "markdown",
      metadata: { colab_type: "text" },
      source: mdContent.split("\n").map(l => l + "\n")
    });

    if (c.code_snippet) {
      cells.push({
        cell_type: "code",
        execution_count: null,
        metadata: { colab: {} },
        outputs: [],
        source: c.code_snippet.split("\n").map((line, idx, arr) => idx === arr.length - 1 ? line : line + "\n")
      });
    }

    if (c.quiz) {
      const q = c.quiz;
      let quizMd = `### Checkpoint Quiz: ${q.question}\n`;
      (q.options || []).forEach((opt, oIdx) => {
        quizMd += `- [ ] **Option ${String.fromCharCode(65 + oIdx)}:** ${opt}\n`;
      });
      quizMd += `\n<details><summary><b>Click to see Diagnostic Explanation</b></summary>\n\n${q.explanation}\n</details>\n`;
      cells.push({
        cell_type: "markdown",
        metadata: { colab_type: "text" },
        source: quizMd.split("\n").map(l => l + "\n")
      });
    }
  });

  return {
    nbformat: 4,
    nbformat_minor: 0,
    metadata: {
      colab: {
        name: title,
        provenance: []
      },
      kernelspec: {
        name: "python3",
        display_name: "Python 3"
      },
      language_info: {
        name: "python"
      }
    },
    cells: cells
  };
}

window.copyFullScriptToClipboard = async function () {
  recordClientActivity("Script Copied", `Copied the full text of '${scriptLabelNow()}'`);
  const text = buildScriptPlainText();
  try {
    await navigator.clipboard.writeText(text);
    const btn = document.getElementById("copyScriptBtn");
    if (btn) {
      const orig = btn.innerHTML;
      btn.innerHTML = `<span>Copied to Clipboard!</span>`;
      btn.style.borderColor = "var(--success-color)";
      btn.style.color = "var(--success-color)";
      setTimeout(() => {
        btn.innerHTML = orig;
        btn.style.borderColor = "";
        btn.style.color = "";
      }, 2500);
    }
  } catch (err) {
    alert("Could not copy script automatically: " + err);
  }
};

// Card Approval Toggle (GTG)
window.toggleCardApproval = async function (cardId, cardIndex) {
  const card = (currentLoadedCards || []).find(c => c.id === cardId);
  const currentStatus = card ? !!card.is_approved : false;
  const nextStatus = !currentStatus;

  const actionName = nextStatus ? `approve Card ${cardIndex} (GTG)` : `revert Card ${cardIndex} to Draft`;
  if (!confirm(`Are you sure you want to ${actionName}?`)) {
    return;
  }

  // Optimistic UI update
  const gtgBtn = document.getElementById(`card-gtg-btn-${cardId}`);
  const badge = document.getElementById(`card-badge-${cardId}`);
  if (gtgBtn) {
    if (nextStatus) {
      gtgBtn.classList.add("is-approved");
      gtgBtn.textContent = "Approved (GTG)";
    } else {
      gtgBtn.classList.remove("is-approved");
      gtgBtn.textContent = "GTG (Approve)";
    }
  }
  if (badge) {
    if (nextStatus) {
      badge.className = "script-meta-badge approved-badge";
      badge.textContent = "Approved";
      badge.style.color = "";
    } else {
      badge.className = "script-meta-badge";
      badge.textContent = "Draft";
      badge.style.color = "var(--text-muted)";
    }
  }
  if (card) {
    card.is_approved = nextStatus;
  }

  try {
    const res = await fetch(`${API_BASE}/scripts/${currentScriptId}/cards/${cardId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_approved: nextStatus })
    });
    if (!res.ok) {
      throw new Error(`Server returned ${res.status}`);
    }
    const saved = await res.json().catch(() => ({}));
    document.getElementById(`continuity-${cardId}`)?.remove();
    if (nextStatus && saved.continuity_note) {                         // two approved cards are read together for continuity
      const block = document.getElementById(`section-block-${cardId}`);
      if (block) block.insertAdjacentHTML("afterbegin", `<div id="continuity-${cardId}" class="mr-note" style="margin-bottom:14px;"><b>Continuity with card ${cardIndex - 1}</b>: ${escapeHtml(saved.continuity_note)}</div>`);
      showNotice(`Card ${cardIndex} may not follow card ${cardIndex - 1} smoothly. See the note on the card.`);
    }

    // If user just approved (GTG) the latest card in the deck and more outline cards remain:
    const totalCards = (currentOutlineCards && currentOutlineCards.length) ? currentOutlineCards.length : (currentLoadedCards ? currentLoadedCards.length : 14);
    const draftedCount = (currentLoadedCards || []).length;
    
    if (nextStatus && cardIndex === draftedCount && cardIndex < totalCards) {
      const nextCardIndex = cardIndex + 1;
      const shouldDraftNext = confirm(`Card ${cardIndex} is now Approved (GTG)!\n\nWould you like to start generating Card ${nextCardIndex} now?`);
      if (shouldDraftNext) {
        await advanceNextCard(nextCardIndex);
      } else {
        updateSectionGateState();
      }
    } else {
      updateSectionGateState();
    }
  } catch (err) {
    console.error("Failed to persist card approval:", err);
    // Revert optimistic update
    if (card) card.is_approved = currentStatus;
    if (gtgBtn) {
      if (currentStatus) {
        gtgBtn.classList.add("is-approved");
        gtgBtn.textContent = "Approved (GTG)";
      } else {
        gtgBtn.classList.remove("is-approved");
        gtgBtn.textContent = "GTG (Approve)";
      }
    }
    if (badge) {
      if (currentStatus) {
        badge.className = "script-meta-badge approved-badge";
        badge.textContent = "Approved";
      } else {
        badge.className = "script-meta-badge";
        badge.textContent = "Draft";
      }
    }
    alert("Could not update approval status: " + err.message);
  }
};

// In-line Edit Handler (PRD FR-11)
window.openCardEditModal = function (cardId) {

  const card = currentLoadedCards.find(c => c.id === cardId);
  if (!card) return;

  document.getElementById("editCardId").value = card.id;
  document.getElementById("editModalTitle").textContent = `Edit Card ${card.card_index}: ${card.title}`;
  document.getElementById("editCardTitle").value = card.title;
  document.getElementById("editCardStageDir").value = card.stage_directions || "";
  document.getElementById("editCardScript").value = card.content_script;
  document.getElementById("editCardCode").value = card.code_snippet || "";

  document.getElementById("cardEditModal").classList.remove("hidden");
};

async function onSaveCardEdit() {
  const cardId = document.getElementById("editCardId").value;
  const title = document.getElementById("editCardTitle").value;
  const stageDir = document.getElementById("editCardStageDir").value;
  const scriptText = document.getElementById("editCardScript").value;
  const code = document.getElementById("editCardCode").value;

  if (!confirm("Are you sure you want to save these edits to the card?")) {
    return;
  }

  try {
    showLoader("Saving Card Edits...");
    await fetch(`${API_BASE}/scripts/${currentScriptId}/cards/${cardId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: title,
        stage_directions: stageDir,
        content_script: scriptText,
        code_snippet: code
      })
    });

    document.getElementById("cardEditModal").classList.add("hidden");
    await loadScriptStudioView();
  } catch (err) {
    alert("Error saving edits: " + err);
  } finally {
    hideLoader();
  }
}

// AI Chat Co-Pilot Handler (PRD FR-11 & FR-21)
window.openChatAssistModal = function (cardIndex, cardTitle) {
  document.getElementById("chatCardIndex").value = cardIndex;
  document.getElementById("chatModalTitle").textContent = `AI Chat Assist (Card ${cardIndex}: ${cardTitle})`;
  document.getElementById("chatPromptInput").value = "";
  document.getElementById("chatAssistModal").classList.remove("hidden");
};

async function onSubmitChatAssist() {
  const cardIndex = parseInt(document.getElementById("chatCardIndex").value);
  const prompt = document.getElementById("chatPromptInput").value;

  if (!prompt) {
    alert("Please enter revision instructions.");
    return;
  }

  if (!confirm(`Are you sure you want to revise Card ${cardIndex} with this AI instruction?`)) {
    return;
  }

  try {
    showLoader("Claude Sonnet Executing Card Revision...");
    const res = await fetch(`${API_BASE}/scripts/${currentScriptId}/chat-assist`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        card_index: cardIndex,
        user_prompt: prompt
      })
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `the server answered ${res.status}`);

    document.getElementById("chatAssistModal").classList.add("hidden");
    await loadScriptStudioView();
    // say how the new code ran, so a change that does not work is never missed
    if (data.code_changed && data.code_run) {
      const run = data.code_run;
      if (run.status === "failed" || run.status === "timeout") alert(`The code was changed, but it does not run: ${run.reason || "it exited with an error"}\n\nOpen the card and use "Run and let AI fix it", or ask the chat again.`);
    }
  } catch (err) {
    alert("The card was not changed. " + (err.message || err));
  } finally {
    hideLoader();
  }
}

// Draft All Remaining Sections Handler: writes the cards that are not written yet, one at a time, with progress and the errors
async function onDraftAllRemaining() {
  const failures = [];
  try {
    showLoader("Checking which cards are still to write...");
    const planRes = await fetch(`${API_BASE}/scripts/${currentScriptId}/draft-plan`);
    if (!planRes.ok) throw new Error((await planRes.json().catch(() => ({}))).detail || `the server answered ${planRes.status}`);
    const plan = await planRes.json();
    let done = 0, stoppedFor = null;
    for (const card of plan.remaining) {
      showLoader(`Writing card ${card.card_index} of ${plan.total}: ${card.title || ""} (${done} done)`);
      const res = await fetch(`${API_BASE}/scripts/${currentScriptId}/generate-section?start_card=${card.card_index}&end_card=${card.card_index}&section_id=${encodeURIComponent(card.section_id)}`, { method: "POST" });
      if (res.ok) { done += 1; continue; }
      const detail = (await res.json().catch(() => ({}))).detail || `the server answered ${res.status}`;
      failures.push(`Card ${card.card_index}: ${detail}`);
      if (/budget/i.test(String(detail))) { stoppedFor = "The AI budget is used up, so drafting stopped."; break; }
    }
    await loadScriptStudioView();
    if (failures.length || stoppedFor) {
      alert(`${done} of ${plan.remaining.length} cards were written.\n\n${stoppedFor ? stoppedFor + "\n\n" : ""}Problems:\n${failures.join("\n")}`);
    }
  } catch (err) {
    alert("Could not draft the remaining cards: " + (err.message || err));
  } finally {
    hideLoader();
  }
}

// ---- Needs from the SME: the [[FROM_SME]] gaps and the program facts the kit lacks, on one screen
function smeError(message) {
  const el = document.getElementById("smeItemsError");
  if (!el) return;
  el.textContent = message || "";
  el.classList.toggle("hidden", !message);
}

async function loadSmeItems() {
  const res = await fetch(`${API_BASE}/scripts/${currentScriptId}/sme-items`);
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `the server answered ${res.status}`);
  return res.json();
}

async function updateSmeCount() {
  const badge = document.getElementById("smeItemsCount");
  if (!badge || !currentScriptId) return;
  try {
    const data = await loadSmeItems();
    const n = data.items.length + data.kit_gaps.length;
    badge.textContent = String(n);
    badge.style.display = n ? "inline-block" : "none";
  } catch (e) { badge.style.display = "none"; }
}

async function openSmeItems() {
  document.getElementById("smeItemsModal")?.classList.remove("hidden");
  await renderSmeItems();
}

function closeSmeItems() {
  document.getElementById("smeItemsModal")?.classList.add("hidden");
  updateSmeCount();
}

async function renderSmeItems() {
  const list = document.getElementById("smeItemsList");
  const gaps = document.getElementById("smeKitGaps");
  try {
    const data = await loadSmeItems();
    smeError("");
    list.innerHTML = data.items.length ? data.items.map((it, i) => `
      <div style="border: 1px solid #e2e8f0; border-radius: 8px; padding: 12px; margin-bottom: 10px;">
        <div style="font-weight: 700; color: #0f172a;">Card ${Number(it.card_index)}: ${escapeHtml(it.title || "")}</div>
        <div style="margin: 4px 0; color: #b45309; font-size: 0.86rem;">Needed: ${escapeHtml(it.need)}</div>
        <div style="font-size: 0.78rem; color: #64748b; margin-bottom: 8px;">...${escapeHtml(it.context)}...</div>
        <div style="display: flex; gap: 8px;">
          <input id="smeValue${i}" class="form-control" style="flex: 1;" maxlength="500" placeholder="Type it here">
          <button class="btn btn-primary" onclick="saveSmeItem(${i})">Save</button>
        </div>
        <input type="hidden" id="smeCard${i}" value="${Number(it.card_index)}">
        <input type="hidden" id="smeMarker${i}" value="${escapeHtml(it.placeholder)}">
      </div>`).join("") : `<p style="color: #059669; font-weight: 600;">No gaps left in the cards.</p>`;
    gaps.innerHTML = data.kit_gaps.length ? `
      <div style="border: 1px solid #fde68a; background: #fffbeb; border-radius: 8px; padding: 12px; margin-top: 8px;">
        <div style="font-weight: 700; color: #92400e; margin-bottom: 4px;">Program details this opening class should cover, which the program kit does not hold</div>
        <div style="font-size: 0.86rem; color: #92400e;">${data.kit_gaps.map(g => escapeHtml(g)).join(", ")}. Add them to the script's additional resources, or ask the Module Owner for them.</div>
      </div>` : "";
  } catch (err) {
    smeError("Could not load the list. " + (err.message || err));
  }
}

async function saveSmeItem(i) {
  const value = (document.getElementById(`smeValue${i}`)?.value || "").trim();
  if (!value) { smeError("Type a value first."); return; }
  try {
    const res = await fetch(`${API_BASE}/scripts/${currentScriptId}/sme-items/fill`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ card_index: Number(document.getElementById(`smeCard${i}`).value), placeholder: document.getElementById(`smeMarker${i}`).value, value })
    });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `the server answered ${res.status}`);
    await renderSmeItems();
    await loadScriptStudioView();
  } catch (err) {
    smeError("Could not save. " + (err.message || err));
  }
}

window.openSmeItems = openSmeItems;
window.closeSmeItems = closeSmeItems;
window.saveSmeItem = saveSmeItem;

// Export Markdown Handler
async function onExportMarkdown() {
  try {
    showLoader("Packaging Markdown Script...");
    const res = await fetch(`${API_BASE}/scripts/${currentScriptId}/export/markdown`);
    const data = await res.json();

    const blob = new Blob([data.markdown], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = data.filename;
    a.click();
    URL.revokeObjectURL(url);
  } catch (err) {
    alert("Error exporting markdown: " + err);
  } finally {
    hideLoader();
  }
}

// Share Script to HackMD.io
window.shareScriptToHackMD = async function () {
  if (!currentScriptId) {
    alert("No active script to share.");
    return;
  }
  try {
    showLoader("Publishing clean script to HackMD.io...");
    const textarea = document.getElementById("sandboxMarkdownTextarea");
    const customContent = textarea ? textarea.value : undefined;

    const res = await fetch(`${API_BASE}/scripts/${currentScriptId}/share-hackmd`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ markdown_content: customContent })
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || "Failed to publish note to HackMD.");
    }

    // Copy live shareable link to clipboard
    if (data.url) {
      await navigator.clipboard.writeText(data.url);
      window.open(data.url, "_blank");
      alert("Successfully published to HackMD.io!\n\nLink copied to clipboard:\n" + data.url);
    }
  } catch (err) {
    alert("Error sharing to HackMD: " + err.message);
  } finally {
    hideLoader();
  }
};

// Assignments & Mock Interviews (PRD FR-23)
async function onGenerateAssignments() {
  try {
    showLoader("Generating Assignments & Mock Interview Questions (PRD FR-23)...");
    const res = await fetch(`${API_BASE}/scripts/${currentScriptId}/generate-assignments`, {
      method: "POST"
    });
    const data = await res.json();

    const container = document.getElementById("assignmentsContent");
    let homeworkHtml = data.homework_problems.map(h => `
      <div style="background:var(--bg-primary); border:1px solid var(--border-subtle); border-radius:10px; padding:14px; margin-bottom:12px;">
        <div style="display:flex; justify-content:space-between; margin-bottom:6px;">
          <strong style="color:var(--primary-glow);">${h.title}</strong>
          <span style="font-size:0.8rem; background:rgba(245,158,11,0.15); color:var(--warning-color); padding:2px 8px; border-radius:10px;">${h.difficulty} - ${h.time_limit_mins}m</span>
        </div>
        <p style="font-size:0.85rem; color:var(--text-secondary); line-height:1.4;">${h.description}</p>
        <p style="font-size:0.75rem; color:var(--text-muted); margin-top:6px;"><em>Evaluation Rubric:</em> ${h.rubric}</p>
      </div>
    `).join('');

    let interviewHtml = data.interview_questions.map(i => `
      <div style="background:var(--bg-primary); border:1px solid var(--border-subtle); border-radius:10px; padding:14px; margin-bottom:12px;">
        <div style="display:flex; justify-content:space-between; margin-bottom:6px;">
          <strong style="color:var(--purple-accent);">${i.company}</strong>
          <span style="font-size:0.75rem; color:var(--text-muted);">Tier-1 Tech Bar</span>
        </div>
        <p style="font-size:0.88rem; color:var(--text-primary); margin-bottom:6px;"><strong>Q:</strong> ${i.question}</p>
        <p style="font-size:0.8rem; color:var(--text-secondary); line-height:1.4;"><em>Expected Answer Depth:</em> ${i.expected_depth}</p>
      </div>
    `).join('');

    container.innerHTML = `
      <h4 style="font-family:var(--font-heading); margin-bottom:12px;">Take-Home Homework Problems</h4>
      ${homeworkHtml}
      <h4 style="font-family:var(--font-heading); margin:18px 0 12px 0;">Industry Mock Interview Questions</h4>
      ${interviewHtml}
    `;

    document.getElementById("assignmentsModal").classList.remove("hidden");
  } catch (err) {
    alert("Error generating assignments: " + err);
  } finally {
    hideLoader();
  }
}

// ==============================================================================
// 4. MULTI-AGENT REVIEW AUDIT & MO GTG SIGN-OFF
// ==============================================================================

// Runs the review and shows what it is doing, because on a rate-limited key it takes minutes.
async function performReview() {
  const id = currentScriptId;
  showLoader("Starting the review...");
  const poll = setInterval(async () => {
    try {
      const r = await fetch(`${API_BASE}/scripts/${id}/review/progress`);
      const p = await r.json();
      if (p.message && !p.done) showLoader(p.message + " (a full review can take several minutes)");
    } catch (e) { /* polling is only for display */ }
  }, 3000);
  try {
    const res = await fetch(`${API_BASE}/scripts/${id}/review`, { method: "POST" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `The review failed (HTTP ${res.status}).`);
    return data;
  } finally {
    clearInterval(poll);
  }
}

async function onRunReview() { return window.runAiReview(); }

function onMoSignOff() {
  if (!currentScriptId) {
    alert("No active script loaded to approve.");
    return;
  }
  if (!confirm("Are you sure you want to grant the official MO GTG sign-off for this script?")) {
    return;
  }
  approveScriptDirectly(currentScriptId);
}

window.submitScriptToMo = async function () {
  if (!currentScriptId) {
    alert("No active script selected to submit.");
    return;
  }
  if (!confirm("Are you sure you want to submit this completed script to the Module Owner for review?")) {
    return;
  }

  try {
    showLoader("Submitting Script to Module Owner for Review...");
    const userParam = currentUser ? `?user_id=${currentUser.id}` : "";
    const res = await fetch(`${API_BASE}/scripts/${currentScriptId}/submit-mo${userParam}`, {
      method: "POST"
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "Failed to submit script for review");
    }
    const data = await res.json();
    alert("Script successfully submitted to Module Owner for review!");
    await loadScriptStudioView();
  } catch (err) {
    alert("Error submitting script: " + err.message);
  } finally {
    hideLoader();
  }
};

async function loadShareState() {
  const res = await fetch(`${API_BASE}/scripts/${currentScriptId}/share`);
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || "Could not read the link state.");
  return res.json();
}

function describeShare(state) {
  if (state.status === "revoked") return "This link is switched off.";
  if (state.status === "expired") return "This link has expired.";
  return state.never_expires ? "This link never expires." : `This link works for ${state.days_left} more day(s) (until ${parseServerTime(state.expires_at).toLocaleDateString()}).`;
}

async function onShareLink() {
  if (!currentScriptId) {
    alert("Shareable link is not ready yet. Please generate or load the script first.");
    return;
  }
  try {
    let state = await loadShareState();
    if (!state.active) {
      if (!confirm(`${describeShare(state)}\n\nMake a new link now?`)) return;
      state = await (await fetch(`${API_BASE}/scripts/${currentScriptId}/share/renew`, { method: "POST" })).json();
    }
    currentShareableUuid = state.url.split("/").pop();
    const shareUrl = `${window.location.origin}${state.url}`;
    recordClientActivity("Share Link Copied", `Copied the share link for '${scriptLabelNow()}'`);
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(shareUrl).then(() => showNotice("Share link copied."))
        .catch(() => prompt("Copy your share link:", shareUrl));
    } else {
      prompt("Copy your share link:", shareUrl);
    }
  } catch (err) {
    alert("Could not get the share link: " + err.message);
  }
}

async function onRevokeShareLink() {
  if (!currentScriptId) return;
  if (!confirm("Switch the public link off now? Anyone who has it will see a 'link switched off' page.")) return;
  const res = await fetch(`${API_BASE}/scripts/${currentScriptId}/share/revoke`, { method: "POST" });
  if (!res.ok) { alert((await res.json().catch(() => ({}))).detail || "Could not revoke the link."); return; }
  alert("The public link is switched off. Use 'New Link' when you want to share the script again.");
}

async function onRenewShareLink() {
  if (!currentScriptId) return;
  const days = prompt("Make a NEW link. The old address stops working.\nHow many days should it stay valid? (0 = never expires)", "30");
  if (days === null) return;
  const n = parseInt(days, 10);
  if (isNaN(n) || n < 0 || n > 365) { alert("Please enter a number from 0 to 365."); return; }
  const res = await fetch(`${API_BASE}/scripts/${currentScriptId}/share/renew?days=${n}`, { method: "POST" });
  if (!res.ok) { alert((await res.json().catch(() => ({}))).detail || "Could not make a new link."); return; }
  await onShareLink();
}

window.onShareLink = onShareLink;
window.onRevokeShareLink = onRevokeShareLink;
window.onRenewShareLink = onRenewShareLink;

// Warn user before leaving/refreshing if they are actively authoring or editing
window.addEventListener("beforeunload", (e) => {
  // If there are unsaved edits in an open modal or if an active draft exists in progress
  const cardModal = document.getElementById("cardEditModal");
  const singleEditModal = document.getElementById("singleCardEditModal");
  const isEditingCard = (cardModal && !cardModal.classList.contains("hidden")) ||
                        (singleEditModal && !singleEditModal.classList.contains("hidden"));
  const hasActiveDraft = !!currentScriptId || outlineIsOpen();

  if (isEditingCard || hasActiveDraft) {
    const confirmationMessage = "You have an active script session or unsaved edits. Are you sure you want to refresh? Any unsaved edits will not be saved.";
    e.preventDefault();
    e.returnValue = confirmationMessage;
    return confirmationMessage;
  }
});

// Utility Helpers
function updateScriptStatusBadge(text, className) {
  const badge = document.getElementById("activeScriptBadge");
  badge.textContent = `Status: ${text}`;
  badge.className = `status-badge ${className}`;
  const pill = document.getElementById("svStatus");
  if (pill) {                                                             // the status shown beside the script's title
    const inReview = /SME Review/i.test(text), approved = /GTG|Approved/i.test(text), pending = !inReview && /MO/i.test(text);
    const revising = document.getElementById("smeRevisionBanner")?.classList.contains("hidden") === false;
    const again = currentLoadedScript && currentLoadedScript.revision_by === "Approval withdrawn";
    pill.textContent = approved ? "Approved" : pending ? "Pending review" : inReview ? "SME review" : (revising ? (again ? "Needs approval again" : "Changes requested") : "Draft");
    pill.className = "sv-pill " + (approved ? "ok" : pending || inReview ? "wait" : revising ? "warn" : "");
  }
  if (smeView === "script") {                                             // the progress bar follows the script's status
    const revise = !document.getElementById("smeRevisionBanner")?.classList.contains("hidden");
    document.getElementById("smeSteps")?.classList.toggle("hidden", /GTG|Approved/i.test(text));      // an approved script has nothing left to do, so no progress bar
    if (/GTG|Approved/i.test(text)) smeSetStep(6, true);
    else if (/SME Review/i.test(text)) smeSetStep(4);
    else if (/MO/i.test(text)) smeSetStep(5);
    else smeSetStep(3, false, revise ? ((currentLoadedScript && currentLoadedScript.revision_by === "Approval withdrawn") ? "Needs approval again" : "Changes requested") : null);
  }
}

// Only http(s) links and our own /generated/ pictures may become a link or an image source.
function safeUrl(url) {
  const u = String(url || "").trim();
  if (/^https?:\/\//i.test(u)) return u;
  if (u.startsWith("/generated/") && !u.includes("..")) return u;
  return "#";
}

// Each animation frame reports how tall its content is; the matching frame is resized so the animation shows whole.
window.addEventListener("message", (ev) => {
  if (!ev.data || ev.data.type !== "scaler-animation-height") return;
  document.querySelectorAll("iframe.animation-frame").forEach((frame) => {
    if (frame.contentWindow === ev.source) frame.style.height = Math.min(Math.max(ev.data.height + 8, 120), 1400) + "px";
  });
});


// ---- visual cards: edit the prompt, regenerate and approve one visual at a time ------------------------------------

async function visualApi(path, method, body) {
  const res = await fetch(`${API_BASE}/scripts/${currentScriptId}/visuals${path}`, {
    method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `The request failed (HTTP ${res.status}).`);
  return data;
}

// When an approved script is changed the server makes it a draft again; the page follows, and tells the author what to do next
async function refreshStatusAfterEdit() {
  if (!currentScriptId || !currentLoadedScript) return;
  try {
    const res = await fetch(`${API_BASE}/scripts/${currentScriptId}`);
    if (!res.ok) return;
    const script = await res.json();
    const wasApproved = currentLoadedScript.status === "GTG";
    Object.assign(currentLoadedScript, { status: script.status, revision_note: script.revision_note, revision_by: script.revision_by, revision_at: script.revision_at });
    applyScriptStatus(script);
    loadAiReview(script);
    if (wasApproved && script.status !== "GTG") {
      showNotice("This script was approved. Your change made it a draft again. Submit it to the Module Owner when you are done.");
      currentLoadedCards = script.cards; renderCardsStream(script.cards);
    }
  } catch (e) { /* the next change tries again */ }
}
// A thin bar at the top of the page shows that something is loading, for every click or action that waits on the server (the quiet background refreshes and polls are left out)
const GlobalBar = (() => {
  let count = 0, timer = null, el = null;
  const bar = () => el || (el = Object.assign(document.body.appendChild(document.createElement("div")), { id: "globalBar", className: "global-bar" }));
  return {
    start() { count += 1; if (count === 1) timer = setTimeout(() => bar().classList.add("on"), 180); },                 // a quick answer shows no flicker
    end() { count = Math.max(0, count - 1); if (count === 0) { clearTimeout(timer); el && el.classList.remove("on"); } }
  };
})();
const QUIET_REQUEST = /\/(activities|review\/progress|llm\/usage|dashboard\/scripts|curriculum\/full-tree|users\/\d+)(\?|$)/;

(function watchEditsToApprovedScripts() {
  const inner = window.fetch;
  window.fetch = async function (input, init) {
    const url0 = typeof input === "string" ? input : (input && input.url) || "";
    const loud = /\/api\//.test(url0) && !QUIET_REQUEST.test(url0);
    if (loud) GlobalBar.start();
    let res;
    try { res = await inner(input, init); } finally { if (loud) GlobalBar.end(); }
    try {
      const url = typeof input === "string" ? input : (input && input.url) || "";
      const method = ((init && init.method) || "GET").toUpperCase();
      if (res.ok && method !== "GET" && currentLoadedScript && currentLoadedScript.status === "GTG" && /\/api\/scripts\/\d+\/(cards\/|chat-assist|visuals\/|.*\/quiz)/.test(url)) setTimeout(refreshStatusAfterEdit, 500);
    } catch (e) { /* never get in the way of a request */ }
    return res;
  };
})();

async function reloadCards() {
  const res = await fetch(`${API_BASE}/scripts/${currentScriptId}`);
  if (!res.ok) return;
  const script = await res.json();
  currentLoadedCards = script.cards;
  renderCardsStream(script.cards);
}

async function regenerateVisual(visualId, instruction, skipWeb) {
  try {
    showLoader("Making the visual again...");
    const result = await visualApi(`/${encodeURIComponent(visualId)}/regenerate`, "POST", { instruction: instruction || null, skip_web: !!skipWeb });
    await reloadCards();
    if (result.warning) alert("A placeholder was used: " + result.warning);
  } catch (err) {
    alert("Could not regenerate the visual: " + err.message);
  } finally {
    hideLoader();
  }
}

async function approveVisual(visualId, approved) {
  try {
    await visualApi(`/${encodeURIComponent(visualId)}/approve?approved=${approved}`, "POST");
    await reloadCards();
  } catch (err) {
    alert(err.message);
  }
}

async function openVisualEditor(visualId) {
  let spec;
  try {
    spec = (await visualApi("", "GET")).find(v => v.visual_id === visualId);
  } catch (err) { alert(err.message); return; }
  if (!spec) { alert("Visual card not found."); return; }
  document.getElementById("visualEditorModal")?.remove();
  const wrap = document.createElement("div");
  wrap.id = "visualEditorModal";
  wrap.style.cssText = "position:fixed;inset:0;background:rgba(0,0,0,0.55);z-index:9999;display:flex;align-items:center;justify-content:center;";
  wrap.innerHTML = `
    <div style="background:var(--bg-card,#fff);color:var(--text-primary,#111);border-radius:12px;padding:20px 22px;width:min(560px,92vw);max-height:90vh;overflow:auto;">
      <h3 style="margin:0 0 4px;">Edit visual ${escapeHtml(spec.visual_id)}</h3>
      <div style="font-size:0.82rem;color:var(--text-muted);margin-bottom:12px;">After card ${escapeHtml(spec.card_index || "?")}. Status: ${escapeHtml(spec.status)}. Diagrams and animations are made by Claude.</div>
      <label style="font-weight:600;font-size:0.85rem;">Title</label>
      <input id="vedTitle" style="width:100%;margin:4px 0 10px;padding:7px;" value="${escapeHtml(spec.title || "")}">
      <label style="font-weight:600;font-size:0.85rem;">Kind</label>
      <select id="vedKind" style="width:100%;margin:4px 0 10px;padding:7px;">
        ${["diagram","animation"].concat(spec.kind === "image" ? ["image"] : []).map(k => `<option value="${k}" ${spec.kind === k ? "selected" : ""}>${k}</option>`).join("")}
      </select>
      <div style="font-size:0.78rem;color:var(--text-muted);margin:-6px 0 10px;">${spec.card_index && spec.status !== "planned" ? "Changing the kind makes the visual again, so the change shows straight away." : "The visual is made when the script is written. Press Save: your changes are used then."}</div>
      <label style="font-weight:600;font-size:0.85rem;">What it must show (the prompt)</label>
      <textarea id="vedPrompt" rows="4" style="width:100%;margin:4px 0 10px;padding:7px;">${escapeHtml(spec.prompt || "")}</textarea>
      <label style="font-weight:600;font-size:0.85rem;">Elements (up to 7, separated by commas)</label>
      <input id="vedElements" style="width:100%;margin:4px 0 10px;padding:7px;" value="${escapeHtml((spec.elements || []).join(", "))}">
      <label style="font-weight:600;font-size:0.85rem;">Extra instruction for this regeneration only (optional)</label>
      <input id="vedInstruction" style="width:100%;margin:4px 0 14px;padding:7px;" placeholder="for example: show it left to right">
      <div style="display:flex;gap:8px;justify-content:flex-end;">
        <button type="button" class="btn btn-secondary" id="vedCancel">Cancel</button>
        <button type="button" class="btn btn-secondary" id="vedSave">Save</button>
        ${spec.card_index && spec.status !== "planned" ? '<button type="button" class="btn btn-primary" id="vedSaveRegen">Save and regenerate</button>' : ""}
      </div>
    </div>`;
  document.body.appendChild(wrap);
  const collect = () => ({
    title: document.getElementById("vedTitle").value,
    kind: document.getElementById("vedKind").value,
    prompt: document.getElementById("vedPrompt").value,
    elements: document.getElementById("vedElements").value.split(",").map(x => x.trim()).filter(Boolean),
  });
  const close = () => wrap.remove();
  document.getElementById("vedCancel").onclick = close;
  document.getElementById("vedSave").onclick = async () => {
    try {
      const edit = collect();
      await visualApi(`/${encodeURIComponent(visualId)}`, "PUT", edit);
      close();
      if (!(spec.card_index && spec.status !== "planned")) showNotice("Saved. The visual is made when the script is written.");
      // a different kind makes the old picture meaningless: make the new one now, if the card it sits on is written
      if (edit.kind !== spec.kind && spec.card_index && spec.status !== "planned") { await regenerateVisual(visualId, ""); return; }
      if (currentLoadedCards && currentLoadedCards.length) await reloadCards();
      const outlineRes = await fetch(`${API_BASE}/scripts/${currentScriptId}/outline`);
      if (outlineRes.ok && !document.getElementById("outlineGate")?.classList.contains("hidden")) renderOutlineGate(await outlineRes.json());
    } catch (err) { alert(err.message); }
  };
  if (document.getElementById("vedSaveRegen")) document.getElementById("vedSaveRegen").onclick = async () => {
    try {
      await visualApi(`/${encodeURIComponent(visualId)}`, "PUT", collect());
      const instruction = document.getElementById("vedInstruction").value.trim();
      close();
      await regenerateVisual(visualId, instruction);
    } catch (err) { alert(err.message); }
  };
}

window.openVisualEditor = openVisualEditor;
window.regenerateVisual = regenerateVisual;
window.approveVisual = approveVisual;

function escapeHtml(text) {
  if (text === null || text === undefined || text === false) return "";
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

// ==============================================================================
// 9. MODULE OWNER (MO) CURRICULUM CONSOLE & REAL-TIME SYNC (PRD FR-3, FR-5)
// ==============================================================================

async function onOpenMoConsole() {
  const modal = document.getElementById("moCurriculumModal");
  if (!modal) return;
  modal.classList.remove("hidden");

  // Populate filter dropdown with 5 programs if not already populated
  const filter = document.getElementById("moProgramFilter");
  if (filter && filter.options.length <= 1) {
    try {
      const res = await fetch((currentUser && currentUser.role === "mo") ? `${API_BASE}/programs?user_id=${currentUser.id}` : `${API_BASE}/programs`);
      const programs = await res.json();
      filter.innerHTML = `<option value="">All ${programs.length} Programs</option>`;
      programs.forEach(p => {
        filter.innerHTML += `<option value="${p.id}">${p.name}</option>`;
      });
    } catch (e) {
      console.error("Error loading MO filter programs:", e);
    }
  }

  fetchAndRenderMoTree();
}

function onCloseMoConsole() {
  const modal = document.getElementById("moCurriculumModal");
  if (modal) modal.classList.add("hidden");
}

async function fetchAndRenderMoTree() {
  const userParam = (currentUser && currentUser.role === "mo") ? `user_id=${currentUser.id}` : "";
  const moScopeFilter = document.getElementById("moScopeProgramFilter");
  const filterVal = moScopeFilter ? moScopeFilter.value : "";
  let url = `${API_BASE}/curriculum/full-tree`;
  const params = [];
  if (userParam) params.push(userParam);
  if (filterVal) params.push(`program_id=${filterVal}`);
  if (params.length > 0) url += `?${params.join("&")}`;

  try {
    const res = await fetch(url);
    const tree = await res.json();
    moLoadedTree = tree;
    const moContainer = document.getElementById("moDashboardTreeContainer");
    const adminContainer = document.getElementById("adminCurriculumTreeContainer");
    const modalContainer = document.getElementById("moCurriculumTreeContainer");
    if (moContainer) applyMoFilters();                       // the Module Owner tree is shown one track at a time
    if (adminContainer) renderCurriculumTree(tree, adminContainer);
    if (modalContainer) renderCurriculumTree(tree, modalContainer);
  } catch (err) {
    console.error("Failed to render tree:", err);
  }
}

async function refreshCurriculumDataGlobally() {
  try {
    // 1. Reload programs in system and cascading selectors
    if (currentUser) {
      await loadPrograms(currentUser.id);
      if (currentUser.role === "sme") {
        await renderSmeProgramCards(currentUser);
      }
    } else {
      await loadPrograms();
    }

    // 2. Refresh MO Dashboard data (re-fetches tree, stats, scripts)
    const moView = document.getElementById("moDashboardView");
    if (moView && !moView.classList.contains("hidden")) {
      await loadMoDashboardData();
    } else {
      await fetchAndRenderMoTree();
    }

    // 3. Refresh Admin Dashboard data if open
    const adminView = document.getElementById("adminDashboardView");
    if (adminView && !adminView.classList.contains("hidden")) {
      await loadAdminDashboardData();
    }
  } catch (err) {
    console.error("Error in refreshCurriculumDataGlobally:", err);
  }
}
window.refreshCurriculumDataGlobally = refreshCurriculumDataGlobally;

function renderMoTree(tree, container = null) {
  const target = container || document.getElementById("moDashboardTreeContainer") || document.getElementById("adminCurriculumTreeContainer") || document.getElementById("moCurriculumTreeContainer");
  if (target) renderCurriculumTree(tree, target);
}

// "..." menu: one main button stays visible and the rest of a node's actions wait inside the menu
function actionMenu(items) {
  const rows = items.filter(Boolean).map(([label, call, danger]) =>
    `<button type="button" class="kmenu-item${danger ? " danger" : ""}" onclick="closeKMenus(); ${call}">${label}</button>`).join("");
  return rows ? `<div class="kmenu"><button type="button" class="kmenu-btn" title="More actions" aria-label="More actions" onclick="toggleKMenu(event, this)">&#8943;</button><div class="kmenu-list hidden">${rows}</div></div>` : "";
}
let kmenuOpenedAt = 0;
window.closeKMenus = function () { document.querySelectorAll(".kmenu-list:not(.hidden)").forEach(m => m.classList.add("hidden")); };
// Esc closes whichever curriculum popup is open
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  ["requestChangesModal", "moduleStatusModal", "curriculumConfirmModal", "reassignTrackModal", "syllabusUploadModal", "curriculumEntityModal", "moEditClassModal"].some(id => {
    const m = document.getElementById(id);
    if (m && !m.classList.contains("hidden")) { m.classList.add("hidden"); return true; }
    return false;
  });
});

window.toggleKMenu = function (ev, btn) {
  ev.stopPropagation();
  const list = btn.parentElement.querySelector(".kmenu-list");
  const wasOpen = !list.classList.contains("hidden");
  closeKMenus();
  if (wasOpen) return;
  list.classList.remove("hidden");
  kmenuOpenedAt = Date.now();
  const r = btn.getBoundingClientRect();                       // fixed, so no box around the tree can clip it
  list.style.top = Math.min(r.bottom + 4, window.innerHeight - list.offsetHeight - 8) + "px";
  list.style.left = Math.max(8, r.right - list.offsetWidth) + "px";
};
document.addEventListener("click", () => closeKMenus());
window.addEventListener("scroll", () => { if (Date.now() - kmenuOpenedAt > 400) closeKMenus(); }, true);

// Who may change a program's curriculum: Admin everywhere, a Module Owner inside the programs assigned to them. Program Managers and SMEs only read.
function canEditProgram(progId) {
  if (!currentUser) return false;
  if (currentUser.role === "admin") return true;
  return currentUser.role === "mo" && (currentUser.program_ids || []).map(Number).includes(Number(progId));
}

// modules the user has opened stay open when the tree is drawn again (after adding or editing something)
const openModules = new Set();

function renderCurriculumTree(tree, container) {
  if (!container) return;

  if (!tree || tree.length === 0) {
    container.innerHTML = '<div style="color:var(--text-muted); padding:16px;">No programs found.</div>';
    return;
  }

  let html = '<div class="curriculum-tree-container">';

  tree.forEach(prog => {
    const editable = canEditProgram(prog.id);
    const trackCount = (prog.tracks || []).length;
    const moduleCount = (prog.tracks || []).reduce((sum, t) => sum + (t.modules ? t.modules.length : 0), 0);
    const classCount = (prog.tracks || []).reduce((sum, t) => sum + (t.modules ? t.modules.reduce((mSum, m) => mSum + (m.classes ? m.classes.length : 0), 0) : 0), 0);

    let levelSummary = `${trackCount} Tracks`;
    if (prog.id === 1) {
      levelSummary = "3 Tracks (with Specialisations)";
    } else if (prog.id === 2) {
      levelSummary = "2 Official Tracks (Beginner & Intermediate Tracks)";
    } else if (prog.id === 3) {
      levelSummary = "1 Unified Engineering Track (Core & Electives)";
    } else if (prog.id === 4) {
      levelSummary = "1 Unified Enterprise Deployment Track";
    } else if (prog.id === 5) {
      levelSummary = "2 Official Tracks (Beginner & Advanced Tracks)";
    } else if (trackCount === 1) {
      levelSummary = "1 Track";
    } else {
      levelSummary = `${trackCount} Tracks`;
    }

    html += `
      <div class="tree-program-node" data-prog-id="${prog.id}">
        <div class="tree-program-header" onclick="toggleTreeNode('prog_${prog.id}')">
          <div class="tree-program-title-area">
            <span class="tree-toggle-caret" id="caret_prog_${prog.id}">▾</span>
            <span style="font-size: 1.15rem; font-weight: 700; color: var(--text-primary); font-family: var(--font-heading);">${escapeHtml(prog.name)}</span>
            <span class="program-stat-pill">${moduleCount} Modules</span>
            <span class="program-stat-pill">${classCount} Classes</span>
          </div>
          <div style="display: flex; gap: 6px; align-items: center;" onclick="event.stopPropagation()">
            ${(currentUser && currentUser.role === "admin") ? `
              ${actionMenu([["Edit program name", `openEditProgramModal(${prog.id})`], ["Delete program", `openDeleteProgramModal(${prog.id})`, true]])}
            ` : ''}
          </div>
        </div>
        <div id="body_prog_${prog.id}" class="tree-program-body">
    `;

    (prog.tracks || []).forEach(trk => {
      let levelClass = "level-beginner";
      let levelLabel = "Track";
      const tLower = trk.name.toLowerCase();
      if (tLower.includes("inter")) { 
        levelClass = "level-intermediate"; 
        levelLabel = "Intermediate Track"; 
      } else if (tLower.includes("adv")) { 
        levelClass = "level-advanced"; 
        levelLabel = "Advanced Track"; 
      } else if (tLower.includes("backend")) { 
        levelClass = "level-specialisation"; 
        levelLabel = "Backend Specialisation"; 
      } else if (tLower.includes("fullstack")) { 
        levelClass = "level-specialisation"; 
        levelLabel = "Fullstack Specialisation"; 
      } else if (tLower.includes("beg")) { 
        levelClass = "level-beginner"; 
        levelLabel = "Beginner Track"; 
      } else if (tLower.includes("core") || tLower.includes("deployment") || tLower.includes("enterprise") || tLower.includes("electives")) { 
        levelClass = "level-core-track"; 
        levelLabel = "Official Track"; 
      }

      html += `
        <div class="tree-track-node" data-track-id="${trk.id}">
          <div class="tree-track-header" onclick="toggleTreeNode('track_${trk.id}')">
            <div class="tree-track-title-area">
              <span class="tree-toggle-caret" id="caret_track_${trk.id}">▾</span>
              <span class="level-badge ${levelClass}">${levelLabel}</span>
              <span style="font-weight: 600; font-size: 0.95rem; color: var(--text-primary);">${escapeHtml(trk.name)}</span>
              <span style="font-size: 0.8rem; color: var(--text-secondary);">(${trk.module_count} Modules)</span>
            </div>
            <div class="tree-head-actions" onclick="event.stopPropagation()">
              ${editable ? `<button class="btn btn-primary tree-main-btn" onclick="openAddModuleModal(${trk.id})">+ Add Module</button>` : ""}
              ${(currentUser && currentUser.role === "admin") ? actionMenu([["Rename track", `openEditTrackModal(${trk.id})`], ["Delete track", `openDeleteTrackModal(${trk.id})`, true]]) : ''}
            </div>
          </div>
          <div id="body_track_${trk.id}" class="tree-track-body">
      `;

      (trk.modules || []).forEach(mod => {
        html += `
          <div class="tree-module-node" data-mod-id="${mod.id}">
            <div class="tree-module-header" onclick="toggleTreeNode('mod_${mod.id}')">
              <div class="tree-module-title-area">
                <div class="tree-module-line">
                  <span class="tree-toggle-caret" id="caret_mod_${mod.id}">${openModules.has(mod.id) ? "▾" : "▸"}</span>
                  <span class="tree-module-name">${escapeHtml(mod.name)}</span>
                  <span class="tree-module-count">${mod.class_count} ${mod.class_count === 1 ? "class" : "classes"}</span>
                </div>
                <div class="tree-module-chips">
                  ${mod.segment ? `<span class="tchip tchip-blue">${escapeHtml(mod.segment)}</span>` : ''}
                  ${mod.duration_label ? `<span class="tchip tchip-purple">${escapeHtml(mod.duration_label)}</span>` : ''}
                  <span class="tchip tchip-green">${escapeHtml(mod.module_type || 'Core')}</span>
                  <span class="plan-badge plan-${(mod.plan_status || 'Locked').replace(/\s+/g, '-').toLowerCase()}">${escapeHtml(mod.plan_status || (mod.is_locked ? 'Locked' : 'Draft'))}</span>
                  ${editable && (mod.plan_gaps || []).length ? `<span class="plan-badge plan-gaps" title="${escapeHtml(mod.plan_gaps.slice(0, 6).join("\n"))}${mod.plan_gaps.length > 6 ? "\n..." : ""}">${mod.plan_gaps.length} ${mod.plan_gaps.length === 1 ? "gap" : "gaps"} in the plan</span>` : ""}
                  <span class="tchip tchip-grey">Source: ${escapeHtml(mod.plan_source || 'manual')}</span>
                  ${mod.owner_name ? `<span class="tchip tchip-grey">Owner: ${escapeHtml(mod.owner_name)}</span>` : ''}
                </div>
              </div>
              <div class="tree-module-actions" onclick="event.stopPropagation()">
                ${!editable ? "" : (mod.plan_status || 'Locked') === 'Locked'
                  ? `<button class="btn btn-secondary tree-main-btn" title="The plan is final. Reopen it to add or change classes." onclick="openModuleStatusModal(${mod.id}, 'reopen')">Reopen for review</button>`
                  : `<button class="btn btn-secondary tree-main-btn ms-lock-btn" onclick="openModuleStatusModal(${mod.id}, 'lock')">Review &amp; lock</button>
                <button class="btn btn-primary tree-main-btn" onclick="openAddClassModal(${mod.id})">+ Add Class</button>
                ${actionMenu([
                  ["Edit module", `openEditModuleModal(${mod.id})`],
                  ["Upload syllabus", `openSyllabusModal(${mod.id}, '${escapeHtml(mod.name).replace(/'/g, "\\'")}')`],
                  ["Reassign to another track", `reassignModuleTrack(${mod.id}, ${trk.id})`],
                  ["Delete module", `openDeleteModuleModal(${mod.id})`, true]])}`}
              </div>
            </div>
            ${(mod.completeness_issues || []).length ? `
              <div class="plan-issues" onclick="event.stopPropagation()">
                <strong>Not ready for script generation:</strong>
                ${mod.completeness_issues.slice(0, 4).map(i => escapeHtml(i)).join(' &bull; ')}
                ${mod.completeness_issues.length > 4 ? ` &bull; +${mod.completeness_issues.length - 4} more` : ''}
              </div>` : ''}
            <div id="body_mod_${mod.id}" class="tree-module-body${openModules.has(mod.id) ? "" : " hidden"}">
        `;

        (mod.classes || []).forEach(cls => {
          const agendas = Array.isArray(cls.agendas) ? cls.agendas : (cls.agendas ? [cls.agendas] : []);
          const agendasCount = agendas.length;
          html += `
            <div class="tree-class-item" data-class-id="${cls.id}">
              <div class="cls-row">
                <span class="tree-class-seq">${classSeqLabel(cls)}</span>
                <div class="cls-main">
                  <div class="tree-class-title">${escapeHtml(cls.title)}</div>
                  <div class="tree-class-meta">${cls.duration_minutes} min &bull; ${agendasCount} ${agendasCount === 1 ? "agenda" : "agendas"}
                    ${agendasCount > 0 ? `<a href="javascript:void(0)" class="cls-agenda-link" onclick="toggleAgendasList('cls_agendas_${cls.id}')">View agendas</a>` : ''}</div>
                </div>
                <div class="cls-badges">${classTypeBadge(cls.class_type)}</div>
                <div class="cls-actions">
                  ${(!editable || (mod.plan_status || 'Locked') === 'Locked') ? "" : `<button class="btn btn-secondary" onclick="openMoEditClassModal(${cls.id})">Edit</button>
                  <button class="btn btn-warning" onclick="openDeleteClassModal(${cls.id})">Delete</button>`}
                </div>
              </div>
              ${agendasCount > 0 ? `
                <div id="cls_agendas_${cls.id}" class="hidden cls-agendas">
                  <div style="font-size: 0.74rem; font-weight: 600; color: var(--text-secondary); margin-bottom: 4px;">Official Agendas:</div>
                  <ul style="margin: 0; padding-left: 16px; font-size: 0.78rem; color: var(--text-muted); line-height: 1.4;">
                    ${agendas.map(ag => `<li>${escapeHtml(ag)}</li>`).join('')}
                  </ul>
                </div>
              ` : ''}
            </div>
          `;
        });

        html += `
            </div>
          </div>
        `;
      });

      html += `
          </div>
        </div>
      `;
    });

    html += `
        </div>
      </div>
    `;
  });

  html += '</div>';
  container.innerHTML = html;
  allTreeExpanded = false;
  const expandBtn = document.getElementById("treeExpandToggleBtn");
  if (expandBtn) expandBtn.textContent = openModules.size ? "Collapse All" : "Expand All";
  allTreeExpanded = openModules.size > 0;
}

// Tree interactive toggle helpers
window.toggleTreeNode = function (nodeKey) {
  const body = document.getElementById("body_" + nodeKey);
  const caret = document.getElementById("caret_" + nodeKey);
  if (!body) return;
  const isHidden = body.classList.toggle("hidden");
  const modMatch = /^mod_(\d+)$/.exec(nodeKey);
  if (modMatch) { isHidden ? openModules.delete(Number(modMatch[1])) : openModules.add(Number(modMatch[1])); }
  if (caret) {
    caret.textContent = isHidden ? "▸" : "▾";
  }
};

window.toggleAgendasList = function (elId) {
  const el = document.getElementById(elId);
  if (el) {
    el.classList.toggle("hidden");
  }
};

let allTreeExpanded = false;                       // modules start collapsed so a long module list stays readable
window.toggleExpandAllTree = function () {
  allTreeExpanded = !allTreeExpanded;
  const btn = document.getElementById("treeExpandToggleBtn");
  if (btn) btn.textContent = allTreeExpanded ? "Collapse All" : "Expand All";

  document.querySelectorAll(".tree-program-body, .tree-track-body, .tree-module-body").forEach(el => {
    if (allTreeExpanded) {
      el.classList.remove("hidden");
    } else {
      el.classList.add("hidden");
    }
  });
  document.querySelectorAll(".tree-toggle-caret").forEach(caret => {
    caret.textContent = allTreeExpanded ? "▾" : "▸";
  });
  openModules.clear();
  if (allTreeExpanded) document.querySelectorAll(".tree-module-node").forEach(n => openModules.add(Number(n.dataset.modId)));
};

window.filterAdminTree = function (query) {
  const q = (query || "").toLowerCase().trim();
  const progNodes = document.querySelectorAll(".tree-program-node");

  if (!progNodes.length) {                                   // the flat track view: modules and classes only
    document.querySelectorAll("#adminCurriculumTreeContainer .tree-module-node, #moDashboardTreeContainer .tree-module-node, #smeCurriculumBody .tree-module-node").forEach(mn => {
      const classNodes = mn.querySelectorAll(".tree-class-item");
      if (!q) { mn.style.display = ""; classNodes.forEach(n => n.style.display = ""); return; }
      let hit = false;
      classNodes.forEach(cn => { const m = cn.textContent.toLowerCase().includes(q); cn.style.display = m ? "" : "none"; hit = hit || m; });
      const headMatch = mn.querySelector(".tree-module-header")?.textContent.toLowerCase().includes(q);
      if (headMatch) classNodes.forEach(n => n.style.display = "");
      mn.style.display = hit || headMatch ? "" : "none";
      if (hit) { mn.querySelector(".tree-module-body")?.classList.remove("hidden"); const id = Number(mn.dataset.modId); openModules.add(id); const c = document.getElementById("caret_mod_" + id); if (c) c.textContent = "▾"; }
    });
    return;
  }

  if (!q) {
    progNodes.forEach(pn => {
      pn.style.display = "";
      pn.querySelectorAll(".tree-track-node, .tree-module-node, .tree-class-item").forEach(n => n.style.display = "");
    });
    return;
  }

  progNodes.forEach(pn => {
    let progMatch = false;
    const trackNodes = pn.querySelectorAll(".tree-track-node");

    trackNodes.forEach(tn => {
      let trackMatch = false;
      const modNodes = tn.querySelectorAll(".tree-module-node");

      modNodes.forEach(mn => {
        let modMatch = false;
        const classNodes = mn.querySelectorAll(".tree-class-item");

        classNodes.forEach(cn => {
          if (cn.textContent.toLowerCase().includes(q)) {
            cn.style.display = "";
            modMatch = true;
          } else {
            cn.style.display = "none";
          }
        });

        if (modMatch || mn.querySelector(".tree-module-header")?.textContent.toLowerCase().includes(q)) {
          mn.style.display = "";
          trackMatch = true;
          mn.querySelector(".tree-module-body")?.classList.remove("hidden");
        } else {
          mn.style.display = "none";
        }
      });

      if (trackMatch || tn.querySelector(".tree-track-header")?.textContent.toLowerCase().includes(q)) {
        tn.style.display = "";
        progMatch = true;
        tn.querySelector(".tree-track-body")?.classList.remove("hidden");
      } else {
        tn.style.display = "none";
      }
    });

    if (progMatch || pn.querySelector(".tree-program-header")?.textContent.toLowerCase().includes(q)) {
      pn.style.display = "";
      pn.querySelector(".tree-program-body")?.classList.remove("hidden");
    } else {
      pn.style.display = "none";
    }
  });
};

// Window-scoped function so inline onclick works
// "What changed" is only asked for once some content has changed
window.syncClassChangeDesc = function () {
  const pct = parseInt(document.getElementById("moClassChangePctInput").value, 10) || 0;
  document.getElementById("moClassChangeDescGroup")?.classList.toggle("hidden", pct <= 0);
};

// A new class has no earlier content to change and takes its tag from the module, so the Module Tag and Content change fields only show when editing.
function setClassPopupMode(editing) {
  document.getElementById("moClassModuleTypeGroup")?.classList.toggle("hidden", !editing);
  const row = document.getElementById("moClassTopRow");
  if (row) row.style.gridTemplateColumns = editing ? "1fr 1fr 1fr" : "1fr 1fr";
}

window.openMoEditClassModal = async function (classId) {
  try {
    showLoader("Loading Class Details for MO Edit...");
    const res = await fetch(`${API_BASE}/class/${classId}`);
    if (!res.ok) throw new Error("Could not load class details");
    const data = await res.json();

    document.getElementById("moEditClassId").value = data.class_id;
    document.getElementById("moClassTitleInput").value = data.title || "";
    document.getElementById("moClassDurationInput").value = data.duration_minutes || 90;
    // Only Live, Lab and Inverted can be chosen. A class that came in with another type (Unspecified, Recorded...) keeps it until one is picked.
    const typeSelect = document.getElementById("moClassTypeInput");
    typeSelect.querySelectorAll("option[data-legacy]").forEach(o => o.remove());
    if (data.class_type && !["Live", "Lab", "Inverted"].includes(data.class_type)) {
      const legacy = document.createElement("option");
      legacy.value = data.class_type; legacy.dataset.legacy = "1"; legacy.textContent = `${data.class_type} (pick Live, Lab or Inverted)`;
      typeSelect.insertBefore(legacy, typeSelect.firstChild);
    }
    typeSelect.value = data.class_type || "Live";
    document.getElementById("moClassModuleTypeInput").value = data.module_type || "Core";
    document.getElementById("moClassAgendasInput").value = (data.agendas || []).join("\n");
    const lo = (data.learning_outcomes || "").trim();                                    // the generic text written when nobody gave any is shown as empty
    document.getElementById("moClassOutcomesInput").value = lo.startsWith("Master foundational concepts and industry best practices for") ? "" : lo;
    document.getElementById("moClassChangePctInput").value = data.content_change_pct || 0;
    document.getElementById("moClassChangeDescInput").value = data.content_change_description || "";
    syncClassChangeDesc();
    moRefScriptReset(data.reference_script_name || (data.has_reference_script ? "a previous script" : ""));

    document.getElementById("moEditClassModal").dataset.targetModuleId = "";
    setClassPopupMode(true);
    document.getElementById("moEditClassTitle").textContent = "Edit Class Details";
    document.getElementById("saveMoClassBtn").textContent = "Save Changes";
    document.getElementById("deleteMoClassBtn")?.classList.remove("hidden");
    document.getElementById("moEditClassModal").classList.remove("hidden");
  } catch (err) {
    alert("Error opening class editor: " + err.message);
  } finally {
    hideLoader();
  }
};

async function onSaveMoClass() {
  const classId = document.getElementById("moEditClassId").value;
  const modal = document.getElementById("moEditClassModal");
  const targetModuleId = modal.dataset.targetModuleId;

  const title = document.getElementById("moClassTitleInput").value.trim();
  const duration = parseInt(document.getElementById("moClassDurationInput").value, 10) || 90;
  const classType = document.getElementById("moClassTypeInput").value;
  const moduleType = document.getElementById("moClassModuleTypeInput").value;
  const rawAgendas = document.getElementById("moClassAgendasInput").value.trim();
  const agendas = rawAgendas.split("\n").map(s => s.trim()).filter(Boolean);
  const outcomes = document.getElementById("moClassOutcomesInput").value.trim();
  const wasLocked = !!(classId && typeof findClassInTree === "function" && (findClassInTree(Number(classId)) || {}).module && (findClassInTree(Number(classId)).module.plan_status === "Locked"));
  const refChanged = document.getElementById("moClassRefScriptChanged").value === "1";           // only sent when the owner attached or removed a script
  const refPart = refChanged ? { reference_script: document.getElementById("moClassRefScriptText").value, reference_script_name: document.getElementById("moClassRefScriptName").value } : {};

  if (!title) {
    alert("Class title cannot be empty.");
    return;
  }

  try {
    showLoader("Saving Official Curriculum to Database...");
    let res, result;
    const userParam = currentUser ? `?user_id=${currentUser.id}` : "";
    if (classId) {
      // Update existing class
      const payload = {
        title: title,
        duration_minutes: duration,
        class_type: classType,
        module_type: moduleType,
        agendas: agendas,
        learning_outcomes: outcomes,
        ...refPart
      };                                                // the old "content changed" note is no longer asked for, and the saved one is left as it is
      res = await fetch(`${API_BASE}/classes/${classId}${userParam}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || "Failed to save class updates.");
      }
      result = await res.json();
      if (wasLocked) showNotice("This module was locked. Your change sent its plan back to review. Lock it again when you are done.");
    } else {
      // Create new class
      const payload = {
        module_id: parseInt(targetModuleId, 10),
        sequence_number: 99,
        title: title,
        duration_minutes: duration,
        class_type: classType,
        module_type: moduleType,
        agendas: agendas,
        learning_outcomes: outcomes || null,
        ...refPart
      };
      res = await fetch(`${API_BASE}/classes${userParam}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData.detail || "Failed to create new class.");
      }
      result = await res.json();
    }

    document.getElementById("moEditClassModal").classList.add("hidden");

    // Real-time two-way sync
    const currentModuleId = document.getElementById("moduleSelect")?.value;
    const classModuleId = (result.class && result.class.module_id) || targetModuleId;
    if (currentModuleId && parseInt(currentModuleId, 10) === parseInt(classModuleId, 10)) {
      const clsRes = await fetch(`${API_BASE}/classes?module_id=${currentModuleId}`);
      const classes = await clsRes.json();
      const classSel = document.getElementById("classSelect");
      if (classSel) {
        const currentSelectedClassId = classSel.value;
        renderClassOptions(classSel, classes);
        if (currentSelectedClassId == classId) {
          classSel.value = classId;
          onClassChange({ target: { value: classId } });
        }
      }
    }

    if (classModuleId) openModules.add(Number(classModuleId));      // show the class that was just added or edited
    await refreshCurriculumDataGlobally();
    alert(`Class '${title}' saved successfully in official catalog.`);
  } catch (err) {
    alert("Error updating class: " + err.message);
  } finally {
    hideLoader();
  }
}

async function onDeleteMoClass() {
  const classId = document.getElementById("moEditClassId").value;
  if (!classId) return;

  if (!confirm("Are you sure you want to delete this class from the official curriculum?")) return;

  try {
    showLoader("Removing Class from Curriculum...");
    const userParam = currentUser ? `?user_id=${currentUser.id}` : "";
    const res = await fetch(`${API_BASE}/classes/${classId}${userParam}`, {
      method: "DELETE"
    });
    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      throw new Error(errData.detail || "Failed to delete class.");
    }

    document.getElementById("moEditClassModal").classList.add("hidden");
    await refreshCurriculumDataGlobally();
    alert("Class successfully removed from official curriculum.");
  } catch (err) {
    alert("Error deleting class: " + err.message);
  } finally {
    hideLoader();
  }
}

// ==============================================================================
// 9.1 MODAL-BASED DYNAMIC CURRICULUM MANAGEMENT (ZERO POPUPS/PROMPTS)
// ==============================================================================

// The Module Owner console and the Admin curriculum tab each load their own copy of the tree. The edit and delete
// dialogs look things up in whichever one is loaded, preferring the signed-in role's own copy.
function loadedCurriculumTree() {
  const isAdmin = currentUser && currentUser.role === "admin";
  const preferred = isAdmin ? adminFullTreeData : moLoadedTree;
  const other = isAdmin ? moLoadedTree : adminFullTreeData;
  return (preferred && preferred.length ? preferred : other) || [];
}

function findProgramInTree(progId) {
  const idNum = parseInt(progId, 10);
  return loadedCurriculumTree().find(p => p.id === idNum) || null;
}

function findTrackInTree(trackId) {
  const idNum = parseInt(trackId, 10);
  for (const p of loadedCurriculumTree()) {
    for (const t of (p.tracks || [])) {
      if (t.id === idNum) return { program: p, track: t };
    }
  }
  return null;
}

function findModuleInTree(moduleId) {
  const idNum = parseInt(moduleId, 10);
  for (const p of loadedCurriculumTree()) {
    for (const t of (p.tracks || [])) {
      for (const m of (t.modules || [])) {
        if (m.id === idNum) return { program: p, track: t, module: m };
      }
    }
  }
  return null;
}

function findClassInTree(classId) {
  const idNum = parseInt(classId, 10);
  for (const p of loadedCurriculumTree()) {
    for (const t of (p.tracks || [])) {
      for (const m of (t.modules || [])) {
        for (const c of (m.classes || [])) {
          if (c.id === idNum) return { program: p, track: t, module: m, class: c };
        }
      }
    }
  }
  return null;
}

// Entity Modal Openers
window.openAddProgramModal = function () {
  if (currentUser && currentUser.role !== "admin") {
    alert("Only Administrators can create new curriculum programs. Module Owners manage assigned programs.");
    return;
  }
  document.getElementById("curriculumEntityType").value = "program";
  document.getElementById("curriculumEntityAction").value = "add";
  document.getElementById("curriculumEntityId").value = "";
  document.getElementById("curriculumEntityParentId").value = "";

  document.getElementById("curriculumEntityModalTitle").textContent = "Add New Curriculum Program";
  document.getElementById("curriculumEntityNameLabel").textContent = "Program Name";
  const nameInput = document.getElementById("curriculumEntityNameInput");
  nameInput.value = "";
  nameInput.placeholder = "e.g. Scaler Cyber Security";

  const descGroup = document.getElementById("curriculumEntityDescGroup");
  if (descGroup) descGroup.classList.remove("hidden");
  document.getElementById("curriculumEntityDescInput").value = "";

  const typeGroup = document.getElementById("curriculumEntityModuleTypeGroup");
  if (typeGroup) typeGroup.classList.add("hidden");
  document.getElementById("curriculumEntityModuleExtraGroup")?.classList.add("hidden");
  document.getElementById("curriculumEntitySourcesGroup")?.classList.add("hidden");
  document.getElementById("curriculumEntityOwnerGroup")?.classList.add("hidden");
  document.getElementById("curriculumEntityKickoffGroup")?.classList.add("hidden");
  document.getElementById("curriculumEntityTatGroup")?.classList.add("hidden");

  document.getElementById("curriculumEntitySubmitBtn").textContent = "Create Program";
  document.getElementById("curriculumEntityModal").classList.remove("hidden");
};

window.openEditProgramModal = function (progId) {
  if (currentUser && currentUser.role !== "admin") {
    alert("Only Administrators can edit program details. Module Owners manage tracks and modules within assigned programs.");
    return;
  }
  const prog = findProgramInTree(progId);
  document.getElementById("curriculumEntityType").value = "program";
  document.getElementById("curriculumEntityAction").value = "edit";
  document.getElementById("curriculumEntityId").value = progId;
  document.getElementById("curriculumEntityParentId").value = "";

  document.getElementById("curriculumEntityModalTitle").textContent = "Edit Program Details";
  document.getElementById("curriculumEntityNameLabel").textContent = "Program Name";
  const nameInput = document.getElementById("curriculumEntityNameInput");
  nameInput.value = prog ? prog.name : "";

  const descGroup = document.getElementById("curriculumEntityDescGroup");
  if (descGroup) descGroup.classList.remove("hidden");
  document.getElementById("curriculumEntityDescInput").value = (prog && prog.description) || "";
  const typeGroup = document.getElementById("curriculumEntityModuleTypeGroup");
  if (typeGroup) typeGroup.classList.add("hidden");
  document.getElementById("curriculumEntityModuleExtraGroup")?.classList.add("hidden");
  document.getElementById("curriculumEntitySourcesGroup")?.classList.add("hidden");
  document.getElementById("curriculumEntityOwnerGroup")?.classList.add("hidden");
  document.getElementById("curriculumEntityKickoffGroup")?.classList.add("hidden");
  document.getElementById("curriculumEntityTatGroup")?.classList.add("hidden");

  document.getElementById("curriculumEntityTatGroup")?.classList.remove("hidden");           // the program's turnaround target (Administrators only reach this form)
  const tatIn = document.getElementById("curriculumEntityTatInput"); if (tatIn) tatIn.value = (prog && prog.tat_target_days) || "";
  document.getElementById("curriculumEntitySubmitBtn").textContent = "Update Program";
  document.getElementById("curriculumEntityModal").classList.remove("hidden");
};

// Resets the shared delete dialog (also used for removing users) to its plain state.
function resetConfirmDialog() {
  const btn = document.getElementById("curriculumConfirmDeleteBtn");
  if (btn) btn.disabled = false;
  document.getElementById("curriculumConfirmImpact")?.classList.add("hidden");
  document.getElementById("curriculumConfirmTypeGroup")?.classList.add("hidden");
  const input = document.getElementById("curriculumConfirmTypeInput");
  if (input) { input.value = ""; input.oninput = null; }
}

// Opens the delete dialog, asks the server what the deletion would remove, shows it, and, when scripts would be
// lost, makes the user type the exact name before the Delete button works.
async function openDeleteDialog(kind, id, name, title, buttonText) {
  resetConfirmDialog();
  document.getElementById("confirmDeleteType").value = kind;
  document.getElementById("confirmDeleteId").value = id;
  document.getElementById("curriculumConfirmTitle").textContent = title;
  document.getElementById("curriculumConfirmMessage").textContent = "Checking what this would delete...";
  const btn = document.getElementById("curriculumConfirmDeleteBtn");
  btn.textContent = buttonText;
  btn.disabled = true;
  document.getElementById("curriculumConfirmModal").classList.remove("hidden");

  try {
    const res = await fetch(`${API_BASE}/${kind === "class" ? "classes" : kind + "s"}/${id}/impact?user_id=${currentUser ? currentUser.id : ""}`);
    const impact = await res.json();
    if (!res.ok) throw new Error(impact.detail || "Could not check what this would delete.");
    name = impact.name || name;   // the server's name is the one it will check the typed confirmation against

    const parts = [];
    if (impact.tracks) parts.push(`<strong>${impact.tracks}</strong> tracks`);
    if (kind !== "class" && impact.modules) parts.push(`<strong>${impact.modules}</strong> modules`);
    if (kind !== "class") parts.push(`<strong>${impact.classes}</strong> classes`);
    parts.push(`<strong>${impact.scripts}</strong> scripts (<strong>${impact.approved_scripts}</strong> approved with the GTG mark)`);
    const box = document.getElementById("curriculumConfirmImpact");
    box.innerHTML = `Deleting '${escapeHtml(name)}' permanently removes ${parts.join(", ")}.` +
      (impact.users_enrolled ? ` ${impact.users_enrolled} ${impact.users_enrolled === 1 ? "user loses" : "users lose"} this enrolment.` : "") +
      (impact.scripts > 0 ? " Scripts cannot be recovered." : "");
    box.classList.toggle("danger", impact.scripts > 0);
    box.classList.remove("hidden");
    document.getElementById("curriculumConfirmMessage").textContent = `Are you sure you want to delete ${title.replace(/^Delete /, "").toLowerCase()} '${name}'?`;

    if (impact.scripts > 0) {
      document.getElementById("curriculumConfirmTypeLabel").textContent = `Type "${name}" to confirm`;
      const group = document.getElementById("curriculumConfirmTypeGroup");
      const input = document.getElementById("curriculumConfirmTypeInput");
      group.classList.remove("hidden");
      input.value = "";
      input.oninput = () => { btn.disabled = input.value.trim().toLowerCase() !== name.trim().toLowerCase(); };
      input.focus();
    } else {
      btn.disabled = false;
    }
  } catch (err) {
    document.getElementById("curriculumConfirmMessage").textContent = err.message;
  }
}

window.openDeleteProgramModal = function (progId) {
  if (currentUser && currentUser.role !== "admin") {
    alert("Only Administrators can delete curriculum programs. Module Owners manage tracks and modules within assigned programs.");
    return;
  }
  const prog = findProgramInTree(progId);
  openDeleteDialog("program", progId, prog ? prog.name : `Program #${progId}`, "Delete Curriculum Program", "Delete Program");
};

window.openAddTrackModal = function (progId) {
  const prog = findProgramInTree(progId);
  const progName = prog ? prog.name : "";
  document.getElementById("curriculumEntityType").value = "track";
  document.getElementById("curriculumEntityAction").value = "add";
  document.getElementById("curriculumEntityId").value = "";
  document.getElementById("curriculumEntityParentId").value = progId;

  document.getElementById("curriculumEntityModalTitle").textContent = progName ? `Add Track to ${progName}` : "Add Track";
  document.getElementById("curriculumEntityNameLabel").textContent = "Track Name";
  const nameInput = document.getElementById("curriculumEntityNameInput");
  nameInput.value = "";
  nameInput.placeholder = "e.g. System Architecture & High-Scale Systems";

  const descGroup = document.getElementById("curriculumEntityDescGroup");
  if (descGroup) descGroup.classList.add("hidden");

  const typeGroup = document.getElementById("curriculumEntityModuleTypeGroup");
  if (typeGroup) typeGroup.classList.add("hidden");
  document.getElementById("curriculumEntityModuleExtraGroup")?.classList.add("hidden");
  document.getElementById("curriculumEntitySourcesGroup")?.classList.add("hidden");
  document.getElementById("curriculumEntityOwnerGroup")?.classList.add("hidden");
  document.getElementById("curriculumEntityKickoffGroup")?.classList.add("hidden");
  document.getElementById("curriculumEntityTatGroup")?.classList.add("hidden");

  document.getElementById("curriculumEntitySubmitBtn").textContent = "Add Track";
  document.getElementById("curriculumEntityModal").classList.remove("hidden");
};

window.openEditTrackModal = function (trackId) {
  const found = findTrackInTree(trackId);
  const name = found ? found.track.name : "";
  document.getElementById("curriculumEntityType").value = "track";
  document.getElementById("curriculumEntityAction").value = "edit";
  document.getElementById("curriculumEntityId").value = trackId;
  document.getElementById("curriculumEntityParentId").value = found ? found.program.id : "";

  document.getElementById("curriculumEntityModalTitle").textContent = "Rename Track / Level";
  document.getElementById("curriculumEntityNameLabel").textContent = "Track / Level Name";
  const nameInput = document.getElementById("curriculumEntityNameInput");
  nameInput.value = name;

  const descGroup = document.getElementById("curriculumEntityDescGroup");
  if (descGroup) descGroup.classList.add("hidden");

  const typeGroup = document.getElementById("curriculumEntityModuleTypeGroup");
  if (typeGroup) typeGroup.classList.add("hidden");
  document.getElementById("curriculumEntityModuleExtraGroup")?.classList.add("hidden");
  document.getElementById("curriculumEntitySourcesGroup")?.classList.add("hidden");
  document.getElementById("curriculumEntityOwnerGroup")?.classList.add("hidden");
  document.getElementById("curriculumEntityKickoffGroup")?.classList.add("hidden");
  document.getElementById("curriculumEntityTatGroup")?.classList.add("hidden");

  document.getElementById("curriculumEntitySubmitBtn").textContent = "Update Track";
  document.getElementById("curriculumEntityModal").classList.remove("hidden");
};

window.openDeleteTrackModal = function (trackId) {
  const found = findTrackInTree(trackId);
  openDeleteDialog("track", trackId, found ? found.track.name : `Track #${trackId}`, "Delete Track / Level", "Delete Track");
};

// Segment and duration belong to Administrators.
function setModuleExtraFields(mod) {
  const isAdmin = !!(currentUser && currentUser.role === "admin");
  document.getElementById("curriculumEntityModuleExtraGroup")?.classList.remove("hidden");
  const seg = document.getElementById("curriculumEntitySegmentInput");
  const dur = document.getElementById("curriculumEntityDurationInput");
  seg.value = mod.segment_label || mod.segment || "";
  // the tag on the module can come from the built-in label ("1 month") when no length was saved, so show that number instead of an empty box
  const fromLabel = /(\d+(?:\.\d+)?)/.exec(mod.duration_label || "");
  dur.value = mod.duration_months || (fromLabel ? fromLabel[1] : "");
  seg.disabled = !isAdmin;
  dur.disabled = !isAdmin;
  document.getElementById("curriculumEntityExtraNote")?.classList.toggle("hidden", isAdmin);
  document.getElementById("curriculumEntitySourcesGroup")?.classList.remove("hidden");           // Module Owners and Administrators both keep the approved list
  document.getElementById("curriculumEntityOwnerGroup")?.classList.remove("hidden");
  const own = document.getElementById("curriculumEntityOwnerInput"); if (own) own.value = mod.owner_name || "";
  document.getElementById("curriculumEntityKickoffGroup")?.classList.remove("hidden");
  const ko = document.getElementById("curriculumEntityKickoffInput"); if (ko) ko.value = mod.kickoff_date || "";
  const src = document.getElementById("curriculumEntitySourcesInput");
  if (src) src.value = mod.approved_sources || "";
}

window.openAddModuleModal = function (trackId) {
  const found = findTrackInTree(trackId);
  const trkName = found ? found.track.name : "";
  document.getElementById("curriculumEntityType").value = "module";
  document.getElementById("curriculumEntityAction").value = "add";
  document.getElementById("curriculumEntityId").value = "";
  document.getElementById("curriculumEntityParentId").value = trackId;

  document.getElementById("curriculumEntityModalTitle").textContent = trkName ? `Add New Module to ${trkName}` : "Add New Module";
  document.getElementById("curriculumEntityNameLabel").textContent = "Module Name";
  const nameInput = document.getElementById("curriculumEntityNameInput");
  nameInput.value = "";
  nameInput.placeholder = "e.g. Distributed Caching & Redis Mastery";

  const descGroup = document.getElementById("curriculumEntityDescGroup");
  if (descGroup) descGroup.classList.add("hidden");


  const typeGroup = document.getElementById("curriculumEntityModuleTypeGroup");
  if (typeGroup) typeGroup.classList.remove("hidden");
  document.getElementById("curriculumEntityModuleTypeSelect").value = "Core";
  document.getElementById("curriculumEntityModuleTypeSelect").disabled = !(currentUser && currentUser.role === "admin");
  setModuleExtraFields({});

  document.getElementById("curriculumEntitySubmitBtn").textContent = "Add Module";
  document.getElementById("curriculumEntityModal").classList.remove("hidden");
};

window.openEditModuleModal = function (moduleId) {
  const found = findModuleInTree(moduleId);
  const name = found ? found.module.name : "";
  const modType = found ? found.module.module_type : "Core";

  document.getElementById("curriculumEntityType").value = "module";
  document.getElementById("curriculumEntityAction").value = "edit";
  document.getElementById("curriculumEntityId").value = moduleId;
  document.getElementById("curriculumEntityParentId").value = found ? found.track.id : "";

  document.getElementById("curriculumEntityModalTitle").textContent = "Edit Module Details";
  document.getElementById("curriculumEntityNameLabel").textContent = "Module Name";
  const nameInput = document.getElementById("curriculumEntityNameInput");
  nameInput.value = name;

  const descGroup = document.getElementById("curriculumEntityDescGroup");
  if (descGroup) descGroup.classList.add("hidden");


  const typeGroup = document.getElementById("curriculumEntityModuleTypeGroup");
  if (typeGroup) typeGroup.classList.remove("hidden");
  document.getElementById("curriculumEntityModuleTypeSelect").value = modType || "Core";
  document.getElementById("curriculumEntityModuleTypeSelect").disabled = !(currentUser && currentUser.role === "admin");
  setModuleExtraFields(found ? found.module : {});

  document.getElementById("curriculumEntitySubmitBtn").textContent = "Update Module";
  document.getElementById("curriculumEntityModal").classList.remove("hidden");
};

window.openDeleteModuleModal = function (moduleId) {
  const found = findModuleInTree(moduleId);
  openDeleteDialog("module", moduleId, found ? found.module.name : `Module #${moduleId}`, "Delete Module", "Delete Module");
};

window.openDeleteClassModal = function (classId) {
  const found = findClassInTree(classId);
  openDeleteDialog("class", classId, found ? found.class.title : `Class #${classId}`, "Delete Class", "Delete Class");
};

window.closeCurriculumEntityModal = function () {
  const modal = document.getElementById("curriculumEntityModal");
  if (modal) modal.classList.add("hidden");
};

window.closeCurriculumConfirmModal = function () {
  resetConfirmDialog();
  const modal = document.getElementById("curriculumConfirmModal");
  if (modal) modal.classList.add("hidden");
};

window.submitCurriculumEntityForm = async function () {
  const entityType = document.getElementById("curriculumEntityType").value;
  const action = document.getElementById("curriculumEntityAction").value;
  const entityId = document.getElementById("curriculumEntityId").value;
  const parentId = document.getElementById("curriculumEntityParentId").value;
  const name = document.getElementById("curriculumEntityNameInput").value.trim();
  const desc = document.getElementById("curriculumEntityDescInput").value.trim();
  const modType = document.getElementById("curriculumEntityModuleTypeSelect").value;
  const segment = document.getElementById("curriculumEntitySegmentInput").value.trim();
  const durationRaw = document.getElementById("curriculumEntityDurationInput").value;
  const sources = document.getElementById("curriculumEntitySourcesInput")?.value.trim() || "";
  const ownerName = document.getElementById("curriculumEntityOwnerInput")?.value.trim() || "";
  const kickoff = document.getElementById("curriculumEntityKickoffInput")?.value || "";
  const tatTarget = document.getElementById("curriculumEntityTatInput")?.value;
  const isAdmin = !!(currentUser && currentUser.role === "admin");

  if (!name) {
    alert("Name cannot be empty.");
    return;
  }
  if (entityType === "module" && isAdmin) {
    if (!segment) { alert("Segment label is required."); return; }
    if (durationRaw === "") { alert("Duration (months) is required."); return; }
  }
  if (entityType === "module" && durationRaw !== "" && !(parseFloat(durationRaw) > 0 && parseFloat(durationRaw) <= 60)) {
    alert("Duration must be more than 0 and at most 60 months.");
    return;
  }

  const userParam = currentUser ? `?user_id=${currentUser.id}` : "";

  try {
    showLoader(`Saving ${entityType}...`);
    let res;

    if (entityType === "program") {
      if (action === "add") {
        const slug = name.toLowerCase().replace(/[^a-z0-9]/g, "-").replace(/-+/g, "-");
        res = await fetch(`${API_BASE}/programs${userParam}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, slug, description: desc })
        });
      } else {
        res = await fetch(`${API_BASE}/programs/${entityId}${userParam}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name, description: desc, ...(tatTarget !== undefined && tatTarget !== "" ? { tat_target_days: parseInt(tatTarget, 10) || 0 } : {}) })
        });
      }
    } else if (entityType === "track") {
      if (action === "add") {
        const slug = name.toLowerCase().replace(/[^a-z0-9]/g, "-").replace(/-+/g, "-");
        res = await fetch(`${API_BASE}/tracks${userParam}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ program_id: parseInt(parentId, 10), name, slug })
        });
      } else {
        res = await fetch(`${API_BASE}/tracks/${entityId}${userParam}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name })
        });
      }
    } else if (entityType === "module") {
      if (action === "add") {
        const slug = name.toLowerCase().replace(/[^a-z0-9]/g, "-").replace(/-+/g, "-");
        res = await fetch(`${API_BASE}/modules${userParam}`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            track_id: parseInt(parentId, 10), name, slug, module_type: modType, is_locked: false, approved_sources: sources, owner_name: ownerName,
            ...(isAdmin && segment ? { segment } : {}),
            ...(isAdmin && durationRaw !== "" ? { duration_months: parseFloat(durationRaw) } : {})
          })
        });
      } else {
        res = await fetch(`${API_BASE}/modules/${entityId}${userParam}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            name, module_type: modType, approved_sources: sources, owner_name: ownerName, kickoff_date: kickoff,
            ...(isAdmin ? { segment, ...(durationRaw !== "" ? { duration_months: parseFloat(durationRaw) } : {}) } : {})
          })
        });
      }
    }

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `Failed to ${action} ${entityType}.`);
    }

    closeCurriculumEntityModal();
    await refreshCurriculumDataGlobally();
  } catch (err) {
    alert("Error: " + err.message);
  } finally {
    hideLoader();
  }
};

window.executeCurriculumDeletion = async function () {
  const entityType = document.getElementById("confirmDeleteType").value;
  const entityId = document.getElementById("confirmDeleteId").value;
  const typedName = document.getElementById("curriculumConfirmTypeGroup").classList.contains("hidden")
    ? "" : document.getElementById("curriculumConfirmTypeInput").value.trim();
  const userParam = (currentUser ? `?user_id=${currentUser.id}` : "") + (typedName ? `&confirm_name=${encodeURIComponent(typedName)}` : "");

  try {
    showLoader(`Deleting ${entityType}...`);
    let res;
    if (entityType === "program") {
      res = await fetch(`${API_BASE}/programs/${entityId}${userParam}`, { method: "DELETE" });
    } else if (entityType === "track") {
      res = await fetch(`${API_BASE}/tracks/${entityId}${userParam}`, { method: "DELETE" });
    } else if (entityType === "module") {
      res = await fetch(`${API_BASE}/modules/${entityId}${userParam}`, { method: "DELETE" });
    } else if (entityType === "class") {
      res = await fetch(`${API_BASE}/classes/${entityId}${userParam}`, { method: "DELETE" });
    } else if (entityType === "user") {
      res = await fetch(`${API_BASE}/users/${entityId}?actor_id=${currentUser ? currentUser.id : ""}`, { method: "DELETE" });
    }

    if (!res || !res.ok) {
      const err = await res?.json().catch(() => ({}));
      throw new Error(err?.detail || `Failed to delete ${entityType}.`);
    }

    closeCurriculumConfirmModal();

    if (entityType === "user") {
      const uRes = await fetch(`${API_BASE}/users?actor_id=${currentUser ? currentUser.id : ""}`);
      allUsers = await uRes.json();
      const userSelect = document.getElementById("activeUserSelector");
      if (userSelect) {
        userSelect.innerHTML = "";
        allUsers.forEach(u => {
          userSelect.innerHTML += `<option value="${u.id}">${escapeHtml(u.name)} (${getRoleLabel(u.role)})</option>`;
        });
      }
      // If deleted user was current user, sign out
      if (currentUser && String(currentUser.id) === String(entityId)) {
        signOutCurrentUser();
        return;
      }
      if (userSelect && currentUser) userSelect.value = currentUser.id;
      await renderAdminUsersTable();
    } else {
      await refreshCurriculumDataGlobally();
    }
  } catch (err) {
    alert("Error deleting: " + err.message);
  } finally {
    hideLoader();
  }
};

// Backward-compatible window aliases
window.openAddProgramPrompt = window.openAddProgramModal;
window.openEditProgramPrompt = window.openEditProgramModal;
window.deleteProgram = window.openDeleteProgramModal;
window.openAddTrackLevelPrompt = window.openAddTrackModal;
window.openAddTrackPrompt = window.openAddTrackModal;
window.openEditTrackLevelPrompt = window.openEditTrackModal;
window.openEditTrackPrompt = window.openEditTrackModal;
window.deleteTrackLevel = window.openDeleteTrackModal;
window.deleteTrack = window.openDeleteTrackModal;
window.openAddModulePrompt = window.openAddModuleModal;
window.openEditModulePrompt = window.openEditModuleModal;
window.deleteModule = window.openDeleteModuleModal;
window.deleteClassDirectly = window.openDeleteClassModal;

// A previous script of this class, attached by the Module Owner as reference for the AI draft (FR-19)
window.moRefScriptChosen = function (input) {
  const file = input.files && input.files[0];
  if (!file) return;
  if (file.size > 190000) { alert("That file is too large. Attach a text file under about 190 KB."); input.value = ""; return; }
  const reader = new FileReader();
  reader.onload = (ev) => {
    document.getElementById("moClassRefScriptText").value = String(ev.target.result || "");
    document.getElementById("moClassRefScriptName").value = file.name;
    document.getElementById("moClassRefScriptChanged").value = "1";
    document.getElementById("moClassRefScriptState").innerHTML = `Will attach <b>${escapeHtml(file.name)}</b> when you save.`;
  };
  reader.readAsText(file);
};
window.moRefScriptRemove = function () {
  document.getElementById("moClassRefScriptText").value = "";
  document.getElementById("moClassRefScriptName").value = "";
  document.getElementById("moClassRefScriptChanged").value = "1";
  document.getElementById("moClassRefScriptInput").value = "";
  document.getElementById("moClassRefScriptState").textContent = "It will be removed when you save.";
};
function moRefScriptReset(name) {
  ["moClassRefScriptText", "moClassRefScriptName", "moClassRefScriptChanged"].forEach(id => { const el = document.getElementById(id); if (el) el.value = ""; });
  const inp = document.getElementById("moClassRefScriptInput"); if (inp) inp.value = "";
  const st = document.getElementById("moClassRefScriptState");
  if (st) st.innerHTML = name ? `Attached: <b>${escapeHtml(name)}</b> &middot; <a href="javascript:void(0)" onclick="moRefScriptRemove()">Remove</a>` : "None attached.";
}

window.openAddClassModal = function (modId) {
  setClassPopupMode(false);
  document.getElementById("moEditClassId").value = "";
  document.getElementById("moClassTitleInput").value = "";
  document.getElementById("moClassDurationInput").value = 90;
  document.getElementById("moClassTypeInput").value = "Live";
  const parentModule = (typeof findModuleInTree === "function" && findModuleInTree(modId)) || null;
  document.getElementById("moClassModuleTypeInput").value = (parentModule && parentModule.module.module_type) || "Core";   // the class takes its tag from its module
  const tagSelect = document.getElementById("moClassModuleTypeInput");
  if (tagSelect.value !== ((parentModule && parentModule.module.module_type) || "Core")) tagSelect.value = "Core";   // a module tag the class list does not offer
  document.getElementById("moClassAgendasInput").value = "";
  document.getElementById("moClassOutcomesInput").value = "";
  document.getElementById("moClassChangePctInput").value = 0;
  document.getElementById("moClassChangeDescInput").value = "";
  moRefScriptReset("");

  document.getElementById("moEditClassModal").dataset.targetModuleId = modId;
  document.getElementById("moEditClassTitle").textContent = "Add New Class to Module";
  document.getElementById("saveMoClassBtn").textContent = "Add Class";
  const delBtn = document.getElementById("deleteMoClassBtn");
  if (delBtn) delBtn.classList.add("hidden");
  document.getElementById("moEditClassModal").classList.remove("hidden");
};

// ==============================================================================
// 10. MULTI-PROFILE DASHBOARD WORKSPACES (PRD USERS & ROLES)
// ==============================================================================

function onRoleChange(e) {
  if (currentUser && currentUser.role === "pm" && e && e.target) e.target.value = "pm";      // a Program Manager only ever has the read-only workspace
  const announce = !!(e && e.announce);              // only a workspace the person chose to switch to is a "workspace opened"; signing in or reloading is not

  const role = e.target.value;
  const homeView = document.getElementById("homeDashboardView");
  const smeView = document.getElementById("smeDashboardView");
  const moView = document.getElementById("moDashboardView");
  const pmView = document.getElementById("pmDashboardView");
  const adminView = document.getElementById("adminDashboardView");
  const scriptBadge = document.getElementById("activeScriptBadge");
  const moGtgBtn = document.getElementById("moGtgBtn");
  const shareBtn = document.getElementById("shareLinkBtn");

  // Hide all views first including homeView
  if (homeView) {
    homeView.classList.add("hidden");
    homeView.classList.remove("active");
  }
  if (smeView) {
    smeView.classList.add("hidden");
    smeView.classList.remove("active");
  }
  if (moView) {
    moView.classList.add("hidden");
    moView.classList.remove("active");
  }
  if (pmView) {
    pmView.classList.add("hidden");
    pmView.classList.remove("active");
  }
  if (adminView) {
    adminView.classList.add("hidden");
    adminView.classList.remove("active");
  }

  // Scroll to top
  window.scrollTo(0, 0);

  if (role === "sme") {
    if (smeView) {
      smeView.classList.remove("hidden");
      smeView.classList.add("active");
    }
    if (scriptBadge) scriptBadge.classList.remove("hidden");
    showSmeLandingView();
    renderSmeProgramCards(currentUser);
    loadSmeAuthoredScripts();
    if (announce) recordClientActivity("Workspace Opened", "Switched to SME Authoring Workspace");
  } else if (role === "mo") {
    if (moView) {
      moView.classList.remove("hidden");
      moView.classList.add("active");
    }
    if (scriptBadge) scriptBadge.classList.add("hidden");
    if (moGtgBtn) moGtgBtn.classList.add("hidden");
    if (shareBtn) shareBtn.classList.add("hidden");
    document.getElementById("revokeLinkBtn")?.classList.add("hidden");
    document.getElementById("renewLinkBtn")?.classList.add("hidden");
    loadMoDashboardData();
    if (announce) recordClientActivity("Workspace Opened", "Switched to Module Owner (MO) Review Workspace");
  } else if (role === "pm") {
    if (pmView) {
      pmView.classList.remove("hidden");
      pmView.classList.add("active");
    }
    if (scriptBadge) scriptBadge.classList.add("hidden");
    if (moGtgBtn) moGtgBtn.classList.add("hidden");
    if (shareBtn) shareBtn.classList.add("hidden");
    document.getElementById("revokeLinkBtn")?.classList.add("hidden");
    document.getElementById("renewLinkBtn")?.classList.add("hidden");
    loadPmDashboardData();
    if (announce) recordClientActivity("Workspace Opened", "Switched to Program Manager (PM) Read-Only Workspace");
  } else if (role === "admin") {
    if (adminView) {
      adminView.classList.remove("hidden");
      adminView.classList.add("active");
    }
    if (scriptBadge) scriptBadge.classList.add("hidden");
    if (moGtgBtn) moGtgBtn.classList.add("hidden");
    if (shareBtn) shareBtn.classList.add("hidden");
    document.getElementById("revokeLinkBtn")?.classList.add("hidden");
    document.getElementById("renewLinkBtn")?.classList.add("hidden");
    loadAdminDashboardData();
    if (announce) recordClientActivity("Workspace Opened", "Switched to Admin Governance Workspace");
  }
}

// Module Owner (MO) Dashboard Data Loader & Multi-Criteria Filtering Engine
let moLoadedScripts = [];
let moLoadedTree = [];

const moPlain = (n) => String(n || "").replace(/\s*\((?:SME|MO|PM|Admin)\)\s*$/i, "").trim();

async function loadMoDashboardData() {
  try {
    showLoader("Loading Module Owner Workspace...");
    const q = (currentUser && currentUser.role === "mo") ? `?user_id=${currentUser.id}` : "";       // the server only returns this owner's programs
    const sRes = await fetch(`${API_BASE}/dashboard/scripts${q}`);
    moLoadedScripts = sRes.ok ? await sRes.json() : [];
    const tRes = await fetch(`${API_BASE}/curriculum/full-tree${q}`);
    moLoadedTree = tRes.ok ? await tRes.json() : [];

    const progSel = document.getElementById("moScriptProgram");
    if (progSel && progSel.options.length <= 1) {
      progSel.innerHTML = '<option value="">All programs</option>' + moLoadedTree.map(p => `<option value="${p.id}">${escapeHtml(p.name)}</option>`).join("");
    }
    moRenderAll();
    let pane = "review"; try { pane = localStorage.getItem("mo_dashboard_pane") || "review"; } catch (e) { /* default */ }
    showMoPane(pane);
    startMoLive();
  } catch (err) {
    console.error("Error loading MO Dashboard data:", err);
    const l = document.getElementById("moReviewList");
    if (l) l.innerHTML = '<div class="sa-empty" style="color:var(--danger-color);">Could not load your workspace. Check your connection and reload the page.</div>';
  } finally {
    hideLoader();
  }
}

// ---- the four tabs; the last one used is remembered
window.showMoPane = function (name) {
  const panes = ["review", "scripts", "curriculum", "turnaround", "activity"];
  if (!panes.includes(name)) name = "review";
  panes.forEach(p => document.getElementById(`moPane-${p}`)?.classList.toggle("hidden", p !== name));
  document.querySelectorAll("#moTabs .cur-tab").forEach(b => b.classList.toggle("active", b.dataset.pane === name));
  try { localStorage.setItem("mo_dashboard_pane", name); } catch (e) { /* storage may be off */ }
  setUrl(`#/mo/${name}`, true);
  if (name === "turnaround") renderTatPanel("tatMo");
};

// Everything on the page that comes from the scripts and the curriculum, redrawn without losing filters or the open tab
function moRenderAll() {
  const scripts = moLoadedScripts || [];
  const toReview = scripts.filter(s => s.status === "MO Approval");
  const mine = currentUser ? moPlain(currentUser.name).toLowerCase() : "";
  const set = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
  set("moToReviewCount", toReview.length);
  set("moApprovedByMeCount", scripts.filter(s => s.status === "GTG" && moPlain(s.approved_by_name).toLowerCase() === mine).length);
  set("moScriptsCount", scripts.length);
  set("moDraftsCount", scripts.filter(s => s.status !== "GTG" && s.status !== "MO Approval").length);
  set("moProgramsCount", (moLoadedTree || []).length);
  set("moReviewBadge", toReview.length || "");
  renderMoReview();
  renderMoScripts();
  applyMoFilters();
  renderMoActivity();
}

const moStage = (s) => s.status === "GTG" ? { key: "gtg", text: "Approved", dot: "sa-ok" } : s.status === "MO Approval" ? { key: "mo", text: "Pending review", dot: "sa-wait" } : { key: "draft", text: "Draft", dot: "" };
const moSpan = (ms) => { const m = Math.max(0, Math.round(ms / 60000)); return m < 60 ? `${m} min` : m < 48 * 60 ? `${Math.round(m / 60)} h` : `${Math.round(m / 1440)} days`; };

function moAiChip(s) {
  if (s.review_passed === true) return `<div class="mr-ai ok">AI review passed &middot; ${Number(s.review_score).toFixed(1)}/10</div>`;
  if (s.review_passed === false) return `<div class="mr-ai warn">AI review: ${s.review_open || 0} open ${s.review_open === 1 ? "finding" : "findings"} left for the author &middot; ${Number(s.review_score).toFixed(1)}/10</div>`;
  return `<div class="mr-ai off">AI review: not run on this version</div>`;
}

function moRowHtml(s, withWaiting) {
  const st = moStage(s);
  const sent = parseServerTime(s.submitted_at || s.created_at).getTime();
  const canApprove = s.status === "MO Approval" && (s.cards_count || 0) > 0;
  return `
    <div class="mr-row">
      <div class="${withWaiting && canApprove ? "mr-pick" : ""}">
        ${withWaiting && canApprove ? `<input type="checkbox" class="mr-check" aria-label="Select for approval" ${moSelected.has(s.id) ? "checked" : ""} onchange="moToggleOne(${s.id}, this.checked)">` : ""}
        <div>
        <div class="sa-class-title">${escapeHtml(s.class_title)}</div>
        <div class="sa-sub">${escapeHtml(s.program_name)} &middot; ${escapeHtml(s.track_name)} &middot; ${escapeHtml(s.module_name)} &middot; ${s.duration_minutes} min</div>
        ${moAiChip(s)}
        ${s.revision_note && s.status !== "GTG" ? `<div class="mr-note"><b>${s.revision_by === "Approval withdrawn" ? (s.status === "MO Approval" ? "Edited after approval, sent again" : "Edited after approval") : (s.status === "MO Approval" ? "Resubmitted. You asked" : "Changes requested")}</b>: ${escapeHtml(s.revision_note)}</div>` : ""}
        </div>
      </div>
      <div>${escapeHtml(moPlain(s.sme_name) || "Unknown")}${s.sme_role ? ` <span class="sa-sub">${roleShort(s.sme_role)}</span>` : ""}</div>
      ${withWaiting
        ? `<div><b>${isNaN(sent) ? "-" : moSpan(Date.now() - sent)}</b><div class="sa-sub">since submission</div></div>`
        : `<div class="sa-state ${st.dot}">${st.text}</div>`}
      <div>${s.cards_count || 0}${s.cards_planned ? ` <span class="sa-sub">of ${s.cards_planned}</span>` : ""}</div>
      <div class="sa-actions">
        ${canApprove ? `<button class="btn btn-primary" onclick="moApprove(${s.id}, this)">Approve</button><button class="btn btn-secondary mr-reject" onclick="moRequestChanges(${s.id})">Not approved</button>` : ""}
        <button class="btn btn-secondary" onclick="viewScriptReadonly(${s.id})">View</button>
      </div>
    </div>`;
}

// ---- 1. scripts sent to this owner
function renderMoReview() {
  const list = document.getElementById("moReviewList");
  if (!list) return;
  const rows = (moLoadedScripts || []).filter(s => s.status === "MO Approval")
    .sort((a, b) => parseServerTime(a.submitted_at || a.created_at) - parseServerTime(b.submitted_at || b.created_at));
  const approvable = new Set(rows.filter(s => (s.cards_count || 0) > 0).map(s => s.id));
  moSelected.forEach(id => { if (!approvable.has(id)) moSelected.delete(id); });           // a script that left the queue is no longer selected
  list.innerHTML = rows.length ? rows.map(s => moRowHtml(s, true)).join("")
    : '<div class="sa-empty">No scripts are pending your review. Submitted scripts appear here.</div>';
  moUpdateBulkBar(approvable.size);
}

// Several scripts at once: each one still goes through the same approval rules on the server
const moSelected = new Set();
function moUpdateBulkBar(total) {
  const bar = document.getElementById("moBulkBar");
  if (!bar) return;
  bar.classList.toggle("hidden", total === 0);
  const n = moSelected.size, all = document.getElementById("moSelectAll");
  all.checked = total > 0 && n === total; all.indeterminate = n > 0 && n < total;
  document.getElementById("moSelectedCount").textContent = n ? `${n} of ${total} selected` : `${total} can be approved`;
  const btn = document.getElementById("moApproveSelectedBtn");
  btn.disabled = n === 0; btn.textContent = n ? `Approve selected (${n})` : "Approve selected";
}
window.moToggleOne = function (id, on) {
  on ? moSelected.add(id) : moSelected.delete(id);
  moUpdateBulkBar(document.querySelectorAll("#moReviewList .mr-check").length);
};
window.moToggleAll = function (on) {
  document.querySelectorAll("#moReviewList .mr-check").forEach(cb => {
    cb.checked = on;
    const id = parseInt(cb.getAttribute("onchange").match(/\d+/)[0], 10);
    on ? moSelected.add(id) : moSelected.delete(id);
  });
  moUpdateBulkBar(document.querySelectorAll("#moReviewList .mr-check").length);
};
window.moApproveSelected = async function () {
  const ids = [...moSelected];
  if (!ids.length) return;
  const titles = ids.map(id => (moLoadedScripts.find(x => x.id === id) || {}).class_title || `Script ${id}`);
  const shown = titles.slice(0, 8).map(t => `\u2022 ${t}`).join("\n") + (titles.length > 8 ? `\n...and ${titles.length - 8} more` : "");
  if (!confirm(`Approve ${ids.length} ${ids.length === 1 ? "script" : "scripts"}?\n\n${shown}\n\nEach one gets the official GTG status and becomes the reference for its next class.`)) return;
  const btn = document.getElementById("moApproveSelectedBtn");
  btn.disabled = true;
  const failed = [];
  for (let i = 0; i < ids.length; i++) {
    btn.textContent = `Approving ${i + 1} of ${ids.length}...`;
    try {
      const res = await fetch(`${API_BASE}/scripts/${ids[i]}/approve-gtg?user_id=${currentUser.id}`, { method: "POST" });
      if (!res.ok) { const e = await res.json().catch(() => ({})); throw new Error(typeof e.detail === "string" ? e.detail : "Approval failed."); }
      const target = moLoadedScripts.find(x => x.id === ids[i]);
      if (target) { target.status = "GTG"; target.approved_by_mo = true; target.approved_by_name = currentUser.name; target.approved_at = new Date().toISOString().replace("Z", ""); }
      moSelected.delete(ids[i]);
    } catch (e) { failed.push(`${titles[i]}: ${e.message}`); }
  }
  moRenderAll();
  if (failed.length) alert(`${ids.length - failed.length} approved. ${failed.length} could not be approved:\n\n${failed.join("\n")}`);
};

// Approving is an official sign-off, so it asks first
window.moApprove = function (id, btn) {
  const s = (moLoadedScripts || []).find(x => x.id === id);
  if (!confirm(`Approve "${s ? s.class_title : "this script"}"?\n\nThis grants the official GTG status. The approved script becomes the reference for the next class.`)) return;
  approveScriptDirectly(id, btn);
};

// Not approved: the owner writes what must change; the author sees it on the script
window.moRequestChanges = function (id) {
  const s = (moLoadedScripts || []).find(x => x.id === id);
  document.getElementById("requestChangesScriptId").value = id;
  document.getElementById("requestChangesTitle").textContent = s ? s.class_title : "This script";
  document.getElementById("requestChangesReason").value = "";
  document.getElementById("requestChangesError").classList.add("hidden");
  document.getElementById("requestChangesBtn").disabled = false;
  document.getElementById("requestChangesModal").classList.remove("hidden");
  setTimeout(() => document.getElementById("requestChangesReason").focus(), 50);
};
window.closeRequestChanges = function () { document.getElementById("requestChangesModal")?.classList.add("hidden"); };
window.submitRequestChanges = async function () {
  const id = parseInt(document.getElementById("requestChangesScriptId").value, 10);
  const reason = document.getElementById("requestChangesReason").value.trim();
  const err = document.getElementById("requestChangesError"), btn = document.getElementById("requestChangesBtn");
  if (reason.length < 10) { err.textContent = "Write what needs to change, so the author knows what to fix."; err.classList.remove("hidden"); return; }
  btn.disabled = true;
  try {
    const res = await fetch(`${API_BASE}/scripts/${id}/request-changes?user_id=${currentUser.id}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ reason }) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) { err.textContent = typeof data.detail === "string" ? data.detail : "Could not send this script back."; err.classList.remove("hidden"); btn.disabled = false; return; }
    const s = (moLoadedScripts || []).find(x => x.id === id);
    if (s) { s.status = "Script WIP"; s.revision_note = reason; s.revision_by = currentUser.name; s.revision_at = new Date().toISOString(); }
    closeRequestChanges();
    moRenderAll();
  } catch (e) { err.textContent = "Could not reach the server. Try again."; err.classList.remove("hidden"); btn.disabled = false; }
};

// ---- 2. every script in this owner's programs
function renderMoScripts() {
  const list = document.getElementById("moScriptsList");
  if (!list) return;
  const val = (id) => document.getElementById(id)?.value || "";
  const q = val("moScriptSearch").toLowerCase().trim(), prog = val("moScriptProgram"), stage = val("moScriptStage");
  const rows = (moLoadedScripts || []).filter(s =>
    (!q || [s.class_title, s.module_name, s.track_name, s.program_name, s.sme_name].some(t => (t || "").toLowerCase().includes(q))) &&
    (!prog || String(s.program_id) === prog) && (!stage || moStage(s).key === stage));
  list.innerHTML = rows.length ? rows.slice(0, moScriptsVisible).map(s => moRowHtml(s, false)).join("") +
      (rows.length > moScriptsVisible ? `<div class="sa-more"><span>Showing ${moScriptsVisible} of ${rows.length}</span><button class="btn btn-secondary" onclick="moScriptsVisible += 50; renderMoScripts()">Show 50 more</button></div>` : "")
    : '<div class="sa-empty">No scripts match the selected filters.</div>';
}
let moScriptsVisible = 50;
window.renderMoScripts = renderMoScripts;
window.clearMoScriptFilters = function () {
  ["moScriptSearch", "moScriptProgram", "moScriptStage"].forEach(id => { const el = document.getElementById(id); if (el) el.value = ""; });
  renderMoScripts();
};

// ---- 3. the curriculum of this owner's programs: choose a program, then a track, then see its modules and classes
let moActiveProgramId = Number(localStorage.getItem("mo_cur_program")) || null, moActiveTrackId = Number(localStorage.getItem("mo_cur_track")) || null;

function moFilteredTree() {
  const type = document.getElementById("moModuleTypeFilter")?.value || "";
  let tree = moLoadedTree || [];
  if (type) {
    tree = tree.map(p => ({ ...p, tracks: (p.tracks || []).map(t => ({ ...t, modules: (t.modules || []).filter(m => m.module_type === type) })).filter(t => t.modules.length > 0) })).filter(p => p.tracks.length > 0);
  }
  return tree;
}

function applyMoFilters() {
  const q = (document.getElementById("moSearchInput")?.value || "").toLowerCase().trim();
  const tree = moFilteredTree();
  const progNav = document.getElementById("moCurriculumProgramTabs"), trackNav = document.getElementById("moCurriculumTrackTabs");
  const box = document.getElementById("moDashboardTreeContainer");
  if (!box || !progNav || !trackNav) return;
  if (!tree.length) {
    progNav.innerHTML = ""; trackNav.innerHTML = "";
    box.innerHTML = '<div class="sa-empty">No curriculum matches the selected filters.</div>';
    return;
  }
  if (!tree.some(p => p.id === moActiveProgramId)) moActiveProgramId = tree[0].id;
  const prog = tree.find(p => p.id === moActiveProgramId);
  const tracks = prog.tracks || [];
  if (!tracks.some(t => t.id === moActiveTrackId)) moActiveTrackId = tracks.length ? tracks[0].id : null;

  try { localStorage.setItem("mo_cur_program", moActiveProgramId); localStorage.setItem("mo_cur_track", moActiveTrackId || ""); } catch (e) { /* storage may be off */ }
  progNav.innerHTML = tree.map(p => {
    const n = (p.tracks || []).length;
    return `<button type="button" class="cur-tab${p.id === moActiveProgramId ? " active" : ""}" onclick="selectMoProgram(${p.id})"><span>${escapeHtml(p.name)}</span><span class="cur-tab-count">${n} ${n === 1 ? "track" : "tracks"}</span></button>`;
  }).join("");
  trackNav.innerHTML = tracks.length
    ? `<div class="cur-tabs-label">Tracks</div>` + tracks.map(t => {
        const n = (t.modules || []).length;
        return `<button type="button" class="cur-tab${t.id === moActiveTrackId ? " active" : ""}" onclick="selectMoTrack(${t.id})"><span>${escapeHtml(t.name)}</span><span class="cur-tab-count">${n} ${n === 1 ? "module" : "modules"}</span></button>`;
      }).join("")
    : '<span class="sa-sub">This program has no tracks yet.</span>';

  const track = tracks.find(t => t.id === moActiveTrackId);
  if (!track) { box.innerHTML = '<div class="sa-empty">This program has no tracks yet.</div>'; return; }
  renderCurriculumTree([{ ...prog, tracks: [track] }], box);
  flattenAdminTrackView(box, prog, track);                      // the same single-track view the administrator uses
  if (q) filterAdminTree(q);
}
window.selectMoProgram = function (id) { moActiveProgramId = id; moActiveTrackId = null; applyMoFilters(); };
window.selectMoTrack = function (id) { moActiveTrackId = id; applyMoFilters(); };

function resetMoFilters() {
  ["moSearchInput", "moModuleTypeFilter"].forEach(id => { const el = document.getElementById(id); if (el) el.value = ""; });
  applyMoFilters();
}
window.applyMoFilters = applyMoFilters;
window.resetMoFilters = resetMoFilters;

// ---- 4. recent script activity in this owner's programs
// What the Module Owner sees: work on scripts and on the curriculum, only in the programs assigned to them (the server narrows the list)
const MO_FEED_PARAM = "Script Initialized,Cards Generated,Outline Generated,AI Review Run,Submitted to MO Review,Official GTG,Changes Requested,Quiz Approved,Card Edited,Script Deleted,Class,Module,Track,Program,Syllabus";
const MO_FEED_TYPES = /^(Script Initialized|Cards Generated|Outline Generated|AI Review Run|Submitted to MO Review|Official GTG|Changes Requested|Quiz Approved|Card Edited|Script Deleted|Class|Module|Track|Program|Syllabus)/i;
function renderMoActivity() {
  const list = document.getElementById("moActivityList");
  if (!list) return;
  const stamp = (v) => { const d = parseServerTime(v); return isNaN(d.getTime()) ? "" : d.toLocaleDateString("en-US", { month: "short", day: "numeric" }) + ", " + d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" }); };
  const rows = [];
  for (const a of moActivities) {
    let info = {}; try { info = a.details ? JSON.parse(a.details) : {}; } catch (e) { /* none */ }
    const t = /script #(\d+)/i.exec(a.description || "");
    const sid = info.ref && info.ref.kind === "script" ? info.ref.id : (t ? Number(t[1]) : null);
    rows.push({ a, sid });
    if (rows.length >= 80) break;
  }
  list.innerHTML = rows.length ? rows.map(({ a, sid }) => `
    <div class="pa-row${/GTG|approv/i.test(a.action_type || "") ? " pa-ok" : ""}">
      <div class="ut-time">${stamp(a.created_at)}</div>
      <div><b>${escapeHtml(a.user_name || "System")}</b>${a.user_role ? ` <span class="sa-sub">${roleShort(a.user_role)}</span>` : ""}</div>
      <div>${sid ? `<a href="javascript:void(0)" class="ut-open" onclick="viewScriptReadonly(${sid})">${escapeHtml(a.description || a.action_type)}</a>` : escapeHtml(a.description || a.action_type)}</div>
    </div>`).join("") : '<div class="sa-empty">No activity in your assigned programs yet.</div>';
}

// ---- live: the page reads the activity log every 15 seconds and redraws when someone has done something
let moActivities = [], moLiveTimer = null, moLastActivityId = 0, moLiveBusy = false;
function startMoLive() {
  if (moLiveTimer) return;
  moLiveTimer = setInterval(() => moLivePoll(false), PM_LIVE_SECONDS * 1000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) moLivePoll(false); });
  moLivePoll(true);
}
async function moLoadFeed() {
  const r = await fetch(`${API_BASE}/activities?types=${encodeURIComponent(MO_FEED_PARAM)}&for_user=${currentUser.id}&limit=120`);
  if (r.ok) { moActivities = await r.json(); renderMoActivity(); }
}

async function moLivePoll(first) {
  const visible = !document.getElementById("moDashboardView")?.classList.contains("hidden");
  if (!visible || document.hidden || !currentUser || currentUser.role !== "mo" || moLiveBusy) return;
  moLiveBusy = true;
  try {
    const head = await fetch(`${API_BASE}/activities?limit=1`);
    if (!head.ok) return;
    const rows = await head.json();
    const newest = rows.length ? rows[0].id : 0;
    if (first) { moLastActivityId = newest; await moLoadFeed(); return; }
    if (newest === moLastActivityId) return;
    const arrivedRes = await fetch(`${API_BASE}/activities?since_id=${moLastActivityId}&limit=300`);
    const arrived = arrivedRes.ok ? await arrivedRes.json() : [];
    moLastActivityId = newest;
    if (arrived.some(a => PM_CURRICULUM_TYPES.test(a.action_type || ""))) { await loadMoDashboardData(); await moLoadFeed(); return; }
    if (arrived.some(a => /script|GTG|quiz|card|outline|review|changes/i.test(a.action_type || ""))) {
      const r2 = await fetch(`${API_BASE}/dashboard/scripts?user_id=${currentUser.id}`);
      if (r2.ok) { moLoadedScripts = await r2.json(); moRenderAll(); }
    }
    if (arrived.some(a => MO_FEED_TYPES.test(a.action_type || ""))) await moLoadFeed();
  } catch (e) { /* the next check tries again */ } finally { moLiveBusy = false; }
}

// Populate moScopeProgramFilter dropdown (called on MO dashboard load)
async function populateMoScopeFilter() {
  const filter = document.getElementById("moScopeProgramFilter");
  if (!filter) return;
  // Only populate if still empty (beyond the default "All" option)
  if (filter.options.length > 1) return;
  try {
    const url = (currentUser && currentUser.role === "mo")
      ? `${API_BASE}/programs?user_id=${currentUser.id}`
      : `${API_BASE}/programs`;
    const res = await fetch(url);
    const programs = await res.json();
    programs.forEach(p => {
      const opt = document.createElement("option");
      opt.value = p.id;
      opt.textContent = p.name;
      filter.appendChild(opt);
    });
  } catch (e) {
    console.error("Error populating MO scope filter:", e);
  }
}

// Lock a module plan (mark as Locked)
window.lockModulePlan = async function (modId) {
  try {
    showLoader("Locking Module Plan...");
    const userParam = currentUser ? `?user_id=${currentUser.id}` : "";
    const res = await fetch(`${API_BASE}/modules/${modId}${userParam}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_locked: true, plan_status: "Locked" })
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      const d = err.detail;
      throw new Error(d && d.message ? `${d.message}${(d.issues || []).length ? ": " + d.issues.join("; ") : ""}` : (d || "Failed to lock module."));
    }
    await refreshCurriculumDataGlobally();
  } catch (err) {
    alert("Error locking module: " + err.message);
  } finally {
    hideLoader();
  }
};

// Reopen / lock now go through a popup that says what will happen first
window.openModuleStatusModal = function (modId, action) {
  const found = findModuleInTree(modId);
  const mod = found ? found.module : null;
  const reopen = action === "reopen";
  document.getElementById("moduleStatusModId").value = modId;
  document.getElementById("moduleStatusAction").value = action;
  document.getElementById("moduleStatusTitle").textContent = reopen ? "Reopen module for review" : "Review and lock module";
  document.getElementById("moduleStatusModuleName").textContent = mod ? mod.name : `Module #${modId}`;
  document.getElementById("moduleStatusFlow").innerHTML = reopen
    ? '<span class="ms-step done">Locked</span><span class="ms-arrow">&rarr;</span><span class="ms-step now">In Review</span>'
    : '<span class="ms-step">In Review</span><span class="ms-arrow">&rarr;</span><span class="ms-step now">Locked</span>';
  const issues = ((mod && mod.completeness_issues) || []).filter(i => !/must be reviewed and Locked/i.test(i));   // that one is just "not locked yet"
  const checks = document.getElementById("moduleStatusChecks");
  const btn = document.getElementById("moduleStatusConfirmBtn");
  btn.disabled = false;
  if (reopen) {
    document.getElementById("moduleStatusMessage").textContent = "The module shows as In Review, and you can add, edit and delete its classes and change the module itself. SMEs cannot generate scripts from it until it is locked again.";
    checks.innerHTML = "";
    btn.textContent = "Reopen for review";
    btn.className = "btn btn-primary";
  } else {
    document.getElementById("moduleStatusMessage").textContent = issues.length
      ? "Fix these before the plan can be locked:"
      : "Locking makes the class list and agendas final: classes and the module can no longer be changed until it is reopened. SMEs can then generate scripts for its classes.";
    checks.innerHTML = issues.length
      ? `<ul>${issues.map(i => `<li>${escapeHtml(i)}</li>`).join("")}</ul>`
      : `<div class="ms-ok">&#10003; ${mod ? mod.class_count : 0} ${mod && mod.class_count === 1 ? "class" : "classes"}, each with a title and agendas</div>`;
    btn.textContent = "Lock plan";
    btn.className = "btn btn-primary";
    btn.disabled = issues.length > 0;
  }
  document.getElementById("moduleStatusModal").classList.remove("hidden");
};
window.closeModuleStatusModal = function () { document.getElementById("moduleStatusModal")?.classList.add("hidden"); };
window.confirmModuleStatus = async function () {
  const modId = document.getElementById("moduleStatusModId").value;
  const action = document.getElementById("moduleStatusAction").value;
  closeModuleStatusModal();
  await (action === "reopen" ? window.submitModulePlanForReview(modId) : window.lockModulePlan(modId));
};

// Reopen a locked module for review (mark as Draft)
window.submitModulePlanForReview = async function (modId) {
  try {
    showLoader("Reopening Module for Review...");
    const userParam = currentUser ? `?user_id=${currentUser.id}` : "";
    const res = await fetch(`${API_BASE}/modules/${modId}${userParam}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_locked: false, plan_status: "Draft" })
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "Failed to reopen module.");
    }
    await refreshCurriculumDataGlobally();
  } catch (err) {
    alert("Error reopening module: " + err.message);
  } finally {
    hideLoader();
  }
};

// Syllabus Upload Modal — calls the real FR-3 parse endpoint
window.openSyllabusModal = function (modId, modName) {
  document.getElementById("syllabusModuleId").value = modId;
  document.getElementById("syllabusModalTitle").textContent = `Upload Syllabus: ${modName || 'Module'}`;
  document.getElementById("syllabusTextInput").value = "";
  document.getElementById("syllabusModeSelect").value = "merge";
  document.getElementById("syllabusUploadModal").classList.remove("hidden");
};

window.submitSyllabusUpload = async function () {
  const modId = document.getElementById("syllabusModuleId").value;
  const syllabusText = document.getElementById("syllabusTextInput").value.trim();
  const mode = document.getElementById("syllabusModeSelect").value;

  if (!syllabusText) {
    alert("Please paste syllabus text before importing.");
    return;
  }

  try {
    showLoader("AI Parsing Syllabus into Classes...");
    const userParam = currentUser ? `?user_id=${currentUser.id}` : "";
    const res = await fetch(`${API_BASE}/modules/${modId}/syllabus${userParam}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ syllabus_text: syllabusText, mode })
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "Syllabus parsing failed.");
    }

    const data = await res.json();
    document.getElementById("syllabusUploadModal").classList.add("hidden");
    await refreshCurriculumDataGlobally();
    alert(`Syllabus parsed successfully: ${data.parsed_classes} classes imported. Module is now In Review — lock it after verifying the classes.`);
  } catch (err) {
    alert("Error uploading syllabus: " + err.message);
  } finally {
    hideLoader();
  }
};

// Outline Edit Modal (FR-10)
window.openOutlineEditModal = function () {
  if (!currentOutlineCards || currentOutlineCards.length === 0) {
    alert("No outline loaded to edit.");
    return;
  }

  const container = document.getElementById("outlineEditCardsList");
  container.innerHTML = "";

  currentOutlineCards.forEach((c, idx) => {
    container.innerHTML += `
      <div style="background: var(--bg-card); border: 1px solid var(--border-subtle); border-radius: 8px; padding: 14px;">
        <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 10px;">
          <span style="font-weight: 700; color: var(--primary-accent); font-size: 0.85rem; min-width: 60px;">Card ${c.card_index}</span>
          <input type="text" class="form-control outline-edit-title" data-idx="${idx}" value="${escapeHtml(c.title)}" style="flex: 1; font-size: 0.88rem;">
          <input type="number" class="form-control outline-edit-duration" data-idx="${idx}" value="${c.duration_mins}" min="1" max="20" style="width: 80px; font-size: 0.88rem;" title="Duration (mins)">
          <select class="form-control outline-edit-phase" data-idx="${idx}" style="width: 160px; font-size: 0.82rem;">
            ${["Metadata & Lock","Agenda","Concept","Failure Mode","Code Trace","Quiz","Recap"].map(p =>
              `<option value="${p}" ${c.pedagogical_phase === p ? 'selected' : ''}>${p}</option>`
            ).join('')}
          </select>
        </div>
      </div>
    `;
  });

  document.getElementById("outlineEditModal").classList.remove("hidden");
};

window.saveOutlineEdits = async function () {
  const titles = document.querySelectorAll(".outline-edit-title");
  const durations = document.querySelectorAll(".outline-edit-duration");
  const phases = document.querySelectorAll(".outline-edit-phase");

  const modifiedCards = currentOutlineCards.map((c, idx) => ({
    ...c,
    title: titles[idx]?.value || c.title,
    duration_mins: parseFloat(durations[idx]?.value) || c.duration_mins,
    pedagogical_phase: phases[idx]?.value || c.pedagogical_phase
  }));

  try {
    showLoader("Saving Outline Edits...");
    const res = await fetch(`${API_BASE}/scripts/${currentScriptId}/outline/action`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "EDIT", modified_cards: modifiedCards })
    });
    if (!res.ok) throw new Error("Failed to save outline edits.");

    currentOutlineCards = modifiedCards;
    document.getElementById("outlineEditModal").classList.add("hidden");

    // Re-render the outline gate with updated cards
    renderOutlineGate({
      total_duration_mins: modifiedCards.reduce((s, c) => s + c.duration_mins, 0),
      total_cards: modifiedCards.length,
      cards: modifiedCards
    });
  } catch (err) {
    alert("Error saving edits: " + err.message);
  } finally {
    hideLoader();
  }
};

// Direct 1-Click MO GTG Approval Handler (Zero Confirmation Modal, Zero Browser Dialogs)
window.approveScriptDirectly = async function (scriptId, btnElem = null) {
  if (!scriptId) return;

  let originalHtml = "";
  if (btnElem) {
    if (btnElem.disabled) return;
    originalHtml = btnElem.innerHTML;
    btnElem.disabled = true;
    btnElem.textContent = "Approving...";
  }

  try {
    const userParam = currentUser ? `?user_id=${currentUser.id}` : "";
    const url = `${API_BASE}/scripts/${scriptId}/approve-gtg${userParam}`;
    const res = await fetch(url, { method: "POST" });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || "Approval failed.");
    }
    const data = await res.json();

    // 1. Update button and row in MO scripts table
    if (btnElem && btnElem.parentElement) {
      btnElem.parentElement.innerHTML = `
        <span style="font-size:0.8rem; color:var(--success-color); font-weight:600;">Approved</span>
        <button class="btn btn-secondary" style="padding:4px 10px; font-size:0.75rem;" onclick="viewScriptReadonly(${scriptId})">Inspect</button>
      `;
    }

    // Update row status pill in MO table
    const moRow = btnElem ? btnElem.closest("tr") : document.querySelector(`button[onclick*="viewScriptReadonly(${scriptId})"]`)?.closest("tr");
    if (moRow) {
      const pill = moRow.querySelector(".status-pill");
      if (pill) {
        pill.className = "status-pill status-gtg";
        pill.textContent = "GTG";
      }
      const actionCell = moRow.cells ? moRow.cells[moRow.cells.length - 1] : null;
      if (actionCell && !btnElem) {
        actionCell.innerHTML = `
          <div style="display: flex; gap: 6px;">
            <span style="font-size:0.8rem; color:var(--success-color); font-weight:600;">Approved</span>
            <button class="btn btn-secondary" style="padding:4px 10px; font-size:0.75rem;" onclick="viewScriptReadonly(${scriptId})">Inspect</button>
          </div>
        `;
      }
    }

    // 2. Update the cached scripts and redraw this owner's numbers and lists
    if (Array.isArray(moLoadedScripts)) {
      const target = moLoadedScripts.find(s => s.id === parseInt(scriptId, 10));
      if (target) {
        target.status = "GTG"; target.approved_by_mo = true;
        target.approved_by_name = currentUser ? currentUser.name : target.approved_by_name;
        target.approved_at = new Date().toISOString().replace("Z", "");
      }
      if (typeof moRenderAll === "function") moRenderAll();
    }

    // 3. Update Inspect modal header if open
    const inspectTitle = document.getElementById("assignmentsModalTitle");
    if (inspectTitle && inspectTitle.textContent.includes("Script Inspection")) {
      const inspectBtn = document.getElementById(`inspectGtgBtn-${scriptId}`);
      if (inspectBtn && inspectBtn.parentElement) {
        inspectBtn.parentElement.innerHTML = `<span class="status-pill status-gtg" style="padding: 4px 12px; font-size: 0.8rem; font-weight: 600;">Official GTG Approved</span>`;
      }
      const inspectStatusPill = document.getElementById("inspectModalStatusPill");
      if (inspectStatusPill) {
        inspectStatusPill.textContent = "GTG";
      }
    }

    // 4. Update SME Authoring Studio if active script is this script
    if (currentScriptId && String(currentScriptId) === String(scriptId)) {
      updateScriptStatusBadge("Official GTG Approved", "status-gtg");
      const gtgBanner = document.getElementById("smeGtgApprovedBanner");
      if (gtgBanner) gtgBanner.classList.remove("hidden");
      const activePill = document.getElementById("smeActiveScriptStatusPill");
      if (activePill) {
        activePill.textContent = "Official GTG Approved";
        activePill.className = "status-pill status-gtg";
        activePill.classList.remove("hidden");
      }
      const scorePill = document.getElementById("studioScorePill");
      if (scorePill) {
        scorePill.textContent = "Status: Official GTG Approved";
        scorePill.className = "meta-pill score-pill status-gtg";
      }
      const moGtgBtn = document.getElementById("moGtgBtn");
      if (moGtgBtn) moGtgBtn.classList.add("hidden");
    }

    // 5. Broadcast real-time update to SME dashboard and other tabs
    broadcastScriptStatusUpdate(scriptId, "GTG");

    // 6. Refresh SME authored scripts list immediately
    if (typeof loadSmeAuthoredScripts === "function") {
      await loadSmeAuthoredScripts();
    }

  } catch (err) {
    alert("Error approving script: " + err.message);
    if (btnElem && originalHtml) {
      btnElem.disabled = false;
      btnElem.innerHTML = originalHtml;
    }
  }
};

// Fallback handlers ensuring modal is never shown
window.openGtgApprovalModal = function (scriptId) {
  window.approveScriptDirectly(scriptId);
};

window.closeMoGtgApprovalModal = function () {
  const modal = document.getElementById("moGtgApprovalModal");
  if (modal) modal.classList.add("hidden");
};

window.executeGtgApproval = function () {
  const scriptId = document.getElementById("gtgApprovalScriptId")?.value;
  if (scriptId) window.approveScriptDirectly(scriptId);
};

window.approveScriptFromMoDashboard = window.approveScriptDirectly;

// Real-Time Cross-Tab & Cross-View Synchronization
function broadcastScriptStatusUpdate(scriptId, status) {
  try {
    localStorage.setItem("gtg_status_sync", JSON.stringify({
      scriptId: parseInt(scriptId, 10),
      status: status,
      timestamp: Date.now()
    }));
  } catch (e) {}

  window.dispatchEvent(new CustomEvent("script_status_updated", {
    detail: { scriptId: parseInt(scriptId, 10), status: status }
  }));
}

function handleIncomingScriptStatusUpdate(detail) {
  if (!detail || !detail.scriptId) return;
  const scriptId = parseInt(detail.scriptId, 10);
  const status = detail.status || "GTG";

  // 1. If currently inside a program in studio, refresh that program's scripts table
  if (currentSelectedProgramId) {
    refreshCurrentProgramScripts();
  }

  const progRow = document.getElementById(`sme-prog-script-row-${scriptId}`);
  if (progRow) {
    const statusCell = progRow.querySelector(".status-pill");
    if (statusCell) {
      statusCell.className = "status-pill status-gtg";
      statusCell.textContent = "Official GTG Approved";
    }
  }

  // 2. If studio has active script matching this script
  if (currentScriptId && String(currentScriptId) === String(scriptId)) {
    updateScriptStatusBadge("Official GTG Approved", "status-gtg");
    const gtgBanner = document.getElementById("smeGtgApprovedBanner");
    if (gtgBanner) gtgBanner.classList.remove("hidden");
    const activePill = document.getElementById("smeActiveScriptStatusPill");
    if (activePill) {
      activePill.textContent = "Official GTG Approved";
      activePill.className = "status-pill status-gtg";
      activePill.classList.remove("hidden");
    }
    const scorePill = document.getElementById("studioScorePill");
    if (scorePill) {
      scorePill.textContent = "Status: Official GTG Approved";
      scorePill.className = "meta-pill score-pill status-gtg";
    }
    const moGtgBtn = document.getElementById("moGtgBtn");
    if (moGtgBtn) moGtgBtn.classList.add("hidden");
  }

  // 3. Re-render landing program cards
  renderSmeProgramCards(currentUser);
}

window.addEventListener("script_status_updated", e => handleIncomingScriptStatusUpdate(e.detail));
window.addEventListener("storage", e => {
  if (e.key === "gtg_status_sync" && e.newValue) {
    try {
      const payload = JSON.parse(e.newValue);
      handleIncomingScriptStatusUpdate(payload);
    } catch (err) {}
  }
});

// Program Manager (PM) Dashboard Data Loader (Read-Only)
let pmAllClassesData = [];

let pmScripts = [];
const PM_GENERIC_AGENDA = /Conceptual Foundations of|Real-World Production Architecture|Hands-On Implementation & Live Problem|Key Misconceptions, FAQs/i;

async function loadPmDashboardData() {
  try {
    showLoader("Compiling Program Manager Global Audit...");

    const statsRes = await fetch(`${API_BASE}/dashboard/stats`);
    const stats = await statsRes.json();
    document.getElementById("pmTotalPrograms").textContent = stats.total_programs ?? 0;
    document.getElementById("pmTotalModules").textContent = (stats.total_modules ?? 0).toLocaleString();
    document.getElementById("pmTotalClasses").textContent = (stats.total_classes ?? 0).toLocaleString();
    document.getElementById("pmTotalScripts").textContent = stats.total_scripts ?? 0;

    const progFilter = document.getElementById("pmProgramFilterSelect");
    if (progFilter && progFilter.options.length <= 1) {
      const progs = await (await fetch(`${API_BASE}/programs`)).json();
      progFilter.innerHTML = `<option value="">All programs</option>` + progs.map(p => `<option value="${p.id}">${escapeHtml(p.name)}</option>`).join("");
      restorePmFilters();
    }

    const scriptsRes = await fetch(`${API_BASE}/dashboard/scripts`);
    pmScripts = scriptsRes.ok ? await scriptsRes.json() : [];

    const treeRes = await fetch(`${API_BASE}/curriculum/full-tree`);
    const tree = await treeRes.json();

    pmAllClassesData = [];
    tree.forEach(p => {
      (p.tracks || []).forEach(t => {
        (t.modules || []).forEach(m => {
          (m.classes || []).forEach(c => {
            const agendas = Array.isArray(c.agendas) ? c.agendas : (c.agendas ? [c.agendas] : []);
            pmAllClassesData.push({
              program_id: p.id, program_name: p.name, track_id: t.id, track_name: t.name, module_id: m.id, module_name: m.name,
              module_status: m.plan_status || (m.is_locked ? "Locked" : "In Review"),
              class_id: c.id, sequence_number: c.sequence_number, title: c.title, class_type: c.class_type,
              agendas, generic_agendas: agendas.length > 0 && agendas.every(a => PM_GENERIC_AGENDA.test(a)),
              duration_minutes: c.duration_minutes
            });
          });
        });
      });
    });

    pmApplyScripts();
    pmRenderAll();
    let pane = "progress"; try { pane = localStorage.getItem("pm_dashboard_pane") || "progress"; } catch (e) { /* default */ }
    showPmPane(pane);
    startPmLive();

  } catch (err) {
    console.error("Error loading PM dashboard:", err);
    const l = document.getElementById("pmCoverageList");
    if (l) l.innerHTML = '<div class="sa-empty" style="color:var(--danger-color);">Could not load the classes. Check your connection and press Refresh.</div>';
  } finally {
    hideLoader();
  }
}

// Put each class's current script (the approved one if there is one, otherwise the newest) onto its row
function pmApplyScripts() {
  const byClass = {};
  pmScripts.forEach(s => {
    const have = byClass[s.class_id], rank = (x) => (x.status === "GTG" ? 1 : 0);
    if (!have || rank(s) > rank(have) || (rank(s) === rank(have) && s.id > have.id)) byClass[s.class_id] = s;
  });
  pmAllClassesData.forEach(c => {
    const sc = byClass[c.class_id];
    c.script_id = sc ? sc.id : null; c.script_status = sc ? sc.status : "Not Started";
    c.author = sc ? sc.sme_name : null; c.author_role = sc ? sc.sme_role : null;
    c.approver = sc && sc.status === "GTG" ? sc.approved_by_name : null; c.owners = sc ? (sc.owners || []) : [];
    c.approved_at = sc ? sc.approved_at : null; c.last_activity_at = sc ? sc.last_activity_at : null;
  });
}

// Everything on the page that is worked out from the classes and scripts, redrawn without losing the filters, the open tab or "Show more"
function pmRenderAll() {
  const distinct = new Map();                                    // each class once, whatever number of tracks it is listed under
  pmAllClassesData.forEach(c => distinct.set(c.class_id, pmScriptState(c.script_status).key));
  const approvedClasses = [...distinct.values()].filter(k => k === "approved").length;
  document.getElementById("pmTotalClasses").textContent = distinct.size.toLocaleString();
  document.getElementById("pmTotalScripts").textContent = pmScripts.length;
  document.getElementById("pmGtgCount").textContent = approvedClasses.toLocaleString();
  document.getElementById("pmGtgRate").textContent = distinct.size ? `${(100 * approvedClasses / distinct.size).toFixed(1)}%` : "0%";
  renderPmProgress();
  renderPmWeekly();
  renderPmWaiting();
  renderPmCoverageTable(pmFilteredClasses(), true);
  const badge = document.getElementById("pmWaitingCount"); if (badge) badge.textContent = pmScripts.filter(x => x.status !== "GTG").length;
}

// ---- live: other people's work shows up here without pressing Refresh
// The activity log is the signal. Every 15 seconds the page asks for the newest entries; when a new one has arrived it re-reads the scripts
// (and the curriculum too, if the entry was a curriculum change), redraws, and adds the entry to "Recent activity".
const PM_LIVE_SECONDS = 15;
const PM_FEED_TYPES = /^(Script Initialized|Cards Generated|Outline Generated|AI Review Run|Submitted to MO Review|Official GTG|Quiz Approved|Card Edited|Script Deleted)/i;
const PM_CURRICULUM_TYPES = /^(Class |Module |Track |Program |Syllabus|User )/i;
let pmLiveTimer = null, pmLastActivityId = 0, pmActivities = [], pmLiveBusy = false;

function startPmLive() {
  if (pmLiveTimer) return;
  pmLiveTimer = setInterval(() => pmLivePoll(false), PM_LIVE_SECONDS * 1000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) pmLivePoll(false); });
  pmLivePoll(true);
}

function pmSetLive(text, ok) {
  const el = document.getElementById("pmLiveText"), box = document.getElementById("pmLive");
  if (el) el.textContent = text;
  if (box) box.classList.toggle("off", !ok);
}

const PM_FEED_PARAM = "Script Initialized,Cards Generated,Outline Generated,AI Review Run,Submitted to MO Review,Official GTG,Quiz Approved,Card Edited,Script Deleted";

async function pmLoadFeed() {
  const r = await fetch(`${API_BASE}/activities?types=${encodeURIComponent(PM_FEED_PARAM)}&limit=80`);
  if (r.ok) { pmActivities = await r.json(); renderPmActivity(); }
}

async function pmLivePoll(first) {
  const visible = !document.getElementById("pmDashboardView")?.classList.contains("hidden");
  if (!visible || document.hidden || !currentUser || currentUser.role !== "pm" || pmLiveBusy) return;
  pmLiveBusy = true;
  try {
    const head = await fetch(`${API_BASE}/activities?limit=1`);                     // one tiny request: what is the newest entry?
    if (!head.ok) throw new Error("activity");
    const rows = await head.json();
    const newest = rows.length ? rows[0].id : 0;
    if (first) { pmLastActivityId = newest; await pmLoadFeed(); return; }
    if (newest === pmLastActivityId) return;                                         // nothing new since the last check
    const arrivedRes = await fetch(`${API_BASE}/activities?since_id=${pmLastActivityId}&limit=300`);
    const arrived = arrivedRes.ok ? await arrivedRes.json() : [];
    pmLastActivityId = newest;
    if (arrived.some(a => PM_CURRICULUM_TYPES.test(a.action_type || ""))) { await loadPmDashboardData(); return; }      // the class list itself changed
    if (arrived.some(a => /script|GTG|quiz|card|outline|review/i.test(a.action_type || ""))) {
      const r2 = await fetch(`${API_BASE}/dashboard/scripts`);
      if (r2.ok) { pmScripts = await r2.json(); pmApplyScripts(); pmRenderAll(); }
    }
    if (arrived.some(a => PM_FEED_TYPES.test(a.action_type || ""))) await pmLoadFeed();
  } catch (e) {
    /* the next check tries again */
  } finally {
    pmLiveBusy = false;
  }
}

// The feed: what people did to scripts, with a link to open the script read-only
function renderPmActivity() {
  const list = document.getElementById("pmActivityList");
  if (!list) return;
  const rows = pmActivities.filter(a => PM_FEED_TYPES.test(a.action_type || "")).slice(0, 80);
  if (!rows.length) { list.innerHTML = '<div class="sa-empty">No script activity yet.</div>'; return; }
  const stamp = (v) => { const d = parseServerTime(v); return isNaN(d.getTime()) ? "" : d.toLocaleDateString("en-US", { month: "short", day: "numeric" }) + ", " + d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit" }); };
  list.innerHTML = rows.map(a => {
    let info = {}; try { info = a.details ? JSON.parse(a.details) : {}; } catch (e) { /* no details */ }
    const idFromText = /script #(\d+)/i.exec(a.description || "");
    const sid = info.ref && info.ref.kind === "script" ? info.ref.id : (idFromText ? Number(idFromText[1]) : null);
    const text = escapeHtml(a.description || a.action_type);
    const approval = /GTG|approv/i.test(a.action_type || "");
    return `<div class="pa-row${approval ? " pa-ok" : ""}">
      <div class="ut-time">${stamp(a.created_at)}</div>
      <div><b>${escapeHtml(a.user_name || "System")}</b>${a.user_role ? ` <span class="sa-sub">${roleShort(a.user_role)}</span>` : ""}</div>
      <div>${sid ? `<a href="javascript:void(0)" class="ut-open" onclick="viewScriptReadonly(${sid})">${text}</a>` : text}</div>
    </div>`;
  }).join("");
}

// ---- small helpers for the Program Manager page
const pmSpan = (ms) => { const m = Math.max(0, Math.round(ms / 60000)); return m < 60 ? `${m} min` : m < 48 * 60 ? `${Math.round(m / 60)} h` : `${Math.round(m / 1440)} days`; };
const pmDay = (v) => { const f = formatAuditDateTime(v); return f ? f.replace(/, \d{1,2}:\d{2}.*$/, "").replace(/, \d{4}/, "") : ""; };     // "Oct 16"
const pmWhen = (v) => { const f = formatAuditDateTime(v); return f ? f.replace(/, \d{4}/, "") : ""; };                                                    // "Oct 16, 11:50 AM"

// ---- 1. progress by program or by track
window.onPmGroupChange = function () { savePmFilters(); renderPmProgress(); };

function renderPmProgress() {
  const list = document.getElementById("pmProgressList");
  if (!list) return;
  const byTrack = document.getElementById("pmGroupSelect")?.value === "track";
  document.getElementById("pmGroupHead").textContent = byTrack ? "Track" : "Program";
  const groups = new Map();
  pmAllClassesData.forEach(c => {
    const key = byTrack ? `${c.program_id}:${c.track_id}` : String(c.program_id);
    if (!groups.has(key)) groups.set(key, { name: byTrack ? `${c.program_name} / ${c.track_name}` : c.program_name, program_id: c.program_id, classes: new Map() });
    groups.get(key).classes.set(c.class_id, pmScriptState(c.script_status).key);     // a class shared by two tracks counts once in its program
  });
  const approvedTimes = {};                                                           // average days from written to approved, per program
  pmScripts.filter(s => s.status === "GTG" && s.approved_at).forEach(s => {
    const ms = parseServerTime(s.approved_at) - parseServerTime(s.created_at);
    if (!isNaN(ms)) (approvedTimes[s.program_id] = approvedTimes[s.program_id] || []).push(ms);
  });
  const rows = [...groups.values()].map(g => {
    const states = [...g.classes.values()];
    const n = (k) => states.filter(x => x === k).length;
    const approved = n("approved"), mo = n("mo"), started = states.length - n("none");
    const t = approvedTimes[g.program_id] || [];
    return { name: g.name, total: states.length, started, mo, approved, pct: states.length ? (100 * approved / states.length) : 0, avg: byTrack ? null : (t.length ? t.reduce((a, b) => a + b, 0) / t.length : null) };
  });
  list.innerHTML = rows.map(r => `
    <div class="pg-row">
      <div class="sa-class-title">${escapeHtml(r.name)}</div>
      <div>${r.total.toLocaleString()}</div><div>${r.started}</div><div>${r.mo}</div><div>${r.approved}</div>
      <div class="pg-bar" title="${r.approved} of ${r.total} classes approved"><span><i style="width:${Math.max(r.pct > 0 ? 2 : 0, Math.min(100, r.pct))}%"></i></span><b>${r.pct.toFixed(1)}%</b></div>
      <div>${byTrack ? '<span class="sa-sub">per program</span>' : (r.avg === null ? '<span class="sa-sub">none yet</span>' : pmSpan(r.avg))}</div>
    </div>`).join("") || '<div class="sa-empty">No data yet.</div>';
}

// ---- 6. approved per week (last 8 weeks, Monday to Sunday, in the viewer's time zone)
function renderPmWeekly() {
  const box = document.getElementById("pmWeekly");
  if (!box) return;
  const startOfWeek = (d) => { const x = new Date(d.getFullYear(), d.getMonth(), d.getDate()); x.setDate(x.getDate() - ((x.getDay() + 6) % 7)); return x; };
  const first = startOfWeek(new Date());
  const weeks = Array.from({ length: 8 }, (_, i) => { const d = new Date(first); d.setDate(d.getDate() - 7 * (7 - i)); return { start: d, n: 0 }; });
  pmScripts.filter(s => s.status === "GTG" && s.approved_at).forEach(s => {
    const d = parseServerTime(s.approved_at);
    if (isNaN(d.getTime())) return;
    const w = weeks.slice().reverse().find(x => d >= x.start);
    if (w && d < new Date(w.start.getTime() + 7 * 86400000 + 3600000)) w.n += 1;
  });
  const max = Math.max(1, ...weeks.map(w => w.n));
  const total = weeks.reduce((a, w) => a + w.n, 0);
  box.innerHTML = `<div class="pm-bars">${weeks.map(w => `
      <div class="pm-bar" title="Week of ${w.start.toLocaleDateString("en-US", { month: "short", day: "numeric" })}: ${w.n} approved">
        <b>${w.n}</b><span style="height:${w.n ? Math.max(6, 100 * w.n / max) : 2}%"></span><em>${w.start.toLocaleDateString("en-US", { month: "short", day: "numeric" })}</em>
      </div>`).join("")}</div>
    <div class="pm-weekly-note">${total ? `${total} approved in the last 8 weeks` : "Nothing approved in the last 8 weeks"}. Each bar is a week starting Monday.</div>`;
}

// ---- 2. scripts not approved yet, longest waiting first
window.renderPmWaiting = function () {
  const list = document.getElementById("pmWaitingList");
  if (!list) return;
  const mode = document.getElementById("pmWaitingSelect")?.value || "all";
  const ms = (v) => { const t = parseServerTime(v).getTime(); return isNaN(t) ? 0 : t; };
  let rows = pmScripts.filter(s => s.status !== "GTG").map(s => {
    const st = scriptStage(s);
    return { ...s, stageKey: st.key, stageText: st.text, since: ms(s.status === "MO Approval" ? (s.submitted_at || s.created_at) : s.created_at) };   // for the Module Owner, since it was sent
  });
  if (mode === "mo") rows = rows.filter(r => r.stageKey === "mo");
  if (mode === "draft") rows = rows.filter(r => r.stageKey !== "mo");
  rows.sort((a, b) => a.since - b.since);
  if (!rows.length) { list.innerHTML = `<div class="sa-empty">${mode === "mo" ? "No script is with a Module Owner right now." : "Nothing is waiting."}</div>`; return; }
  list.innerHTML = rows.slice(0, pmWaitingVisible).map(r => `
    <div class="pw-row">
      <div><div class="sa-class-title">${escapeHtml(r.class_title)}</div><div class="sa-sub">${escapeHtml(r.program_name)} &middot; ${escapeHtml(r.module_name)}</div></div>
      <div class="sa-state ${r.stageKey === "mo" ? "sa-wait" : ""}">${r.stageText}</div>
      <div>${escapeHtml(r.sme_name || "Unknown")}</div>
      <div>${(r.owners || []).length ? escapeHtml(r.owners.join(", ")) : '<span class="sa-sub">none assigned</span>'}</div>
      <div><b>${r.since ? pmSpan(Date.now() - r.since) : "-"}</b></div>
      <div class="sa-sub">${r.last_activity_at ? pmWhen(r.last_activity_at) : "-"}</div>
      <div class="sa-actions"><button class="btn btn-secondary" onclick="viewScriptReadonly(${r.id})">View script</button></div>
    </div>`).join("") + (rows.length > pmWaitingVisible ? `<div class="sa-more"><span>Showing ${pmWaitingVisible} of ${rows.length}</span><button class="btn btn-secondary" onclick="pmWaitingVisible += 15; renderPmWaiting()">Show 15 more</button></div>` : "");
};
let pmWaitingVisible = 10;

// ---- Turnaround: how long scripts take from module kickoff to approval, per program, and how much the authors work on them (PRD Success metrics)
async function renderTatPanel(boxId, quiet = false) {
  const box = document.getElementById(boxId);
  if (!box || !currentUser) return;
  if (!quiet) box.innerHTML = '<div class="sa-empty">Loading turnaround...</div>';
  try {
    const res = await fetch(`${API_BASE}/metrics/tat?user_id=${currentUser.id}`);
    const d = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof d.detail === "string" ? d.detail : "Could not load the turnaround figures.");
    const days = (v) => v === null || v === undefined ? "-" : `${v} ${v === 1 ? "day" : "days"}`;
    const mins = (v) => v === null || v === undefined ? "-" : v < 90 ? `${v} min` : `${(v / 60).toFixed(1)} h`;
    const o = d.overall || {};
    const isAdmin = currentUser.role === "admin";
    const tile = (n, label) => `<div><span>${n}</span><label>${label}</label></div>`;
    const withTarget = d.programs.filter(p => p.target_days && p.approved);
    const rows = d.programs.map(p => {
      const pct = p.within_target_pct;
      const bar = pct === null || pct === undefined ? '<span class="sa-sub">No target set</span>' : `<div class="tat-bar" title="${pct}% of approved scripts were within the target"><span style="width:${pct}%"></span></div><span class="sa-sub">${pct}% within target</span>`;
      const target = p.target_days ? `${p.target_days} days` : "-";
      const detail = (p.slowest.length || p.modules.length) ? `
        <details class="tat-more"><summary>Slowest scripts and modules</summary>
          ${p.slowest.length ? `<div class="tat-sub-title">Slowest approved scripts</div><ul>${p.slowest.map(x => `<li><b>${escapeHtml(x.class_title)}</b> <span class="sa-sub">${escapeHtml(x.module)}</span> &middot; ${days(x.days)} &middot; ${mins(x.author_minutes)} of author time</li>`).join("")}</ul>` : ""}
          ${p.modules.length ? `<div class="tat-sub-title">Modules</div><ul>${p.modules.map(m => `<li><b>${escapeHtml(m.name)}</b> &middot; average ${days(m.avg_days)} over ${m.approved} ${m.approved === 1 ? "script" : "scripts"} <span class="sa-sub">(counted from ${escapeHtml(m.basis || "n/a")}${m.kickoff ? ", " + new Date(m.kickoff).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }) : ""})</span></li>`).join("")}</ul>` : ""}
        </details>` : "";
      return `<div class="tat-row">
        <div><div class="sa-class-title">${escapeHtml(p.name)}</div>${detail}</div>
        <div>${p.approved}</div><div>${days(p.avg_days)}<div class="sa-sub">median ${days(p.median_days)}</div></div>
        <div>${target}${isAdmin ? ` <a href="javascript:void(0)" class="tat-edit" onclick="tatSetTarget(${p.id}, ${p.target_days || 0})">${p.target_days ? "Change" : "Set"}</a>` : ""}</div>
        <div>${bar}</div><div>${mins(p.avg_author_minutes)}</div>
        <div>${p.open}<div class="sa-sub">${p.waiting_owner} with the owner${p.oldest_open_days !== null && p.oldest_open_days !== undefined ? `, oldest ${p.oldest_open_days} d` : ""}</div></div>
      </div>`;
    }).join("");
    box.innerHTML = `
      <div class="pm-stats tat-tiles">${tile(o.approved ?? 0, "Approved scripts")}${tile(days(o.avg_days), "Average turnaround")}${tile(withTarget.length ? `${Math.round(withTarget.reduce((n, p) => n + p.within_target_pct * p.approved, 0) / withTarget.reduce((n, p) => n + p.approved, 0))}%` : "-", "Within target")}${tile(mins(o.avg_author_minutes), "Average author time")}</div>
      <div class="sa-wrap">
        <div class="sa-head"><div><h2 class="sa-title">Turnaround by program</h2><div class="sa-sub" style="margin-top:4px;">${escapeHtml(d.notes.tat)}</div></div></div>
        <div class="sa-table">
          <div class="tat-row sa-cols"><div>Program</div><div>Approved</div><div>Turnaround</div><div>Target</div><div>Within target</div><div>Author time</div><div>In progress</div></div>
          ${rows || '<div class="sa-empty">No programs yet.</div>'}
        </div>
      </div>
      <p class="tat-note"><b>Author time</b>: ${escapeHtml(d.notes.author_minutes)}</p>`;
  } catch (e) {
    box.innerHTML = `<div class="sa-empty" style="color:var(--danger-color);">${escapeHtml(e.message)}</div>`;
  }
}
window.tatSetTarget = async function (progId, current) {
  const v = prompt("Target for script turnaround, in days from module kickoff to the Module Owner's approval.\nEnter 0 to remove the target.", String(current || ""));
  if (v === null) return;
  const n = parseInt(v, 10);
  if (isNaN(n) || n < 0) { alert("Enter a number of days, such as 14."); return; }
  try {
    const res = await fetch(`${API_BASE}/programs/${progId}?user_id=${currentUser.id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ tat_target_days: n }) });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || "Could not save the target.");
    renderTatPanel("tatAdmin");
  } catch (e) { alert(e.message); }
};

// ---- the three sections are tabs: one shows at a time, and the last one used is remembered
window.showPmPane = function (name) {
  const panes = ["progress", "waiting", "classes", "turnaround", "activity"];
  if (!panes.includes(name)) name = "progress";
  panes.forEach(p => document.getElementById(`pmPane-${p}`)?.classList.toggle("hidden", p !== name));
  document.querySelectorAll("#pmTabs .cur-tab").forEach(b => b.classList.toggle("active", b.dataset.pane === name));
  try { localStorage.setItem("pm_dashboard_pane", name); } catch (e) { /* storage may be off */ }
  setUrl(`#/pm/${name}`, true);
  if (name === "turnaround") renderTatPanel("tatPm");
};

// ---- saved view
const PM_FILTER_KEY = "pm_dashboard_view";
const PM_FILTER_IDS = ["pmProgramFilterSelect", "pmStatusFilterSelect", "pmModuleFilterSelect", "pmGapFilterSelect", "pmTypeFilterSelect", "pmGroupSelect", "pmWaitingSelect"];
function savePmFilters() {
  try { localStorage.setItem(PM_FILTER_KEY, JSON.stringify(Object.fromEntries(PM_FILTER_IDS.map(id => [id, document.getElementById(id)?.value || ""])))); } catch (e) { /* storage may be off */ }
}
function restorePmFilters() {
  try {
    const saved = JSON.parse(localStorage.getItem(PM_FILTER_KEY) || "null");
    if (!saved) return;
    PM_FILTER_IDS.forEach(id => { const el = document.getElementById(id); if (el && saved[id] !== undefined && [...el.options].some(o => o.value === saved[id])) el.value = saved[id]; });
  } catch (e) { /* ignore a bad saved value */ }
}

let pmVisibleRows = 100;

// where a class's script stands, in words a Program Manager uses
function pmScriptState(status) {
  if (status === "GTG") return { key: "approved", text: "Approved", dot: "sa-ok" };
  if (status === "MO Approval") return { key: "mo", text: "Module Owner review", dot: "sa-wait" };
  if (status === "SME Review") return { key: "review", text: "SME review", dot: "sa-wait" };
  if (!status || status === "Not Started") return { key: "none", text: "Not generated", dot: "" };
  return { key: "progress", text: "Draft", dot: "sa-wait" };
}

function renderPmCoverageTable(classesList, keepVisible = false) {
  const list = document.getElementById("pmCoverageList");
  if (!list) return;
  if (!keepVisible) pmVisibleRows = 100;
  window.pmRenderedList = classesList;

  if (!classesList || classesList.length === 0) {
    list.innerHTML = '<div class="sa-empty">No classes match these filters.</div>';
    return;
  }

  let html = classesList.slice(0, pmVisibleRows).map(c => {
    const st = pmScriptState(c.script_status);
    const agendas = c.agendas || [];
    const flags = [c.module_status === "Locked" ? "Module locked" : "Module in review"].concat(c.generic_agendas ? ["Generic agendas"] : [], ["Live", "Lab", "Inverted"].includes(c.class_type) ? [] : ["Class type not set"]);
    const people = c.script_id
      ? `<div>${escapeHtml(c.author || "Unknown")}${c.author_role ? ` <span class="sa-sub">${roleShort(c.author_role)}</span>` : ""}</div>
         <div class="sa-sub">${c.approver ? "Approved by " + escapeHtml(c.approver) : (c.owners.length ? "Module Owners: " + escapeHtml(c.owners.join(", ")) : "No Module Owner assigned")}</div>`
      : '<span class="sa-sub">-</span>';
    const updated = c.script_id ? (c.approved_at ? `<div>${pmDay(c.approved_at)}</div><div class="sa-sub">approved</div>` : (c.last_activity_at ? `<div>${pmDay(c.last_activity_at)}</div><div class="sa-sub">edited</div>` : "-")) : '<span class="sa-sub">-</span>';
    return `
      <div class="pm-row">
        <div>
          <div class="sa-class-title">${classSeqLabel({ sequence_number: c.sequence_number })} ${escapeHtml(c.title)}</div>
          <div class="sa-sub">${escapeHtml(c.program_name)} &middot; ${escapeHtml(c.track_name)} &middot; ${escapeHtml(c.module_name)}</div>
          <div class="pm-flags">${flags.map(f => `<span class="${/generic|not set|in review/i.test(f) ? "warn" : ""}">${f}</span>`).join("")}</div>
        </div>
        <div>${classTypeBadge(c.class_type)}</div>
        <div>${c.duration_minutes} min</div>
        <div>${agendas.length
          ? `<details class="pm-agendas"><summary>${agendas.length} ${agendas.length === 1 ? "agenda" : "agendas"}</summary><ul>${agendas.map(a => `<li>${escapeHtml(a)}</li>`).join("")}</ul></details>`
          : `<span class="sa-sub">None</span>`}</div>
        <div><div class="sa-state ${c.script_id ? "sa-ok" : ""}">${c.script_id ? "Generated" : "Not generated"}</div>${c.script_id ? `<div class="sa-sub">${st.text}</div>` : ""}</div>
        <div>${people}</div>
        <div>${updated}</div>
        <div class="sa-actions">${c.script_id ? `<button class="btn btn-secondary" onclick="viewScriptReadonly(${c.script_id})">View script</button>` : ""}</div>
      </div>`;
  }).join("");

  if (classesList.length > pmVisibleRows) {
    const remaining = classesList.length - pmVisibleRows;
    html += `<div class="sa-more"><span>Showing ${pmVisibleRows} of ${classesList.length} classes</span>
      <button class="btn btn-secondary" onclick="pmShowMoreRows(100)">Show 100 more</button>
      <button class="btn btn-secondary" onclick="pmShowMoreRows(${remaining})">Show all</button></div>`;
  }
  list.innerHTML = html;
}


window.pmShowMoreRows = function (n) {
  pmVisibleRows += n;
  renderPmCoverageTable(window.pmRenderedList || pmAllClassesData, true);
};

function pmFilteredClasses() {
  const val = (id) => document.getElementById(id)?.value || "";
  const query = val("pmClassSearchInput").toLowerCase().trim();
  const prog = val("pmProgramFilterSelect"), status = val("pmStatusFilterSelect"), type = val("pmTypeFilterSelect");
  const mod = val("pmModuleFilterSelect"), gap = val("pmGapFilterSelect");
  return pmAllClassesData.filter(c =>
    (!query || c.title.toLowerCase().includes(query) || c.module_name.toLowerCase().includes(query) || c.track_name.toLowerCase().includes(query) || c.program_name.toLowerCase().includes(query)) &&
    (!prog || String(c.program_id) === prog) &&
    (!status || (status === "generated" ? pmScriptState(c.script_status).key !== "none" : pmScriptState(c.script_status).key === status)) &&
    (!type || c.class_type === type) &&
    (!mod || c.module_status === mod) &&
    (!gap || (gap === "generic" ? c.generic_agendas : !["Live", "Lab", "Inverted"].includes(c.class_type))));
}

function filterPmCoverageTable() {
  savePmFilters();
  renderPmCoverageTable(pmFilteredClasses());
}
window.filterPmCoverageTable = filterPmCoverageTable;

window.clearPmFilters = function () {
  ["pmClassSearchInput", "pmProgramFilterSelect", "pmStatusFilterSelect", "pmModuleFilterSelect", "pmGapFilterSelect", "pmTypeFilterSelect"].forEach(id => { const el = document.getElementById(id); if (el) el.value = ""; });
  filterPmCoverageTable();
};

// Admin Curriculum Architecture Program & Track Sub-Tabs State
let adminFullTreeData = [];
let adminActiveProgramId = null;
let adminActiveTrackId = null;

async function loadAdminCurriculumArchitecture(silent = false) {
  const container = document.getElementById("adminCurriculumTreeContainer");
  const progSubTabsNav = document.getElementById("adminCurriculumProgramSubTabs");
  const trackSubTabsNav = document.getElementById("adminCurriculumTrackSubTabs");
  if (!container) return;

  try {
    if (!silent) showLoader("Loading Curriculum Architecture...");
    const treeRes = await fetch(`${API_BASE}/curriculum/full-tree`);
    adminFullTreeData = await treeRes.json();

    if (!adminFullTreeData || adminFullTreeData.length === 0) {
      container.innerHTML = '<div style="color:var(--text-secondary); padding:20px;">No programs found in curriculum.</div>';
      if (progSubTabsNav) progSubTabsNav.innerHTML = '';
      if (trackSubTabsNav) trackSubTabsNav.innerHTML = '';
      return;
    }

    if (!adminActiveProgramId) { try { const sel = JSON.parse(localStorage.getItem("admin_curriculum_selection") || "null"); if (sel) { adminActiveProgramId = sel.p; adminActiveTrackId = sel.t; } } catch (e) { /* none saved */ } }
    // Default to first program if none currently selected or selected ID no longer exists
    if (!adminActiveProgramId || !adminFullTreeData.some(p => p.id === adminActiveProgramId)) {
      adminActiveProgramId = adminFullTreeData[0].id;
    }

    const currentProg = adminFullTreeData.find(p => p.id === adminActiveProgramId);
    const progTracks = currentProg ? (currentProg.tracks || []) : [];
    if (!adminActiveTrackId || !progTracks.some(t => t.id === adminActiveTrackId)) {
      adminActiveTrackId = progTracks.length > 0 ? progTracks[0].id : null;
    }

    renderAdminProgramSubTabs();
    renderAdminTrackSubTabs();
    renderSelectedAdminTrackTree();
  } catch (err) {
    console.error("Error loading curriculum architecture:", err);
    container.innerHTML = `<div style="color:var(--danger-color); padding:16px;">Error loading curriculum: ${err.message}</div>`;
  } finally {
    if (!silent) hideLoader();
  }
}

window.loadAdminCurriculumArchitecture = loadAdminCurriculumArchitecture;

window.renderAdminProgramSubTabs = function () {
  const subTabsNav = document.getElementById("adminCurriculumProgramSubTabs");
  if (!subTabsNav) return;
  // program-level actions live beside "+ Add Program", and name the program they act on
  const progMenu = document.getElementById("adminProgramMenu");
  const activeProg = adminFullTreeData.find(p => p.id === adminActiveProgramId);
  if (progMenu) {
    progMenu.innerHTML = (activeProg && currentUser && currentUser.role === "admin")
      ? actionMenu([[`Rename ${escapeHtml(activeProg.name)}`, `openEditProgramModal(${activeProg.id})`], [`Delete ${escapeHtml(activeProg.name)}`, `openDeleteProgramModal(${activeProg.id})`, true]]) : "";
  }

  let html = "";
  adminFullTreeData.forEach(p => {
    const isActive = p.id === adminActiveProgramId;
    const trackCount = (p.tracks || []).length;
    html += `
      <button type="button" class="cur-tab${isActive ? " active" : ""}" onclick="selectAdminProgramSubTab(${p.id})">
        <span>${escapeHtml(p.name)}</span>
        <span class="cur-tab-count">${trackCount} ${trackCount === 1 ? "track" : "tracks"}</span>
      </button>`;
  });
  subTabsNav.innerHTML = html;
};

window.renderAdminTrackSubTabs = function () {
  const trackNav = document.getElementById("adminCurriculumTrackSubTabs");
  if (!trackNav) return;

  const prog = adminFullTreeData.find(p => p.id === adminActiveProgramId);
  if (!prog || !prog.tracks || prog.tracks.length === 0) {
    trackNav.innerHTML = `<span style="color:var(--text-secondary); font-size:0.84rem; padding:6px 0;">No tracks configured for this program yet.</span>
      <div class="tree-head-actions" style="margin-left: auto;"><button class="btn btn-primary" style="padding: 5px 12px; font-size: 0.78rem;" onclick="openAddTrackModal(${prog.id})">+ Add Track</button></div>`;
    return;
  }

  // Ensure active track is valid for this program
  if (!adminActiveTrackId || !prog.tracks.some(t => t.id === adminActiveTrackId)) {
    adminActiveTrackId = prog.tracks[0].id;
  }

  let html = `<div class="cur-tabs-label">Tracks</div>`;
  prog.tracks.forEach(trk => {
    const isActive = trk.id === adminActiveTrackId;
    const modCount = (trk.modules || []).length;
    html += `
      <button type="button" class="cur-tab${isActive ? " active" : ""}" onclick="selectAdminTrackSubTab(${trk.id})">
        <span>${escapeHtml(trk.name)}</span>
        <span class="cur-tab-count">${modCount} ${modCount === 1 ? "module" : "modules"}</span>
      </button>`;
  });

  html += `
    <div style="margin-left: auto; display: flex; align-items: center; gap: 6px;">
      <button class="btn btn-primary" style="padding: 5px 12px; font-size: 0.78rem;" onclick="openAddTrackModal(${prog.id})">+ Add Track</button>
    </div>
  `;

  trackNav.innerHTML = html;
};

window.selectAdminProgramSubTab = function (progId) {
  adminActiveProgramId = progId;
  const prog = adminFullTreeData.find(p => p.id === progId);
  if (prog && prog.tracks && prog.tracks.length > 0) {
    adminActiveTrackId = prog.tracks[0].id;
  } else {
    adminActiveTrackId = null;
  }
  renderAdminProgramSubTabs();
  renderAdminTrackSubTabs();
  renderSelectedAdminTrackTree();
  if (prog) {
    recordClientActivity("Program Sub-Tab Selected", `Viewed curriculum program: ${prog.name}`);
  }
  const searchInput = document.getElementById("adminTreeSearchInput");
  if (searchInput && searchInput.value) {
    filterAdminTree(searchInput.value);
  }
};

window.selectAdminTrackSubTab = function (trackId) {
  adminActiveTrackId = trackId;
  renderAdminTrackSubTabs();
  renderSelectedAdminTrackTree();
  const prog = adminFullTreeData.find(p => p.id === adminActiveProgramId);
  const track = prog?.tracks?.find(t => t.id === trackId);
  if (track) {
    recordClientActivity("Track Sub-Tab Selected", `Switched to ${track.name} in ${prog.name}`);
  }
  const searchInput = document.getElementById("adminTreeSearchInput");
  if (searchInput && searchInput.value) {
    filterAdminTree(searchInput.value);
  }
};

window.renderSelectedAdminTrackTree = function () {
  try { localStorage.setItem("admin_curriculum_selection", JSON.stringify({ p: adminActiveProgramId, t: adminActiveTrackId })); } catch (e) { /* storage may be off */ }
  const container = document.getElementById("adminCurriculumTreeContainer");
  if (!container) return;

  const prog = adminFullTreeData.find(p => p.id === adminActiveProgramId);
  if (!prog) {
    container.innerHTML = '<div style="color:var(--text-secondary); padding:16px;">Select a program sub-tab above to view its curriculum tracks.</div>';
    return;
  }

  const selectedTrack = (prog.tracks || []).find(t => t.id === adminActiveTrackId);
  if (!selectedTrack) {
    container.innerHTML = '<div style="color:var(--text-secondary); padding:16px;">This program has no tracks yet. Use "+ Add Track" above to create the first one.</div>';
    return;
  }

  // Build a single-track program representation to display exclusively this track's tree
  const singleTrackProgram = {
    ...prog,
    tracks: [selectedTrack]
  };

  renderCurriculumTree([singleTrackProgram], container);
  flattenAdminTrackView(container, prog, selectedTrack);
};

// The Program and Track tabs above already say where you are, so the selected track shows its modules and classes directly,
// under one slim bar with that track's actions, instead of repeating the program and track boxes.
function flattenAdminTrackView(container, prog, track) {
  const body = container.querySelector(".tree-track-body");
  if (!body) return;
  const modules = track.modules || [];
  const classes = modules.reduce((n, m) => n + (m.classes ? m.classes.length : 0), 0);
  const isAdmin = !!(currentUser && currentUser.role === "admin");
  const bar = document.createElement("div");
  bar.className = "tree-flat-bar";
  bar.innerHTML = `
    <div class="tree-flat-title"><span>${escapeHtml(prog.name)}</span><span class="tree-flat-sep">&rsaquo;</span><strong>${escapeHtml(track.name)}</strong>
      <span class="program-stat-pill">${modules.length} ${modules.length === 1 ? "module" : "modules"}</span>
      <span class="program-stat-pill">${classes} ${classes === 1 ? "class" : "classes"}</span></div>
    <div class="tree-head-actions">
      ${canEditProgram(prog.id) ? `<button class="btn btn-primary tree-main-btn" onclick="openAddModuleModal(${track.id})">+ Add Module</button>` : ""}
      ${isAdmin ? actionMenu([["Rename track", `openEditTrackModal(${track.id})`], ["Delete track", `openDeleteTrackModal(${track.id})`, true]]) : ""}
    </div>`;
  const list = document.createElement("div");
  list.className = "tree-module-list";
  while (body.firstChild) list.appendChild(body.firstChild);
  container.innerHTML = "";
  container.appendChild(bar);
  container.appendChild(list);
  if (!modules.length) list.innerHTML = '<div style="color:var(--text-secondary); padding:16px;">This track has no modules yet. Use "+ Add Module" to create the first one.</div>';
}

window.renderSelectedAdminProgramTree = window.renderSelectedAdminTrackTree;

// Admin Dashboard Data Loader
async function loadAdminDashboardData() {
  try {
    showLoader("Loading Admin Architecture & Directory...");

    // 1. Load Curriculum Architecture with Program Sub-Tabs
    await loadAdminCurriculumArchitecture();

    // 2. Load User Governance Directory
    await renderAdminUsersTable();

  } catch (err) {
    console.error("Error loading Admin dashboard:", err);
  } finally {
    hideLoader();
  }
}

// Render Admin Users Table
async function renderAdminUsersTable() {
  const box = document.getElementById("adminUsersGroups");
  if (!box) return;
  try {
    const res = await fetch(`${API_BASE}/users?actor_id=${currentUser ? currentUser.id : ""}`);
    allUsers = await res.json();

    if (!allUsers || allUsers.length === 0) {
      box.innerHTML = '<div style="text-align:center; color:var(--text-secondary); padding:16px;">No users found in directory.</div>';
      return;
    }

    const byRole = {};
    allUsers.forEach(u => (byRole[u.role] = byRole[u.role] || []).push(u));
    const muted = (t) => `<span class="au-muted">${t}</span>`;
    // [[program html, access html]] for one user: one line per program, the program on the left and what they can reach inside it on the right
    const programRows = (u) => {
      if (u.role === "admin" || u.role === "pm") {
        return [[`<span class="program-tag-pill">All programs</span>`, muted(u.role === "admin" ? "Full access to everything" : "Read-only (LO team)")]];
      }
      const byProgram = {};
      (u.program_names || []).forEach(p => { byProgram[p] = byProgram[p] || []; });
      (u.track_names || []).forEach(t => {
        const cut = t.indexOf(" / ");
        const prog = cut > -1 ? t.slice(0, cut) : "Other";
        (byProgram[prog] = byProgram[prog] || []).push(cut > -1 ? t.slice(cut + 3) : t);
      });
      return Object.keys(byProgram).map(prog => [`<span class="program-tag-pill">${escapeHtml(prog)}</span>`,
        byProgram[prog].length ? byProgram[prog].map(t => `<span class="track-tag-pill">${escapeHtml(t)}</span>`).join("")
          : muted(u.role === "mo" ? "Own modules and classes" : "No tracks assigned")]);
    };
    const userRow = (u, n) => {
      const rows = programRows(u);
      const programs = rows.length
        ? rows.map(([a, b]) => `<div class="au-prog"><div>${a}</div><div class="au-access">${b}</div></div>`).join("")
        : `<span class="au-none">No programs assigned</span>`;
      return `<div class="au-row">
        <div class="au-num">${n === null ? "" : n}</div>
        <div class="au-user"><div class="au-name">${escapeHtml(u.name)}</div><div class="au-email">${escapeHtml(u.email)}</div></div>
        <div class="au-programs">${programs}</div>
        <div class="au-actions">
          <button class="btn btn-secondary" onclick="openEditUserModal(${u.id})">Edit Scope</button>
          <button class="btn btn-warning" onclick="deleteUserDirectly(${u.id}, '${escapeHtml(u.name)}')">Delete</button>
        </div>
      </div>`;
    };
    // one box per role: Admin, Program Manager, Module Owner, SME / Author, then any other role. No number for admins; the others count 1, 2, 3 from the top of their box.
    const order = ["admin", "pm", "mo", "sme"];
    const roles = order.filter(r => byRole[r]).concat(Object.keys(byRole).filter(r => !order.includes(r)));
    box.innerHTML = roles.map(r => `
      <div class="au-group">
        <div class="au-group-head">
          <span class="role-tag-badge role-tag-${escapeHtml(r)}">${escapeHtml(getRoleLabel(r))}</span>
          <span class="au-count">${byRole[r].length} user${byRole[r].length === 1 ? "" : "s"}</span>
        </div>
        <div class="au-row au-cols">
          <div class="au-num">${r === "admin" ? "" : "#"}</div>
          <div>User</div>
          <div class="au-programs"><div class="au-prog"><div>Joined program</div><div>Access scope</div></div></div>
          <div class="au-actions"></div>
        </div>
        ${byRole[r].map((u, i) => userRow(u, r === "admin" ? null : i + 1)).join("")}
      </div>`).join("");
  } catch (err) {
    box.innerHTML = `<div style="text-align:center; color:var(--danger-color); padding:16px;">Error loading users: ${err.message}</div>`;
  }
}

// User Management Modal Handlers (PRD Users & Roles)
window.openAddUserModal = async function () {
  document.getElementById("userManageModalTitle").textContent = "Add New User";
  document.getElementById("editUserId").value = "";
  document.getElementById("userNameInput").value = "";
  document.getElementById("userEmailInput").value = "";
  document.getElementById("userRoleInput").value = "sme";

  await renderProgramCheckboxes([], []);
  document.getElementById("userManageModal").classList.remove("hidden");
};

window.openEditUserModal = async function (userId) {
  const u = allUsers.find(item => item.id === userId);
  if (!u) return;

  document.getElementById("userManageModalTitle").textContent = `Edit User Scope: ${u.name}`;
  document.getElementById("editUserId").value = u.id;
  document.getElementById("userNameInput").value = u.name;
  document.getElementById("userEmailInput").value = u.email;
  document.getElementById("userRoleInput").value = u.role;

  await renderProgramCheckboxes(u.program_ids || [], u.track_ids || []);
  document.getElementById("userManageModal").classList.remove("hidden");
};

const programTracksCache = {};

async function getProgramTracks(progId) {
  if (!programTracksCache[progId]) {
    const res = await fetch(`${API_BASE}/tracks?program_id=${progId}`);
    programTracksCache[progId] = res.ok ? await res.json() : [];
  }
  return programTracksCache[progId];
}

// Programs with their tracks. SMEs and PMs join a program and, where it has tracks, a track in it (PRD FR-1);
// Module Owners and Admins join programs only.
async function renderProgramCheckboxes(selectedIds = [], selectedTrackIds = []) {
  const container = document.getElementById("userProgramCheckboxes");
  if (!container) return;

  if (!allSystemPrograms || allSystemPrograms.length === 0) {
    const res = await fetch(`${API_BASE}/programs`);
    allSystemPrograms = await res.json();
  }

  let html = "";
  for (const p of allSystemPrograms) {
    const checked = selectedIds.includes(p.id) ? "checked" : "";
    const tracks = await getProgramTracks(p.id);
    let trackHtml = "";
    if (tracks.length === 1) {
      trackHtml = `<span>Track: <strong>${escapeHtml(tracks[0].name)}</strong> (joined automatically)</span>
        <input type="checkbox" class="user-track-checkbox" value="${tracks[0].id}" data-prog="${p.id}" checked hidden>`;
    } else if (tracks.length > 1) {
      trackHtml = `<span>Tracks:</span>` + tracks.map(t => `
        <label><input type="checkbox" class="user-track-checkbox" value="${t.id}" data-prog="${p.id}" ${selectedTrackIds.includes(t.id) ? "checked" : ""}> ${escapeHtml(t.name)}</label>`).join("");
    }
    html += `
      <div class="user-prog-block" data-prog="${p.id}">
        <label style="display:flex; align-items:center; gap:8px; font-size:0.85rem; color:var(--text-primary); cursor:pointer;">
          <input type="checkbox" class="user-prog-checkbox" value="${p.id}" ${checked}>
          <span>${escapeHtml(p.name)}</span>
        </label>
        ${trackHtml ? `<div class="user-track-group" data-prog="${p.id}">${trackHtml}</div>` : ""}
      </div>
    `;
  }
  container.innerHTML = html;
  container.onchange = refreshUserTrackGroups;
  const roleSel = document.getElementById("userRoleInput");
  if (roleSel) roleSel.onchange = refreshUserTrackGroups;
  refreshUserTrackGroups();
}

function refreshUserTrackGroups() {
  const role = document.getElementById("userRoleInput")?.value;
  const needsTracks = role === "sme" || role === "pm";
  document.querySelectorAll("#userProgramCheckboxes .user-prog-block").forEach(block => {
    const joined = block.querySelector(".user-prog-checkbox")?.checked;
    const group = block.querySelector(".user-track-group");
    if (group) group.classList.toggle("hidden", !(joined && needsTracks));
  });
}

window.onSaveUser = async function () {
  const userId = document.getElementById("editUserId").value;
  const name = document.getElementById("userNameInput").value.trim();
  const email = document.getElementById("userEmailInput").value.trim();
  const role = document.getElementById("userRoleInput").value;

  if (!name || !email) {
    alert("Please enter both Name and Email.");
    return;
  }

  const checkboxes = document.querySelectorAll(".user-prog-checkbox:checked");
  const progIds = Array.from(checkboxes).map(cb => parseInt(cb.value, 10));
  // Tracks only count for programs that are ticked, and only for roles that join tracks
  const trackIds = (role === "sme" || role === "pm")
    ? Array.from(document.querySelectorAll(".user-track-checkbox:checked"))
        .filter(cb => progIds.includes(parseInt(cb.dataset.prog, 10)))
        .map(cb => parseInt(cb.value, 10))
    : [];

  try {
    showLoader("Saving User & Scope Configuration...");
    let res;
    if (userId) {
      res = await fetch(`${API_BASE}/users/${userId}?actor_id=${currentUser ? currentUser.id : ""}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, email, role, program_ids: progIds, track_ids: trackIds })
      });
    } else {
      res = await fetch(`${API_BASE}/users?actor_id=${currentUser ? currentUser.id : ""}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, email, role, program_ids: progIds, track_ids: trackIds })
      });
    }

    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || "Failed to save user");
    }

    const savedUser = await res.json();
    document.getElementById("userManageModal").classList.add("hidden");

    // Refresh user list and selectors
    const uRes = await fetch(`${API_BASE}/users?actor_id=${currentUser ? currentUser.id : ""}`);
    allUsers = await uRes.json();

    const userSelect = document.getElementById("activeUserSelector");
    if (userSelect) {
      userSelect.innerHTML = "";
      allUsers.forEach(u => {
        userSelect.innerHTML += `<option value="${u.id}">${escapeHtml(u.name)} (${getRoleLabel(u.role)})</option>`;
      });
      userSelect.value = currentUser ? currentUser.id : savedUser.id;
    }

    await renderAdminUsersTable();

    // If edited user is current user, update badge and scope tags
    if (currentUser && currentUser.id === savedUser.id) {
      await switchActiveUser(savedUser.id, false);
    }

    alert(`User '${savedUser.name}' saved successfully.`);
  } catch (err) {
    alert("Error: " + err.message);
  } finally {
    hideLoader();
  }
};

window.deleteUserDirectly = async function (userId, userName) {
  // Use the curriculum confirm modal pattern instead of browser confirm()
  resetConfirmDialog();
  document.getElementById("confirmDeleteType").value = "user";
  document.getElementById("confirmDeleteId").value = userId;
  document.getElementById("curriculumConfirmTitle").textContent = "Remove User";
  document.getElementById("curriculumConfirmMessage").textContent = `Are you sure you want to permanently remove user '${userName}' from the platform?`;
  document.getElementById("curriculumConfirmDeleteBtn").textContent = "Remove User";
  document.getElementById("curriculumConfirmModal").classList.remove("hidden");
};

// Read-Only Script Inspection Modal
window.viewScriptReadonly = async function (scriptId) {
  try {
    showLoader("Fetching Script in Read-Only Mode...");
    const res = await fetch(`${API_BASE}/scripts/${scriptId}`);
    if (!res.ok) throw new Error("Could not load script details.");
    const script = await res.json();

    const titleEl = document.getElementById("assignmentsModalTitle");
    const bodyEl = document.getElementById("assignmentsContent");

    titleEl.textContent = `Script Inspection: ${script.class_title} [${script.status}]`;

    const isApproved = (script.status === "GTG");
    const gtgButtonHtml = isApproved
      ? `<span class="status-pill status-gtg" style="padding: 5px 12px; font-size: 0.8rem; font-weight: 600;">Official GTG Approved</span>`
      : `<button id="inspectGtgBtn-${script.id}" class="btn btn-success" style="padding: 5px 12px; font-size: 0.8rem;" onclick="approveScriptDirectly(${script.id}, this)">Approve GTG</button>`;

    let contentHtml = `
      <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:16px; padding-bottom:12px; border-bottom:1px solid var(--border-subtle); flex-wrap:wrap; gap:8px;">
        <div>
          <span id="inspectModalStatusPill" style="font-weight:700; color:var(--primary-glow);">${script.status}</span> &bull; 
          <span>${script.duration_minutes} Minutes</span> &bull; 
          <span>${script.cards ? script.cards.length : 0} Spoken Cards</span>
        </div>
        <div style="display:flex; align-items:center; gap:8px;">
          <button class="btn btn-secondary" style="padding: 5px 12px; font-size: 0.8rem;" onclick="navigator.clipboard.writeText(window.location.origin + '/script/' + '${script.shareable_uuid || script.uuid}'); alert('Permalink copied!');">Copy Shareable Link</button>
          ${currentUser && (currentUser.role === 'admin' || currentUser.role === 'mo' || (currentUser.role === 'sme' && script.user_id === currentUser.id)) ? `<button class="btn btn-warning" style="padding: 5px 12px; font-size: 0.8rem; background:rgba(239,68,68,0.12); color:var(--danger-color); border:1px solid rgba(239,68,68,0.3);" onclick="deleteScriptDirectly(${script.id}, '${escapeHtml(script.class_title)}')">Delete Script</button>` : ''}
          ${currentUser && (currentUser.role === 'mo' || currentUser.role === 'admin') ? gtgButtonHtml : ''}
        </div>
      </div>
      <div style="display:flex; flex-direction:column; gap:16px;">
    `;

    (script.cards || []).forEach(c => {
      contentHtml += `
        <div style="border:1px solid var(--border-subtle); border-radius:8px; padding:16px; background:var(--bg-card);">
          <div style="display:flex; justify-content:space-between; margin-bottom:8px;">
            <span style="font-weight:700; color:var(--text-primary);">Card #${c.card_index}: ${escapeHtml(c.title)}</span>
            <span style="font-size:0.8rem; color:var(--text-secondary);">${c.duration_mins} mins</span>
          </div>
          ${c.stage_directions ? `<div style="font-size:0.8rem; color:var(--purple-accent); font-style:italic; margin-bottom:8px;">${escapeHtml(c.stage_directions)}</div>` : ''}
          <div style="font-size:0.88rem; color:var(--text-primary); line-height:1.6;">${formatCardText(c.content_script || "", c.sources)}</div>
          ${c.quiz ? formatQuiz(c.quiz) : ""}
          ${c.code_snippet ? `<pre style="background:var(--bg-main); padding:10px; border-radius:6px; margin-top:10px; font-family:monospace; font-size:0.8rem; overflow-x:auto;"><code>${escapeHtml(c.code_snippet)}</code></pre>` : ''}
        </div>
      `;
    });

    contentHtml += '</div>';
    let reviewHtml = "";
    try {                                                              // what the AI review found, for the Module Owner to read before deciding
      const rr = await fetch(`${API_BASE}/scripts/${scriptId}/review/latest`);
      const rv = rr.ok ? await rr.json() : null;
      reviewHtml = rv && rv.ran
        ? `<section class="air" style="margin-bottom:16px;"><div class="air-head"><div><h3 class="air-title">AI review <span class="air-state ${rv.passed ? "ok" : "warn"}">${rv.passed ? "Passed" : "Open findings left for the author"}</span></h3><div class="air-sub">${Number(rv.score).toFixed(1)}/10 &middot; the AI fixed the script ${rv.repairs || 0} ${rv.repairs === 1 ? "time" : "times"} &middot; ${parseServerTime(rv.at).toLocaleString()}</div></div></div>${aiReviewBodyHtml(rv)}</section>`
        : `<section class="air" style="margin-bottom:16px;"><div class="air-head"><div><h3 class="air-title">AI review <span class="air-state">Not run on this version</span></h3></div></div></section>`;
    } catch (e) { /* the script is still shown */ }
    bodyEl.innerHTML = reviewHtml + contentHtml;

    document.getElementById("assignmentsModal").classList.remove("hidden");

  } catch (err) {
    alert("Error inspecting script: " + err.message);
  } finally {
    hideLoader();
  }
};

// Permanent Script Deletion Handler
window.deleteScriptDirectly = async function (scriptId, scriptTitle) {
  const confirmed = confirm(`Are you sure you want to permanently delete script #${scriptId} ("${scriptTitle || 'Selected Script'}")?\n\nThis will remove all associated script cards, stage directions, and approval records.`);
  if (!confirmed) return;

  try {
    showLoader("Deleting Script...");
    const userParam = currentUser ? `?user_id=${currentUser.id}` : "";
    const res = await fetch(`${API_BASE}/scripts/${scriptId}${userParam}`, {
      method: "DELETE"
    });

    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      throw new Error(errData.detail || "Failed to delete script");
    }

    const data = await res.json();

    // Close inspection modal if currently open
    const inspectModal = document.getElementById("assignmentsModal");
    if (inspectModal && !inspectModal.classList.contains("hidden")) {
      inspectModal.classList.add("hidden");
    }

    // Refresh Admin Scripts table if function exists
    if (typeof loadAdminScriptsTable === "function") {
      await loadAdminScriptsTable();
    }

    // Refresh SME program scripts table if function exists
    if (typeof refreshCurrentProgramScripts === "function") {
      refreshCurrentProgramScripts();
    }

    // Refresh PM table if function exists
    if (typeof loadPmDashboardData === "function") {
      loadPmDashboardData();
    }

    alert(data.message || `Script #${scriptId} deleted successfully.`);
  } catch (err) {
    alert("Error deleting script: " + err.message);
  } finally {
    hideLoader();
  }
};

// ==============================================================================
// 11. EMAIL SIGN-IN & AUTH FLOW
// ==============================================================================

window.openEmailLoginModal = function () {
  const modal = document.getElementById("emailLoginModal");
  if (modal) {
    modal.classList.remove("hidden");
    const input = document.getElementById("emailLoginInput");
    if (input) {
      input.value = currentUser ? currentUser.email : "";
      setTimeout(() => input.focus(), 100);
    }
  }
};

window.closeEmailLoginModal = function () {
  const modal = document.getElementById("emailLoginModal");
  if (modal) modal.classList.add("hidden");
};

window.fillEmailLogin = function (email) {
  const input = document.getElementById("emailLoginInput");
  if (input) input.value = email;
};

window.submitEmailLogin = async function () {
  const input = document.getElementById("emailLoginInput");
  const email = input ? input.value.trim().toLowerCase() : "";
  if (!email) {
    alert("Please enter a valid work email address.");
    return;
  }

  const loginRes = await fetch(`${API_BASE}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ email })
  });
  const user = loginRes.ok ? await loginRes.json() : null;
  if (user) allUsers = [user];
  if (!user) {
    alert(`No user found with email '${email}'. Please verify your email or ask the Administrator to enroll your profile.`);
    return;
  }

  closeEmailLoginModal();
  showLoader(`Signing in as ${user.name} (${getRoleLabel(user.role)})...`);
  try {
    await switchActiveUser(user.id, true);
  } finally {
    hideLoader();
  }
};

// ==============================================================================
// 12. ADMIN GOVERNANCE CONSOLE - TABS & PROGRAM / TRACK / LEVEL MANAGEMENT
// ==============================================================================

window.switchAdminTab = function (tabName) {
  try { localStorage.setItem("admin_dashboard_tab", tabName); } catch (e) { /* storage may be off */ }
  setUrl(`#/admin/${tabName}`, true);
  const tabs = ["users", "curriculum", "scripts", "tat", "activities"];
  tabs.forEach(t => {
    let btnId = "adminTabBtnUsers";
    let panelId = "adminTabUsers";
    if (t === "curriculum") {
      btnId = "adminTabBtnCurriculum";
      panelId = "adminTabCurriculum";
    } else if (t === "scripts") {
      btnId = "adminTabBtnScripts";
      panelId = "adminTabScripts";
    } else if (t === "tat") {
      btnId = "adminTabBtnTat";
      panelId = "adminTabTat";
    } else if (t === "activities") {
      btnId = "adminTabBtnActivities";
      panelId = "adminTabActivities";
    }

    const btn = document.getElementById(btnId);
    const panel = document.getElementById(panelId);

    if (btn) {
      if (t === tabName) btn.classList.add("active");
      else btn.classList.remove("active");
    }
    if (panel) {
      if (t === tabName) panel.classList.remove("hidden");
      else panel.classList.add("hidden");
    }
  });

  if (tabName === "users") {
    renderAdminUsersTable();
    recordClientActivity("Tab Switched", "Navigated to Users & SME Enrollment tab");
  } else if (tabName === "curriculum") {
    loadAdminCurriculumArchitecture();
    recordClientActivity("Tab Switched", "Navigated to Curriculum Architecture tab");
  } else if (tabName === "scripts") {
    loadAdminScriptsTable();
    recordClientActivity("Tab Switched", "Navigated to Generated Scripts & Approvals audit tab");
  } else if (tabName === "tat") {
    renderTatPanel("tatAdmin");
    recordClientActivity("Tab Switched", "Navigated to Turnaround tab");
  } else if (tabName === "activities") {
    loadUserActivities();
    recordClientActivity("Tab Switched", "Navigated to User activity tab");
  }
};

let adminLoadedScripts = [];

// The server stores every time in UTC without a time-zone mark ("2026-10-16 06:20:00"). A browser would read that as local time and show it hours
// early (5.5 hours in India), so a time with no zone is read as UTC here and then shown in the viewer's own time zone.
function parseServerTime(value) {
  if (value instanceof Date) return value;
  const text = String(value || "").trim();
  if (!text) return new Date(NaN);
  const hasZone = /(Z|[+-]\d{2}:?\d{2})$/i.test(text);
  return new Date(hasZone ? text : text.replace(" ", "T") + "Z");
}

function formatAuditDateTime(dateStr) {
  if (!dateStr) return null;
  try {
    const d = parseServerTime(dateStr);
    if (isNaN(d.getTime())) return dateStr;
    return d.toLocaleString("en-US", {
      month: "short",
      day: "numeric",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      hour12: true
    });
  } catch (e) {
    return dateStr;
  }
}

window.loadAdminScriptsTable = async function (silent) {
  const tbody = document.getElementById("adminScriptsList");
  if (!tbody) return;

  try {
    if (silent !== true) showLoader("Loading Generated Scripts & Approvals Audit...");
    const res = await fetch(`${API_BASE}/dashboard/scripts?user_id=${currentUser ? currentUser.id : ""}`);
    if (!res.ok) throw new Error("Could not fetch generated scripts.");
    adminLoadedScripts = await res.json();

    // Populate Program Filter Options dynamically
    const progFilter = document.getElementById("adminScriptsProgramFilter");
    if (progFilter) {
      const selectedProg = progFilter.value;
      const programsMap = new Map();
      adminLoadedScripts.forEach(s => {
        if (s.program_id && s.program_name) {
          programsMap.set(String(s.program_id), s.program_name);
        }
      });
      progFilter.innerHTML = '<option value="">All Programs</option>';
      programsMap.forEach((pName, pId) => {
        const opt = document.createElement("option");
        opt.value = pId;
        opt.textContent = pName;
        progFilter.appendChild(opt);
      });
      progFilter.value = selectedProg || "";
    }

    const authorFilter = document.getElementById("adminScriptsAuthorFilter");
    if (authorFilter) {
      const keep = authorFilter.value;
      const names = [...new Set(adminLoadedScripts.map(x => x.sme_name).filter(Boolean))].sort((a, b) => a.localeCompare(b));
      authorFilter.innerHTML = '<option value="">All authors</option>' + names.map(n => `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join("");
      authorFilter.value = names.includes(keep) ? keep : "";
    }
    try {                                                   // Module Owners for the filter
      const uRes = await fetch(`${API_BASE}/users?actor_id=${currentUser ? currentUser.id : ""}`);
      if (uRes.ok) allUsers = await uRes.json();
    } catch (e) { /* the filter just stays empty */ }
    const moFilter = document.getElementById("adminScriptsMoFilter");
    if (moFilter) {
      const keep = moFilter.value;
      const mos = (allUsers || []).filter(u => u.role === "mo").sort((a, b) => a.name.localeCompare(b.name));
      moFilter.innerHTML = '<option value="">All Module Owners</option>' + mos.map(u => `<option value="${u.id}">${escapeHtml(u.name)}</option>`).join("");
      moFilter.value = mos.some(u => String(u.id) === keep) ? keep : "";
    }

    filterAdminScriptsTable(silent === true);
  } catch (err) {
    console.error("Error loading admin scripts table:", err);
    if (silent !== true) tbody.innerHTML = `<div class="sa-empty" style="color:var(--danger-color);">Could not load the scripts: ${escapeHtml(err.message)}</div>`;
  } finally {
    if (silent !== true) hideLoader();
  }
};

window.filterAdminScriptsTable = function (keepVisible) {
  const list = document.getElementById("adminScriptsList");
  if (!list) return;

  const val = (id) => document.getElementById(id)?.value || "";
  const searchQuery = val("adminScriptsSearchInput").toLowerCase().trim();
  const selectedProg = val("adminScriptsProgramFilter");
  const selectedStatus = val("adminScriptsStatusFilter");
  const author = val("adminScriptsAuthorFilter");
  const moId = val("adminScriptsMoFilter");
  const from = val("adminScriptsFromDate");
  const to = val("adminScriptsToDate");
  const sort = val("adminScriptsSort") || "newest";

  let filtered = adminLoadedScripts || [];
  if (selectedProg) filtered = filtered.filter(s => String(s.program_id) === String(selectedProg));
  if (selectedStatus) filtered = filtered.filter(s => selectedStatus === "GTG" ? s.status === "GTG" : selectedStatus === "MO Approval" ? s.status === "MO Approval" : (s.status !== "GTG" && s.status !== "MO Approval"));
  if (author) filtered = filtered.filter(s => s.sme_name === author);
  if (moId) {
    const mo = (allUsers || []).find(u => String(u.id) === String(moId));
    const progs = ((mo && mo.program_ids) || []).map(Number);
    // the Module Owner's scripts: ones they approved, and the ones now waiting for a Module Owner in their programs
    filtered = filtered.filter(s => mo && ((s.status === "GTG" && (s.approved_by_name || "").trim().toLowerCase() === mo.name.trim().toLowerCase())
      || (s.status === "MO Approval" && progs.includes(Number(s.program_id)))));
  }
  if (from || to) {                                         // by the day it was generated, in the viewer's own time zone
    filtered = filtered.filter(s => {
      const d = parseServerTime(s.created_at);
      if (isNaN(d.getTime())) return false;
      const day = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
      return (!from || day >= from) && (!to || day <= to);
    });
  }
  if (searchQuery) {
    filtered = filtered.filter(s =>
      (s.sme_name && s.sme_name.toLowerCase().includes(searchQuery)) ||
      (s.sme_role && roleShort(s.sme_role).toLowerCase().includes(searchQuery)) ||
      (s.class_title && s.class_title.toLowerCase().includes(searchQuery)) ||
      (s.module_name && s.module_name.toLowerCase().includes(searchQuery)) ||
      (s.track_name && s.track_name.toLowerCase().includes(searchQuery)) ||
      (s.program_name && s.program_name.toLowerCase().includes(searchQuery)) ||
      (s.approved_by_name && s.approved_by_name.toLowerCase().includes(searchQuery)) ||
      (String(s.id).includes(searchQuery))
    );
  }

  const ms = (v) => { const t = parseServerTime(v).getTime(); return isNaN(t) ? 0 : t; };
  const waited = (s) => (s.status === "GTG" ? -1 : Date.now() - ms(s.status === "MO Approval" ? (s.submitted_at || s.created_at) : s.created_at));
  const sorters = {
    newest: (a, b) => ms(b.created_at) - ms(a.created_at),
    oldest: (a, b) => ms(a.created_at) - ms(b.created_at),
    approved: (a, b) => ms(b.approved_at) - ms(a.approved_at),
    cost: (a, b) => (b.cost_usd || 0) - (a.cost_usd || 0),
    waiting: (a, b) => waited(b) - waited(a)
  };
  filtered = [...filtered].sort(sorters[sort] || sorters.newest);

  renderAdminScriptsTable(filtered, keepVisible === true);
};

window.clearAdminScriptFilters = function () {
  ["adminScriptsSearchInput", "adminScriptsProgramFilter", "adminScriptsStatusFilter", "adminScriptsAuthorFilter", "adminScriptsMoFilter", "adminScriptsFromDate", "adminScriptsToDate"]
    .forEach(id => { const el = document.getElementById(id); if (el) el.value = ""; });
  const sort = document.getElementById("adminScriptsSort"); if (sort) sort.value = "newest";
  filterAdminScriptsTable();
};

// short role names for lists: SME, MO, PM, Admin
function roleShort(role) { return ({ sme: "SME", mo: "Module Owner", pm: "Program Manager", admin: "Admin" })[role] || role; }

// Where a script really is: the status the server keeps, in words
function scriptStage(s) {
  if (s.status === "GTG") return { key: "gtg", text: "Approved", dot: "sa-ok", since: "" };
  if (s.status === "MO Approval") return { key: "mo", text: "Module Owner review", dot: "sa-wait", since: "for" };
  if (s.status === "SME Review") return { key: "review", text: "SME review", dot: "sa-wait", since: "for" };
  return { key: "draft", text: "Draft", dot: "", since: "started" };
}

let adminScriptsVisible = 50;

window.renderAdminScriptsTable = function (scripts, keepVisible) {
  const list = document.getElementById("adminScriptsList");
  if (!list) return;
  if (!keepVisible) adminScriptsVisible = 50;
  window.adminRenderedScripts = scripts || [];

  const all = adminLoadedScripts || [];
  const approved = all.filter(s => s.status === "GTG").length;
  const hasCost = all.some(s => s.cost_usd !== undefined);          // only the administrator's request carries costs
  document.querySelector("#adminTabScripts .sa-table")?.classList.toggle("sa-no-cost", !hasCost);
  if (!scripts || scripts.length === 0) {
    list.innerHTML = '<div class="sa-empty">No scripts match this search.</div>';
    return;
  }

  const day = (d) => { const f = formatAuditDateTime(d); return f ? f.replace(/, \d{1,2}:\d{2}.*$/, "") : ""; };
  const time = (d) => { const f = formatAuditDateTime(d); const m = f && /(\d{1,2}:\d{2}\s?[AP]M)/i.exec(f); return m ? m[1] : ""; };
  const span = (ms) => {                                            // 40 min, 17 h, 3 days
    const mins = Math.max(0, Math.round(ms / 60000));
    if (mins < 60) return `${mins} min`;
    if (mins < 48 * 60) return `${Math.round(mins / 60)} h`;
    return `${Math.round(mins / 1440)} days`;
  };
  const when = (d) => { const f = formatAuditDateTime(d); return f ? f.replace(/, \d{4}/, "") : ""; };      // "Oct 16, 11:50 AM"

  let html = scripts.slice(0, adminScriptsVisible).map(s => {
    const isGtg = s.status === "GTG";
    const created = parseServerTime(s.created_at).getTime();
    const approvedAt = s.approved_at ? parseServerTime(s.approved_at).getTime() : NaN;
    const sinceMs = (v) => { const t = parseServerTime(v).getTime(); return isNaN(t) ? NaN : t; };
    const stage = scriptStage(s);
    const waitSince = s.status === "MO Approval" ? sinceMs(s.submitted_at || s.last_activity_at || s.created_at) : created;       // for the Module Owner, since it was sent
    const approval = isGtg
      ? `<div class="sa-state sa-ok">Approved</div>
         <div class="sa-sub">${s.approved_by_name ? "by " + escapeHtml(s.approved_by_name) + (s.approved_by_role ? ` (${roleShort(s.approved_by_role)})` : "") : ""}</div>`
      : `<div class="sa-state ${stage.dot}">${stage.text}</div><div class="sa-sub">${isNaN(waitSince) ? "" : stage.since + " " + span(Date.now() - waitSince)}</div>`;
    const approvedOn = isGtg && s.approved_at
      ? `<div>${day(s.approved_at)}</div><div class="sa-sub">${time(s.approved_at)}${isNaN(created) || isNaN(approvedAt) ? "" : ` &middot; took ${span(approvedAt - created)}`}</div>`
      : `<div class="sa-sub">${isGtg ? "Time not recorded" : "-"}</div>`;
    const planned = s.cards_planned;
    const progress = `<div>${s.cards_count}${planned ? ` of ${planned}` : ""} ${s.cards_count === 1 && !planned ? "card" : "cards"}</div>
      <div class="sa-sub">${s.last_activity_at ? "edited " + when(s.last_activity_at) : ""}</div>`;
    const basis = { reported: "The charge reported by the AI provider for this script's calls", estimated: "Partly estimated from our price list: some calls were not priced by the provider", partial: "Incomplete: some calls had no price, so the real cost is higher" };
    const cost = s.cost_usd !== undefined
      ? `<div class="sa-cost-col" title="${escapeHtml(basis[s.cost_basis] || "")}">${s.cost_basis && s.cost_basis !== "reported" ? "~" : ""}$${s.cost_usd.toFixed(2)}</div>` : "";
    const menu = actionMenu([["Copy link", `copyScriptLink('${escapeHtml(s.uuid)}')`], ["Delete script", `deleteScriptDirectly(${s.id}, '${escapeHtml(s.class_title).replace(/'/g, "\\'")}')`, true]]);
    return `
      <div class="sa-row">
        <div class="sa-id">#${s.id}</div>
        <div class="sa-class">
          <div class="sa-class-title">${escapeHtml(s.class_title)}</div>
          <div class="sa-sub">${escapeHtml(s.program_name)} &middot; ${escapeHtml(s.track_name)} &middot; ${escapeHtml(s.module_name)} &middot; ${s.duration_minutes} min</div>
        </div>
        <div class="sa-author">${escapeHtml(s.sme_name || "Unknown")}${s.sme_role ? `<div class="sa-sub">${roleShort(s.sme_role)}</div>` : ""}</div>
        <div><div>${day(s.created_at) || "-"}</div><div class="sa-sub">${time(s.created_at)}</div></div>
        <div>${progress}</div>
        <div>${approvedOn}</div>
        <div>${approval}</div>
        ${cost}
        <div class="sa-actions"><button class="btn btn-secondary" onclick="viewScriptReadonly(${s.id})">Open</button>${menu}</div>
      </div>`;
  }).join("");

  if (scripts.length > adminScriptsVisible) {
    html += `<div class="sa-more"><span>Showing ${adminScriptsVisible} of ${scripts.length}</span>
      <button class="btn btn-secondary" onclick="adminScriptsVisible += 50; renderAdminScriptsTable(window.adminRenderedScripts, true)">Show 50 more</button></div>`;
  }
  list.innerHTML = html;
};

// Everything the filters currently show, as a spreadsheet file
window.exportAdminScriptsCsv = function () {
  const rows = window.adminRenderedScripts || [];
  if (!rows.length) { alert("There are no scripts to export with the current filters."); return; }
  const hasCost = rows.some(s => s.cost_usd !== undefined);
  const pad = (n) => String(n).padStart(2, "0");
  const local = (v) => {
    const d = parseServerTime(v);
    return isNaN(d.getTime()) ? "" : `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };
  const hours = (s) => {
    const a = parseServerTime(s.created_at).getTime(), b = parseServerTime(s.approved_at).getTime();
    return s.status === "GTG" && !isNaN(a) && !isNaN(b) ? ((b - a) / 3600000).toFixed(1) : "";
  };
  const head = ["Script ID", "Class", "Program", "Track", "Module", "Duration (min)", "Author", "Author role", "Generated", "Last activity", "Cards written", "Cards planned",
    "Stage", "Approved on", "Approved by", "Hours to approve", "Days from module kickoff"].concat(hasCost ? ["Cost (USD)", "Cost basis"] : []);
  const cell = (v) => {
    let t = v === null || v === undefined ? "" : String(v);
    if (/^[=+\-@\t\r]/.test(t)) t = "'" + t;                         // a cell must never start a spreadsheet formula
    return /[",\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t;
  };
  const lines = [head].concat(rows.map(s => [s.id, s.class_title, s.program_name, s.track_name, s.module_name, s.duration_minutes, s.sme_name || "", s.sme_role ? roleShort(s.sme_role) : "",
    local(s.created_at), local(s.last_activity_at), s.cards_count, s.cards_planned ?? "", scriptStage(s).text, local(s.approved_at), s.approved_by_name || "", hours(s), s.tat_days ?? ""]
    .concat(hasCost ? [(s.cost_usd || 0).toFixed(4), s.cost_basis === "reported" ? "provider-reported" : s.cost_basis || ""] : [])));
  const csv = "\ufeff" + lines.map(r => r.map(cell).join(",")).join("\r\n");
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  a.download = `generated-scripts-${new Date().toISOString().slice(0, 10)}.csv`;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
};

window.copyScriptLink = function (uuid) {
  recordClientActivity("Share Link Copied", `Copied the share link for script ${(adminLoadedScripts.find(x => x.uuid === uuid) || {}).class_title ? "'" + adminLoadedScripts.find(x => x.uuid === uuid).class_title + "'" : ""}`.trim());
  const link = `${window.location.origin}/script/${uuid}`;
  (navigator.clipboard ? navigator.clipboard.writeText(link) : Promise.reject()).then(
    () => alert("Script link copied."), () => window.prompt("Copy this link:", link));
};

// Toggle Module Lock Status (PRD FR-1)
window.toggleModuleLock = async function (modId, currentLockStatus) {
  try {
    showLoader("Updating Module Lock Status...");
    const userParam = currentUser ? `?user_id=${currentUser.id}` : "";
    const res = await fetch(`${API_BASE}/modules/${modId}${userParam}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_locked: !currentLockStatus })
    });
    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      throw new Error(errData.detail || "Failed to toggle module lock status");
    }
    await refreshCurriculumDataGlobally();
    alert(`Module is now ${!currentLockStatus ? "Locked" : "Unlocked"}.`);
  } catch (err) {
    alert("Error: " + err.message);
  } finally {
    hideLoader();
  }
};

// Reassign Module to Different Track / Proficiency Level
window.reassignModuleTrack = async function (modId, currentTrackId) {
  try {
    showLoader("Fetching available tracks...");
    const userParam = (currentUser && currentUser.role === "mo") ? `?user_id=${currentUser.id}` : "";
    const treeRes = await fetch(`${API_BASE}/curriculum/full-tree${userParam}`);
    const tree = await treeRes.json();

    const sel = document.getElementById("reassignTrackSelect");
    sel.innerHTML = "";
    tree.forEach(p => {
      const grp = document.createElement("optgroup");
      grp.label = p.name;
      (p.tracks || []).forEach(t => {
        const opt = document.createElement("option");
        opt.value = t.id;
        opt.textContent = t.name;
        if (t.id === currentTrackId) opt.disabled = true;
        grp.appendChild(opt);
      });
      sel.appendChild(grp);
    });

    document.getElementById("reassignModuleId").value = modId;
    document.getElementById("reassignTrackModal").classList.remove("hidden");
  } catch (err) {
    alert("Error loading tracks: " + err.message);
  } finally {
    hideLoader();
  }
};

window.confirmReassignModuleTrack = async function () {
  const modId = document.getElementById("reassignModuleId").value;
  const targetId = parseInt(document.getElementById("reassignTrackSelect").value, 10);
  if (!targetId) { alert("Please select a target track."); return; }

  try {
    showLoader("Reassigning Module...");
    document.getElementById("reassignTrackModal").classList.add("hidden");
    const param = currentUser ? `?user_id=${currentUser.id}` : "";
    const res = await fetch(`${API_BASE}/modules/${modId}${param}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ track_id: targetId })
    });
    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      throw new Error(errData.detail || "Failed to reassign module");
    }
    await refreshCurriculumDataGlobally();
  } catch (err) {
    alert("Error: " + err.message);
  } finally {
    hideLoader();
  }
};

// ==============================================================================
// 13. USER ACTIVITY STICKY NOTES BOARD
// ==============================================================================

let adminAllActivities = [];

async function loadUserActivities(silent) {
  const list = document.getElementById("adminActivitiesList");
  if (!list) return;
  try {
    if (silent !== true) list.innerHTML = '<div class="sa-empty">Loading activity...</div>';
    const res = await fetch(`${API_BASE}/activities?limit=3000`);
    if (!res.ok) throw new Error("Failed to load activities");
    adminAllActivities = await res.json();
    const personSel = document.getElementById("adminActivityPersonFilter");
    if (personSel) {
      const keep = personSel.value;
      const names = [...new Set(adminAllActivities.map(a => a.user_name).filter(Boolean))].sort((x, y) => x.localeCompare(y));
      personSel.innerHTML = '<option value="">All people</option>' + names.map(n => `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join("");
      personSel.value = names.includes(keep) ? keep : "";
    }
    filterAdminActivities(silent === true);
  } catch (err) {
    console.error("Error loading activities:", err);
    if (silent !== true) list.innerHTML = `<div class="sa-empty" style="color:var(--danger-color);">Could not load the activity: ${escapeHtml(err.message)}</div>`;
  }
}

window.loadUserActivities = loadUserActivities;

function formatActivityDate(dateStr) {
  if (!dateStr) return { date: "Today", time: "" };
  try {
    // If dateStr ends without Z and has no timezone offset, append Z if it is from UTC
    let normalized = dateStr;
    if (typeof dateStr === "string" && !dateStr.endsWith("Z") && !dateStr.includes("+")) {
      normalized = dateStr.includes("T") ? dateStr + "Z" : dateStr.replace(" ", "T") + "Z";
    }
    const d = new Date(normalized);
    if (isNaN(d.getTime())) {
      const fallback = new Date(dateStr);
      if (isNaN(fallback.getTime())) return { date: dateStr, time: "" };
      return {
        date: fallback.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }),
        time: fallback.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", hour12: true })
      };
    }
    const dateFormatted = d.toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
      year: "numeric"
    });
    const timeFormatted = d.toLocaleTimeString("en-US", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: true
    });
    return { date: dateFormatted, time: timeFormatted };
  } catch {
    return { date: dateStr, time: "" };
  }
}

// Which group an action belongs to, so the page-navigation noise can be hidden
function activityGroup(type) {
  const t = String(type || "").toLowerCase();
  if (/^(click|filter changed|search)$/.test(t)) return "clicks";
  if (/tab switched|workspace opened|sub-tab selected/.test(t)) return "navigation";
  if (/sign-in|sign in|login|sign-out|session resumed/.test(t)) return "signin";
  if (/gtg|approv|sign-off/.test(t)) return "approvals";
  if (/script|langgraph|mo review|draft|outline|card|quiz|visual|code run|review run|setup|assignment|share|hackmd|download|copied|export|details filled|opened/.test(t)) return "scripts";
  return "admin";
}

let adminActivityVisible = 150;

const ACTIVITY_TYPE_LABEL = { clicks: "Click / filter", navigation: "Navigation", signin: "Account", approvals: "Approval", scripts: "Script", admin: "Curriculum / users" };

function activityDayLabel(d) {
  const sameDay = (a, b) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  const now = new Date(), yesterday = new Date(now.getTime() - 86400000);
  const text = d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric", year: "numeric" });
  return sameDay(d, now) ? `Today, ${text}` : sameDay(d, yesterday) ? `Yesterday, ${text}` : text;
}

// Several identical entries in a row by the same person (ten sign-ins, repeated opens) are shown once with a count
function collapseActivities(list) {
  const out = [];
  list.forEach(a => {
    const last = out[out.length - 1];
    if (last && last.user_name === a.user_name && last.action_type === a.action_type && last.description === a.description) {
      last.count += 1; last.first_at = a.created_at;
    } else {
      out.push({ ...a, count: 1, first_at: a.created_at });
    }
  });
  return out;
}

// A person's consecutive entries (less than 30 minutes apart) form one block with their name shown once, so a visit reads as a story
function groupActivityBlocks(rows) {
  const blocks = [];
  rows.forEach(r => {
    const t = parseServerTime(r.created_at).getTime();
    const last = blocks[blocks.length - 1];
    if (last && last.user_name === r.user_name && last.lastT - t < 30 * 60 * 1000 && last.dayKey === activityDayLabel(new Date(t))) {
      last.entries.push(r); last.lastT = t; last.first_at = r.first_at || r.created_at;
    } else {
      blocks.push({ user_name: r.user_name, user_role: r.user_role, entries: [r], lastT: t, newest_at: r.created_at, first_at: r.first_at || r.created_at, dayKey: activityDayLabel(new Date(t)) });
    }
  });
  return blocks;
}

const ACTIVITY_PREVIEW = 8;

window.showMoreInBlock = function (btn) {
  const block = btn.closest(".ua-block");
  block.querySelectorAll(".ua-entry.ua-extra").forEach(e => e.classList.remove("ua-extra"));
  btn.remove();
};
window.filterActivityByPerson = function (name) {
  const sel = document.getElementById("adminActivityPersonFilter");
  if (sel) { sel.value = name; filterAdminActivities(); }
};
window.filterActivityByType = function (group) {
  const sel = document.getElementById("adminActivityTypeFilter");
  if (sel && [...sel.options].some(o => o.value === group)) { sel.value = group; filterAdminActivities(); }
};

function renderActivitiesTable(activities, keepVisible) {
  const list = document.getElementById("adminActivitiesList");
  if (!list) return;
  if (!keepVisible) adminActivityVisible = 150;
  window.adminRenderedActivities = activities || [];

  if (!activities || activities.length === 0) {
    const dayBox = document.getElementById("adminActivityDate");
    const oneDay = document.getElementById("adminActivityPeriod")?.value === "day";
    list.innerHTML = `<div class="sa-empty">${oneDay ? (window.adminActivityFollowToday !== false ? "No activity yet today (it counts from 12:00 AM). Pick another day in the calendar above to see earlier activity." : `No activity on ${dayBox ? dayBox.value : "that day"}.`) : "No activity matches these filters."}</div>`;
    return;
  }

  const rows = activities.map(a => ({ ...a, count: 1 }));          // every entry is its own row with its own time; nothing is merged
  const shown = rows.slice(0, adminActivityVisible);
  const stamp = (v) => {
    const d = parseServerTime(v);
    if (isNaN(d.getTime())) return "";
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric" }) + ", " + d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: true });
  };
  const dayCount = {};
  rows.forEach(r => { const x = parseServerTime(r.created_at); const k = isNaN(x.getTime()) ? "Unknown date" : activityDayLabel(x); dayCount[k] = (dayCount[k] || 0) + 1; });

  let html = "", currentDay = "";
  shown.forEach(act => {
    const d = parseServerTime(act.created_at);
    const day = isNaN(d.getTime()) ? "Unknown date" : activityDayLabel(d);
    if (day !== currentDay) {
      currentDay = day;
      const hours = document.getElementById("adminActivityPeriod")?.value === "day" ? " &middot; 12:00 AM to 11:59 PM" : "";
      html += `<div class="ua-day">${escapeHtml(day)}${hours}<span>${dayCount[day]} ${dayCount[day] === 1 ? "entry" : "entries"}</span></div>`;
    }
    const group = activityGroup(act.action_type);
    const typeText = act.action_type || "";
    const blockedEntry = /blocked|sign-in failed/i.test(typeText);
    const bad = blockedEntry || /delete|revoke/i.test(typeText);
    const kind = bad ? "danger" : group;
    html += `
      <div class="ut-row ut-${kind}">
        <div class="ut-time">${stamp(act.created_at)}</div>
        <div class="ut-user"><span class="ut-link">${escapeHtml(act.user_name || "System")}</span></div>
        <div>${act.user_role ? `<span class="role-tag-badge role-tag-${escapeHtml(act.user_role)}">${roleShort(act.user_role)}</span>` : ""}</div>
        <div><span class="ua-type ua-t-${kind}">${escapeHtml(typeText || "Activity")}</span></div>
        <div class="ut-detail">${activityTextHtml(act)}${activityChangesHtml(act)}</div>
      </div>`;
  });

  if (rows.length > adminActivityVisible) {
    html += `<div class="sa-more"><span>Showing ${adminActivityVisible} of ${rows.length}</span>
      <button class="btn btn-secondary" onclick="adminActivityVisible += 150; renderActivitiesTable(window.adminRenderedActivities, true)">Show 150 more</button></div>`;
  }
  list.innerHTML = html;
}

// An entry's extra information: the item it is about (so it can be opened) and what changed. Older entries have neither.
function activityInfo(act) {
  try { const d = act && act.details ? JSON.parse(act.details) : null; return d && typeof d === "object" ? d : {}; } catch (e) { return {}; }
}
function activityTextHtml(act) {
  const info = activityInfo(act), text = escapeHtml(act.description || "");
  const ref = info.ref;
  return ref && ref.kind && ref.id
    ? `<a href="javascript:void(0)" class="ut-open" title="Open this ${escapeHtml(ref.kind)}" onclick="openActivityItem('${escapeHtml(ref.kind)}', ${Number(ref.id)})">${text}</a>`
    : text;
}
function activityChangesHtml(act) {
  const changes = activityInfo(act).changes;
  if (!Array.isArray(changes) || !changes.length) return "";
  return `<div class="ut-changes">${changes.map(c => `<div>${escapeHtml(String(c)).replace(/ -&gt; /g, " &rarr; ")}</div>`).join("")}</div>`;
}

// Open what an activity is about: a script opens read-only; a program, track, module or class opens in the Curriculum tab
window.openActivityItem = async function (kind, id) {
  if (kind === "script") { viewScriptReadonly(id); return; }
  try {
    switchAdminTab("curriculum");
    await loadAdminCurriculumArchitecture();
    const found = kind === "class" ? findClassInTree(id) : kind === "module" ? findModuleInTree(id) : kind === "track" ? findTrackInTree(id) : findProgramInTree(id);
    if (!found) { alert(`This ${kind} no longer exists.`); return; }
    const prog = found.program || found;                                  // the finders return the item with its parents, or the program itself
    const trk = found.track || (kind === "track" ? found.track : null);
    selectAdminProgramSubTab(prog.id);
    if (trk) selectAdminTrackSubTab(trk.id);
    const mod = found.module;
    if (mod) { openModules.add(mod.id); renderSelectedAdminTrackTree(); }
    if (kind === "class" || kind === "module") {
      setTimeout(() => {
        const el = kind === "class" ? document.querySelector(`[data-class-id="${id}"]`) : document.querySelector(`[data-mod-id="${id}"]`);
        if (el) { el.scrollIntoView({ behavior: "smooth", block: "center" }); el.classList.add("ut-flash"); setTimeout(() => el.classList.remove("ut-flash"), 2500); }
      }, 250);
    }
  } catch (err) { alert("Could not open it: " + err.message); }
};

// One place that turns rows into a spreadsheet file, so every export escapes and encodes the same way
function downloadCsv(filename, rows) {
  const cell = (v) => {
    let t = v === null || v === undefined ? "" : String(v);
    if (/^[=+\-@\t\r]/.test(t)) t = "'" + t;                         // a cell must never start a spreadsheet formula
    return /[",\n\r]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t;
  };
  const csv = "\ufeff" + rows.map(r => r.map(cell).join(",")).join("\r\n");        // the BOM makes Excel read accents and symbols correctly
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
}
const todayStamp = () => { const d = new Date(); return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };

// What the filters show, as a spreadsheet file
window.exportActivityCsv = function () {
  const rows = window.adminRenderedActivities || [];
  if (!rows.length) { alert("There is no activity to export with the current filters."); return; }
  const pad = (n) => String(n).padStart(2, "0");
  const local = (v) => { const d = parseServerTime(v); return isNaN(d.getTime()) ? "" : `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`; };
  const cell = (v) => {
    let t = v === null || v === undefined ? "" : String(v);
    if (/^[=+\-@\t\r]/.test(t)) t = "'" + t;                         // a cell must never start a spreadsheet formula
    return /[",\n]/.test(t) ? `"${t.replace(/"/g, '""')}"` : t;
  };
  const head = ["Date and time", "User", "Role", "Action", "Details", "What changed", "Item"];
  const lines = [head].concat(rows.map(a => {
    const info = activityInfo(a);
    return [local(a.created_at), a.user_name || "", a.user_role ? roleShort(a.user_role) : "", a.action_type || "", a.description || "",
      (info.changes || []).join("; "), info.ref ? `${info.ref.kind} #${info.ref.id}` : ""];
  }));
  const csv = "\ufeff" + lines.map(r => r.map(cell).join(",")).join("\r\n");
  const period = document.getElementById("adminActivityPeriod")?.value || "day";
  const tag = period === "day" ? (document.getElementById("adminActivityDate")?.value || "") : period;
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv;charset=utf-8" }));
  a.download = `user-activity-${tag}.csv`;
  document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
};

window.filterAdminActivities = function (keepVisible) {
  const val = (id) => (document.getElementById(id)?.value || "").trim();
  const q = val("adminActivitySearchInput").toLowerCase();
  const role = val("adminActivityRoleFilter").toLowerCase();
  const person = val("adminActivityPersonFilter");
  const type = val("adminActivityTypeFilter") || "work";
  const period = val("adminActivityPeriod") || "day";
  const pad = (n) => String(n).padStart(2, "0");
  const ymd = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
  const now = new Date();
  const dateBox = document.getElementById("adminActivityDate");
  if (dateBox) {
    dateBox.max = ymd(now);
    if (window.adminActivityFollowToday !== false) dateBox.value = ymd(now);       // still on "today": a page left open past midnight moves to the new day
    if (!dateBox.value) dateBox.value = ymd(now);
  }
  let from = "", to = "";
  if (period === "day") { from = to = (dateBox && dateBox.value) || ymd(now); }
  else if (period === "week") { from = ymd(new Date(now.getTime() - 6 * 86400000)); to = ymd(now); }
  const dayOf = (v) => {
    const d = parseServerTime(v);
    return isNaN(d.getTime()) ? "" : `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  };

  const filtered = (adminAllActivities || []).filter(act => {
    if (q && !((act.user_name || "").toLowerCase().includes(q) || (act.description || "").toLowerCase().includes(q) || (act.action_type || "").toLowerCase().includes(q))) return false;
    if (role && (act.user_role || "").toLowerCase() !== role) return false;
    if (person && act.user_name !== person) return false;
    const g = activityGroup(act.action_type);
    if (type === "work" ? (g === "navigation" || g === "clicks") : (type !== "all" && g !== type)) return false;
    if (from || to) {
      const day = dayOf(act.created_at);
      if ((from && day < from) || (to && day > to)) return false;
    }
    return true;
  });
  renderActivitiesTable(filtered, keepVisible === true);
};

window.onActivityPeriodChange = function () {
  const day = document.getElementById("adminActivityPeriod")?.value === "day";
  const box = document.getElementById("adminActivityDayPicker");
  if (box) box.style.display = day ? "inline-flex" : "none";
  filterAdminActivities();
};

const ymdOf = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

window.onActivityDatePicked = function () {                      // the calendar was used
  const box = document.getElementById("adminActivityDate");
  window.adminActivityFollowToday = !!box && box.value === ymdOf(new Date());
  filterAdminActivities();
};
window.setActivityDay = function (which) {
  const box = document.getElementById("adminActivityDate");
  if (!box) return;
  if (which === "today") { window.adminActivityFollowToday = true; box.value = ymdOf(new Date()); }
  filterAdminActivities();
};
window.stepActivityDay = function (delta) {                      // previous / next day, never past today
  const box = document.getElementById("adminActivityDate");
  if (!box) return;
  const [y, m, d] = (box.value || ymdOf(new Date())).split("-").map(Number);
  const next = new Date(y, m - 1, d + delta);
  if (next > new Date()) return;
  box.value = ymdOf(next);
  window.adminActivityFollowToday = box.value === ymdOf(new Date());
  filterAdminActivities();
};

window.clearAdminActivityFilters = function () {
  ["adminActivitySearchInput", "adminActivityRoleFilter", "adminActivityPersonFilter"].forEach(id => { const el = document.getElementById(id); if (el) el.value = ""; });
  const type = document.getElementById("adminActivityTypeFilter"); if (type) type.value = "work";
  const period = document.getElementById("adminActivityPeriod"); if (period) period.value = "day";
  window.adminActivityFollowToday = true;
  onActivityPeriodChange();
};

// Global helper to post activities dynamically from client actions (e.g. login, export)
// the name of the script the person is working in, for activity text
function scriptLabelNow() {
  return (currentLoadedScript && currentLoadedScript.class_title) || document.getElementById("studioClassTitle")?.textContent || "a script";
}

async function recordClientActivity(actionType, description) {
  if (!currentUser) return;
  try {
    await fetch(`${API_BASE}/activities`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_name: currentUser.name || "User",
        user_role: currentUser.role || "user",
        action_type: actionType,
        description: description
      })
    });
  } catch (e) {
    // Silent fail for telemetry
    console.debug("Activity log skipped:", e);
  }
}

window.recordClientActivity = recordClientActivity;

// ==============================================================================
// AI API USAGE & COST CONTROLLER
// ==============================================================================

window.openApiUsageModal = function () {
  const modal = document.getElementById("apiUsageModal");
  if (modal) {
    modal.classList.remove("hidden");
    loadApiUsageData();
  }
};

window.closeApiUsageModal = function () {
  const modal = document.getElementById("apiUsageModal");
  if (modal) {
    modal.classList.add("hidden");
  }
};

function usageMoney(value, digits) {
  return `$${Number(value || 0).toFixed(digits)}`;
}

function usageNum(value) {
  return Number(value || 0).toLocaleString();
}

function usageEmptyRow(cols, text) {
  return `<tr><td colspan="${cols}" style="padding: 14px; text-align: center; color: #94a3b8;">${escapeHtml(text)}</td></tr>`;
}

function usageSetText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

function usageShowError(message) {
  const el = document.getElementById("usageError");
  if (!el) return;
  if (message) {
    el.textContent = message;
    el.classList.remove("hidden");
  } else {
    el.textContent = "";
    el.classList.add("hidden");
  }
}

async function loadApiUsageData() {
  const modal = document.getElementById("apiUsageModal");
  const modalOpen = !!modal && !modal.classList.contains("hidden");
  const daysEl = document.getElementById("usageDays");
  const days = daysEl ? Number(daysEl.value || 0) : 0;
  try {
    const res = await fetch(`${API_BASE}/llm/usage?days=${days}`);
    if (!res.ok) {
      let detail = "";
      try { detail = (await res.json()).detail || ""; } catch (e) { /* not JSON */ }
      throw new Error(`The server answered ${res.status}${detail ? `: ${detail}` : ""}`);
    }
    const data = await res.json();
    const summary = data.summary || {};
    usageShowError("");

    usageSetText("usageTotalScripts", usageNum(summary.total_scripts_measured));
    usageSetText("usageTotalCalls", usageNum(summary.total_calls));
    usageSetText("usagePromptTokens", usageNum(summary.total_prompt_tokens));
    usageSetText("usageCompletionTokens", usageNum(summary.total_completion_tokens));
    usageSetText("usageTotalCost", usageMoney(summary.total_cost_usd, 4));
    usageSetText("apiUsageBadge", usageMoney(summary.total_cost_usd, 2));

    const periodText = days ? `the last ${days === 1 ? "24 hours" : days + " days"}` : "all time";
    usageSetText("usageSubtitle", `Token use and estimated cost of every model call, ${periodText}. Average ${Number(summary.avg_latency_seconds || 0).toFixed(1)}s per call.`);

    // Budget bar (all time, whatever period is chosen above)
    const bud = summary.budget || {};
    const budText = document.getElementById("usageBudgetText");
    const budBar = document.getElementById("usageBudgetBar");
    if (budText && budBar) {
      if (bud.limit_usd) {
        const pct = Math.min(100, Number(bud.used_fraction || 0) * 100);
        budText.textContent = `${usageMoney(bud.spent_usd, 2)} of ${usageMoney(bud.limit_usd, 2)} used, ${usageMoney(bud.remaining_usd, 2)} left`;
        budBar.style.width = `${pct}%`;
        budBar.style.background = bud.exceeded ? "#dc2626" : (bud.warning ? "#f59e0b" : "#10b981");
      } else {
        budText.textContent = `${usageMoney(bud.spent_usd, 2)} spent, no limit set`;
        budBar.style.width = "0%";
      }
    }

    // Warnings: failed calls and calls whose price is unknown
    const warnings = [];
    if (bud.exceeded) warnings.push(`The ${usageMoney(bud.limit_usd, 2)} budget is used up. Model calls are blocked until LLM_BUDGET_USD is raised in backend/.env.`);
    else if (bud.warning) warnings.push(`${(Number(bud.used_fraction) * 100).toFixed(0)}% of the ${usageMoney(bud.limit_usd, 2)} budget is used.`);
    if (summary.error_calls) warnings.push(`${usageNum(summary.error_calls)} of ${usageNum(summary.total_calls)} calls failed (${(Number(summary.error_rate || 0) * 100).toFixed(1)}%).`);
    if (summary.unknown_price_calls) warnings.push(`${usageNum(summary.unknown_price_calls)} calls used a model with no known price, so their cost is counted as $0. The total is a minimum.`);
    const warnEl = document.getElementById("usageWarnings");
    if (warnEl) {
      warnEl.innerHTML = warnings.map(w => `<div style="background: #fffbeb; border: 1px solid #fde68a; color: #92400e; border-radius: 8px; padding: 8px 12px; margin-bottom: 8px; font-size: 0.84rem;">${escapeHtml(w)}</div>`).join("");
    }

    // By script (the last row, when present, is the calls not linked to any script)
    const scriptTbody = document.getElementById("usageByScriptTable");
    if (scriptTbody) {
      const rows = data.by_script || [];
      scriptTbody.innerHTML = rows.length === 0 ? usageEmptyRow(7, "No calls in this period.") : rows.map(row => {
        const touchpoints = (row.agent_touchpoints || []).map(tp => `
          <span style="display: inline-flex; gap: 4px; background: #f1f5f9; border: 1px solid #cbd5e1; border-radius: 6px; padding: 2px 6px; font-size: 0.72rem; color: #334155;">
            <strong>${escapeHtml(tp.agent)}</strong>: ${usageNum(tp.calls)} calls (${usageNum(tp.total_tokens)} tokens, ${usageMoney(tp.cost_usd, 4)})
          </span>`).join("");
        const unlinked = row.script_id === null || row.script_id === undefined;
        return `
          <tr style="border-bottom: 1px solid #f1f5f9;${unlinked ? " background: #f8fafc;" : ""}">
            <td style="padding: 10px 14px; font-weight: 600; color: #0f172a;">
              <div>${escapeHtml(row.title)}</div>
              ${unlinked ? "" : `<div style="font-size: 0.74rem; color: #64748b;">ID: ${escapeHtml(row.script_id)}</div>`}
              ${touchpoints ? `<div style="display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px;">${touchpoints}</div>` : ""}
            </td>
            <td style="padding: 10px 14px;"><span style="background: rgba(139, 92, 246, 0.12); color: #7c3aed; padding: 2px 8px; border-radius: 12px; font-size: 0.76rem; font-weight: 600;">${escapeHtml(row.status || "-")}</span></td>
            <td style="padding: 10px 14px; color: #475569;">${usageNum(row.calls)}</td>
            <td style="padding: 10px 14px; color: #0284c7; font-family: monospace;">${usageNum(row.prompt_tokens)}</td>
            <td style="padding: 10px 14px; color: #10b981; font-family: monospace;">${usageNum(row.completion_tokens)}</td>
            <td style="padding: 10px 14px; font-family: monospace; font-weight: 600; color: #334155;">${usageNum(row.total_tokens)}</td>
            <td style="padding: 10px 14px; font-weight: 700; color: #e11d48; font-family: monospace;">${usageMoney(row.cost_usd, 5)}</td>
          </tr>`;
      }).join("");
    }

    // By agent
    const agentTbody = document.getElementById("usageByAgentTable");
    if (agentTbody) {
      const rows = data.by_agent || [];
      agentTbody.innerHTML = rows.length === 0 ? usageEmptyRow(6, "No calls in this period.") : rows.map(row => `
        <tr style="border-bottom: 1px solid #f1f5f9;">
          <td style="padding: 10px 14px; font-weight: 600; color: #1e293b;">${escapeHtml(row.agent || "general")}</td>
          <td style="padding: 10px 14px; color: #475569;">${usageNum(row.calls)}</td>
          <td style="padding: 10px 14px; color: ${row.errors ? "#dc2626" : "#94a3b8"};">${usageNum(row.errors)}</td>
          <td style="padding: 10px 14px; color: #0284c7; font-family: monospace;">${usageNum(row.prompt_tokens)}</td>
          <td style="padding: 10px 14px; color: #10b981; font-family: monospace;">${usageNum(row.completion_tokens)}</td>
          <td style="padding: 10px 14px; font-weight: 700; color: #e11d48; font-family: monospace;">${usageMoney(row.cost_usd, 5)}</td>
        </tr>`).join("");
    }

    // By model
    const sourceLabel = { provider: "Provider's figure", table: "Price list", unknown: "No price known" };
    const modelTbody = document.getElementById("usageByModelTable");
    if (modelTbody) {
      const rows = data.by_model || [];
      modelTbody.innerHTML = rows.length === 0 ? usageEmptyRow(6, "No calls in this period.") : rows.map(row => `
        <tr style="border-bottom: 1px solid #f1f5f9;">
          <td style="padding: 10px 14px; color: #475569;">${escapeHtml(row.provider)}</td>
          <td style="padding: 10px 14px; font-family: monospace; font-size: 0.78rem; color: #1e293b;">${escapeHtml(row.model)}</td>
          <td style="padding: 10px 14px; color: #475569;">${usageNum(row.calls)}</td>
          <td style="padding: 10px 14px; font-family: monospace;">${usageNum(row.total_tokens)}</td>
          <td style="padding: 10px 14px; font-weight: 700; color: #e11d48; font-family: monospace;">${usageMoney(row.cost_usd, 5)}</td>
          <td style="padding: 10px 14px; color: ${row.cost_source === "unknown" ? "#b45309" : "#64748b"};">${escapeHtml(sourceLabel[row.cost_source] || row.cost_source)}</td>
        </tr>`).join("");
    }

    // Prices in use, taken from the server so the page never shows stale numbers
    const seenPrices = new Set();
    const priced = (data.pricing || []).filter(p => {
      if (p.input_per_million === null || p.input_per_million === undefined || seenPrices.has(p.model)) return false;
      seenPrices.add(p.model);
      return true;
    });
    usageSetText("usagePricingNote", priced.length
      ? "Prices per 1M tokens (input/output): " + priced.map(p => `${p.model} $${p.input_per_million}/$${p.output_per_million}`).join(", ")
      : "");

    // Recent calls
    const recentTbody = document.getElementById("usageRecentTable");
    if (recentTbody) {
      const rows = data.recent_logs || [];
      recentTbody.innerHTML = rows.length === 0 ? usageEmptyRow(7, "No calls in this period.") : rows.map(log => {
        const when = log.created_at ? parseServerTime(log.created_at).toLocaleString() : "--";
        const ok = log.status === "success";
        const badge = ok
          ? `<span style="background: rgba(16, 185, 129, 0.15); color: #059669; padding: 2px 6px; border-radius: 4px; font-weight: 600;">OK</span>`
          : `<span title="${escapeHtml(log.error_message || "")}" style="background: rgba(239, 68, 68, 0.15); color: #dc2626; padding: 2px 6px; border-radius: 4px; font-weight: 600;">ERR</span>`;
        return `
          <tr style="border-bottom: 1px solid #f8fafc;">
            <td style="padding: 8px 12px; color: #64748b; white-space: nowrap;">${escapeHtml(when)}</td>
            <td style="padding: 8px 12px; font-weight: 600; color: #334155;">${escapeHtml(log.agent || "general")}</td>
            <td style="padding: 8px 12px; color: #475569; font-family: monospace; font-size: 0.78rem;">${escapeHtml(log.model)}</td>
            <td style="padding: 8px 12px; font-family: monospace;">${usageNum(log.total_tokens)}</td>
            <td style="padding: 8px 12px; color: #64748b;">${escapeHtml(log.latency_seconds)}s</td>
            <td style="padding: 8px 12px; font-weight: 600; color: #e11d48;">${usageMoney(log.estimated_cost_usd, 5)}</td>
            <td style="padding: 8px 12px;">${badge}${ok ? "" : `<div style="font-size: 0.72rem; color: #b91c1c; margin-top: 2px;">${escapeHtml(log.error_message || "")}</div>`}</td>
          </tr>`;
      }).join("");
    }
  } catch (err) {
    console.error("Failed loading API usage:", err);
    // Say so in the open dialog; never show zeros as if they were real figures
    if (modalOpen) usageShowError(`Could not load usage figures. ${err.message || err}`);
    usageSetText("apiUsageBadge", "n/a");
  }
}

// Refresh the header badge every 60 seconds
setInterval(loadApiUsageData, 60000);

// Initial fetch on boot
setTimeout(loadApiUsageData, 1500);




// The sign-in (home) screen shows no API usage and no theme switch: they appear once someone is signed in, on every profile's screen.
(function keepHeaderControlsOffTheSignInScreen() {
  const home = document.getElementById("homeDashboardView");
  if (!home) return;
  const sync = () => document.body.classList.toggle("on-home", home.classList.contains("active") && !home.classList.contains("hidden"));
  new MutationObserver(sync).observe(home, { attributes: true, attributeFilter: ["class"] });
  sync();
})();
