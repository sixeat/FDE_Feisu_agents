"use strict";
const $ = id => document.getElementById(id);
const state = { csrf: "", members: [], detail: null, dirty: false, busy: false };
const ownerState = {csrf:"", tasks:[], busy:false, drafts:new Map(), retries:new Map()};
const organizerState = {csrf:"", tasks:[], busy:false};
const agentState = {agents:[], drafts:[], memberRequests:[], csrf:"", canPrecheck:false, canConfirm:false, busy:false};
const viewNames={meetings:"会议审核","owner-tasks":"我的待办","organizer-tasks":"发起的任务","follow-up":"跟进概览",agents:"Agent 管理"};
let activeView="";
const ownerStatusNames = {WAITING_OWNER:"等待你确认",IN_PROGRESS:"已接受，进行中",WAITING_HUMAN:"已退回，等待会议发起人处理",COMPLETED:"已完成",CANCELLED:"已取消",UNKNOWN:"等待核验",DUE_SOON:"即将到期",OVERDUE:"已逾期"};
const taskNames = {WAITING_REVIEW:"待核对",WAITING_APPROVAL:"待审批",CANCELLED:"已取消",RUNNING:"执行中",RECONCILING:"待核验",SUCCEEDED:"已完成",PARTIAL_SUCCESS:"部分完成",FAILED:"执行失败"};
const reasonNames = {ASSIGNEE_UNMAPPED:"负责人待确认",ASSIGNEE_INVALID:"负责人不可用",DUE_DATE_UNCONFIRMED:"截止日期待确认",DUE_DATE_INVALID:"截止日期格式无效",TITLE_MISSING:"标题待确认",POSSIBLE_DUPLICATE_OR_CONFLICT:"存在关联或冲突，需人工裁定",DISCARDED_BY_REVIEWER:"已舍弃"};
const executionNames = {PREPARED:"待执行",DISPATCHED:"执行中",SUCCEEDED:"已创建",FAILED:"已阻断/失败",UNKNOWN:"结果待核验",RECONCILING:"结果待核验"};
const agentTypeNames = {PERSONAL_ASSISTANT:"个人助手",BUSINESS_AGENT:"业务 Agent",MANAGEMENT_AGENT:"管理 Agent"};
const agentStatusNames = {ACTIVE:"运行中",PAUSED:"已暂停",UNVERSIONED:"待接入"};
const draftStatusNames = {PENDING_REVIEW:"待审核",APPROVED:"已批准",REJECTED:"已拒绝",PUBLISHED:"已发布"};
const reviewStatusNames = {READY_FOR_ADMIN:"预审通过，待管理员确认",BLOCKED:"预审阻断"};
function node(tag, text, cls) { const el=document.createElement(tag); if(text!==undefined) el.textContent=text; if(cls) el.className=cls; return el; }
function notice(text, type="") { $("notice").textContent=text; $("notice").className=type; $("notice").hidden=!text; }
function ownerNotice(text,type="") { const panel=$("owner-notice");panel.textContent=text;panel.className=type;panel.hidden=!text; }
function setOwnerBusy(value) {
  ownerState.busy=value;$("owner-reload").disabled=value;
  $("owner-task-list").setAttribute("aria-busy",String(value));
  $("owner-task-list").querySelectorAll("button,textarea").forEach(el=>el.disabled=value);
}
function ownerDraftKey(task) {return JSON.stringify([task.task_ref,task.source_revision]);}
function renderOwnerTasks() {
  const panel=$("owner-task-list");panel.replaceChildren();
  if(!ownerState.tasks.length){panel.append(node("p","暂无分配给你的任务。","muted"));return;}
  ownerState.tasks.forEach(task=>{
    const card=node("article",undefined,"owner-task"),head=node("div",undefined,"owner-task-head");
    const label=ownerStatusNames[task.status]||(task.decision==="ACCEPTED"?"已接受":task.decision==="RETURNED"?"已退回":"等待核验");
    head.append(node("h3",task.title||"未命名任务"),node("span",label,`badge ${task.decision==="ACCEPTED"?"ready":task.decision==="RETURNED"?"closed":""}`));
    card.append(head,node("p",`截止日期：${task.due_date||"待确认"}`,"muted"));
    if(task.decision)card.append(node("p",task.decision==="ACCEPTED"?"你的决定：已接受":"你的决定：已退回","owner-decision"));
    if(task.due_mismatch)card.append(node("p","截止日期与飞书任务不一致，需发起人核验。","owner-warning"));
    if(task.reason)card.append(node("p",`退回原因：${task.reason}`,"owner-reason"));
    if(task.can_decide) {
      const reasonLabel=node("label",undefined,"field"),reason=node("textarea"),key=ownerDraftKey(task);
      reasonLabel.append(node("span","退回原因（退回时必填，最多 1000 字）"));
      reason.rows=2;reason.maxLength=1000;reason.value=ownerState.drafts.get(key)||"";
      reason.addEventListener("input",()=>{reason.setCustomValidity("");ownerState.drafts.set(key,reason.value);});
      reasonLabel.append(reason);card.append(reasonLabel);
      const commands=node("div",undefined,"commands"),accept=node("button","接受任务","primary"),reject=node("button","退回任务");
      accept.type="button";reject.type="button";
      accept.addEventListener("click",()=>submitOwnerDecision(task,"ACCEPTED",reason));
      reject.addEventListener("click",()=>submitOwnerDecision(task,"RETURNED",reason));
      commands.append(reject,accept);card.append(commands);
    } else if(!task.decision&&!["COMPLETED","CANCELLED"].includes(task.status))card.append(node("p","当前任务暂不能确认，请等待状态核验后刷新。","muted"));
    panel.append(card);
  });
}
async function fetchOwnerTasks() {
  const data=await api("/api/meetings/owner-tasks");ownerState.csrf=data.csrf_token;ownerState.tasks=data.tasks;renderOwnerTasks();
}
async function refreshDigest() {
  try{renderDigest(await api("/api/meetings/digest"));}
  catch(error){const panel=$("follow-up-digest");panel.hidden=false;panel.textContent=error.message;panel.className="digest error";}
}
async function loadOwnerTasks() {
  if(ownerState.busy)return;setOwnerBusy(true);
  try{await fetchOwnerTasks();}catch(error){ownerNotice(error.message,"error");if(!ownerState.tasks.length)$("owner-task-list").replaceChildren(node("p","任务暂时无法加载，请稍后刷新。","muted"));}
  finally{setOwnerBusy(false);}
}
async function submitOwnerDecision(task,decision,reasonInput) {
  if(ownerState.busy||!task.can_decide)return;
  const reason=decision==="RETURNED"?reasonInput.value.trim():null;
  if(decision==="RETURNED"&&(!reason||reason.length>1000)) {
    reasonInput.setCustomValidity("请填写 1 至 1000 字的退回原因。");reasonInput.reportValidity();return;
  }
  const key=ownerDraftKey(task),signature=JSON.stringify([decision,reason]);
  let retry=ownerState.retries.get(key);
  if(!retry||retry.signature!==signature){retry={signature,eventId:crypto.randomUUID()};ownerState.retries.set(key,retry);}
  setOwnerBusy(true);ownerNotice("正在保存你的决定…");
  try {
    await api(`/api/meetings/owner-tasks/${encodeURIComponent(task.task_ref)}/decision`,{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":ownerState.csrf},body:JSON.stringify({event_id:retry.eventId,source_revision:task.source_revision,decision,reason})});
    task.can_decide=false;task.decision=decision;task.reason=reason;task.status=decision==="ACCEPTED"?"IN_PROGRESS":"WAITING_HUMAN";
    ownerState.drafts.delete(key);ownerState.retries.delete(key);renderOwnerTasks();setOwnerBusy(true);
    const success=decision==="ACCEPTED"?"已接受任务。":"已退回任务，等待会议发起人处理。";
    ownerNotice(success,"success");
    try{await fetchOwnerTasks();}catch(error){ownerNotice(success+" 列表刷新失败，请稍后刷新任务。","success");}
  } catch(error){ownerNotice(error.message,"error");}
  finally{setOwnerBusy(false);}
}
function organizerNotice(message,type="") {
  const panel=$("organizer-notice");panel.textContent=message;panel.className=type;panel.hidden=!message;
}
function renderOrganizerTasks() {
  const panel=$("organizer-task-list");panel.replaceChildren();
  if(!organizerState.tasks.length){panel.append(node("p","暂无已创建的会议任务。","muted"));return;}
  organizerState.tasks.forEach(task=>{
    const card=node("article",undefined,"owner-task"),head=node("div",undefined,"owner-task-head");
    head.append(node("h3",task.title||"会议待办"),node("span",task.due_mismatch?"日期待核验":"日期已核对",`badge ${task.due_mismatch?"":"ready"}`));
    card.append(head,node("p",`批准日期：${task.approved_due_date||"待确认"} · 飞书日期：${task.remote_due_date||"未知"}`,"muted"));
    if(task.correction)card.append(node("p",`改期提案：${({PENDING:"等待你确认",APPROVED:"已确认，等待执行",DISPATCHED:"执行中",UNKNOWN:"结果待对账",SUCCEEDED:"已回查成功",FAILED:"已阻断",INVALIDATED:"已失效"})[task.correction.status]||task.correction.status}`,"owner-warning"));
    if(task.can_propose_correction){
      const button=node("button","提出日期修正");button.type="button";
      button.addEventListener("click",()=>proposeOrganizerCorrection(task));card.append(button);
    } else if(task.correction?.status==="PENDING") {
      const button=node("button","确认按批准日期修正","primary");button.type="button";
      button.addEventListener("click",()=>approveOrganizerCorrection(task));card.append(button);
    } else if(task.due_mismatch&&!task.correction)card.append(node("p","当前状态不能自动修正，请先核验飞书任务。","muted"));
    panel.append(card);
  });
}
async function loadOrganizerTasks() {
  if(organizerState.busy)return;organizerState.busy=true;$("organizer-reload").disabled=true;
  $("organizer-task-list").setAttribute("aria-busy","true");
  try{const data=await api("/api/meetings/organizer-tasks");organizerState.csrf=data.csrf_token;organizerState.tasks=data.tasks;renderOrganizerTasks();}
  catch(error){organizerNotice(error.message,"error");}
  finally{organizerState.busy=false;$("organizer-reload").disabled=false;$("organizer-task-list").setAttribute("aria-busy","false");}
}
async function proposeOrganizerCorrection(task) {
  if(organizerState.busy)return;
  if(!window.confirm(`为这条任务提出日期修正？\n飞书当前日期：${task.remote_due_date}\n原批准日期：${task.approved_due_date}`))return;
  organizerState.busy=true;organizerNotice("正在保存改期提案…");
  try{
    await api(`/api/meetings/organizer-tasks/${encodeURIComponent(task.task_ref)}/due-correction`,{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":organizerState.csrf},body:JSON.stringify({source_revision:task.source_revision,observation_id:task.observation_id})});
    organizerNotice("提案已保存。请再次核对两个日期，然后明确确认。","success");
  }catch(error){organizerNotice(error.message,"error");}
  finally{organizerState.busy=false;await loadOrganizerTasks();}
}
async function approveOrganizerCorrection(task) {
  if(organizerState.busy)return;
  if(!window.confirm(`确认后，独立 Worker 才能把飞书任务截止日期从 ${task.remote_due_date} 改为 ${task.approved_due_date}。确定批准？`))return;
  organizerState.busy=true;organizerNotice("正在记录你的确认…");
  try{
    await api(`/api/meetings/organizer-tasks/${encodeURIComponent(task.task_ref)}/due-correction/${encodeURIComponent(task.correction.correction_id)}/approve`,{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":organizerState.csrf},body:JSON.stringify({proposal_hash:task.correction.proposal_hash})});
    organizerNotice("已确认修正，等待受控执行。","success");
  }catch(error){organizerNotice(error.message,"error");}
  finally{organizerState.busy=false;await loadOrganizerTasks();}
}
function agentNotice(message,type="") {
  const panel=$("agents-notice");panel.textContent=message;panel.className=type;panel.hidden=!message;
}
function renderAgents() {
  const panel=$("agent-list");panel.replaceChildren();
  if(!agentState.agents.length){panel.append(node("p","当前租户还没有可展示的 Agent。","muted"));return;}
  agentState.agents.forEach(agent=>{
    const card=node("article",undefined,"agent-row"),head=node("div",undefined,"agent-row-head");
    const status=agentStatusNames[agent.status]||agent.status;
    head.append(node("h3",agent.agent_id),node("span",status,`badge ${agent.status==="ACTIVE"?"ready":"closed"}`));
    card.append(head,node("p",`${agentTypeNames[agent.actor_type]||agent.actor_type} · 当前版本 ${agent.current_version==null?"未发布":`v${agent.current_version}`} · 历史版本 ${agent.version_count}`,"muted"));
    if(agent.capabilities.length)card.append(node("p",`能力：${agent.capabilities.join("、")}`,"agent-capabilities"));
    if(agent.skill_version)card.append(node("p",`Skill：${agent.skill_version}`,"muted"));
    panel.append(card);
  });
}
function renderAgentDrafts() {
  const panel=$("agent-draft-list");panel.replaceChildren();
  if(!agentState.drafts.length){panel.append(node("p","暂无配置草稿。","muted"));return;}
  agentState.drafts.forEach(draft=>{
    const card=node("article",undefined,"agent-draft-row"),head=node("div",undefined,"agent-row-head");
    head.append(node("h3",draft.name),node("span",draftStatusNames[draft.status]||draft.status,`badge ${draft.status==="PUBLISHED"||draft.status==="APPROVED"?"ready":""}`));
    card.append(head,node("p",`${agentTypeNames[draft.actor_type]||draft.actor_type} · 提交时间 ${draft.created_at}${draft.created_by_current_member?" · 我提交":""}`,"muted"),node("p",draft.description,"agent-draft-description"));
    if(draft.capabilities.length)card.append(node("p",`申请能力：${draft.capabilities.join("、")}`,"agent-capabilities"));
    if(draft.review_status){
      card.append(node("p",`审核官预审：${reviewStatusNames[draft.review_status]||draft.review_status}`,`agent-review-status ${draft.review_status==="READY_FOR_ADMIN"?"ready":"blocked"}`));
      if(draft.review_blockers?.length)card.append(node("p",`阻断项：${draft.review_blockers.join("、")}`,"agent-review-findings"));
      if(draft.review_warnings?.length)card.append(node("p",`风险提示：${draft.review_warnings.join("、")}`,"agent-review-findings"));
    }
    if(agentState.canPrecheck && draft.status==="PENDING_REVIEW"){
      const actions=node("div",undefined,"commands agent-review-actions"),button=node("button","运行审核预审");
      button.type="button";button.addEventListener("click",()=>precheckAgentDraft(draft,button));actions.append(button);card.append(actions);
    }
    if(agentState.canConfirm && draft.status!=="PUBLISHED" && draft.review_status==="READY_FOR_ADMIN"){
      const actions=node("div",undefined,"commands agent-review-actions"),button=node("button","管理员确认发布");
      button.type="button";button.className="primary";button.addEventListener("click",()=>confirmAgentDraft(draft,button));actions.append(button);card.append(actions);
    }
    panel.append(card);
  });
}
function renderMemberAccess() {
  const section=$("member-access-panel"),panel=$("member-access-list");section.hidden=!agentState.canConfirm;panel.replaceChildren();
  if(!agentState.canConfirm)return;
  if(!agentState.memberRequests.length){panel.append(node("p","暂无待批准成员。","muted"));return;}
  agentState.memberRequests.forEach(item=>{
    const row=node("article",undefined,"agent-draft-row"),button=node("button","批准接入","primary");
    button.type="button";button.addEventListener("click",()=>approveMemberAccess(item,button));
    row.append(node("h3",item.request_id),node("p",`申请时间：${item.created_at}。请先与该成员确认识别码。`,"muted"),button);panel.append(row);
  });
}
async function approveMemberAccess(item,button) {
  if(agentState.busy)return;
  if(!window.confirm(`已与成员核对识别码 ${item.request_id}？批准后该成员可登录工作台。`))return;
  agentState.busy=true;button.disabled=true;agentNotice("正在批准成员接入…");
  try{
    await api(`/api/meetings/member-access-requests/${encodeURIComponent(item.request_id)}/approve`,{method:"POST",headers:{"X-CSRF-Token":agentState.csrf}});
    agentNotice("成员已绑定。请让该成员重新打开工作台。","success");
    agentState.memberRequests=(await api("/api/meetings/member-access-requests")).requests||[];renderMemberAccess();
  }catch(error){agentNotice(error.message,"error");button.disabled=false;}
  finally{agentState.busy=false;}
}
async function loadAgents() {
  if(agentState.busy)return;agentState.busy=true;$("agents-reload").disabled=true;$("agent-list").setAttribute("aria-busy","true");
  try{const data=await api("/api/meetings/agents");const drafts=await api("/api/meetings/agent-drafts");agentState.agents=data.agents||[];agentState.drafts=drafts.drafts||[];agentState.canPrecheck=Boolean(drafts.can_precheck);agentState.canConfirm=Boolean(drafts.can_confirm);agentState.csrf=drafts.csrf_token;agentState.memberRequests=agentState.canConfirm?(await api("/api/meetings/member-access-requests")).requests||[]:[];renderAgents();renderAgentDrafts();renderMemberAccess();agentNotice("");}
  catch(error){agentNotice(error.message,"error");if(!agentState.agents.length)$("agent-list").replaceChildren(node("p","Agent 目录暂时无法加载，请稍后刷新。","muted"));}
  finally{agentState.busy=false;$("agents-reload").disabled=false;$("agent-list").setAttribute("aria-busy","false");}
}
async function precheckAgentDraft(draft,button) {
  if(agentState.busy)return;
  agentState.busy=true;button.disabled=true;agentNotice(`正在预审“${draft.name}”…`);
  try{const result=await api(`/api/meetings/agent-drafts/${encodeURIComponent(draft.draft_id)}/precheck`,{method:"POST",headers:{"X-CSRF-Token":agentState.csrf}});agentNotice(result.status==="READY_FOR_ADMIN"?"预审通过，等待管理员确认。":"预审已阻断，请处理报告中的问题。",result.status==="READY_FOR_ADMIN"?"success":"error");agentState.busy=false;await loadAgents();}
  catch(error){agentNotice(error.message,"error");button.disabled=false;}
  finally{agentState.busy=false;}
}
async function confirmAgentDraft(draft,button) {
  if(agentState.busy)return;
  if(!window.confirm(`确认发布“${draft.name}”的 Agent v1？这会创建运行版本并启用申请的能力。`))return;
  agentState.busy=true;button.disabled=true;agentNotice(`正在确认发布“${draft.name}”…`);
  try{const result=await api(`/api/meetings/agent-drafts/${encodeURIComponent(draft.draft_id)}/confirm`,{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":agentState.csrf},body:JSON.stringify({review_id:draft.review_id,input_hash:draft.review_input_hash})});agentNotice(result.duplicate?"发布确认已处理，返回原版本。":"Agent 已发布为 v1。","success");agentState.busy=false;await loadAgents();}
  catch(error){agentNotice(error.message,"error");button.disabled=false;}
  finally{agentState.busy=false;}
}
async function submitAgentDraft(event) {
  event.preventDefault();
  if(agentState.busy)return;
  const capabilities=$("agent-draft-capabilities").value.split(",").map(value=>value.trim()).filter(Boolean);
  agentState.busy=true;$("agent-draft-form").querySelectorAll("input,select,textarea,button").forEach(el=>el.disabled=true);agentNotice("正在提交配置草稿…");
  try{
    await api("/api/meetings/agent-drafts",{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":agentState.csrf},body:JSON.stringify({name:$("agent-draft-name").value.trim(),actor_type:$("agent-draft-type").value,description:$("agent-draft-description").value.trim(),capabilities,skill_version:$("agent-draft-skill").value.trim()||null})});
    $("agent-draft-form").reset();$("agent-draft-editor").hidden=true;agentNotice("配置草稿已提交，等待审核官和管理员处理。","success");
    const drafts=await api("/api/meetings/agent-drafts");agentState.drafts=drafts.drafts||[];agentState.canPrecheck=Boolean(drafts.can_precheck);agentState.canConfirm=Boolean(drafts.can_confirm);agentState.csrf=drafts.csrf_token;renderAgentDrafts();
  }catch(error){agentNotice(error.message,"error");}
  finally{agentState.busy=false;$("agent-draft-form").querySelectorAll("input,select,textarea,button").forEach(el=>el.disabled=false);}
}
async function api(path, options={}) {
  const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),15000);
  try {
    const response=await fetch(path,{credentials:"same-origin",...options,signal:controller.signal});
    if(response.status===401) { window.location.assign("/oauth/start"); throw new Error("登录已过期，请重新登录"); }
    if(!response.headers.get("content-type")?.includes("application/json")) throw new Error("服务暂时不可用，请稍后重新加载。");
    const result=await response.json();
    if(!response.ok) throw new Error(typeof result.detail==="string"?result.detail:"请求未通过校验，请核对后重试");
    return result;
  } catch(error) {
    if(error.name==="AbortError") throw new Error("响应超时，请重新加载确认当前状态。");
    if(error instanceof TypeError) throw new Error("暂时无法连接服务，请稍后重新加载。");
    throw error;
  } finally {clearTimeout(timer);}
}
function setBusy(value) { state.busy=value; $("save").disabled=value; $("submit").disabled=value; $("reload").disabled=value; document.querySelectorAll("#meetings button,#module-nav button").forEach(b=>b.disabled=value); }
function recount() {
  const kept=[...document.querySelectorAll(".keep")].filter(el=>el.checked).length;
  $("selection").textContent=`保留 ${kept} 项 · 舍弃 ${state.detail.todos.length-kept} 项`;
}
function renderExecution(execution) {
  const panel=$("execution"); panel.replaceChildren();
  const items=execution?.items||[]; if(!items.length){panel.hidden=true;return;}
  const counts=execution.counts||{};
  panel.hidden=false; panel.append(node("strong","任务执行结果"));
  panel.append(node("span",Object.entries(counts).map(([status,count])=>`${executionNames[status]||status} ${count}`).join(" · "),"execution-summary"));
  const list=node("ul",undefined,"execution-list");
  items.forEach(item=>{
    const label=executionNames[item.status]||item.status;
    const detail=item.error_code?`（${item.error_code}）`:item.remote_ref_present?"（已有远端引用）":"";
    list.append(node("li",label+detail));
  });
  panel.append(list);
}
function renderDigest(digest) {
  const panel=$("follow-up-digest"); panel.replaceChildren(); panel.hidden=false;panel.className="digest";
  const counts=digest?.counts||{};
  const summary=Object.entries(counts).map(([status,count])=>`${status} ${count}`).join(" · ")||"暂无观察记录";
  panel.append(node("strong",`每日跟进预览 · ${digest.business_date}`),node("span",summary));
  panel.append(node("small",`待关注 ${digest.attention_count||0} 项 · 仅预览，未发送消息，未写入飞书任务`));
}
function render(detail) {
  state.detail=detail; state.dirty=false;
  const editable=detail.task_status==="WAITING_REVIEW";
  $("title").textContent=detail.title; $("metadata").textContent=`文档版本 ${detail.revision_id} · ${taskNames[detail.task_status]||detail.task_status}`;
  $("summary").hidden=false; $("summary").replaceChildren(node("strong",`${detail.todos.length} 条待办`),node("span",`来源证据 ${detail.todos.reduce((n,t)=>n+t.evidence.length,0)} 处`));
  renderExecution(detail.execution);
  const container=$("todos"); container.replaceChildren(); container.setAttribute("aria-busy","false");
  detail.todos.forEach((todo,index)=>{
    const card=node("section",undefined,"todo"); card.dataset.ref=todo.todo_ref;
    const head=node("div",undefined,"todo-head"), keepLabel=node("label",undefined,"keep-label"), keep=node("input");
    keep.type="checkbox"; keep.className="keep"; keep.checked=todo.status!=="DISCARDED"; keep.disabled=!editable;
    keepLabel.append(keep,node("span",`待办 ${String(index+1).padStart(2,"0")}`));
    const badge=node("span",todo.status==="DISCARDED"?"已舍弃":todo.status==="READY_FOR_APPROVAL"?"已核对":"待核对",`badge ${todo.status==="READY_FOR_APPROVAL"?"ready":todo.status==="DISCARDED"?"closed":""}`);
    head.append(keepLabel,badge); card.append(head);
    const fields=node("div",undefined,"fields");
    const titleLabel=node("label",undefined,"field title-field"); titleLabel.append(node("span","待办内容"));
    const title=node("textarea"); title.className="todo-title"; title.rows=2; title.maxLength=2000; title.value=todo.title; titleLabel.append(title);
    const assigneeLabel=node("label",undefined,"field"); assigneeLabel.append(node("span","负责人"));
    const assignee=node("select"); assignee.className="assignee"; const placeholder=node("option","待确认"); placeholder.value=""; assignee.append(placeholder);
    state.members.forEach(member=>{const option=node("option",member.name);option.value=member.actor_id;assignee.append(option);}); assignee.value=todo.assignee_actor_id||""; assigneeLabel.append(assignee);
    const dateLabel=node("label",undefined,"field"); dateLabel.append(node("span","截止日期")); const date=node("input");date.type="date";date.className="due-date";date.value=todo.due_date||"";dateLabel.append(date);
    fields.append(titleLabel,assigneeLabel,dateLabel);card.append(fields);
    const reasons=node("ul",undefined,"reasons"); todo.confirmation_reasons.forEach(reason=>reasons.append(node("li",reasonNames[reason]||reason))); if(reasons.children.length)card.append(reasons);
    if(todo.confirmation_reasons.includes("POSSIBLE_DUPLICATE_OR_CONFLICT")) {
      const label=node("label",undefined,"conflict"), check=node("input");check.type="checkbox";check.className="resolve-conflict";label.append(check,node("span","已核对关联事项并完成裁定"));card.append(label);
    }
    const evidence=node("details",undefined,"evidence"); evidence.append(node("summary",`查看来源证据（${todo.evidence.length} 处）`));
    todo.evidence.forEach(item=>{const quote=node("blockquote");quote.append(node("small",`原文段落 ${item.block_id}`),node("span",item.text||"来源证据暂不可用"));evidence.append(quote);});card.append(evidence);
    function syncKeep(){card.classList.toggle("excluded",!keep.checked);fields.querySelectorAll("input,select,textarea").forEach(el=>el.disabled=!editable||!keep.checked);const check=card.querySelector(".resolve-conflict");if(check)check.disabled=!editable||!keep.checked;recount();}
    keep.addEventListener("change",syncKeep);card.addEventListener("input",()=>state.dirty=true);container.append(card);syncKeep();
  });
  $("actions").hidden=!editable; recount();
  if(!editable) {
    if(detail.task_status==="WAITING_APPROVAL") {
      const pending=detail.approvals.filter(a=>a.status==="PENDING").length;
      const approved=detail.approvals.filter(a=>a.status==="APPROVED").length;
      const rejected=detail.approvals.filter(a=>a.status==="REJECTED").length;
      const unknown=detail.approvals.filter(a=>a.card_status==="UNKNOWN").length;
      const sent=detail.approvals.filter(a=>a.card_status==="SENT").length;
      const decisions=approved||rejected?` · 已批准 ${approved} 项 · 已拒绝 ${rejected} 项`:"";
      notice(`已提交，${pending} 项操作待审批。${decisions} 卡片已发送 ${sent} 项${unknown?` · 发送结果待核实 ${unknown} 项`:""}。`,unknown?"error":"success");
    } else notice("本次会议已结束，草稿保留供查阅。","success");
  }
}
async function openMeeting(id) {
  if(state.busy)return;
  if(state.dirty&&!window.confirm("有未保存的修改，确定切换会议？"))return;
  setBusy(true);notice("");
  try{const detail=await api(`/api/meetings/${encodeURIComponent(id)}`);render(detail);document.querySelectorAll("#meetings button").forEach(b=>b.classList.toggle("active",b.dataset.id===id));}
  catch(error){notice(error.message,"error");}finally{setBusy(false);}
}
async function load() {
  if(state.dirty&&!window.confirm("有未保存的修改，确定重新加载？"))return;
  setBusy(true);notice("");
  try {
    const data=await api("/api/meetings");state.csrf=data.csrf_token;state.members=data.members;
    const nav=$("meetings");nav.replaceChildren();
    data.meetings.forEach(item=>{const button=node("button");button.type="button";button.dataset.id=item.record_id;button.append(node("span",item.title),node("small",taskNames[item.status]||item.status));button.addEventListener("click",()=>openMeeting(item.record_id));nav.append(button);});
    if(!data.meetings.length){state.detail=null;$("title").textContent="会议审核";$("metadata").textContent="";$("todos").replaceChildren(node("div","暂无待审核会议","empty"));$("actions").hidden=true;$("summary").hidden=true;$("execution").hidden=true;return;}
    const id=data.meetings.some(m=>m.record_id===state.detail?.record_id)?state.detail.record_id:data.meetings[0].record_id;
    render(await api(`/api/meetings/${encodeURIComponent(id)}`));nav.querySelector(`[data-id="${CSS.escape(id)}"]`)?.classList.add("active");
  }catch(error){notice(error.message,"error");}finally{setBusy(false);$("todos").setAttribute("aria-busy","false");}
}
function edits(){
  const updates={};document.querySelectorAll(".todo").forEach(card=>{
    if(!card.querySelector(".keep").checked){updates[card.dataset.ref]={discard:true};return;}
    const edit={title:card.querySelector(".todo-title").value.trim(),assignee_actor_id:card.querySelector(".assignee").value||null,due_date:card.querySelector(".due-date").value||null};
    if(state.detail.todos.find(t=>t.todo_ref===card.dataset.ref).status==="DISCARDED")edit.discard=false;
    const conflict=card.querySelector(".resolve-conflict");if(conflict?.checked)edit.resolve_conflict=true;
    updates[card.dataset.ref]=edit;
  });return updates;
}
async function persist(submit){
  if(state.busy||!state.detail)return;
  const updates=edits();
  if(submit){
    const kept=Object.values(updates).filter(t=>!t.discard);
    if(kept.some(t=>!t.title||!t.assignee_actor_id||!t.due_date)){notice("请补齐保留待办的内容、负责人和截止日期。","error");return;}
    $("confirm-text").textContent=kept.length?`保留 ${kept.length} 项待办，舍弃 ${state.detail.todos.length-kept.length} 项。确认后进入待审批状态。`:`将舍弃全部 ${state.detail.todos.length} 项待办，并结束本次会议审核。`;
    const decision=await new Promise(resolve=>{const dialog=$("confirm-dialog");dialog.returnValue="cancel";dialog.addEventListener("close",()=>resolve(dialog.returnValue),{once:true});dialog.showModal();});
    if(decision!=="confirm")return;
  }
  setBusy(true);const inputs=[...document.querySelectorAll(".todo input,.todo textarea,.todo select")];inputs.forEach(el=>el.disabled=true);
  try{const detail=await api(`/api/meetings/${encodeURIComponent(state.detail.record_id)}/${submit?"submit":"save"}`,{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":state.csrf},body:JSON.stringify({snapshot:state.detail.snapshot,updates})});render(detail);if(!submit)notice("草稿已保存。","success");const status=$("meetings").querySelector("button.active small");if(status)status.textContent=taskNames[detail.task_status]||detail.task_status;}
  catch(error){notice(error.message,"error");inputs.forEach(el=>el.disabled=false);document.querySelectorAll(".keep").forEach(el=>el.dispatchEvent(new Event("change")));}
  finally{setBusy(false);}
}
$("reload").addEventListener("click",load);$("save").addEventListener("click",()=>persist(false));$("review-form").addEventListener("submit",event=>{event.preventDefault();persist(true);});
window.addEventListener("beforeunload",event=>{if(state.dirty){event.preventDefault();event.returnValue="";}});
$("owner-reload").addEventListener("click",loadOwnerTasks);
$("organizer-reload").addEventListener("click",loadOrganizerTasks);
$("follow-up-reload").addEventListener("click",refreshDigest);
$("agents-reload").addEventListener("click",loadAgents);
$("agent-draft-toggle").addEventListener("click",()=>{const editor=$("agent-draft-editor");editor.hidden=!editor.hidden;if(!editor.hidden)$("agent-draft-name").focus();});
$("agent-draft-cancel").addEventListener("click",()=>{$("agent-draft-form").reset();$("agent-draft-editor").hidden=true;});
$("agent-draft-form").addEventListener("submit",submitAgentDraft);
function showView(view,{updateHash=true}={}) {
  if(!Object.hasOwn(viewNames,view))view="meetings";
  if(view===activeView)return true;
  if(state.dirty&&activeView==="meetings"&&!window.confirm("会议草稿尚未保存，确定切换栏目？")){
    if(updateHash===false)history.replaceState(null,"",`#${activeView}`);
    return false;
  }
  if(state.dirty&&activeView==="meetings")state.dirty=false;
  activeView=view;
  Object.keys(viewNames).forEach(key=>{
    $(`view-${key}`).hidden=key!==view;
    const tab=$(`module-${key}`);tab.classList.toggle("active",key===view);
    tab.setAttribute("aria-selected",String(key===view));tab.tabIndex=key===view?0:-1;
  });
  $("module-label").textContent=viewNames[view];
  $("meeting-context").hidden=view!=="meetings";
  if(updateHash)history.pushState(null,"",`#${view}`);
  if(view==="meetings")load();
  if(view==="owner-tasks")loadOwnerTasks();
  if(view==="organizer-tasks")loadOrganizerTasks();
  if(view==="follow-up")refreshDigest();
  if(view==="agents")loadAgents();
  return true;
}
$("module-nav").querySelectorAll("button").forEach(button=>{
  button.addEventListener("click",()=>showView(button.dataset.view));
  button.addEventListener("keydown",event=>{
    if(!["ArrowUp","ArrowDown","ArrowLeft","ArrowRight"].includes(event.key))return;
    event.preventDefault();const keys=Object.keys(viewNames),index=keys.indexOf(button.dataset.view);
    const next=keys[(index+(event.key==="ArrowUp"||event.key==="ArrowLeft"?-1:1)+keys.length)%keys.length];
    if(showView(next))$(`module-${next}`).focus();
  });
});
window.addEventListener("hashchange",()=>showView(location.hash.slice(1),{updateHash:false}));
window.addEventListener("popstate",()=>showView(location.hash.slice(1),{updateHash:false}));
showView(location.hash.slice(1));
