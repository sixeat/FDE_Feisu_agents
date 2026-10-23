(() => {
  window.fdeLocalDemo = true;
  const allowed = new Set(["agents", "audit"]);
  const normalizeHash = () => {
    const view = location.hash.slice(1);
    if (!allowed.has(view) || (view === "audit" && document.getElementById("module-audit")?.hidden))
      history.replaceState(null, "", "#agents");
  };
  normalizeHash();
  window.addEventListener("hashchange", normalizeHash);
  window.addEventListener("popstate", normalizeHash);
  document.addEventListener("DOMContentLoaded", () => {
    for (const view of ["meetings", "owner-tasks", "organizer-tasks", "follow-up", "members"])
      document.getElementById(`module-${view}`).hidden = true;
    const account = document.querySelector(".topbar .account");
    account.textContent = "本地演练";
    const link = document.createElement("a");
    link.href = "/local-demo";
    link.className = "local-demo-switch";
    link.textContent = "切换身份";
    document.getElementById("logout").before(link);
    const members = document.getElementById("module-members");
    new MutationObserver(() => { if (!members.hidden) members.hidden = true; })
      .observe(members, {attributes: true, attributeFilter: ["hidden"]});
    new MutationObserver(normalizeHash).observe(document.getElementById("module-audit"),
      {attributes: true, attributeFilter: ["hidden"]});
    normalizeHash();
  });
})();
