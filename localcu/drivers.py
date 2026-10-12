import base64
import html
import json
import queue
import subprocess
import threading
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class NativeDriver:
    """One persistent JSONL process retains native AX references between steps."""

    def __init__(self, binary=None):
        self.binary = Path(binary or ROOT / ".local/computer-use-helper")
        self._lock = threading.Lock()
        self.process = None
        self._responses = queue.Queue()

    def _start(self):
        if not self.binary.is_file():
            raise RuntimeError("原生组件尚未编译，请先运行 ./scripts/build-native.sh。")
        self._responses = queue.Queue()
        self.process = subprocess.Popen([str(self.binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, encoding="utf-8", bufsize=1)
        process, responses = self.process, self._responses

        def read():
            try:
                for line in process.stdout:
                    responses.put(line)
            finally:
                responses.put(None)
        threading.Thread(target=read, daemon=True).start()

    def call(self, op, **args):
        with self._lock:
            if self.process is None or self.process.poll() is not None:
                self._start()
            try:
                self.process.stdin.write(json.dumps({"op": op, **args}, ensure_ascii=False) + "\n")
                self.process.stdin.flush()
                line = self._responses.get(timeout=25)
                if line is None:
                    raise RuntimeError("原生组件已退出。请重新启动服务。")
                result = json.loads(line)
            except (queue.Empty, BrokenPipeError, json.JSONDecodeError) as error:
                self.close()
                raise RuntimeError("原生组件超时或通信失败，已停止本轮操作。") from error
            if not result.get("ok"):
                raise RuntimeError(result.get("error", "原生操作失败。"))
            return result["result"]

    def close(self):
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            for stream in (self.process.stdin, self.process.stdout):
                if stream:
                    stream.close()
            self.process = None


class DemoDriver:
    """In-memory practice window. Never reads or controls the actual desktop."""
    target = {"bundle_id": "demo.notes", "window_id": 1}

    def __init__(self):
        self.text = ""
        self.saved = False
        self.generation = ""
        self.actions = []

    def call(self, op, **args):
        if op in {"doctor", "request_permissions"}:
            return {"accessibility": True, "screen_recording": True, "platform": "demo"}
        if op == "windows":
            return {"windows": [{**self.target, "app": "练习便签", "title": "无需权限 · 本地演示"}]}
        if op == "observe":
            if {k: args.get(k) for k in self.target} != self.target:
                raise ValueError("演示窗口不存在。")
            self.generation = uuid.uuid4().hex
            content = html.escape(self.text or "在这里写下一句话…")
            status = "已保存 ✓" if self.saved else "尚未保存"
            svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="960" height="600" viewBox="0 0 960 600">
            <rect width="960" height="600" rx="16" fill="#f9fafb"/>
            <path d="M16 0h928a16 16 0 0 1 16 16v34H0V16A16 16 0 0 1 16 0" fill="#e9edf0"/>
            <circle cx="24" cy="25" r="6" fill="#fb6c65"/><circle cx="45" cy="25" r="6" fill="#f5be4f"/>
            <circle cx="66" cy="25" r="6" fill="#5dca67"/>
            <g font-family="Arial, sans-serif" fill="#16352d"><text x="450" y="31" font-size="15">Notes</text>
            <text x="64" y="120" font-size="32" font-weight="bold">我的第一条便签</text>
            <text x="64" y="158" font-size="16" fill="#718078">在演示窗口中练习一次完整的操作</text>
            <rect x="64" y="192" width="832" height="216" rx="12" fill="white" stroke="#d3ded8"/>
            <text x="88" y="235" font-size="22">{content}</text>
            <rect x="746" y="438" width="150" height="52" rx="10" fill="#176b50"/>
            <text x="802" y="472" font-size="18" fill="white">保存</text>
            <text x="64" y="471" font-size="16" fill="#718078">{status}</text>
            <text x="64" y="554" font-size="14" fill="#87928b">DEMO · 所有内容仅保存在本次进程内存中</text></g></svg>'''
            return {"generation": self.generation, "target": {**self.target, "title": "练习便签"},
                    "elements": [{"id": "e1", "role": "AXTextArea", "title": "便签内容", "value": self.text,
                                  "enabled": True, "depth": 0},
                                 {"id": "e2", "role": "AXButton", "title": "保存", "value": status,
                                  "enabled": True, "depth": 0, "actions": ["AXPress"]}],
                    "screenshot": {"width": 960, "height": 600,
                                   "data_url": "data:image/svg+xml;base64," + base64.b64encode(svg.encode()).decode()}}
        if op == "act":
            if args.get("generation") != self.generation or not self.generation:
                raise RuntimeError("演示观察已过期。")
            if {k: args.get(k) for k in self.target} != self.target:
                raise RuntimeError("演示目标窗口不匹配。")
            self.generation = ""
            action = args["action"]
            if action["kind"] == "set_value" and action.get("element_id") == "e1":
                self.text = action["text"]
                self.saved = False
            elif action["kind"] == "click" and action.get("element_id") == "e2":
                self.saved = True
            else:
                raise RuntimeError("演示模式只实现填写便签与保存。")
            self.actions.append(action)
            return {"ok": True, "kind": action["kind"]}
        raise ValueError("未知操作。")

    def close(self):
        pass
