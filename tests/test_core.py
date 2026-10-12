import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from localcu.actions import validate_action
from localcu.drivers import DemoDriver
from localcu.engine import Engine
from localcu.provider import DemoPlanner, NoRedirect, OpenAIPlanner, endpoint_url


def wait_for(engine, status, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = engine.state()
        if state["status"] == status:
            return state
        if state["status"] == "error" and status != "error":
            raise AssertionError(state["message"])
        time.sleep(0.005)
    raise AssertionError(f"Expected {status}, got {engine.state()}")


class ActionTests(unittest.TestCase):
    def setUp(self):
        self.driver = DemoDriver()
        self.obs = self.driver.call("observe", **self.driver.target)

    def test_coordinates_are_image_relative_and_bounded(self):
        valid = {"kind": "click", "x": 959, "y": 599, "reason": "测试"}
        self.assertEqual(validate_action(valid, self.obs), valid)
        for value in [-1, 960, float("nan"), float("inf"), True, "1"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_action({**valid, "x": value}, self.obs)

    def test_stale_disabled_secure_and_unknown_actions_rejected(self):
        action = {"kind": "set_value", "element_id": "missing", "text": "test", "reason": "测试"}
        with self.assertRaises(ValueError):
            validate_action(action, self.obs)
        action["element_id"] = "e1"
        for change in ({"enabled": False}, {"subrole": "AXSecureTextField"}):
            obs = copy.deepcopy(self.obs)
            obs["elements"][0].update(change)
            with self.assertRaises(ValueError):
                validate_action(action, obs)
        for change in ({"kind": "shell"}, {"command": "anything"}, {"text": "x" * 4001}):
            with self.assertRaises(ValueError):
                validate_action({**action, **change}, self.obs)

    def test_parameter_confusion_and_global_shortcuts_rejected(self):
        for action in [
            {"kind": "click", "element_id": "e2", "x": 1, "y": 1},
            {"kind": "press_key", "key": "cmd+q"},
            {"kind": "scroll", "x": 10, "y": 10, "scroll_y": 801},
        ]:
            with self.assertRaises(ValueError):
                validate_action({**action, "reason": "测试"}, self.obs)

    def test_generation_is_single_use_and_target_bound(self):
        args = {**self.driver.target, "generation": self.obs["generation"],
                "action": {"kind": "set_value", "element_id": "e1", "text": "hello"}}
        with self.assertRaises(RuntimeError):
            self.driver.call("act", **{**args, "window_id": 2})
        self.driver.call("act", **args)
        with self.assertRaises(RuntimeError):
            self.driver.call("act", **args)


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.driver = DemoDriver()
        self.engine = Engine(self.driver, DemoPlanner)

    def tearDown(self):
        self.engine.close()

    def start(self):
        self.engine.start("填写并保存演示便签", self.driver.target)
        return wait_for(self.engine, "awaiting_approval")

    def test_end_to_end_requires_two_distinct_approvals_and_verifies(self):
        state = self.start()
        self.assertEqual(self.driver.text, "")
        first = state["pending"]["id"]
        with self.assertRaises(ValueError):
            self.engine.decide("wrong", True)
        self.engine.decide(first, True)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            state = self.engine.state()
            if state["pending"] and state["pending"]["id"] != first:
                break
            time.sleep(.005)
        self.assertEqual(state["pending"]["action"]["kind"], "click")
        self.assertFalse(self.driver.saved)
        with self.assertRaises(ValueError):
            self.engine.decide(first, True)
        self.engine.decide(state["pending"]["id"], True)
        done = wait_for(self.engine, "completed")
        self.assertTrue(self.driver.saved)
        self.assertEqual(len(self.driver.actions), 2)
        self.assertIn("已保存", done["observation"]["elements"][1]["value"])

    def test_reject_and_stop_never_execute_pending_action(self):
        state = self.start()
        self.engine.decide(state["pending"]["id"], False)
        wait_for(self.engine, "cancelled")
        self.engine._thread.join(1)
        self.start()
        self.engine.stop()
        wait_for(self.engine, "cancelled")
        self.assertEqual(self.driver.actions, [])

    def test_cannot_retarget_or_refresh_during_a_task(self):
        self.start()
        with self.assertRaises(ValueError):
            self.engine.start("new task", self.driver.target)
        with self.assertRaises(ValueError):
            self.engine.inspect("observe", **self.driver.target)

    def test_expiry_does_not_execute(self):
        self.engine.approval_seconds = .05
        self.start()
        wait_for(self.engine, "error")
        self.assertFalse(self.driver.actions)

    def test_stop_during_model_request_discards_late_action(self):
        entered, release = threading.Event(), threading.Event()

        class SlowPlanner(DemoPlanner):
            def next(self, observation, step):
                entered.set(); release.wait(2)
                return super().next(observation, step)

        self.engine.planner_factory = SlowPlanner
        self.engine.start("test", self.driver.target)
        self.assertTrue(entered.wait(1))
        self.engine.stop()
        with self.assertRaises(ValueError):
            self.engine.start("replacement", self.driver.target)
        release.set()
        wait_for(self.engine, "cancelled")
        self.assertEqual(self.driver.actions, [])

    def test_step_limit_preserves_last_observation(self):
        self.engine.max_steps = 1
        state = self.start()
        self.engine.decide(state["pending"]["id"], True)
        result = wait_for(self.engine, "error")
        self.assertEqual(len(self.driver.actions), 1)
        self.assertTrue(result["observation"]["elements"][0]["value"])
        self.assertFalse(self.driver.saved)

    def test_audit_contains_metadata_only(self):
        with tempfile.TemporaryDirectory() as folder:
            self.engine.audit_path = Path(folder) / "audit.jsonl"
            self.engine.start("secret-task-123", self.driver.target)
            wait_for(self.engine, "awaiting_approval")
            self.engine.stop(); self.engine._thread.join(1)
            text = self.engine.audit_path.read_text()
            self.assertNotIn("secret-task-123", text)
            self.assertNotIn("你好", text)
            self.assertNotIn("data_url", text)
            self.assertTrue(all(set(json.loads(line)) <= {"time", "kind", "step", "action_kind", "run_id"}
                                for line in text.splitlines()))


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, limit):
        return json.dumps(self.body).encode()


class PlannerTests(unittest.TestCase):
    def test_protocol_preserves_tool_outputs_reasoning_and_latest_image(self):
        planner = OpenAIPlanner("test", key="test-key", model="test-model")
        requests = []
        output = [{"type": "reasoning", "id": "rs_test", "summary": [], "encrypted_content": "test"},
                  {"type": "function_call", "id": "fc_test", "call_id": "call_test", "name": "computer_action",
                   "arguments": json.dumps({"kind": "click", "element_id": "e2", "reason": "测试"})}]

        class Opener:
            def open(self, request, timeout):
                requests.append(json.loads(request.data))
                return FakeResponse({"status": "completed", "output": output})

        planner.opener = Opener()
        driver = DemoDriver()
        obs = driver.call("observe", **driver.target)
        planner.next(obs, 1); planner.feedback({"ok": True}); planner.next(obs, 2)
        payload = requests[1]
        self.assertFalse(payload["store"])
        self.assertFalse(payload["parallel_tool_calls"])
        self.assertEqual(payload["input"][1], output[0])
        self.assertEqual(payload["input"][3]["call_id"], "call_test")
        images = [c for item in payload["input"] if item.get("role") == "user"
                  for c in item["content"] if c["type"] == "input_image"]
        self.assertEqual(len(images), 1)

    def test_malformed_or_multiple_model_calls_rejected(self):
        planner = OpenAIPlanner("test", key="test-key")

        class Opener:
            def open(self, request, timeout):
                return FakeResponse({"output": [{"type": "function_call"}, {"type": "function_call"}]})

        planner.opener = Opener()
        driver = DemoDriver()
        with self.assertRaises(RuntimeError):
            planner.next(driver.call("observe", **driver.target), 1)

    def test_provider_endpoint_validation(self):
        for value in ["http://remote.example/v1", "https://user:password@example.com/v1", "https://example.com/v1?key=x", "file:///tmp/model"]:
            with self.assertRaises(ValueError):
                endpoint_url(value)
        self.assertEqual(endpoint_url("http://127.0.0.1:8080/v1"), "http://127.0.0.1:8080/v1/responses")
        with self.assertRaises(RuntimeError):
            NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example")


if __name__ == "__main__":
    unittest.main()
