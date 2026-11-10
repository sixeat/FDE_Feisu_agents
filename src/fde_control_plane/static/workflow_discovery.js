"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const state = { csrf: "", subscriptions: [], opportunities: [], busy: false };
  const statusNames = {
    ACTIVE: "订阅中", REVOKED: "已撤销", DISCOVERED: "待人工判断",
    ACCEPTED: "已采纳", REJECTED: "已拒绝", DEFERRED: "已暂缓",
    AGENT_DRAFT_CREATED: "已生成草稿", SOURCE_REVOKED: "来源已撤销",
  };
  const typeNames = { GROUP: "群聊", P2P: "单聊" };
  const api = async (path, options = {}) => {
    const response = await fetch(path, {
      credentials: "same-origin", ...options,
      headers: { ...(options.body ? { "Content-Type": "application/json" } : {}),
        ...(options.headers || {}) },
    });
    let payload;
    try { payload = await response.json(); } catch { payload = {}; }
    if (!response.ok) {
      const detail = typeof payload.detail === "string" ? payload.detail.toLowerCase() : "";
      if (response.status === 403 && (path.includes("/scan") || path.includes("/evidence-preview"))
          && (detail.includes("authorization") || detail.includes("chat-read permission"))) {
        throw new Error("成员聊天授权缺失或已过期。请点击“重新授权聊天读取”，完成授权后刷新状态再试。");
      }
      if (response.status === 503 && (path.includes("/scan") || path.includes("/evidence-preview"))
          && detail.startsWith("feishu_chat_read_")) {
        throw new Error("读取未完成。请刷新授权状态；若显示需授权，请重新授权。若授权有效，请确认你仍可访问该会话后重试。本次没有生成新候选。");
      }
      throw new Error(payload.detail || `请求失败（${response.status}）`);
    }
    return payload;
  };
  function notice(message, kind = "") {
    const panel = $("workflow-notice");
    panel.textContent = message;
    panel.className = kind;
    panel.hidden = !message;
  }
  function button(label, action, kind = "") {
    const item = document.createElement("button");
    item.type = "button";
    item.textContent = label;
    if (kind) item.className = kind;
    item.addEventListener("click", action);
    return item;
  }
  function formatConsentTime(value) {
    if (typeof value !== "string") return "时间未记录";
    const date = new Date(`${value.replace(" ", "T")}Z`);
    return Number.isNaN(date.getTime()) ? "时间未记录" : date.toLocaleString("zh-CN", { hour12: false });
  }
  function formatEvidenceTime(value) {
    if (typeof value !== "string") return "时间未记录";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? "时间未记录" : date.toLocaleString("zh-CN", { hour12: false });
  }
  function renderSubscriptions() {
    const panel = $("workflow-subscriptions");
    panel.replaceChildren();
    if (!state.subscriptions.length) {
      panel.append(Object.assign(document.createElement("p"), {
        className: "muted", textContent: "尚未订阅聊天来源。",
      }));
      return;
    }
    for (const source of state.subscriptions) {
      const row = document.createElement("article");
      row.className = "workflow-row";
      const details = document.createElement("div");
      details.className = "workflow-row-details";
      const title = document.createElement("h3");
      title.textContent = source.display_label;
      const meta = document.createElement("p");
      meta.className = "muted";
      meta.textContent = `${typeNames[source.chat_type] || source.chat_type} · ${statusNames[source.status] || source.status}`;
      const consent = document.createElement("p");
      consent.className = "muted";
      const version = /^workflow-source-v(\d+)$/.exec(source.consent_version || "");
      consent.textContent = `读取说明 ${version ? `v${version[1]}` : "未记录版本"} · 同意时间 ${formatConsentTime(source.consented_at)}`;
      const consentOutdated = source.consent_version !== $("workflow-consent").dataset.consentVersion;
      if (source.status === "ACTIVE" && consentOutdated) {
        consent.textContent += " · 需重新确认后才能扫描";
      }
      details.append(title, meta, consent);
      const actions = document.createElement("div");
      actions.className = "commands workflow-actions";
      if (source.status === "ACTIVE") {
        if (consentOutdated) {
          actions.append(button("重新确认读取说明", () => renewConsent(source.subscription_id)));
        } else {
          actions.append(button("扫描最近 24 小时", () => scan(source.subscription_id)));
        }
        actions.append(button("撤销订阅", () => revoke(source.subscription_id)));
      }
      row.append(details, actions);
      panel.append(row);
    }
  }
  function renderAuthorizationStatus(status) {
    const panel = $("workflow-chat-auth-status");
    const group = status.group_chat ? "已授权" : "需授权";
    const p2p = status.p2p_chat ? "已授权" : "需授权";
    panel.textContent = `聊天读取授权：群聊 ${group} · 单聊 ${p2p}`;
    panel.classList.toggle("needs-authorization", !status.group_chat || !status.p2p_chat);
  }
  function renderOpportunity(item) {
    const card = document.createElement("article");
    card.className = "workflow-row workflow-opportunity";
    const head = document.createElement("div");
    head.className = "workflow-row-head";
    const title = document.createElement("h3");
    title.textContent = `${item.source_label} · 重复文本候选`;
    const badge = document.createElement("span");
    badge.className = "badge";
    badge.textContent = statusNames[item.status] || item.status;
    head.append(title, badge);
    const meta = document.createElement("p");
    meta.className = "muted";
    meta.textContent = `相同文本出现 ${item.occurrence_count} 次 · ${item.pattern_type} · ${item.evidence_refs.length} 条脱敏证据引用`;
    const timing = document.createElement("p");
    timing.className = "muted";
    timing.textContent = `首次出现 ${formatEvidenceTime(item.first_seen)} · 最近出现 ${formatEvidenceTime(item.last_seen)}`;
    card.append(head, meta, timing);
    if (item.source_active && item.source_consent_current) {
      const preview = document.createElement("div");
      preview.className = "workflow-preview";
      preview.hidden = true;
      const previewButton = button("查看匹配证据摘录", () => previewEvidence(item, preview, previewButton));
      card.append(previewButton, preview);
    }
    if (item.decision_reason) {
      const reason = document.createElement("p");
      reason.className = "workflow-reason";
      reason.textContent = `人工意见：${item.decision_reason}`;
      card.append(reason);
    }
    if (item.status === "DISCOVERED") {
      const reasonLabel = document.createElement("label");
      reasonLabel.className = "field workflow-reason-field";
      const caption = document.createElement("span");
      caption.textContent = "判断理由（5 至 500 字）";
      const input = document.createElement("textarea");
      input.rows = 2;
      input.minLength = 5;
      input.maxLength = 500;
      input.required = true;
      input.placeholder = "说明为什么值得转成 Agent，或为什么暂不处理";
      reasonLabel.append(caption, input);
      const actions = document.createElement("div");
      actions.className = "commands workflow-actions";
      for (const [label, decision] of [["采纳", "ACCEPTED"], ["暂缓", "DEFERRED"], ["拒绝", "REJECTED"]]) {
        actions.append(button(label, async () => {
          const reason = input.value.trim();
          if (reason.length < 5) {
            input.setCustomValidity("请填写至少 5 个字的判断理由。");
            input.reportValidity();
            return;
          }
          input.setCustomValidity("");
          await mutateOpportunity(item.opportunity_id, decision, reason);
        }, decision === "ACCEPTED" ? "primary" : ""));
      }
      card.append(reasonLabel, actions);
    }
    if (item.status === "ACCEPTED") {
      const form = document.createElement("form");
      form.className = "workflow-draft-form";
      const nameLabel = document.createElement("label");
      nameLabel.className = "field";
      const nameText = document.createElement("span"); nameText.textContent = "Agent 名称";
      const name = document.createElement("input"); name.required = true; name.minLength = 2; name.maxLength = 100;
      name.value = `${item.source_label}流程 Agent`;
      nameLabel.append(nameText, name);
      const descLabel = document.createElement("label");
      descLabel.className = "field";
      const descText = document.createElement("span"); descText.textContent = "职责说明";
      const description = document.createElement("textarea"); description.required = true; description.maxLength = 2000; description.rows = 3;
      description.value = "整理重复出现的流程信息并给出待确认建议；不申请工具权限，所有后续执行须经过独立评测和人工审核。";
      descLabel.append(descText, description);
      const submit = document.createElement("button"); submit.type = "submit"; submit.className = "primary"; submit.textContent = "生成待审核草稿";
      form.append(nameLabel, descLabel, submit);
      form.addEventListener("submit", async event => {
        event.preventDefault();
        await createDraft(item.opportunity_id, name.value.trim(), description.value.trim());
      });
      card.append(form);
    }
    if (item.status === "AGENT_DRAFT_CREATED" && item.agent_draft_id) {
      const detail = document.createElement("p");
      detail.className = "muted";
      detail.textContent = `已关联 Agent 配置草稿 ${item.agent_draft_id}，状态为待审核。`;
      card.append(detail, button("查看 Agent 草稿", () => $("module-agents").click()));
    }
    return card;
  }
  function renderOpportunities() {
    const panel = $("workflow-opportunities");
    panel.replaceChildren();
    if (!state.opportunities.length) {
      const empty = document.createElement("p");
      empty.className = "muted";
      empty.textContent = "还没有流程候选。登记来源并手动扫描后，重复信号会显示在这里。";
      panel.append(empty);
      return;
    }
    state.opportunities.forEach(item => panel.append(renderOpportunity(item)));
  }
  async function refresh() {
    const [subscriptions, opportunities, authorization] = await Promise.all([
      api("/api/workflow-opportunities/subscriptions"),
      api("/api/workflow-opportunities"),
      api("/api/workflow-opportunities/authorization"),
    ]);
    state.csrf = subscriptions.csrf_token;
    state.subscriptions = subscriptions.subscriptions;
    state.opportunities = opportunities.opportunities;
    renderAuthorizationStatus(authorization.chat_read);
    renderSubscriptions();
    renderOpportunities();
  }
  async function mutate(path, options) {
    return api(path, { ...options, headers: { "X-CSRF-Token": state.csrf, ...(options.headers || {}) } });
  }
  async function run(action, success) {
    if (state.busy) return false;
    state.busy = true;
    $("workflow-reload").disabled = true;
    try {
      const message = await action();
      await refresh();
      notice(typeof success === "function" ? success(message) : success, "success");
      return true;
    } catch (error) {
      notice(error.message, "error");
      return false;
    } finally {
      state.busy = false;
      $("workflow-reload").disabled = false;
    }
  }
  async function scan(id) {
    await run(() => mutate(`/api/workflow-opportunities/subscriptions/${encodeURIComponent(id)}/scan`, { method: "POST" }),
      result => `扫描完成：读取 ${result.messages_scanned} 条消息，发现 ${result.opportunity_ids.length} 个候选。`);
  }
  async function previewEvidence(item, panel, control) {
    if (state.busy) return;
    state.busy = true;
    control.disabled = true;
    panel.hidden = false;
    panel.replaceChildren();
    const pending = document.createElement("p");
    pending.textContent = "正在重新读取最近 24 小时的匹配证据…";
    panel.append(pending);
    try {
      const result = await mutate(`/api/workflow-opportunities/${encodeURIComponent(item.opportunity_id)}/evidence-preview`, {
        method: "POST", cache: "no-store",
      });
      panel.replaceChildren();
      if (!result.samples.length) {
        const empty = document.createElement("p");
        empty.textContent = "最近 24 小时没有可重新核对的匹配消息；历史正文未保存。";
        panel.append(empty);
      }
      for (const sample of result.samples) {
        const excerpt = document.createElement("p");
        excerpt.textContent = `${sample.created_at} · 证据 ${sample.evidence_ref} · ${sample.excerpt}`;
        panel.append(excerpt);
      }
    } catch (error) {
      panel.replaceChildren();
      const failure = document.createElement("p");
      failure.textContent = error.message;
      panel.append(failure);
    } finally {
      state.busy = false;
      control.disabled = false;
    }
  }
  async function renewConsent(id) {
    const checkbox = $("workflow-consent");
    if (!checkbox.checked) {
      notice("请先勾选页面上当前版本的读取说明，再为该订阅重新确认。", "error");
      checkbox.focus();
      return;
    }
    await run(() => mutate(`/api/workflow-opportunities/subscriptions/${encodeURIComponent(id)}/consent`, {
      method: "POST",
      body: JSON.stringify({ consent_confirmed: true, consent_version: checkbox.dataset.consentVersion }),
    }), "读取说明已重新确认，原订阅仍有效，可以继续扫描。");
  }
  async function revoke(id) {
    await run(() => mutate(`/api/workflow-opportunities/subscriptions/${encodeURIComponent(id)}`, { method: "DELETE" }),
      "订阅已撤销，后续扫描已停止。");
  }
  async function mutateOpportunity(id, decision, reason) {
    await run(() => mutate(`/api/workflow-opportunities/${encodeURIComponent(id)}/decision`, {
      method: "POST", body: JSON.stringify({ decision, reason }),
    }), `已记录人工决定：${statusNames[decision]}。`);
  }
  async function createDraft(id, name, description) {
    await run(() => mutate(`/api/workflow-opportunities/${encodeURIComponent(id)}/agent-draft`, {
      method: "POST", body: JSON.stringify({ name, description }),
    }), result => `已生成“${result.name}”配置草稿，状态为待审核；未授予工具权限。`);
  }
  async function loadWorkflowWorkspace() {
    if (state.busy) return;
    try { await refresh(); notice(""); }
    catch (error) { notice(error.message, "error"); }
  }
  $("workflow-subscribe-form").addEventListener("submit", event => {
    event.preventDefault();
    if (!$("workflow-consent").checked) return;
    const payload = {
      chat_id: $("workflow-chat-id").value.trim(),
      chat_type: $("workflow-chat-type").value,
      display_label: $("workflow-chat-label").value.trim(),
      consent_confirmed: $("workflow-consent").checked,
      consent_version: $("workflow-consent").dataset.consentVersion,
    };
    void run(() => mutate("/api/workflow-opportunities/subscriptions", {
      method: "POST", body: JSON.stringify(payload),
    }), "来源订阅已登记。现在可以手动扫描最近 24 小时。").then(ok => {
      if (ok) $("workflow-subscribe-form").reset();
    });
  });
  $("workflow-reload").addEventListener("click", () => void run(refresh, "订阅和候选已刷新。"));
  $("workflow-reauthorize").addEventListener("click", () => {
    window.location.assign("/oauth/start");
  });
  if (window.fdeLocalDemo) {
    $("workflow-demo-banner").hidden = false;
    $("workflow-reauthorize").hidden = true;
    $("workflow-chat-auth-status").hidden = true;
  }
  window.loadWorkflowWorkspace = loadWorkflowWorkspace;
  window.addEventListener("hashchange", () => {
    if (location.hash === "#workflow-discovery") void loadWorkflowWorkspace();
  });
})();
