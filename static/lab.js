const select = (selector) => document.querySelector(selector);
const currency = (cents) => cents == null ? "—" : (cents < 0 ? "−" : "") + "¥" + (Math.abs(cents) / 100).toFixed(2);
const signedCurrency = (cents) => (cents > 0 ? "+" : "") + currency(cents);
const labels = {
  pending: "待回调", paid: "已支付", refunded: "已全额退款", partially_refunded: "部分退款",
  succeeded: "成功", failed: "失败", applied: "已入账", duplicate_event: "重复事件",
  duplicate_operation: "重复交易", amount_mismatch: "金额不一致",
  missing_local: "本地缺失", missing_channel: "渠道缺失", refund_limit: "剩余可退金额不足",
  terminal_state_conflict: "交易已结束，回调结果冲突"
};
const label = (value) => labels[value] || value;
let state;
let orderId = sessionStorage.getItem("payment-qa-order");
let lastEvents = new Map();
let deliveries = [];

function note(message) {
  select("#message").textContent = message;
}

async function api(path, method = "GET", data, headers = {}) {
  const json = typeof data === "object";
  const response = await fetch(path, {
    method, headers: {...(json ? {"Content-Type": "application/json"} : {}), ...headers},
    body: json ? JSON.stringify(data) : data
  });
  const result = await response.json();
  const log = document.createElement("button");
  log.textContent = method + " " + (path.replace("/api/orders/" + orderId, "") || "订单") + " · " + response.status;
  if (data?.operation_id) log.dataset.operation = data.operation_id;
  log.onclick = () => {
    select("#payload").textContent = JSON.stringify({method, path, request: data, status: response.status, response: result}, null, 2);
  };
  select("#requests").prepend(log);
  log.click();
  if (!response.ok) throw new Error(label(result.error));
  return result;
}

function rows(selector, items) {
  const body = select(selector);
  body.replaceChildren();
  for (const values of items) {
    const row = body.insertRow();
    for (const value of values) {
      const cell = row.insertCell();
      if (value instanceof Node) cell.append(value);
      else cell.textContent = value;
    }
  }
}

function actionButton(text, action) {
  const button = document.createElement("button");
  button.textContent = text;
  button.addEventListener("click", () => run(action));
  return button;
}

function render() {
  const order = state.order;
  for (const field of ["paid", "refunded", "available"]) select("#" + field).textContent = currency(order[field]);
  select("#order-state").textContent = label(order.state);
  select("#order-id").textContent = "ORDER / " + order.id;
  rows("#operations", state.operations.map(op => {
    const controls = document.createElement("div");
    if (op.state === "pending") {
      controls.append(actionButton("成功", () => deliver(op, "succeeded")));
      controls.append(actionButton("失败", () => deliver(op, "failed")));
    }
    if (lastEvents.has(op.id)) controls.append(actionButton("重发", () => deliver(op, lastEvents.get(op.id).outcome, true)));
    return [(op.kind === "payment" ? "支付" : "退款") + " · " + op.id, currency(op.amount), label(op.state), controls];
  }));
  const history = deliveries.length ? deliveries : state.events;
  rows("#events", history.map(e => [e.event_id, e.operation_id, label(e.decision)]));
}

async function refresh() {
  state = await api("/api/orders/" + orderId);
  render();
}

async function newOrder() {
  select("#requests").replaceChildren();
  select("#statement").value = "";
  select("#totals").textContent = "完成回调投递后，导入渠道账单开始对账。";
  rows("#differences", []);
  state = await api("/api/orders", "POST");
  orderId = state.order.id;
  sessionStorage.setItem("payment-qa-order", orderId);
  lastEvents = new Map();
  deliveries = [];
  render();
}

async function refund(amount) {
  const result = await api("/api/orders/" + orderId + "/refunds", "POST", {amount}, {"Idempotency-Key": crypto.randomUUID()});
  await refresh();
  return result;
}

async function deliver(op, outcome, repeat = false) {
  const event = repeat ? lastEvents.get(op.id) : {
    event_id: "evt_" + crypto.randomUUID().slice(0, 8), operation_id: op.id, amount: op.amount, outcome
  };
  const result = await api("/api/orders/" + orderId + "/callbacks", "POST", event);
  lastEvents.set(op.id, event);
  if (!deliveries.length) deliveries = [...state.events];
  deliveries.push({...event, decision: result.decision});
  await refresh();
  note(label(result.decision) + " · " + event.event_id);
}

function sample() {
  const successful = state.operations.filter(op => op.state === "succeeded");
  const refund = successful.findLast(op => op.kind === "refund");
  if (!refund) throw new Error("先完成至少一笔退款。");
  const lines = successful.map(op => {
    const delta = op.kind === "payment" ? op.amount : -op.amount;
    return op.id + "," + (op.id === refund.id ? delta - 100 : delta);
  });
  select("#statement").value = ["operation_id,delta", ...lines].join("\n");
}

async function reconcile() {
  const source = select("#statement").value;
  const r = await api("/api/orders/" + orderId + "/statements", "POST", source, {"Content-Type": "text/csv"});
  const totalsMatch = r.order_net === r.ledger_net && r.ledger_net === r.channel_net;
  select("#totals").textContent = [
    "订单净额 " + currency(r.order_net), "本地流水 " + currency(r.ledger_net),
    "渠道净额 " + currency(r.channel_net), r.differences.length + " 笔差异",
    totalsMatch ? "三方净额一致" : r.projection_mismatch ? "订单与流水不平" : "渠道与本地净额不一致",
    r.duplicate ? "重复导入" : "账单已导入"
  ].join(" · ");
  rows("#differences", r.differences.map(d => [
    actionButton(d.operation_id, () => {
      select("details").open = true;
      const match = Array.from(select("#requests").children).find(item =>
        item.dataset.operation === d.operation_id);
      if (match) match.click();
      else select("#payload").textContent = "";
      select("#payload").textContent += "\n\n" + JSON.stringify({
        operation: state.operations.find(op => op.id === d.operation_id),
        callbacks: state.events.filter(event => event.operation_id === d.operation_id),
        local: d.local, channel: d.channel
      }, null, 2);
      select("details").scrollIntoView({block: "nearest"});
    }),
    label(d.reason), currency(d.local), currency(d.channel),
    d.local == null || d.channel == null ? "—" : signedCurrency(d.channel - d.local)
  ]));
}

async function demo() {
  await newOrder();
  const payment = state.operations[0];
  await deliver(payment, "succeeded");
  const partialRefund = await refund(3000);
  await deliver(partialRefund, "succeeded");
  // Fixed channel amounts are independent of the order projection and ledger.
  select("#statement").value = ["operation_id,delta", payment.id + ",10100", partialRefund.id + ",-3100"].join("\n");
  await reconcile();
  note("主案例完成。点击两条差异的交易 ID 查看回调与流水。");
}

async function replay() {
  await newOrder();
  await deliver(state.operations[0], "succeeded");
  const first = await refund(3000);
  const second = await refund(2000);
  await deliver(second, "succeeded");
  await deliver(second, "succeeded", true);
  await deliver(first, "succeeded");
  sample();
  await reconcile();
  note("乱序重发场景完成。点击差异行的交易 ID 查看回调和流水。");
}

let busy = false;
async function run(action) {
  if (busy) return;
  busy = true;
  try {
    await action();
  } catch (error) {
    note(error.message);
  } finally {
    busy = false;
  }
}

const actions = {new: newOrder, demo, replay, sample, reconcile};
for (const button of document.querySelectorAll("[data-action]")) {
  button.onclick = () => run(actions[button.dataset.action]);
}
select("#file").onchange = () => run(async () => {
  const file = select("#file").files[0];
  if (file) select("#statement").value = await file.text();
});
select("#refund-form").onsubmit = (event) => {
  event.preventDefault();
  run(async () => {
    const input = select("#refund-input").value.trim();
    if (!/^\d+(\.\d{1,2})?$/.test(input)) throw new Error("请输入金额，最多两位小数。");
    const [yuan, cents = ""] = input.split(".");
    await refund(Number(yuan) * 100 + Number(cents.padEnd(2, "0")));
    note("退款已申请，等待渠道回调。");
  });
};
run(async () => {
  if (orderId) await refresh();
  else await newOrder();
  note("运行主案例，检查净额相同时能否检出逐笔差异。");
});
