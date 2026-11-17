// Start serve-records-demo.py first. Requires Playwright; optional module/browser paths come from environment.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const base = process.env.RECORDS_TEST_URL || 'http://127.0.0.1:18827';
const out = process.env.RECORDS_SCREENSHOTS || path.resolve('runtime/records-glass-ui');
async function main(){
  await fs.mkdir(out,{recursive:true});
  const browser = await chromium.launch({headless:true,...(process.env.CHROME_EXECUTABLE?{executablePath:process.env.CHROME_EXECUTABLE}:{})});
  const page = await browser.newPage({viewport:{width:1440,height:1000},deviceScaleFactor:1});
  const errors=[]; page.on('pageerror',e=>errors.push(e.message));
  await page.goto(base);
  await page.locator('.records-person-row').first().waitFor();
  assert.equal(await page.locator('.records-person-row').count(),4);
  assert.equal(await page.locator('select').count(),0);
  assert.equal(await page.locator('.records-departments button').count(),4);
  assert.equal(await page.locator('input').count(),0);
  assert.equal(await page.locator('.records-page button').count(),8);
  const url=page.url();
  await page.screenshot({path:path.join(out,'desktop.png'),fullPage:true});
  const opener=page.getByRole('button',{name:'查看林晓的全部3条提价日志'});
  await opener.focus(); await page.keyboard.press('Enter');
  const modal=page.getByRole('dialog');
  const waitModal=async()=>{
    await modal.waitFor({state:'visible'});
    await page.waitForFunction(()=>{
      const overlay=document.querySelector('.records-overlay');
      const dialog=document.querySelector('.records-dialog');
      return overlay&&dialog&&getComputedStyle(overlay).opacity==='1'&&getComputedStyle(dialog).opacity==='1'
        && !overlay.getAnimations({subtree:true}).some(a=>a.playState==='running');
    });
  };
  await waitModal();
  assert.equal(await page.locator('.records-log').count(),3);
  assert.equal(page.url(),url);
  assert.match(await modal.innerText(),/建议上传价/);
  assert.match(await modal.innerText(),/回读实际价/);
  assert.match(await modal.innerText(),/模拟提价/);
  assert.doesNotMatch(await modal.innerText(),/更新时间|商品 \/ 子商品 ID|执行进度|核验确认时间|保护起算时间|分口径价格存档|观察时间|批次 demo/);
  assert.match(await modal.innerText(),/已提价/);
  assert.match(await modal.innerText(),/待处理/);
  assert.match(await modal.innerText(),/活动中/);
  const rect=await modal.boundingBox();
  assert.ok(Math.abs(rect.x-(1440-rect.width)/2)<3);
  await page.screenshot({path:path.join(out,'dialog-desktop.png')});
  await page.keyboard.press('Escape');
  await modal.waitFor({state:'hidden'});
  await page.waitForFunction(()=>document.activeElement?.classList.contains('records-person-button'));
  await page.locator('.records-person-row').nth(1).click();
  await waitModal();
  assert.match(await modal.innerText(),/陈雨的提价日志/);
  assert.equal(await page.locator('.records-log').count(),3);
  await page.locator('.records-dialog .el-dialog__headerbtn').click();
  await modal.waitFor({state:'hidden'});
  await page.getByRole('button',{name:'Tiktok五部',exact:true}).click();
  await page.getByRole('button',{name:'查看苏晴的全部3条提价日志'}).waitFor();
  assert.equal(await page.locator('.records-person-row').count(),2);
  assert.equal(await page.getByRole('button',{name:'查看林晓的全部3条提价日志'}).count(),0);
  // 延迟旧部门响应，验证快速切换不会让旧数据覆盖新选择。
  await page.route('**/api/records?department=*',async route=>{
    if(decodeURIComponent(route.request().url()).includes('Tiktok一部')){await new Promise(r=>setTimeout(r,500));}
    try{await route.continue();}catch{}
  });
  await page.getByRole('button',{name:'Tiktok一部',exact:true}).click();
  await page.getByRole('button',{name:'Tiktok五部',exact:true}).click();
  await page.getByRole('button',{name:'查看苏晴的全部3条提价日志'}).waitFor();
  await page.waitForTimeout(650);
  assert.equal(await page.locator('.records-person-row').count(),2);
  await page.unroute('**/api/records?department=*');
  await page.route('**/api/records?department=*',route=>route.fulfill({status:500,contentType:'application/json',body:JSON.stringify({detail:'演示取数失败'})}));
  await page.getByRole('button',{name:'Tiktok一部',exact:true}).click();
  await page.getByRole('alert').waitFor();
  assert.equal(await page.locator('.records-person-row').count(),0);
  assert.equal(await page.getByRole('dialog').count(),0);
  await page.unroute('**/api/records?department=*');
  await page.getByRole('button',{name:'Tiktok五部',exact:true}).click();
  await page.getByRole('button',{name:'查看苏晴的全部3条提价日志'}).waitFor();
  for(const width of [768,390,320]){
    await page.setViewportSize({width,height:844});
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    await page.locator('.records-person-row').first().click();
    await waitModal();
    assert.equal(await page.locator('.records-log').count(),3);
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1));
    assert.ok(await page.locator('.records-log-list').evaluate(e=>e.scrollWidth<=e.clientWidth+1));
    const box=await modal.boundingBox();assert.ok(box.x>=0&&box.x+box.width<=width+1&&box.y>=0&&box.y+box.height<=845,JSON.stringify({width,box}));
    if(width===390)await page.screenshot({path:path.join(out,'dialog-mobile.png')});
    await page.keyboard.press('Escape');await modal.waitFor({state:'hidden'});
    if(width===390)await page.screenshot({path:path.join(out,'mobile.png'),fullPage:true});
  }
  await page.getByRole('button',{name:'Tiktok十二部',exact:true}).click();
  await page.getByRole('heading',{name:'暂无提价记录'}).waitFor();
  await page.route('**/api/records*',route=>route.fulfill({contentType:'application/json',body:JSON.stringify({departments:[],department:null,total:0,shops:0,counts:{},items:[]})}));
  await page.reload();await page.getByRole('heading',{name:'暂无提价记录'}).waitFor();
  assert.equal(await page.locator('.records-person-row').count(),0);
  assert.equal(await page.locator('.records-departments button').count(),4);
  assert.deepEqual(errors,[]);
  await browser.close();
  console.log('PASS: real demo API, all seller logs, keyboard/row/modal/focus, department race/error/empty, 1440/768/390/320px. No page errors.');
}
main().catch(error=>{console.error(error);process.exit(1)});
