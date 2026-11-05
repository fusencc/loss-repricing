"""仅针对本机演示环境的实际浏览器验收；读取本地凭据但不打印或截图密码。"""
from pathlib import Path
import sys
import re
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.config import get_settings
from playwright.sync_api import sync_playwright, expect

config = get_settings()
if config.adapter_mode != "demo":
    raise RuntimeError("界面演示测试只允许demo模式")
output = config.runtime_dir / "ui-check"
output.mkdir(parents=True, exist_ok=True)
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1000}, device_scale_factor=1)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto("http://127.0.0.1:18765", wait_until="networkidle")
    expect(page.get_by_role("button", name="登录工作台")).to_be_visible()
    page.get_by_placeholder("请输入账号").fill(config.admin_username)
    page.get_by_placeholder("请输入密码").fill(config.admin_password)
    page.get_by_role("button", name="登录工作台").click()
    expect(page.get_by_role("button", name="运行演示批次")).to_be_visible(timeout=15000)
    page.get_by_role("button", name="运行演示批次").click()
    # 强制重启后旧worker的120秒租约可能尚未到期；等待恢复，不绕过持久锁。
    expect(page.get_by_text(re.compile("运行演示批次已完成"))).to_be_visible(timeout=180000)
    page.screenshot(path=str(output / "overview.png"), full_page=True, animations="disabled")
    # 全局列表展示当前状态，重复演示后可能进入72小时保护；计算证据从不可变批次历史查看。
    batch_history = page.request.get("http://127.0.0.1:18765/api/batches").json()
    sample = None
    for historical_batch in reversed(batch_history):
        detail = page.request.get(f"http://127.0.0.1:18765/api/batches/{historical_batch['id']}").json()
        sample = next((item for item in detail["items"] if item["key"]["sku"] == "DEMO-SKU-000" and item["status"] == "已核验"), None)
        if sample:
            break
    assert sample is not None, "未找到已核验的演示计算记录"
    page.get_by_role("link", name="运行批次", exact=True).click()
    page.get_by_role("row").filter(has_text=sample["batch_id"]).get_by_role("button", name="查看批次").click()
    expect(page.get_by_text("DEMO-SKU-000", exact=True)).to_be_visible(timeout=15000)
    page.get_by_role("row").filter(has_text="DEMO-SKU-000").get_by_role("button", name="查看详情").click()
    expect(page.get_by_role("heading", name="补亏计算")).to_be_visible()
    expect(page.locator(".el-drawer .el-loading-mask")).to_be_hidden(timeout=15000)
    page.screenshot(path=str(output / "calculation.png"), full_page=True, animations="disabled")
    page.keyboard.press("Escape")
    page.get_by_role("link", name="运行配置", exact=True).click()
    expect(page.get_by_text("试点店铺", exact=False).first).to_be_visible()
    page.screenshot(path=str(output / "settings.png"), full_page=True, animations="disabled")
    page.set_viewport_size({"width": 390, "height": 844})
    page.get_by_role("link", name="运行概览", exact=True).click()
    page.screenshot(path=str(output / "mobile.png"), full_page=True, animations="disabled")
    assert not errors, errors
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 2"), "页面在窄屏横向溢出"
    browser.close()
print("UI PASS: 登录、实际演示任务、批次详情、计算明细、运行配置、390px窄屏；无页面JavaScript异常")
