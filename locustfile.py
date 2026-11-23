from uuid import uuid4

from locust import HttpUser, between, events, task


class PaymentUser(HttpUser):
    wait_time = between(0.05, 0.15)

    def send(self, method, url, expected_code, name, **options):
        with self.client.request(method, url, catch_response=True, name=name, **options) as response:
            if response.status_code != expected_code:
                response.failure(f"Expected {expected_code}, got {response.status_code}")
                return None
            return response.json()

    def callback(self, path, operation):
        event = {
            "operation_id": operation["id"], "event_id": uuid4().hex,
            "outcome": "succeeded", "amount": operation["amount"],
        }
        with self.client.post(path + "/callbacks", name="POST callbacks",
                              catch_response=True, json=event) as response:
            if response.status_code != 200 or response.json().get("decision") != "applied":
                response.failure("Callback was not applied")
                return False
        return True

    @task
    def payment_refund_reconcile(self):
        initial = self.send("POST", "/api/orders", 201, "POST order")
        if initial is None:
            return
        path = "/api/orders/" + initial["order"]["id"]
        payment = initial["operations"][0]
        if not self.callback(path, payment):
            return
        refund = self.send("POST", path + "/refunds", 201, "POST refund",
                           json={"amount": 3000}, headers={"Idempotency-Key": uuid4().hex})
        if refund is None:
            return
        if not self.callback(path, refund):
            return
        csv = f'operation_id,delta\n{payment["id"]},10000\n{refund["id"]},-3000'
        with self.client.post(path + "/statements", data=csv, name="POST statements",
                              catch_response=True) as response:
            if response.status_code != 200:
                response.failure("Statement import failed")
                return
            result = response.json()
            if (result["differences"] or result["projection_mismatch"]
                    or result["order_net"] != 7000 or result["channel_net"] != 7000):
                response.failure("Unexpected reconciliation result")


@events.quitting.add_listener
def check(environment, **_):
    total = environment.stats.total
    finished = environment.stats.get("POST statements", "POST").num_requests
    if (finished < 10 or total.num_failures or environment.runner.exceptions
            or total.get_response_time_percentile(0.95) > 250):
        environment.process_exit_code = 1
