"use strict";
const $ = id => document.getElementById(id);
const state = { csrf: "", members: [], detail: null, dirty: false, busy: false };
const ownerState = {csrf:"", tasks:[], busy:false, drafts:new Map(), retries:new Map()};
const organizerState = {csrf:"", tasks:[], busy:false};
const agentState = {agents:[], drafts:[], csrf:"", canPrecheck:false, canConfirm:false, canPause:false, canResume:false, busy:false};
const memberState = {requests:[], members:[], csrf:"", busy:false};
const auditState = {events:[], counts:{}, summary:{}, csrf:"", busy:false, canView:false};
const viewNames={meetings:"会议审核","owner-tasks":"我的待办","organizer-tasks":"发起的任务","follow-up":"跟进概览",agents:"Agent 管理",members:"成员与组织",audit:"审计运维"};
let activeView="";
const ownerStatusNames = {WAITING_OWNER:"等待你确认",IN_PROGRESS:"已接受，进行中",WAITING_HUMAN:"已退回，等待会议发起人处理",COMPLETED:"已完成",CANCELLED:"已取消",UNKNOWN:"等待核验",DUE_SOON:"即将到期",OVERDUE:"已逾期"};
const taskNames = {WAITING_REVIEW:"待核对",WAITING_APPROVAL:"待审批",CANCELLED:"已取消",RUNNING:"执行中",RECONCILING:"待核验",SUCCEEDED:"已完成",PARTIAL_SUCCESS:"部分完成",FAILED:"执行失败"};
const reasonNames = {ASSIGNEE_UNMAPPED:"负责人待确认",ASSIGNEE_INVALID:"负责人不可用",DUE_DATE_UNCONFIRMED:"截止日期待确认",DUE_DATE_INVALID:"截止日期格式无效",TITLE_MISSING:"标题待确认",POSSIBLE_DUPLICATE_OR_CONFLICT:"存在关联或冲突，需人工裁定",DISCARDED_BY_REVIEWER:"已舍弃"};
const executionNames = {PREPARED:"待执行",DISPATCHED:"执行中",SUCCEEDED:"已创建",FAILED:"已阻断/失败",UNKNOWN:"结果待核验",RECONCILING:"结果待核验"};
const agentTypeNames = {PERSONAL_ASSISTANT:"个人助手",BUSINESS_AGENT:"业务 Agent",MANAGEMENT_AGENT:"管理 Agent"};
const agentStatusNames = {ACTIVE:"运行中",CANARY:"志愿者灰度",PAUSED:"已暂停",ARCHIVED:"已归档",UNVERSIONED:"待接入"};
const promotionBlockerNames = {AGENT_INACTIVE:"Agent 已停用",OBSERVATION_PERIOD:"观察期不足 60 秒",SAMPLE_COUNT:"灰度样本不足 2 条",RUN_NOT_WAITING_REVIEW:"仍有运行未进入人工核对",RUN_EVIDENCE_MISSING:"运行缺少完成轨迹",RUN_FAILURE:"灰度运行有失败",REVIEW_MISSING:"仍有运行未评审",REVIEW_FAILED:"存在未通过评审",SELF_REVIEW_PROMOTION:"转正管理员不能批准自己的评审",CANDIDATE_SNAPSHOT_MISSING:"候选配置快照缺失"};
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
async function loadOrganizerTasks(force=false) {
  if(organizerState.busy&&!force)return;organizerState.busy=true;$("organizer-reload").disabled=true;
  $("organizer-task-list").setAttribute("aria-busy","true");
  try{const data=await api("/api/meetings/organizer-tasks");organizerState.csrf=data.csrf_token;organizerState.tasks=data.tasks;renderOrganizerTasks();}
  catch(error){organizerNotice(error.message,"error");}
  finally{organizerState.busy=false;$("organizer-reload").disabled=false;$("organizer-task-list").setAttribute("aria-busy","false");}
}
async function submitMeetingStart(event) {
  event.preventDefault();
  if(organizerState.busy)return;
  const url=$("meeting-document-url").value.trim();
  if(!url)return;
  organizerState.busy=true;$("meeting-document-url").disabled=true;
  const panel=$("meeting-start-notice");panel.textContent="个人助手正在读取文档并调用会议 Agent，可能需要 1–2 分钟，请勿重复提交…";panel.className="";panel.hidden=false;
  try {
    const result=await api("/api/meetings/start",{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":organizerState.csrf},body:JSON.stringify({document_url:url})},150000);
    if(result.created) {
      panel.textContent=`已创建会议任务：${result.title}。提取 ${result.todo_count} 项，其中 ${result.needs_confirmation} 项需要人工确认。`;
      panel.className="success";
      $("meeting-document-url").value="";
      await loadOrganizerTasks(true);
      if(result.record_id)showView("meetings");
    } else if(result.runtime_status==="UNKNOWN" || result.task_status==="RECONCILING") {
      panel.textContent=`会议任务已进入待核验状态，暂未生成审核草稿。错误标识：${result.error_code||"UNKNOWN"}。请稍后刷新或查看审计日志。`;
      panel.className="error";
      await loadOrganizerTasks(true);
    } else {
      panel.textContent=`会议 Agent 执行失败，未创建审核草稿。错误标识：${result.error_code||"RUNTIME_FAILED"}。`;
      panel.className="error";
      await loadOrganizerTasks(true);
    }
  } catch(error) {
    panel.textContent=error.code==="REQUEST_TIMEOUT"
      ? "等待响应超时，后台可能已经生成草稿。请进入“会议审核”刷新查看，再决定是否重试。"
      : error.message;panel.className="error";
  } finally {
    organizerState.busy=false;$("meeting-document-url").disabled=false;
  }
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
    card.append(head,node("p",`${agentTypeNames[agent.actor_type]||agent.actor_type} · 稳定版 ${agent.current_version==null?"未发布":`v${agent.current_version}`} · 历史版本 ${agent.version_count}`,"muted"));
    if(agent.canary_version)card.append(node("p",`灰度 v${agent.canary_version} · 志愿者 ${agent.volunteer_count} 人 · 只产出建议`,"muted"));
    if(agent.capabilities.length)card.append(node("p",`能力：${agent.capabilities.join("、")}`,"agent-capabilities"));
    if(agent.skill_version)card.append(node("p",`Skill：${agent.skill_version}`,"muted"));
    if(agent.pause_reason)card.append(node("p",`暂停原因：${agent.pause_reason}`,"muted"));
    if(agent.candidate_version && ["ACTIVE","CANARY"].includes(agent.status)){
      const actions=node("div",undefined,"commands agent-review-actions");
      const button=node("button",agent.volunteer_enrolled?"退出灰度报名":"报名灰度试运行");
      button.type="button";button.addEventListener("click",()=>changeAgentVolunteer(agent,button));
      actions.append(button);card.append(actions);
    }
    if(agent.can_run_sample){
      const actions=node("div",undefined,"commands agent-review-actions");
      for(const [caseId,label] of [["read_only","运行只读样例"],["write_boundary","运行写入边界样例"]]){
        const button=node("button",label);button.type="button";
        button.addEventListener("click",()=>runAgentSample(agent,caseId,button));actions.append(button);
      }
      card.append(actions);
    }
    if(agent.status==="CANARY" && agentState.canPrecheck){
      const actions=node("div",undefined,"commands agent-review-actions");
      const button=node("button","查看并评审灰度运行");button.type="button";
      const details=node("div",undefined,"agent-draft-row");details.hidden=true;
      button.addEventListener("click",()=>loadCanaryRuns(agent,details,button));
      actions.append(button);card.append(actions,details);
    }
    if(agentState.canPause && agent.current_version!=null && agent.status!=="PAUSED" && agent.status!=="ARCHIVED"){
      const actions=node("div",undefined,"commands agent-review-actions"),button=node("button","暂停 Agent");
      button.type="button";button.addEventListener("click",()=>pauseAgent(agent,button));actions.append(button);card.append(actions);
    }else if(agentState.canResume && agent.status==="PAUSED" && agent.current_version!=null){
      const actions=node("div",undefined,"commands agent-review-actions"),button=node("button","恢复 Agent");
      button.type="button";button.className="primary";button.addEventListener("click",()=>resumeAgent(agent,button));actions.append(button);card.append(actions);
    }
    if(agentState.canConfirm){
      const actions=node("div",undefined,"commands agent-review-actions");
      const add=(label,fn)=>{const button=node("button",label);button.type="button";button.addEventListener("click",()=>fn(button));actions.append(button);};
      const comparisonPanel=node("div",undefined,"agent-draft-row");comparisonPanel.hidden=true;
      add("运行诊断与版本对比",button=>inspectAgent(agent,comparisonPanel,button));
      const history=node("div",undefined,"agent-draft-row");history.hidden=true;
      add("查看转正证据历史",button=>loadPromotionHistory(agent,history,button));
      if(agent.status!=="ARCHIVED" && agent.current_version!=null){
        const candidates=agent.versions.filter(version=>version>agent.current_version);
        if(agent.status==="ACTIVE" && candidates.length)add("启动志愿者灰度",button=>changeAgentRoute(agent,"canary",button));
        if(agent.status==="CANARY"){
          add("检查转正条件",button=>inspectPromotionReadiness(agent,button));
          add("人工转正",button=>changeAgentRoute(agent,"promote",button));
        }
        if(agent.status==="CANARY" || agent.current_version>1)add("人工回退",button=>changeAgentRoute(agent,"rollback",button));
        add("下架归档",button=>changeAgentRoute(agent,"archive",button));
      }
      card.append(actions,comparisonPanel,history);
    }
    panel.append(card);
  });
}
async function loadCanaryRuns(agent,panel,button){
  button.disabled=true;panel.hidden=false;panel.replaceChildren(node("p","正在读取灰度运行元数据…","muted"));
  try{
    const result=await api(`/api/meetings/agents/${encodeURIComponent(agent.agent_id)}/canary-runs`);
    panel.replaceChildren();
    if(!result.runs.length){panel.append(node("p","当前灰度期还没有运行记录。","muted"));return;}
    result.runs.forEach(run=>{
      const row=node("div",undefined,"agent-draft-row");
      row.append(node("p",`运行 ${run.task_run_id} · ${taskNames[run.status]||run.status} · ${run.review_outcome||"待独立评审"}`,"muted"));
      if(run.sample_response)row.append(node("p",`${run.sample_case_id}：${run.sample_response}`,"agent-draft-description"));
      if(run.review_outcome)row.append(node("p",`审核人：${run.reviewer_id} · 证据编号：${run.evidence_ref}`,"muted"));
      if(run.status==="WAITING_REVIEW" && !run.review_outcome){
        for(const [outcome,label] of [["PASS","通过"],["FAIL","未通过"]]){
          const reviewButton=node("button",label);reviewButton.type="button";
          reviewButton.addEventListener("click",()=>submitCanaryReview(agent,run,outcome,panel,button));
          row.append(reviewButton);
        }
      }
      panel.append(row);
    });
  }catch(error){panel.replaceChildren(node("p",error.message,"error"));}
  finally{button.disabled=false;}
}
async function runAgentSample(agent,caseId,button){
  if(agentState.busy)return;
  agentState.busy=true;button.disabled=true;
  try{
    const result=await api(`/api/meetings/agents/${encodeURIComponent(agent.agent_id)}/sample-runs`,{
      method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":agentState.csrf},
      body:JSON.stringify({case_id:caseId})
    });
    agentNotice(result.status==="WAITING_REVIEW"?`样例运行完成，等待审核官核对。运行编号：${result.task_run_id}`:
      `样例运行状态：${taskNames[result.status]||result.status}。运行编号：${result.task_run_id}`,
      result.status==="WAITING_REVIEW"?"success":"error");
  }catch(error){agentNotice(error.message,"error");}
  finally{agentState.busy=false;button.disabled=false;}
}
async function submitCanaryReview(agent,run,outcome,panel,button){
  if(agentState.busy)return;
  const evidence_ref=window.prompt("请填写独立核对材料的引用编号（不填正文或链接）：");
  if(evidence_ref===null)return;
  if(!/^[A-Za-z0-9_.:-]{3,200}$/.test(evidence_ref)){agentNotice("证据引用需为 3–200 位编号。","error");return;}
  if(!window.confirm(`确认将运行 ${run.task_run_id} 评为 ${outcome}？评审记录不可修改。`))return;
  agentState.busy=true;
  try{await api(`/api/meetings/agents/${encodeURIComponent(agent.agent_id)}/canary-runs/${encodeURIComponent(run.task_run_id)}/review`,{
    method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":agentState.csrf},
    body:JSON.stringify({expected_version:agent.canary_version,outcome,evidence_ref})
  });agentNotice("灰度运行评审已记录。","success");await loadCanaryRuns(agent,panel,button);}
  catch(error){agentNotice(error.message,"error");}
  finally{agentState.busy=false;}
}
async function changeAgentVolunteer(agent,button){
  if(agentState.busy)return;
  const enroll=!agent.volunteer_enrolled;
  if(!window.confirm(enroll?`报名“${agent.agent_id}”v${agent.candidate_version} 的只读建议灰度？`:
    `退出“${agent.agent_id}”的灰度？退出后不再路由到候选版。`))return;
  agentState.busy=true;button.disabled=true;
  try{await api(`/api/meetings/agents/${encodeURIComponent(agent.agent_id)}/volunteer`,{
    method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":agentState.csrf},
    body:JSON.stringify({candidate_version:agent.candidate_version,enroll})
  });agentState.busy=false;await loadAgents();agentNotice(enroll?"灰度报名已记录。":"已退出灰度报名。","success");}
  catch(error){agentNotice(error.message,"error");button.disabled=false;}
  finally{agentState.busy=false;}
}
async function inspectAgent(agent,panel,button){
  button.disabled=true;panel.hidden=false;panel.replaceChildren(node("p","正在读取运行与版本记录…","muted"));
  try{
    const base=`/api/meetings/agents/${encodeURIComponent(agent.agent_id)}`;
    const diagnostic=await api(`${base}/diagnostics`);
    const versions=agent.versions;
    panel.replaceChildren(node("h4","运行诊断"),node("p",`累计 ${diagnostic.total} 次运行。`,"muted"));
    for(const [version,item] of Object.entries(diagnostic.versions)){
      panel.append(node("p",`v${version}：${item.total} 次，异常 ${item.failures} 次；状态 ${JSON.stringify(item.statuses)}`,"muted"));
    }
    if(versions.length>1){
      const newer=agent.canary_version||versions[0],older=versions.find(version=>version<newer);
      if(older){const comparison=await api(`${base}/compare?old_version=${older}&new_version=${newer}`);
        panel.append(node("h4",`v${older} → v${newer}`));
        if(Object.keys(comparison.changes).length){
          for(const [field,values] of Object.entries(comparison.changes)){
            panel.append(node("p",`${field}：${JSON.stringify(values.old)} → ${JSON.stringify(values.new)}`,"muted"));
          }
        }else panel.append(node("p","配置字段没有变化。","muted"));
        const cases=Object.entries(comparison.sample_comparison);
        if(!cases.length)panel.append(node("p","这两个版本还没有可对比的样例回答。","muted"));
        for(const [caseId,answers] of cases){
          panel.append(node("h4",caseId==="read_only"?"只读建议样例":"写入边界样例"));
          for(const [label,answer] of [[`v${older}`,answers.old],[`v${newer}`,answers.new]]){
            panel.append(node("p",answer?`${label} · ${answer.task_run_id} · ${answer.created_at}`:`${label} · 尚无样例回答`,"muted"));
            if(answer)panel.append(node("p",answer.response,"agent-draft-description"));
          }
        }
      }
    }
  }catch(error){panel.replaceChildren(node("p",error.message,"error"));}
  finally{button.disabled=false;}
}
function promotionReadinessText(readiness){
  const counts=`灰度窗口 ${readiness.canary_epoch} · 观察 ${readiness.observation_seconds}/${readiness.required_observation_seconds} 秒 · 样本 ${readiness.sample_count}/${readiness.required_sample_count} 条 · 缺运行轨迹 ${readiness.missing_runtime_count} · 未评审 ${readiness.unreviewed_count} · 未通过评审 ${readiness.failed_review_count} · 失败运行 ${readiness.failed_run_count}`;
  return readiness.can_promote?`${counts}\n已满足本地转正门槛，仍须管理员人工确认。`:
    `${counts}\n待处理：${readiness.blockers.map(code=>promotionBlockerNames[code]||code).join("；")}`;
}
async function inspectPromotionReadiness(agent,button){
  button.disabled=true;
  try{
    const result=await api(`/api/meetings/agents/${encodeURIComponent(agent.agent_id)}/promotion-readiness`);
    window.alert(promotionReadinessText(result));
  }catch(error){agentNotice(error.message,"error");}
  finally{button.disabled=false;}
}
async function loadPromotionHistory(agent,panel,button){
  button.disabled=true;panel.hidden=false;panel.replaceChildren(node("p","正在读取转正证据…","muted"));
  try{
    const result=await api(`/api/meetings/agents/${encodeURIComponent(agent.agent_id)}/promotion-history`);
    panel.replaceChildren();
    if(!result.promotions.length){panel.append(node("p","暂无转正记录。","muted"));return;}
    result.promotions.forEach(item=>{
      const entry=node("article",undefined,"agent-draft-row");
      entry.append(node("h3",`v${item.stable_version} → v${item.candidate_version} · 灰度窗口 ${item.canary_epoch}`),
        node("p",`批准人：${item.approver_id} · 观察 ${item.observation_seconds} 秒 · ${item.created_at}`,"muted"),
        node("p",`审计编号：${item.audit_id} · 证据摘要：${item.evidence_hash} · ${item.integrity_ok?"摘要校验通过":"摘要不匹配，请停止后续发布并核验"}`,item.integrity_ok?"muted":"error"));
      item.runs.forEach(run=>entry.append(node("p",`运行 ${run.task_run_id} · ${taskNames[run.status]||run.status} · ${run.outcome} · 审核人 ${run.reviewer_id} · 证据编号 ${run.evidence_ref}`,"muted")));
      panel.append(entry);
    });
  }catch(error){panel.replaceChildren(node("p",error.message,"error"));}
  finally{button.disabled=false;}
}
async function changeAgentRoute(agent,action,button){
  if(agentState.busy)return;
  const labels={canary:"志愿者灰度",promote:"人工转正",rollback:"人工回退",archive:"下架归档"};
  let target=null,volunteer_ids=[];
  if(action==="promote"){
    try{
      const readiness=await api(`/api/meetings/agents/${encodeURIComponent(agent.agent_id)}/promotion-readiness`);
      if(!readiness.can_promote){agentNotice(promotionReadinessText(readiness),"error");return;}
    }catch(error){agentNotice(error.message,"error");return;}
  }
  if(action==="canary"){
    target=Math.max(...agent.versions.filter(version=>version>agent.current_version));
    const input=window.prompt("志愿者成员编号（逗号分隔；灰度版只产出建议）：");if(input===null)return;
    volunteer_ids=input.split(",").map(value=>value.trim()).filter(Boolean);
    if(!volunteer_ids.length){agentNotice("至少指定一名志愿者。","error");return;}
  }else if(action==="promote")target=agent.canary_version;
  else if(action==="rollback"){
    target=agent.status==="CANARY"?agent.current_version:Number(window.prompt(`回退到哪个历史版本？可选：${agent.versions.filter(version=>version<agent.current_version).join("、")}`));
    if(!agent.versions.includes(target)){agentNotice("请填写有效的历史版本。","error");return;}
  }
  const reason=window.prompt(`${labels[action]}“${agent.agent_id}”的原因（至少 5 个字）：`);
  if(reason===null)return;
  if(reason.trim().length<5){agentNotice("请填写至少 5 个字的原因。","error");return;}
  if(!window.confirm(`确认${labels[action]}“${agent.agent_id}”？`))return;
  agentState.busy=true;button.disabled=true;
  try{
    await api(`/api/meetings/agents/${encodeURIComponent(agent.agent_id)}/${action}`,{
      method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":agentState.csrf},
      body:JSON.stringify({expected_version:agent.current_version,target_version:target,volunteer_ids,reason:reason.trim()})
    });
    agentState.busy=false;await loadAgents();agentNotice(`${labels[action]}已记录，审计与路由同步更新。`,"success");
  }catch(error){agentNotice(error.message,"error");button.disabled=false;}
  finally{agentState.busy=false;}
}
async function pauseAgent(agent,button) {
  if(agentState.busy)return;
  const reason=window.prompt(`暂停“${agent.agent_id}”的原因（至少 5 个字）：`);
  if(reason===null)return;
  if(reason.trim().length<5){agentNotice("请填写至少 5 个字的暂停原因。","error");return;}
  if(!window.confirm(`确认暂停“${agent.agent_id}”？新的调度和后续写入会被阻断。`))return;
  agentState.busy=true;button.disabled=true;agentNotice("正在暂停 Agent…");
  try{
    await api(`/api/meetings/agents/${encodeURIComponent(agent.agent_id)}/pause`,{
      method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":agentState.csrf},
      body:JSON.stringify({expected_version:agent.current_version,reason:reason.trim()})
    });
    const data=await api("/api/meetings/agents");agentState.agents=data.agents||[];renderAgents();agentNotice("Agent 已暂停；恢复需管理员复核当前版本。","success");
  }catch(error){agentNotice(error.message,"error");button.disabled=false;}
  finally{agentState.busy=false;}
}
async function resumeAgent(agent,button) {
  if(agentState.busy)return;
  const reason=window.prompt(`恢复“${agent.agent_id}”的原因（至少 5 个字）：`);
  if(reason===null)return;
  if(reason.trim().length<5){agentNotice("请填写至少 5 个字的恢复原因。","error");return;}
  if(!window.confirm(`确认恢复“${agent.agent_id}”当前 v${agent.current_version}？不会自动重试旧任务或执行远端写入。`))return;
  agentState.busy=true;button.disabled=true;agentNotice("正在恢复 Agent…");
  try{
    await api(`/api/meetings/agents/${encodeURIComponent(agent.agent_id)}/resume`,{
      method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":agentState.csrf},
      body:JSON.stringify({expected_version:agent.current_version,reason:reason.trim()})
    });
    const data=await api("/api/meetings/agents");agentState.agents=data.agents||[];renderAgents();agentNotice("Agent 已恢复；旧任务不会自动重试。","success");
  }catch(error){agentNotice(error.message,"error");button.disabled=false;}
  finally{agentState.busy=false;}
}
function renderAgentDrafts() {
  const panel=$("agent-draft-list");panel.replaceChildren();
  if(!agentState.drafts.length){panel.append(node("p","暂无配置草稿。","muted"));return;}
  agentState.drafts.forEach(draft=>{
    const card=node("article",undefined,"agent-draft-row"),head=node("div",undefined,"agent-row-head");
    head.append(node("h3",draft.name),node("span",draftStatusNames[draft.status]||draft.status,`badge ${draft.status==="PUBLISHED"||draft.status==="APPROVED"?"ready":""}`));
    card.append(head,node("p",`${agentTypeNames[draft.actor_type]||draft.actor_type} · ${draft.base_version?`升级自 v${draft.base_version}`:"初版"} · 提交时间 ${draft.created_at}${draft.created_by_current_member?" · 我提交":""}`,"muted"),node("p",draft.description,"agent-draft-description"));
    if(draft.evaluation_status)card.append(node("p",`独立评测：${draft.evaluation_status==="PASSED"?"通过":"未通过"}`,"muted"));
    if(draft.capabilities.length)card.append(node("p",`申请能力：${draft.capabilities.join("、")}`,"agent-capabilities"));
    if(draft.review_status){
      card.append(node("p",`审核官预审：${reviewStatusNames[draft.review_status]||draft.review_status}`,`agent-review-status ${draft.review_status==="READY_FOR_ADMIN"?"ready":"blocked"}`));
      if(draft.review_blockers?.length)card.append(node("p",`阻断项：${draft.review_blockers.join("、")}`,"agent-review-findings"));
      if(draft.review_warnings?.length)card.append(node("p",`风险提示：${draft.review_warnings.join("、")}`,"agent-review-findings"));
    }
    if(agentState.canPrecheck && draft.status==="PENDING_REVIEW"){
      const actions=node("div",undefined,"commands agent-review-actions"),button=node("button","运行审核预审");
      button.type="button";button.addEventListener("click",()=>precheckAgentDraft(draft,button));actions.append(button);card.append(actions);
      if(draft.review_input_hash){const evaluate=node("button","登记独立评测");evaluate.type="button";
        evaluate.addEventListener("click",()=>evaluateAgentDraft(draft,evaluate));actions.append(evaluate);}
    }
    if(agentState.canConfirm && draft.status!=="PUBLISHED" && draft.review_status==="READY_FOR_ADMIN"){
      const actions=node("div",undefined,"commands agent-review-actions"),button=node("button","管理员确认发布");
      button.type="button";button.className="primary";button.addEventListener("click",()=>confirmAgentDraft(draft,button));actions.append(button);card.append(actions);
    }
    panel.append(card);
  });
}
function renderMemberAccess() {
  const panel=$("member-access-list");panel.replaceChildren();
  if(!memberState.requests.length){panel.append(node("p","暂无历史待处理申请。","muted"));return;}
  memberState.requests.forEach(item=>{
    const row=node("article",undefined,"agent-draft-row"),button=node("button","批准接入","primary");
    button.type="button";button.addEventListener("click",()=>approveMemberAccess(item,button));
    row.append(node("h3",item.request_id),node("p",`申请时间：${item.created_at}。请先与该成员确认识别码。`,"muted"),button);panel.append(row);
  });
}
function renderMemberDirectory() {
  const panel=$("member-directory-list");panel.replaceChildren();
  if(!memberState.members.length){panel.append(node("p","暂无已接入成员。","muted"));return;}
  memberState.members.forEach(member=>{
    const row=node("article",undefined,"member-row"),head=node("div",undefined,"agent-row-head");
    head.append(node("h3",member.display_name||member.actor_id),node("span",member.status==="ACTIVE"?"已启用":"已停用",`badge ${member.status==="ACTIVE"?"ready":"closed"}`));
    row.append(head,node("p",`成员编号：${member.actor_id} · 应用角色：${member.role==="ADMIN"?"管理员":member.reviewer?"Agent 审核官":"普通成员"} · 接入时间：${member.joined_at||"初始绑定"}`,"muted"));
    if(member.status_reason)row.append(node("p",`最近处置：${member.status_reason}`,"muted"));
    if(member.reviewer_reason)row.append(node("p",`审核官角色最近变更：${member.reviewer_reason}`,"muted"));
    if(member.role!=="ADMIN"){
      const button=node("button",member.status==="ACTIVE"?"停用成员":"恢复成员");button.type="button";
      button.addEventListener("click",()=>changeMemberStatus(member,button));
      row.append(button);
      if(member.status==="ACTIVE"){
        const roleButton=node("button",member.reviewer?"撤销审核官":"授予审核官");roleButton.type="button";
        roleButton.addEventListener("click",()=>changeReviewerRole(member,roleButton));
        row.append(roleButton);
      }
    }
    panel.append(row);
  });
}
async function changeMemberStatus(member,button){
  if(memberState.busy)return;
  const next=member.status==="ACTIVE"?"INACTIVE":"ACTIVE";
  const label=next==="ACTIVE"?"恢复":"停用";
  const reason=window.prompt(`${label}成员“${member.actor_id}”的原因（至少 5 个字）：`);
  if(reason===null)return;
  if(reason.trim().length<5){memberNotice("请填写至少 5 个字的原因。","error");return;}
  if(!window.confirm(`确认${label}成员“${member.actor_id}”？`))return;
  memberState.busy=true;button.disabled=true;memberNotice(`正在${label}成员…`);
  try{
    await api(`/api/meetings/member-directory/${encodeURIComponent(member.actor_id)}/status`,{
      method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":memberState.csrf},
      body:JSON.stringify({expected_status:member.status,status:next,reason:reason.trim()})
    });
    memberNotice(`成员已${next==="ACTIVE"?"恢复":"停用"}。`,"success");
    memberState.busy=false;
    await loadMembers();
  }catch(error){memberNotice(error.message,"error");button.disabled=false;}
  finally{memberState.busy=false;}
}
async function changeReviewerRole(member,button){
  if(memberState.busy)return;
  const reviewer=!member.reviewer,label=reviewer?"授予":"撤销";
  const reason=window.prompt(`${label}成员“${member.actor_id}”的 Agent 审核官角色，原因（至少 5 个字）：`);
  if(reason===null)return;
  if(reason.trim().length<5){memberNotice("请填写至少 5 个字的原因。","error");return;}
  if(!window.confirm(`确认${label}成员“${member.actor_id}”的 Agent 审核官角色？`))return;
  memberState.busy=true;button.disabled=true;memberNotice(`正在${label}审核官角色…`);
  try{
    await api(`/api/meetings/member-directory/${encodeURIComponent(member.actor_id)}/reviewer-role`,{
      method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":memberState.csrf},
      body:JSON.stringify({expected_reviewer:Boolean(member.reviewer),reviewer,reason:reason.trim()})
    });
    memberState.busy=false;
    await loadMembers();
    memberNotice(`已${label} Agent 审核官角色。`,"success");
  }catch(error){memberNotice(error.message,"error");button.disabled=false;}
  finally{memberState.busy=false;}
}
function memberNotice(message,type="") {
  const panel=$("members-notice");panel.textContent=message;panel.className=type;panel.hidden=!message;
}
async function loadMembers() {
  if(memberState.busy)return;
  memberState.busy=true;$("members-reload").disabled=true;$("member-directory-list").setAttribute("aria-busy","true");memberNotice("");
  try{
    const [directory,data]=await Promise.all([
      api("/api/meetings/member-directory"),api("/api/meetings/member-access-requests")
    ]);
    memberState.members=directory.members||[];memberState.requests=data.requests||[];memberState.csrf=data.csrf_token;
    renderMemberDirectory();renderMemberAccess();
  }catch(error){memberNotice(error.message,"error");$("member-directory-list").replaceChildren(node("p","成员目录暂时无法加载，请稍后刷新。","muted"));}
  finally{memberState.busy=false;$("members-reload").disabled=false;$("member-directory-list").setAttribute("aria-busy","false");}
}
async function approveMemberAccess(item,button) {
  if(memberState.busy)return;
  if(!window.confirm(`已与成员核对识别码 ${item.request_id}？批准后该成员可登录工作台。`))return;
  memberState.busy=true;button.disabled=true;memberNotice("正在批准成员接入…");
  try{
    await api(`/api/meetings/member-access-requests/${encodeURIComponent(item.request_id)}/approve`,{method:"POST",headers:{"X-CSRF-Token":memberState.csrf}});
    memberNotice("成员已绑定。请让该成员重新打开工作台。","success");
    const [directory,data]=await Promise.all([
      api("/api/meetings/member-directory"),api("/api/meetings/member-access-requests")
    ]);
    memberState.members=directory.members||[];memberState.requests=data.requests||[];memberState.csrf=data.csrf_token;
    renderMemberDirectory();renderMemberAccess();
  }catch(error){memberNotice(error.message,"error");button.disabled=false;}
  finally{memberState.busy=false;}
}
async function loadAgents() {
  if(agentState.busy)return;agentState.busy=true;$("agents-reload").disabled=true;$("agent-list").setAttribute("aria-busy","true");
  try{const data=await api("/api/meetings/agents");const drafts=await api("/api/meetings/agent-drafts");agentState.agents=data.agents||[];agentState.drafts=drafts.drafts||[];agentState.canPause=Boolean(data.can_pause);agentState.canResume=Boolean(data.can_resume);agentState.canPrecheck=Boolean(drafts.can_precheck);agentState.canConfirm=Boolean(drafts.can_confirm);agentState.csrf=drafts.csrf_token;$("agent-eval-dataset").hidden=!agentState.canPrecheck;renderAgents();renderAgentDrafts();agentNotice("");}
  catch(error){agentNotice(error.message,"error");if(!agentState.agents.length)$("agent-list").replaceChildren(node("p","Agent 目录暂时无法加载，请稍后刷新。","muted"));}
  finally{agentState.busy=false;$("agents-reload").disabled=false;$("agent-list").setAttribute("aria-busy","false");}
}
function auditNotice(message,type="") { const panel=$("audit-notice");panel.textContent=message;panel.className=type;panel.hidden=!message; }
function renderAuditSummary(){
  const panel=$("audit-summary");panel.replaceChildren();
  const incidents=auditState.summary.incidents||{},tools=auditState.summary.tool_calls||{};
  const metrics=[
    ["待处理异常",incidents.OPEN||0],
    ["已认领异常",incidents.ACKNOWLEDGED||0],
    ["已核实关闭",incidents.RESOLVED||0],
    ["工具成功",tools.SUCCEEDED||0],
    ["工具待对账",(tools.UNKNOWN||0)+(tools.RECONCILING||0)],
    ["工具失败",tools.FAILED||0],
  ];
  metrics.forEach(([label,value])=>{const card=node("div",undefined,"audit-metric");card.append(node("strong",String(value)),node("span",label));panel.append(card);});
}
function renderAudit() {
  const panel=$("audit-list");panel.replaceChildren();
  if(!auditState.events.length){panel.append(node("p","暂无审计事件。","muted"));return;}
  const categoryNames={AGENT_GOVERNANCE:"Agent治理",APPROVAL:"审批",SOURCE:"来源校验",MEMBER:"成员",TASK_FOLLOW_UP:"任务跟进",TASK:"任务",TOOL:"工具",OPERATIONS:"运维处置",SYSTEM:"系统"};
  const actionNames={NONE:"无需处理",CHECK_POLICY_OR_SOURCE:"检查权限、审批或来源版本",RECONCILE_BEFORE_RETRY:"先对账确认，再考虑人工重试"};
  const triageNames={OPEN:"待处理",ACKNOWLEDGED:"已认领",RESOLVED:"已核实关闭"};
  auditState.events.forEach(item=>{
    const row=node("article",undefined,"audit-row"),head=node("div",undefined,"audit-row-head");
    const positive=["ACCEPTED","APPROVED","SUCCEEDED"].includes(item.outcome);
    const negative=["DENIED","ERROR","FAILED"].includes(item.outcome);
    head.append(node("strong",item.event_type),node("span",item.outcome,`badge ${positive?"ready":negative?"closed":""}`));
    row.append(head,node("p",`${item.created_at} · 审计 ${item.audit_ref} · 载荷 ${item.payload_ref}`,"muted"));
    row.append(node("p",`分类：${categoryNames[item.category]||item.category||"系统"} · 级别：${item.severity||"INFO"} · 建议：${actionNames[item.recommended_action]||"人工核对"}`,`audit-action ${(item.severity||"INFO").toLowerCase()}`));
    if(item.task_ref)row.append(node("p",`任务引用：${item.task_ref}`,"audit-ref"));
    if(["WARN","HIGH"].includes(item.severity)){
      row.append(node("p",`处置状态：${triageNames[item.triage_status]||"待处理"}`,"muted"));
      if(item.triage_status==="OPEN"){
        const button=node("button","认领核查");button.type="button";
        button.addEventListener("click",()=>triageAudit(item,"ACKNOWLEDGED",null));row.append(button);
      }else if(item.triage_status==="ACKNOWLEDGED"){
        const button=node("button","记录结论并关闭");button.type="button";
        button.addEventListener("click",()=>{
          const resolution=window.prompt("请填写核实结论。若远端写入结果未知，请先对账；关闭记录不会执行重试。","");
          if(resolution!==null)triageAudit(item,"RESOLVED",resolution.trim());
        });row.append(button);
      }
    }
    panel.append(row);
  });
}
async function triageAudit(item,status,resolution){
  if(auditState.busy)return;
  if(status==="RESOLVED"&&!resolution){auditNotice("请填写核实结论。","error");return;}
  auditState.busy=true;$("audit-reload").disabled=true;auditNotice("正在保存处置记录…");
  try{
    await api(`/api/meetings/audit/${encodeURIComponent(item.audit_ref)}/triage`,{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":auditState.csrf},body:JSON.stringify({expected_status:item.triage_status,status,resolution})});
    auditNotice("处置记录已保存。","success");
  }catch(error){auditNotice(error.message,"error");}
  finally{auditState.busy=false;$("audit-reload").disabled=false;}
  await loadAudit();
}
async function loadAudit() {
  if(auditState.busy)return;auditState.busy=true;$("audit-reload").disabled=true;$("audit-list").setAttribute("aria-busy","true");
  try{const data=await api("/api/meetings/audit");auditState.canView=Boolean(data.can_view);auditState.events=data.events||[];auditState.counts=data.counts||{};auditState.summary=data.summary||{};auditState.csrf=data.csrf_token||"";$("module-audit").hidden=!auditState.canView;$("module-members").hidden=!auditState.canView;$("audit-counts").textContent=Object.entries(auditState.counts).map(([key,value])=>`${key} ${value}`).join(" · ");renderAuditSummary();renderAudit();}
  catch(error){auditNotice(error.message,"error");}
  finally{auditState.busy=false;$("audit-reload").disabled=false;$("audit-list").setAttribute("aria-busy","false");}
}
async function precheckAgentDraft(draft,button) {
  if(agentState.busy)return;
  agentState.busy=true;button.disabled=true;agentNotice(`正在预审“${draft.name}”…`);
  try{const result=await api(`/api/meetings/agent-drafts/${encodeURIComponent(draft.draft_id)}/precheck`,{method:"POST",headers:{"X-CSRF-Token":agentState.csrf}});agentNotice(result.status==="READY_FOR_ADMIN"?"预审通过，等待管理员确认。":"预审已阻断，请处理报告中的问题。",result.status==="READY_FOR_ADMIN"?"success":"error");agentState.busy=false;await loadAgents();}
  catch(error){agentNotice(error.message,"error");button.disabled=false;}
  finally{agentState.busy=false;}
}
async function evaluationHash(value){
  const bytes=new TextEncoder().encode(value);
  return [...new Uint8Array(await crypto.subtle.digest("SHA-256",bytes))].map(byte=>byte.toString(16).padStart(2,"0")).join("");
}
async function createEvalDataset(){
  if(agentState.busy)return;
  const dataset_id=window.prompt("新评测集编号（不可覆盖）：");if(dataset_id===null)return;
  const input=window.prompt("每行一例：案例编号|期望结果|证据引用。至少两例；仅保存期望结果哈希。");
  if(input===null)return;
  const rows=input.trim().split(/\r?\n/).map(line=>line.split("|").map(value=>value.trim()));
  if(rows.length<2 || rows.some(row=>row.length!==3 || row.some(value=>!value))){agentNotice("请填写至少两条完整的评测案例。","error");return;}
  const cases=[];
  for(const [case_id,expected,evidence_ref] of rows)cases.push({case_id,expected_hash:await evaluationHash(expected),evidence_ref});
  agentState.busy=true;
  try{await api("/api/meetings/agent-evaluation-datasets",{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":agentState.csrf},body:JSON.stringify({dataset_id,cases})});
    agentNotice(`评测集 ${dataset_id} 已登记，案例和期望结果不可覆盖。`,"success");}
  catch(error){agentNotice(error.message,"error");}
  finally{agentState.busy=false;}
}
async function evaluateAgentDraft(draft,button){
  if(agentState.busy)return;
  const dataset_id=window.prompt("独立评测集编号：");if(dataset_id===null)return;
  const input=window.prompt("每行一例：案例编号|期望结果|实际结果|证据引用。至少两例；仅保存结果哈希。");
  if(input===null)return;
  const rows=input.trim().split(/\r?\n/).map(line=>line.split("|").map(value=>value.trim()));
  if(rows.length<2 || rows.some(row=>row.length!==4 || row.some(value=>!value))){agentNotice("请填写至少两条完整评测案例。","error");return;}
  const cases=[];
  for(const [case_id,expected,observed,evidence_ref] of rows)cases.push({case_id,expected_hash:await evaluationHash(expected),observed_hash:await evaluationHash(observed),evidence_ref});
  agentState.busy=true;button.disabled=true;
  try{const result=await api(`/api/meetings/agent-drafts/${encodeURIComponent(draft.draft_id)}/evaluate`,{
    method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":agentState.csrf},
    body:JSON.stringify({input_hash:draft.review_input_hash,dataset_id,cases})
  });agentState.busy=false;await loadAgents();agentNotice(`评测${result.status==="PASSED"?"通过":"未通过"}：${result.passed}/${result.total}；请重新运行审核预审。`,result.status==="PASSED"?"success":"error");}
  catch(error){agentNotice(error.message,"error");button.disabled=false;}
  finally{agentState.busy=false;}
}
async function confirmAgentDraft(draft,button) {
  if(agentState.busy)return;
  if(!window.confirm(`确认发布“${draft.name}”的${draft.base_version?"候选新版本":" Agent v1"}？${draft.base_version?"候选版须另行启动灰度。":"这会启用申请的能力。"}`))return;
  agentState.busy=true;button.disabled=true;agentNotice(`正在确认发布“${draft.name}”…`);
  try{const result=await api(`/api/meetings/agent-drafts/${encodeURIComponent(draft.draft_id)}/confirm`,{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":agentState.csrf},body:JSON.stringify({review_id:draft.review_id,input_hash:draft.review_input_hash})});agentNotice(result.duplicate?"发布确认已处理，返回原版本。":`Agent v${result.version} 已发布；候选版需另行启动灰度。`,"success");agentState.busy=false;await loadAgents();}
  catch(error){agentNotice(error.message,"error");button.disabled=false;}
  finally{agentState.busy=false;}
}
async function submitAgentDraft(event) {
  event.preventDefault();
  if(agentState.busy)return;
  const capabilities=$("agent-draft-capabilities").value.split(",").map(value=>value.trim()).filter(Boolean);
  agentState.busy=true;$("agent-draft-form").querySelectorAll("input,select,textarea,button").forEach(el=>el.disabled=true);agentNotice("正在提交配置草稿…");
  try{
    await api("/api/meetings/agent-drafts",{method:"POST",headers:{"Content-Type":"application/json","X-CSRF-Token":agentState.csrf},body:JSON.stringify({name:$("agent-draft-name").value.trim(),actor_type:$("agent-draft-type").value,description:$("agent-draft-description").value.trim(),capabilities,skill_version:$("agent-draft-skill").value.trim()||null,base_version:$("agent-draft-base-version").value?Number($("agent-draft-base-version").value):null})});
    $("agent-draft-form").reset();$("agent-draft-editor").hidden=true;agentNotice("配置草稿已提交，等待审核官和管理员处理。","success");
    const drafts=await api("/api/meetings/agent-drafts");agentState.drafts=drafts.drafts||[];agentState.canPrecheck=Boolean(drafts.can_precheck);agentState.canConfirm=Boolean(drafts.can_confirm);agentState.csrf=drafts.csrf_token;renderAgentDrafts();
  }catch(error){agentNotice(error.message,"error");}
  finally{agentState.busy=false;$("agent-draft-form").querySelectorAll("input,select,textarea,button").forEach(el=>el.disabled=false);}
}
async function api(path, options={}, timeoutMs=15000) {
  const controller=new AbortController(), timer=setTimeout(()=>controller.abort(),timeoutMs);
  try {
    const response=await fetch(path,{credentials:"same-origin",...options,signal:controller.signal});
    if(response.status===401) { window.location.assign("/oauth/start"); throw new Error("登录已过期，请重新登录"); }
    if(!response.headers.get("content-type")?.includes("application/json")) throw new Error("服务暂时不可用，请稍后重新加载。");
    const result=await response.json();
    if(!response.ok) throw new Error(typeof result.detail==="string"?result.detail:"请求未通过校验，请核对后重试");
    return result;
  } catch(error) {
    if(error.name==="AbortError") {const timeoutError=new Error("响应超时，请重新加载确认当前状态。");timeoutError.code="REQUEST_TIMEOUT";throw timeoutError;}
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
$("logout").addEventListener("click",async()=>{
  if(state.dirty&&!window.confirm("会议草稿尚未保存，确定退出登录？"))return;
  const button=$("logout");button.disabled=true;
  try{
    const csrf=state.csrf||auditState.csrf||agentState.csrf||memberState.csrf||(await api("/api/meetings")).csrf_token;
    await api("/api/meetings/logout",{method:"POST",headers:{"X-CSRF-Token":csrf}});
    state.dirty=false;window.location.replace("/api/meetings/logged-out");
  }catch(error){button.disabled=false;window.alert(`退出失败：${error.message}`);}
});
window.addEventListener("beforeunload",event=>{if(state.dirty){event.preventDefault();event.returnValue="";}});
$("owner-reload").addEventListener("click",loadOwnerTasks);
$("organizer-reload").addEventListener("click",loadOrganizerTasks);
$("meeting-start-form").addEventListener("submit",submitMeetingStart);
$("follow-up-reload").addEventListener("click",refreshDigest);
$("agents-reload").addEventListener("click",loadAgents);
  $("agent-eval-dataset").addEventListener("click",createEvalDataset);
$("members-reload").addEventListener("click",loadMembers);
$("audit-reload").addEventListener("click",loadAudit);
$("agent-draft-toggle").addEventListener("click",()=>{const editor=$("agent-draft-editor");editor.hidden=!editor.hidden;if(!editor.hidden)$("agent-draft-name").focus();});
$("agent-draft-cancel").addEventListener("click",()=>{$("agent-draft-form").reset();$("agent-draft-editor").hidden=true;});
$("agent-draft-form").addEventListener("submit",submitAgentDraft);
function showView(view,{updateHash=true}={}) {
  if(!Object.hasOwn(viewNames,view))view="meetings";
  if($("module-"+view).hidden)view="meetings";
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
  if(view==="members")loadMembers();
  if(view==="audit")loadAudit();
  return true;
}
$("module-nav").querySelectorAll("button").forEach(button=>{
  button.addEventListener("click",()=>showView(button.dataset.view));
  button.addEventListener("keydown",event=>{
    if(!["ArrowUp","ArrowDown","ArrowLeft","ArrowRight"].includes(event.key))return;
    event.preventDefault();const keys=Object.keys(viewNames),index=keys.indexOf(button.dataset.view);
    const visible=keys.filter(key=>!$("module-"+key).hidden),visibleIndex=visible.indexOf(button.dataset.view);
    const next=visible[(visibleIndex+(event.key==="ArrowUp"||event.key==="ArrowLeft"?-1:1)+visible.length)%visible.length];
    if(showView(next))$(`module-${next}`).focus();
  });
});
window.addEventListener("hashchange",()=>showView(location.hash.slice(1),{updateHash:false}));
window.addEventListener("popstate",()=>showView(location.hash.slice(1),{updateHash:false}));
loadAudit().then(()=>showView(location.hash.slice(1)));
