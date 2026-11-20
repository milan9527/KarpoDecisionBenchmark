"""Route 1/2/3 replicas across a two-GPU primary and a one-GPU secondary."""
import smallbatch_proxy as base


class Router(base.Router):
    def __init__(self, urls):
        assert len(urls) == 2
        super().__init__(urls)
        self.capacities = [2, 1]

    def configure(self, config):
        replicas = config["replicas"]
        if replicas not in (1, 2, 3):
            raise ValueError("Unsupported replica count")
        with self.lock:
            if any(self.busy):
                raise ValueError("Requests are pending")
            for capacity, client in zip(self.capacities, self.clients, strict=True):
                response = client.post("/configure", json=dict(config, replicas=min(replicas, capacity)))
                response.raise_for_status()
            self.active = 2 if replicas == 3 else 1
            self.config = dict(config)
        return self.config

    def call(self, request):
        with self.lock:
            index = min(range(self.active), key=lambda i: (
                self.busy[i] / self.capacities[i], (i - self.counter) % self.active))
            self.busy[index] += 1
            self.counter += 1
        try:
            response = self.clients[index].post("/v1/systemone", json=request)
            body = response.json()
            if response.is_success:
                body["metrics"]["replica_host"] = index
                body["metrics"]["global_gpu"] = index * 2 + body["metrics"]["gpu"]
            return response.status_code, body
        finally:
            with self.lock:
                self.busy[index] -= 1


if __name__ == "__main__":
    base.Router = Router
    base.main()
