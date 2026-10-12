"""Single-task state machine. Approval is bound to a specific observation and action."""
import copy
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .actions import validate_action

ACTIVE = {"observing", "thinking", "awaiting_approval", "executing", "stopping"}


class Engine:
    def __init__(self, driver, planner_factory, audit_path=None, max_steps=20, max_seconds=600, approval_seconds=120):
        self.driver = driver
        self.planner_factory = planner_factory
        self.audit_path = Path(audit_path) if audit_path else None
        self.max_steps = max_steps
        self.max_seconds = max_seconds
        self.approval_seconds = approval_seconds
        self._lock = threading.RLock()
        self._driver_lock = threading.Lock()
        self._stop = threading.Event()
        self._decision = threading.Event()
        self._thread = None
        self._approved = False
        self._pending_deadline = 0
        self._state = {"status": "idle", "task": "", "step": 0, "target": None,
                       "observation": None, "pending": None, "message": "选择窗口，开始你的第一个任务。", "events": []}

    def _event(self, kind, message, **data):
        event = {"id": uuid.uuid4().hex, "time": datetime.now(timezone.utc).isoformat(),
                 "kind": kind, "message": message, **data}
        with self._lock:
            self._state["events"].append(event)
            self._state["events"] = self._state["events"][-150:]
        if self.audit_path:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Audit stores metadata only: no task text, screenshots, labels, typed text or model prose.
            record = {"time": event["time"], "kind": kind,
                      **{k: v for k, v in data.items() if k in {"step", "action_kind", "run_id"}}}
            fd = os.open(self.audit_path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as stream:
                stream.write(json.dumps(record) + "\n")

    def state(self):
        with self._lock:
            result = copy.deepcopy(self._state)
            result["busy"] = bool(self._thread and self._thread.is_alive())
            return result

    def _set(self, **values):
        with self._lock:
            self._state.update(values)

    def _call(self, op, **kwargs):
        with self._driver_lock:
            return self.driver.call(op, **kwargs)

    def inspect(self, op, **kwargs):
        # Holding the state lock prevents a new run from racing an inspection.
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise ValueError("任务运行中。请先停止，再刷新窗口或观察。")
            result = self._call(op, **kwargs)
            if op == "observe":
                self._state["observation"] = result
                self._state["target"] = kwargs
            return result

    def start(self, task, target):
        if not isinstance(task, str) or not 1 <= len(task.strip()) <= 4000:
            raise ValueError("请输入 1–4000 个字符的任务。")
        if (not isinstance(target, dict) or set(target) != {"bundle_id", "window_id"}
                or not isinstance(target["bundle_id"], str) or not target["bundle_id"]
                or type(target["window_id"]) is not int or target["window_id"] <= 0):
            raise ValueError("请选择有效窗口。")
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise ValueError("已有任务在运行，请先停止并等待当前调用结束。")
            planner = self.planner_factory(task.strip())
            self._stop.clear(); self._decision.clear(); self._approved = False
            self._state.update(status="observing", task=task.strip(), target=copy.deepcopy(target), step=0,
                               pending=None, observation=None, message="正在读取窗口…", events=[])
            run_id = uuid.uuid4().hex
            self._thread = threading.Thread(target=self._run, args=(planner, copy.deepcopy(target), run_id), daemon=True)
            self._thread.start()
        return {"ok": True}

    def decide(self, proposal_id, approve):
        with self._lock:
            pending = self._state["pending"]
            if (self._state["status"] != "awaiting_approval" or not pending
                    or pending["id"] != proposal_id or self._decision.is_set()):
                raise ValueError("此操作已失效或已处理。请查看最新操作。")
            if time.monotonic() > self._pending_deadline:
                raise ValueError("预览已过期，请停止后重新开始任务。")
            self._approved = approve
            self._decision.set()
            self._state["pending"] = None
            self._state["status"] = "executing" if approve else "stopping"
        return {"ok": True}

    def stop(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                self._stop.set(); self._decision.set()
                self._state.update(status="stopping", pending=None,
                                   message="正在停止；当前已发出的系统操作无法撤回，之后不会执行新操作。")
        return {"ok": True}

    def _check(self, deadline):
        if self._stop.is_set():
            raise InterruptedError("任务已停止。")
        if time.monotonic() >= deadline:
            raise TimeoutError("已达到本次任务的时间上限。")

    def _run(self, planner, target, run_id):
        deadline = time.monotonic() + self.max_seconds
        try:
            self._event("started", "任务开始：只操作所选窗口。", run_id=run_id)
            for step in range(1, self.max_steps + 2):
                self._check(deadline)
                self._set(status="observing", step=min(step, self.max_steps), message="正在读取最新窗口…")
                observation = self._call("observe", **target)
                self._check(deadline)
                self._set(observation=observation, status="thinking", message="正在分析下一步…")
                proposal = planner.next(observation, step)
                self._check(deadline)
                if "finish" in proposal:
                    self._set(status="completed", pending=None, message=proposal["finish"])
                    self._event("finished", proposal["finish"], step=step - 1)
                    return
                if step > self.max_steps:
                    raise TimeoutError("已达到操作步数上限。请检查结果后再继续。")
                action = validate_action(proposal["action"], observation)
                self._decision.clear(); self._approved = False
                with self._lock:
                    self._check(deadline)
                    self._pending_deadline = min(deadline, time.monotonic() + self.approval_seconds)
                    self._state.update(status="awaiting_approval", message="请检查这一步操作及窗口截图。",
                                       pending={"id": uuid.uuid4().hex, "action": action,
                                                "expires_in": self.approval_seconds})
                self._event("proposed", action["reason"], step=step, action_kind=action["kind"])
                if not self._decision.wait(max(0, self._pending_deadline - time.monotonic())):
                    raise TimeoutError("操作预览已过期。请重新开始以读取最新窗口。")
                self._check(deadline)
                if not self._approved:
                    raise InterruptedError("已拒绝操作，任务停止。")
                # This boundary marks an operation as dispatched. Stopping cannot retract it.
                with self._lock:
                    self._check(deadline)
                    self._state.update(status="executing", pending=None, message="正在执行已批准的操作…")
                result = self._call("act", **target, generation=observation["generation"], action=action)
                self._event("executed", "已执行：" + action["kind"], step=step, action_kind=action["kind"])
                planner.feedback(result)
        except InterruptedError as error:
            self._set(status="cancelled", pending=None, message=str(error))
            self._event("cancelled", str(error))
        except Exception as error:
            self._set(status="error", pending=None, message=str(error)[:1000])
            self._event("error", str(error)[:1000])

    def close(self):
        self.stop()
        if self._thread:
            self._thread.join(timeout=1)
        self.driver.close()
