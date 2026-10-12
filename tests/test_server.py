import http.client
import json
import threading
import unittest

from localcu.drivers import DemoDriver
from localcu.engine import Engine
from localcu.provider import DemoPlanner
from localcu.server import LocalServer


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(DemoDriver(), DemoPlanner)
        self.server = LocalServer(0, self.engine, demo=True)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(1); self.engine.close()

    def request(self, method="GET", path="/api/state", body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
        defaults = {"Authorization": "Bearer " + self.server.token, "Content-Type": "application/json"}
        defaults.update(headers or {})
        connection.request(method, path, json.dumps(body) if body is not None else None, defaults)
        response = connection.getresponse()
        result = response.status, response.read(), dict(response.getheaders())
        connection.close()
        return result

    def test_auth_origin_host_and_fetch_site_enforced(self):
        self.assertEqual(self.request()[0], 200)
        for headers, code in [({"Authorization": ""}, 401), ({"Origin": "https://evil.example"}, 403),
                              ({"Host": "evil.example"}, 403), ({"Sec-Fetch-Site": "cross-site"}, 403)]:
            with self.subTest(headers=headers):
                self.assertEqual(self.request(headers=headers)[0], code)

    def test_static_routes_cannot_read_arbitrary_files_or_leak_token(self):
        code, body, headers = self.request(path="/")
        self.assertEqual(code, 200)
        self.assertNotIn(self.server.token.encode(), body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(self.request(path="/../../.env")[0], 404)
        self.assertEqual(self.request(path="/.env")[0], 404)

    def test_bad_json_content_type_and_unknown_route(self):
        self.assertEqual(self.request("POST", "/api/start", {"task": "t"}, {"Content-Type": "text/plain"})[0], 415)
        self.assertEqual(self.request("POST", "/api/start", ["wrong"])[0], 400)
        self.assertEqual(self.request("POST", "/api/unknown", {})[0], 404)

    def test_observe_then_start_and_reject_via_http(self):
        target = DemoDriver.target
        code, body, _ = self.request("POST", "/api/observe", {"target": target})
        self.assertEqual(code, 200)
        self.assertIn("screenshot", json.loads(body))
        self.assertEqual(self.request("POST", "/api/start", {"target": target, "task": "demo"})[0], 200)
        from test_core import wait_for
        state = wait_for(self.engine, "awaiting_approval")
        self.assertEqual(self.request("POST", "/api/reject", {"id": state["pending"]["id"]})[0], 200)
        wait_for(self.engine, "cancelled")
        self.assertEqual(self.engine.driver.actions, [])


if __name__ == "__main__":
    unittest.main()
