"""
watcher.py
本地文件监听器 + 增量推送
监听两类变化：
  1. 代码文件（.swift/.m/.h）→ 解析单文件 → HTTP POST 推送变更
  2. .git/HEAD              → 检测分支切换 → 更新当前分支标识
"""

from __future__ import annotations
import os
import sys
import time
import logging
import argparse
import subprocess
import threading
import requests
from pathlib import Path
from datetime import datetime

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [watcher] %(message)s")
logger = logging.getLogger(__name__)

GRAPH_API_URL = os.getenv("GRAPH_API_URL", "http://localhost:8080")
HEARTBEAT_INTERVAL = 30      # 秒


class FileWatcher:
    def __init__(self, workspace: str, api_url: str, use_polling: bool = False):
        self.workspace = Path(workspace).expanduser().resolve()
        self.api_url = api_url.rstrip("/")
        self.use_polling = use_polling
        self.current_branch = self._read_current_branch()
        self._stop_event = threading.Event()
        logger.info(f"Watcher started | workspace={self.workspace} | branch={self.current_branch}")

    def start(self) -> None:
        # 启动心跳线程
        threading.Thread(target=self._heartbeat_loop, daemon=True).start()

        if self.use_polling or not self._try_fsevents():
            self._polling_loop()
        # FSEvents 版本在 _try_fsevents 内部运行

    def stop(self) -> None:
        self._stop_event.set()

    # ── 分支感知 ──────────────────────────────────────────────────────────────

    def _read_current_branch(self) -> str:
        """读取 .git/HEAD 获取当前分支"""
        try:
            head = (self.workspace / ".git" / "HEAD").read_text().strip()
            if head.startswith("ref: refs/heads/"):
                return head.replace("ref: refs/heads/", "")
            return head[:8]  # detached HEAD，用 commit hash 前8位
        except Exception:
            return "master"

    def _on_branch_switch(self) -> None:
        new_branch = self._read_current_branch()
        if new_branch != self.current_branch:
            logger.info(f"Branch switched: {self.current_branch} → {new_branch}")
            self.current_branch = new_branch
            # 通知 graph-api 切换分支
            self._post("/api/watcher/branch-switch",
                       {"branch": new_branch, "repo": str(self.workspace)})

    # ── 文件变化处理 ──────────────────────────────────────────────────────────

    def _on_file_change(self, file_path: str) -> None:
        path = Path(file_path)
        if path.suffix not in (".swift", ".m", ".h"):
            return
        if "Pods/" in str(path) or ".build/" in str(path):
            return

        logger.info(f"File changed: {path.name} | branch={self.current_branch}")
        self._post("/api/watcher/file-changed", {
            "filePath": str(path),
            "branch": self.current_branch,
            "repo": str(self.workspace),
            "timestamp": datetime.utcnow().isoformat(),
        })

    # ── FSEvents（macOS 原生，Docker 外使用）─────────────────────────────────

    def _try_fsevents(self) -> bool:
        """尝试使用 watchdog 的 FSEvents observer"""
        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent

            class Handler(FileSystemEventHandler):
                def __init__(self, watcher: FileWatcher):
                    self._w = watcher

                def on_modified(self, event):
                    if not event.is_directory:
                        if ".git/HEAD" in event.src_path:
                            self._w._on_branch_switch()
                        else:
                            self._w._on_file_change(event.src_path)

                def on_created(self, event):
                    if not event.is_directory:
                        self._w._on_file_change(event.src_path)

            observer = Observer()
            observer.schedule(Handler(self), str(self.workspace), recursive=True)
            observer.start()
            logger.info("Using FSEvents observer (native)")

            while not self._stop_event.is_set():
                time.sleep(1)
            observer.stop()
            observer.join()
            return True

        except Exception as e:
            logger.info(f"FSEvents not available ({e}), falling back to polling")
            return False

    # ── Polling（Docker 内使用）────────────────────────────────────────────────

    def _polling_loop(self) -> None:
        """文件 mtime 轮询，~500ms 延迟"""
        logger.info("Using polling mode (Docker/fallback)")
        mtimes: dict[str, float] = {}
        git_head_path = self.workspace / ".git" / "HEAD"
        git_head_mtime = git_head_path.stat().st_mtime if git_head_path.exists() else 0

        while not self._stop_event.is_set():
            # 检查 .git/HEAD
            if git_head_path.exists():
                new_mtime = git_head_path.stat().st_mtime
                if new_mtime != git_head_mtime:
                    git_head_mtime = new_mtime
                    self._on_branch_switch()

            # 扫描代码文件
            for ext in ("*.swift", "*.m", "*.h"):
                for path in self.workspace.rglob(ext):
                    try:
                        mtime = path.stat().st_mtime
                        key = str(path)
                        if key not in mtimes:
                            mtimes[key] = mtime
                        elif mtime > mtimes[key]:
                            mtimes[key] = mtime
                            self._on_file_change(key)
                    except OSError:
                        pass

            time.sleep(0.5)

    # ── HTTP 推送 ─────────────────────────────────────────────────────────────

    def _post(self, endpoint: str, data: dict) -> None:
        try:
            requests.post(f"{self.api_url}{endpoint}", json=data, timeout=5)
        except Exception as e:
            logger.warning(f"POST {endpoint} failed: {e}")

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.is_set():
            self._post("/api/watcher/heartbeat", {
                "branch": self.current_branch,
                "repo": str(self.workspace),
                "timestamp": datetime.utcnow().isoformat(),
            })
            time.sleep(HEARTBEAT_INTERVAL)


# ── main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Code Graph File Watcher")
    parser.add_argument("--path", default=os.getenv("WORKSPACE", "."),
                        help="监听的工作区路径")
    parser.add_argument("--api-url", default=GRAPH_API_URL,
                        help="graph-api 地址")
    parser.add_argument("--polling", action="store_true",
                        help="强制使用 polling 模式（Docker 内自动启用）")
    args = parser.parse_args()

    watcher = FileWatcher(args.path, args.api_url, use_polling=args.polling)
    try:
        watcher.start()
    except KeyboardInterrupt:
        logger.info("Watcher stopped")
        watcher.stop()
