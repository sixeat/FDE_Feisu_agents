const assert = require('node:assert/strict');
const { chromium } = require(process.env.FDE_PLAYWRIGHT_MODULE || 'playwright');

const base = process.env.FDE_LOCAL_DEMO_URL || 'http://127.0.0.1:8212';
const runId = Date.now();
const chatId = `oc_${String(runId).slice(-10)}123`;
const label = `流程演练 ${runId}`;

async function main() {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1365, height: 900 } });
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(`${base}/local-demo`);
    await page.getByRole('button', { name: '普通成员', exact: true }).click();
    await page.getByRole('tab', { name: '流程发现' }).click();
    await page.locator('#workflow-demo-banner').waitFor({ state: 'visible' });
    await page.locator('#workflow-reauthorize').waitFor({ state: 'hidden' });
    await page.locator('#workflow-chat-id').fill(chatId);
    await page.locator('#workflow-chat-label').fill(label);
    await page.locator('#workflow-consent').check();
    await page.getByRole('button', { name: '登记订阅' }).click();
    const subscription = page.locator('#workflow-subscriptions .workflow-row').filter({ hasText: label });
    await subscription.waitFor();
    await subscription.getByText(/读取说明 v1 · 同意时间/).waitFor();
    await subscription.getByRole('button', { name: '扫描最近 24 小时' }).click();
    await page.locator('#workflow-notice').getByText(/扫描完成：读取 2 条消息，发现 1 个候选/).waitFor();
    const opportunity = page.locator('#workflow-opportunities .workflow-opportunity').filter({ hasText: label });
    await opportunity.waitFor();
    await opportunity.locator('textarea').fill('每周都会重复整理课程资料');
    await opportunity.getByRole('button', { name: '采纳' }).click();
    await opportunity.getByRole('button', { name: '生成待审核草稿' }).waitFor();
    await opportunity.locator('input').fill(`${label} Agent`);
    await opportunity.getByRole('button', { name: '生成待审核草稿' }).click();
    await page.locator('#workflow-notice').getByText(/状态为待审核；未授予工具权限/).waitFor();
    assert.match(await opportunity.innerText(), /已生成草稿/);
    await opportunity.getByRole('button', { name: '查看 Agent 草稿' }).click();
    await page.locator('#view-agents').waitFor({ state: 'visible' });
    await page.locator('#agent-draft-list').getByText(`${label} Agent`).waitFor();
    await page.getByRole('tab', { name: '流程发现' }).click();
    await subscription.getByRole('button', { name: '撤销订阅' }).click();
    await subscription.getByText('已撤销').waitFor();
    await subscription.getByRole('button', { name: '扫描最近 24 小时' }).waitFor({ state: 'detached' });

    const p2pLabel = `单聊演练 ${runId}`;
    await page.locator('#workflow-chat-type').selectOption('P2P');
    await page.locator('#workflow-chat-id').fill(`oc_${String(runId).slice(-10)}456`);
    await page.locator('#workflow-chat-label').fill(p2pLabel);
    await page.locator('#workflow-consent').check();
    await page.getByRole('button', { name: '登记订阅' }).click();
    const p2p = page.locator('#workflow-subscriptions .workflow-row').filter({ hasText: p2pLabel });
    await p2p.getByText('单聊 · 订阅中').waitFor();
    await p2p.getByRole('button', { name: '扫描最近 24 小时' }).click();
    await page.locator('#workflow-opportunities .workflow-opportunity').filter({ hasText: p2pLabel }).waitFor();
    await page.route('**/api/workflow-opportunities/subscriptions/*/scan', route => route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'FEISHU_CHAT_READ_99991663' }),
    }));
    await p2p.getByRole('button', { name: '扫描最近 24 小时' }).click();
    const failureNotice = await page.locator('#workflow-notice').innerText();
    assert.match(failureNotice, /请刷新授权状态/);
    assert.doesNotMatch(failureNotice, /99991663/);
    await page.unroute('**/api/workflow-opportunities/subscriptions/*/scan');
    await p2p.getByRole('button', { name: '撤销订阅' }).click();
    await p2p.getByText('已撤销').waitFor();
    assert.deepEqual(errors, []);
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    console.log(JSON.stringify({ flow: 'group-subscribe-scan-accept-draft-revoke; p2p-subscribe-scan-revoke', status: 'passed', page_errors: 0 }));
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
