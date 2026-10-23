const assert = require('node:assert/strict');
const { chromium } = require(process.env.FDE_PLAYWRIGHT_MODULE || 'playwright');

const base = process.env.FDE_LOCAL_DEMO_URL || 'http://127.0.0.1:8213';
const output = process.env.FDE_UI_OUTPUT_DIR || 'F:/Temp';
const name = `browser-lifecycle-${Date.now()}`;
const dataset = `browser-eval-${Date.now()}`;

async function main() {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1365, height: 900 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const card = () => page.locator('#agent-list > .agent-row').filter({ hasText: name });
  const draft = () => page.locator('#agent-draft-list > .agent-draft-row').filter({ hasText: name }).first();
  const notice = page.locator('#agents-notice');

  async function role(label) {
    await page.goto(`${base}/local-demo`);
    await page.getByRole('button', { name: label, exact: true }).click();
    await page.locator('#view-agents').waitFor({ state: 'visible' });
    await page.locator('#agent-list[aria-busy="false"]').waitFor();
    assert.equal(await page.locator('#module-meetings').isVisible(), false);
    assert.equal(await page.locator('#module-owner-tasks').isVisible(), false);
  }

  async function dialogs(actions, click) {
    let index = 0;
    let resolveDone, rejectDone;
    const done = new Promise((resolve, reject) => { resolveDone = resolve; rejectDone = reject; });
    const timeout = setTimeout(() => rejectDone(new Error('expected browser dialogs were not shown')), 15000);
    const handler = async dialog => {
      try {
        const action = actions[index++];
        if (action === undefined) throw new Error(`unexpected ${dialog.type()} dialog: ${dialog.message()}`);
        if (action === null) await dialog.dismiss();
        else await dialog.accept(action);
        if (index === actions.length) resolveDone();
      } catch (error) { rejectDone(error); }
    };
    page.on('dialog', handler);
    try {
      await Promise.all([click(), done]);
    } finally {
      clearTimeout(timeout);
      page.off('dialog', handler);
    }
  }

  async function submitDraft(baseVersion, description) {
    await page.locator('#agent-draft-toggle').click();
    await page.locator('#agent-draft-name').fill(name);
    await page.locator('#agent-draft-description').fill(description);
    await page.locator('#agent-draft-capabilities').fill('doc.read');
    if (baseVersion) await page.locator('#agent-draft-base-version').fill(String(baseVersion));
    await page.locator('#agent-draft-form button[type="submit"]').click();
    await draft().waitFor();
  }

  async function evaluateDraft(createDataset) {
    if (createDataset) {
      await dialogs([dataset, 'case-read|READ|qa-read\ncase-human|ASK_HUMAN|qa-human'],
        () => page.locator('#agent-eval-dataset').click());
      await notice.getByText(new RegExp(dataset)).waitFor();
    }
    await draft().getByRole('button', { name: '运行审核预审' }).click();
    await draft().getByRole('button', { name: '登记独立评测' }).waitFor();
    await dialogs([dataset, 'case-read|READ|READ|qa-read\ncase-human|ASK_HUMAN|ASK_HUMAN|qa-human'],
      () => draft().getByRole('button', { name: '登记独立评测' }).click());
    await draft().getByText('独立评测：通过').waitFor();
    await draft().getByRole('button', { name: '运行审核预审' }).click();
    await draft().getByText('审核官预审：预审通过，待管理员确认').waitFor();
  }

  async function publish(version) {
    await dialogs([''], () => draft().getByRole('button', { name: '管理员确认发布' }).click());
    if (version === 1) await card().getByText('稳定版 v1', { exact: false }).waitFor();
    else await card().getByRole('button', { name: '报名灰度试运行' }).waitFor();
  }

  try {
    await role('普通成员');
    await page.screenshot({ path: `${output}/fde-lifecycle-desktop.png` });
    await submitDraft(null, '只读建议与人工核对');
    await role('独立审核官');
    await evaluateDraft(true);
    await role('管理员');
    assert.equal(await page.locator('#agent-list > .agent-row').filter({ hasText: 'local-assistant' })
      .getByRole('button', { name: '暂停 Agent' }).count(), 0);
    await publish(1);
    console.log('browser: v1 published');
    await role('普通成员');
    await card().getByRole('button', { name: '运行只读样例' }).click();
    await notice.getByText(/样例运行完成/).waitFor();
    for (const [label, status] of [['模拟执行失败', '执行失败'], ['模拟结果未知', '待核验']]) {
      await card().getByRole('button', { name: label }).click();
      await notice.getByText(new RegExp(`样例运行状态：${status}`)).waitFor();
    }
    await card().getByRole('button', { name: '运行只读样例' }).click();
    await notice.getByText(/尚未人工核验/).waitFor();
    await role('管理员');
    await card().getByRole('button', { name: '运行诊断与版本对比' }).click();
    await card().getByText('异常运行 2 条').waitFor();
    await card().getByText(/SAMPLE_DEMO_FAILURE/).waitFor();
    await card().getByText(/SAMPLE_DEMO_UNKNOWN/).waitFor();
    await dialogs(['browser:checked-unknown', ''],
      () => card().getByRole('button', { name: '登记核验结论' }).click());
    await card().getByText(/已登记不作评审证据/).waitFor();
    await role('普通成员');
    await card().getByRole('button', { name: '运行只读样例' }).click();
    await notice.getByText(/样例运行完成/).waitFor();
    await submitDraft(1, '只读建议与更严格的人工核对');
    await role('独立审核官');
    await evaluateDraft(false);
    await role('管理员');
    await publish(2);
    console.log('browser: v2 candidate published');
    await role('普通成员');
    await dialogs([''], () => card().getByRole('button', { name: '报名灰度试运行' }).click());
    await card().getByRole('button', { name: '退出灰度报名' }).waitFor();
    await role('管理员');
    await dialogs(['member', '本地页面验收灰度', ''],
      () => card().getByRole('button', { name: '启动志愿者灰度' }).click());
    const canaryStarted = Date.now();
    await card().getByText('灰度 v2', { exact: false }).waitFor();
    await role('普通成员');
    for (const label of ['运行只读样例', '运行写入边界样例']) {
      await card().getByRole('button', { name: label }).click();
      await notice.getByText(/样例运行完成/).waitFor();
    }
    await role('独立审核官');
    await card().getByRole('button', { name: '查看并评审灰度运行' }).click();
    for (let index = 0; index < 2; index++) {
      await card().getByRole('button', { name: '通过', exact: true }).first().waitFor();
      await dialogs([`browser:review-${index + 1}`, ''],
        () => card().getByRole('button', { name: '通过', exact: true }).first().click());
      await card().getByText(`browser:review-${index + 1}`, { exact: false }).waitFor();
    }
    console.log('browser: canary samples independently reviewed');
    await role('管理员');
    await page.waitForTimeout(Math.max(0, 62000 - (Date.now() - canaryStarted)));
    const [readiness] = await Promise.all([
      page.waitForEvent('dialog'),
      card().getByRole('button', { name: '检查转正条件' }).click(),
    ]);
    assert.match(readiness.message(), /已满足本地转正门槛/);
    await readiness.accept();
    await dialogs(['本地页面验收转正', ''], () => card().getByRole('button', { name: '人工转正' }).click());
    await card().getByText('稳定版 v2', { exact: false }).waitFor();
    await dialogs(['本地页面验收暂停', ''], () => card().getByRole('button', { name: '暂停 Agent' }).click());
    await card().getByText('已暂停', { exact: true }).waitFor();
    await dialogs(['本地页面验收恢复', ''], () => card().getByRole('button', { name: '恢复 Agent' }).click());
    await card().getByText('运行中', { exact: true }).waitFor();
    await card().getByRole('button', { name: '运行诊断与版本对比' }).click();
    await card().getByText('v1：建议先核对', { exact: false }).waitFor();
    await card().getByText('v2：建议先核对', { exact: false }).waitFor();
    await page.screenshot({ path: `${output}/fde-lifecycle-comparison.png`, fullPage: true });
    await dialogs(['1', '本地页面验收回退', ''], () => card().getByRole('button', { name: '人工回退' }).click());
    await card().getByText('稳定版 v1', { exact: false }).waitFor();
    await dialogs(['本地页面验收归档', ''], () => card().getByRole('button', { name: '下架归档' }).click());
    await card().getByText('已归档', { exact: true }).waitFor();

    await page.setViewportSize({ width: 390, height: 844 });
    await page.screenshot({ path: `${output}/fde-lifecycle-mobile.png`, fullPage: true });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    const actionWidths = await page.locator('#agent-list .agent-review-actions button')
      .evaluateAll(buttons => buttons.map(button => button.getBoundingClientRect().width));
    assert.equal(actionWidths.every(width => width >= 110), true);
    assert.equal(await page.locator('#view-agents h1').evaluate(heading => heading.getBoundingClientRect().width >= 200), true);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ agent: name, lifecycle: 'passed', mobile: 'passed', page_errors: 0 }));
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
