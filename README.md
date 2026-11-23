# Trace Ledger · 电商支付、退款与对账测试平台

**三方净额都是 ¥70.00，为什么仍有两笔错账？** 一笔 ¥100.00 的支付和一笔 ¥30.00 的退款，在渠道账单上分别变成了 ¥101.00 和 ¥31.00。汇总后仍是 ¥70.00，按交易 ID 比较才看得出两笔错账。

![主案例实拍，三方净额一致且检出两笔反向账差](reports/preview.png)

## 快速演示

在 Windows 安装 Python 3.13 和 Microsoft Edge。解压后在项目根目录打开 PowerShell，运行以下命令。

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python app.py
```

打开 http://127.0.0.1:5051/ 并点击“运行主案例 · 净额相同”。先核对订单净额、本地流水和渠道净额均为 ¥70.00，再看差异表。

| 交易 | 本地流水 | 渠道账单 | 差额（渠道 − 本地） |
| --- | ---: | ---: | ---: |
| 支付 | ¥100.00 | ¥101.00 | +¥1.00 |
| 退款 | −¥30.00 | −¥31.00 | −¥1.00 |

点击两条差异的交易 ID，核对相应的回调与金额。渠道 CSV 使用预先设定的 10100 分和 −3100 分，只借用接口返回的交易 ID 做关联。再次点击“导入并对账”，同一账单显示为重复导入，差异仍为两笔。

面试时可以当场运行 `.\.venv\Scripts\python tools/verify_faults.py`。脚本在临时副本里分别跑正常实现和改成只比较净额的实现。正常实现通过，故意改错的版本实际返回零笔差异，被同一条测试拦下。[接口断言](tests/test_payments.py#L117) · [实验结果](reports/faults.json)

## 备用场景

点击“备用场景 · 乱序重发”，先申请退 30 元和 20 元，再先投递 20 元回调并重发一次。核对“重复事件”、累计退款 ¥50.00 和渠道账单的单笔差异。并发超退和流水写入回滚可直接运行相应的 [接口测试](tests/test_payments.py)。

手动操作时先点击支付行的“成功”，提交两笔退款，再选择回调顺序。点击“重发”会沿用原事件 ID。可以贴入或上传带有 `operation_id,delta` 表头的 CSV，金额单位是分，支付为正，退款为负。模拟数据存储在本地 SQLite。

## 质量证据

| 风险 | 怎么测 | 预期与证据 |
| --- | --- | --- |
| 支付回调延迟或金额错误 | Requests 请求接口，核对回复和订单快照 | 回调成功前禁止退款，错误金额和冲突终态不入账 |
| 两笔退款乱序、重复回调 | pytest 按两种顺序回放，同一交易使用相同和不同事件 ID 重试，直接查询 SQLite | 累计退款恰好 5000 分，只写三条资金流水 |
| 并发重试与超额退款 | 用线程屏障同时发起真实 HTTP 请求 | 8 次回调只记账一次；已退 50 元后并发申请两笔 40 元，仅受理一笔 |
| 退款失败和数据库中途写入失败 | 失败回调后复查占额；用 SQLite 触发器模拟流水写入失败并重试 | 失败退款释放额度；写入故障时订单、流水与回调记录一起回滚 |
| 净额相同仍有账差 | 让支付与退款各相差 100 分且方向相反 | 精确断言交易 ID、本地金额、渠道金额与差异类型，并验证两条有符号差额 |
| 界面展示与报文一致 | Playwright 操作网页并读取真实浏览器 HAR | 核对状态、回调先后、相同请求体、去重回执、对账金额及移动宽度下的布局 |

两次定向故障实验分别跳过退款占额、跳过净额相等时的逐笔对账。两条对应测试在正常版本通过，在故意改错的临时副本中都因业务断言失败。对照结果记录了两个 201 和漏检两笔差异的实际断言。[故障实验结果](reports/faults.json)

本地完整回归 **12 项通过**。Locust 用 4 个虚拟用户执行 8 秒混合压测，每个流程创建订单、支付、部分退款并导入相符账单。此次完成 140 个对账流程，共 701 个 HTTP 请求，零失败，整体 P95 为 67 ms。自动验收要求至少完成 10 个流程、零失败、整体 P95 不超过 250 ms。这组数字描述本机冒烟运行。

```powershell
.\.venv\Scripts\python -m pytest -q --junitxml=reports/results.xml
.\.venv\Scripts\python tools/verify_faults.py
```

- 主案例和备用场景均有真实浏览器 HTTP 记录。导入 Edge 或 Chrome DevTools 的 Network 面板后，可核对 `/refunds`、`/callbacks` 和 `/statements`。[主案例 HAR](reports/equal-totals.har) · [乱序重发 HAR](reports/payment-session.har)
- [Locust HTML 报告](reports/load.html) 可以直接用浏览器打开。[流水线统计 CSV](reports/load_stats.csv)
- [pytest JUnit 报告](reports/results.xml) 保存每条用例的执行结果。

## 实现范围与参考

固定订单金额、模拟渠道、SQLite 单机事务。退款申请先预占未退金额，成功回调再把占额转成退款流水。同一事件 ID 防重复投递，已完成交易防换事件 ID 再入账。对账同时校验订单净额、流水净额和按交易 ID 比较后的渠道差异。

回调场景参考 Stripe 关于乱序与重复投递的公开文档。页面、HTTP 和数据交叉核验参考 Cypress Real World App 的支付用例。浏览器抓包使用 Playwright 的 HAR 录制接口。[Stripe Webhooks](https://docs.stripe.com/webhooks) · [Cypress 支付测试源码](https://github.com/cypress-io/cypress-realworld-app/blob/develop/cypress/tests/ui/new-transaction.spec.ts) · [Playwright Python](https://github.com/microsoft/playwright-python)
