import json
import sqlite3
from contextlib import closing
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
            expect(page.get_by_role("status")).to_contain_text("运行主案例")
            page.get_by_role("button", name="备用场景").click()
            expect(page.get_by_role("status")).to_contain_text("乱序重发场景完成")
            expect(page.locator("#refunded")).to_have_text("¥50.00")
            expect(page.locator("#order-state")).to_have_text("部分退款")
            expect(page.locator("#totals")).to_contain_text("1 笔差异")
            expect(page.locator("#events")).to_contain_text("重复事件")
            expect(page.locator("#differences")).to_contain_text("−¥1.00")
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


def test_equal_totals_show_both_signed_differences(base_url):
    reports = Path("reports")
    reports.mkdir(exist_ok=True)
    har = reports / "equal-totals.har"
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge")
        context = browser.new_context(
            viewport={"width": 1360, "height": 980}, record_har_path=har,
            record_har_content="embed", record_har_url_filter=f"{base_url}/api/**",
        )
        try:
            page = context.new_page()
            page.goto(base_url)
            expect(page.get_by_role("status")).to_contain_text("运行主案例")
            page.get_by_role("button", name="运行主案例").click()
            expect(page.get_by_role("status")).to_contain_text("主案例完成")
            expect(page.locator("#totals")).to_have_text(
                "订单净额 ¥70.00 · 本地流水 ¥70.00 · 渠道净额 ¥70.00 · 渠道逐笔 2 笔差异 · 三方净额一致 · 账单已导入"
            )
            expect(page.locator("#differences tr")).to_have_count(2)
            payment = page.locator("#differences tr").filter(has_text="+¥1.00")
            refund = page.locator("#differences tr").filter(has_text="−¥1.00")
            expect(payment.locator("td").nth(2)).to_have_text("¥100.00")
            expect(payment.locator("td").nth(3)).to_have_text("¥101.00")
            expect(refund.locator("td").nth(2)).to_have_text("−¥30.00")
            expect(refund.locator("td").nth(3)).to_have_text("−¥31.00")
            payment_id = payment.get_by_role("button").inner_text()
            refund_id = refund.get_by_role("button").inner_text()
            expect(page.locator("#statement")).to_have_value(
                f"operation_id,delta\n{payment_id},10100\n{refund_id},-3100"
            )
            page.screenshot(path=reports / "preview.png", full_page=True)
            for row, operation_id, cents in ((payment, payment_id, 10100), (refund, refund_id, -3100)):
                row.get_by_role("button").click()
                payload = page.locator("#payload")
                expect(payload).to_contain_text(f'"operation_id": "{operation_id}"')
                expect(payload).to_contain_text(f'"channel": {cents}')
            page.get_by_role("button", name="导入并对账").click()
            expect(page.locator("#totals")).to_contain_text("2 笔差异 · 三方净额一致 · 重复导入")
        finally:
            context.close()
            browser.close()

    entries = json.loads(har.read_text(encoding="utf-8"))["log"]["entries"]
    imports = [e for e in entries if e["request"]["url"].endswith("/statements")]
    assert len(imports) == 2
    for entry, duplicate in zip(imports, (False, True)):
        assert entry["request"]["postData"]["text"] == f"operation_id,delta\n{payment_id},10100\n{refund_id},-3100"
        result = json.loads(entry["response"]["content"]["text"])
        assert result["duplicate"] is duplicate
        assert result["order_net"] == result["ledger_net"] == result["channel_net"] == 7000
        assert {d["operation_id"]: d["channel"] - d["local"] for d in result["differences"]} == {
            payment_id: 100, refund_id: -100,
        }


def test_reconciliation_shows_order_drift_even_when_net_matches(base_url, db_path):
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="msedge")
        try:
            page = browser.new_page()
            page.goto(base_url)
            expect(page.get_by_role("status")).to_contain_text("运行主案例")
            page.get_by_role("button", name="运行主案例").click()
            expect(page.get_by_role("status")).to_contain_text("主案例完成")
            drift = page.locator("#projection-differences")
            expect(drift).to_be_hidden()
            statement = page.locator("#statement")
            statement.fill(statement.input_value().replace(",10100", ",10000").replace(",-3100", ",-3000"))
            with closing(sqlite3.connect(db_path)) as db, db:
                db.execute("UPDATE orders SET paid = 9900, refunded = 2900 WHERE refunded = 3000")
            page.get_by_role("button", name="导入并对账").click()
            expect(page.locator("#totals")).to_contain_text("渠道逐笔 0 笔差异 · 三方净额一致")
            expect(page.locator("#paid")).to_have_text("¥99.00")
            expect(page.locator("#refunded")).to_have_text("¥29.00")
            expect(drift).to_have_text(
                "实付不一致（订单 ¥99.00，资金流水 ¥100.00） · 已退款不一致（订单 ¥29.00，资金流水 ¥30.00）"
            )
            page.screenshot(path="reports/order-drift.png", full_page=True)
        finally:
            browser.close()
