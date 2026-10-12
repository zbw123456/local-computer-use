import argparse
import os
import platform
import sys
import threading
import webbrowser

from .drivers import DemoDriver, NativeDriver, ROOT
from .engine import Engine
from .provider import DemoPlanner, OpenAIPlanner
from .server import LocalServer


def load_env(path):
    if not path.exists():
        return
    # Intentionally not a shell: no interpolation, execution, exports, or escape expansion.
    allowed = {"OPENAI_API_KEY", "OPENAI_MODEL", "OPENAI_BASE_URL"}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if sep and key.strip() in allowed:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


def main():
    parser = argparse.ArgumentParser(description="Local Computer Use — 本地 macOS 窗口操作助手")
    parser.add_argument("--demo", action="store_true", help="使用内存演示窗口，无需 API Key 和系统权限")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--max-steps", type=int, default=20)
    parser.add_argument("--max-seconds", type=int, default=600)
    parser.add_argument("--no-audit", action="store_true", help="不保存本地操作元数据")
    args = parser.parse_args()
    if not (0 <= args.port <= 65535 and 1 <= args.max_steps <= 100 and 10 <= args.max_seconds <= 3600):
        parser.error("端口、步数或时长不在有效范围内。")
    if not args.demo and platform.system() != "Darwin":
        parser.error("真实电脑操作需要 macOS 14 或更新版本；其他系统可使用 --demo。")
    load_env(ROOT / ".env")
    driver = DemoDriver() if args.demo else NativeDriver()
    engine = Engine(driver, DemoPlanner if args.demo else OpenAIPlanner,
                    audit_path=None if args.no_audit else ROOT / ".local/audit.jsonl",
                    max_steps=args.max_steps, max_seconds=args.max_seconds)
    try:
        server = LocalServer(args.port, engine, demo=args.demo,
                             model="本地固定演示" if args.demo else os.environ.get("OPENAI_MODEL", "gpt-5.4"),
                             configured=args.demo or bool(os.environ.get("OPENAI_API_KEY")) or
                             os.environ.get("OPENAI_BASE_URL", "").startswith("http://"))
    except OSError as error:
        parser.error(f"无法启动本机服务：{error}。可用 --port 8766 更换端口。")
    print("\nLocal Computer Use  /  " + ("演示模式" if args.demo else "macOS 原生模式"))
    print("打开下面的本机链接（口令仅在本次运行有效，请勿分享）：\n" + server.launch_url, flush=True)
    print("按 Ctrl+C 关闭。截图默认不保存到磁盘。\n", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(server.launch_url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        engine.close()
        server.server_close()


if __name__ == "__main__":
    main()
