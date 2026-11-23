import json
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright


def test_preset_tracks_out_of_order_duplicate_and_difference(base_url):
    reports = Path("reports")
    reports.mkdir(exist_ok=True)
    har = reports / "payment-session.har"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge")
        context = browser.new_context(viewport={"width": 1360, "height": 1060},
                                      record_har_path=har, record_har_content="embed",
                                      record_har_url_filter=f"{base_url}/api/**")
        try:
            page = context.new_page()
            page.goto(base_url)
            expect(page.get_by_role("status")).to_contain_text("可手动投递")
            page.get_by_role("button", name="运行预设场景").click()
            expect(page.get_by_role("status")).to_contain_text("预设场景完成")
            expect(page.locator("#refunded")).to_have_text("¥50.00")
            expect(page.locator("#order-state")).to_have_text("部分退款")
            expect(page.locator("#totals")).to_contain_text("1 笔差异")
            expect(page.locator("#events")).to_contain_text("重复事件")
            expect(page.locator("#differences")).to_contain_text("¥-1.00")
            page.screenshot(path=reports / "preview.png", full_page=True)
            page.locator("#differences button").click()
            expect(page.locator("#payload")).to_contain_text('"channel": -2100')
            page.get_by_role("button", name="导入并对账").click()
            expect(page.locator("#totals")).to_contain_text("重复导入")
            page.set_viewport_size({"width": 390, "height": 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        finally:
            context.close()
            browser.close()

    entries = json.loads(har.read_text(encoding="utf-8"))["log"]["entries"]
    by_route = {}
    for item in entries:
        req, res = item["request"], item["response"]
        route = urlsplit(req["url"]).path.rsplit("/", 1)[-1]
        if req["method"] == "POST" and route in {"refunds", "callbacks", "statements"}:
            by_route.setdefault(route, []).append(
                (req["postData"]["text"], json.loads(res["content"]["text"]))
            )
    submitted = [item[1]["id"] for item in by_route["refunds"]]
    callbacks = by_route["callbacks"]
    assert [json.loads(item[0])["operation_id"] for item in callbacks[1:]] == [
        submitted[1], submitted[1], submitted[0],
    ]
    assert callbacks[1][0] == callbacks[2][0]
    assert [item[1]["decision"] for item in callbacks] == ["applied", "applied", "duplicate_event", "applied"]
    assert [item[1]["duplicate"] for item in by_route["statements"]] == [False, True]
