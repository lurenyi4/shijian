"""Local-only MinerU 4 adapter. Never calls MinerU cloud or sends source files away."""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from pathlib import Path

from PIL import Image

from imaging import manuscript_rgb
from storage import Library


def terminate_process(process):
    """Stop only this job and its child processes, including Windows launcher children."""
    if not process or process.poll() is not None:
        return
    if os.name == "nt":
        taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
        try:
            subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
    else:
        import signal

        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def find_engine(configured=""):
    if configured:
        candidate = Path(configured).expanduser()
        return str(candidate.resolve()) if candidate.is_file() else ""
    names = ["mineru-kit.exe", "mineru-kit"]
    roots = [
        Path(__file__).resolve().parent,
        Path(sys.executable).resolve().parent,
        Path(__file__).resolve().parent.parent,
        Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Shijian",
    ]
    roots.extend(list(Path(sys.executable).resolve().parents)[:5])
    for root in roots:
        for folder in (root / ".mineru" / "Scripts", root / ".mineru" / "bin"):
            for name in names:
                if (folder / name).is_file():
                    return str(folder / name)
    return shutil.which("mineru-kit") or ""


def read_result(path):
    if path.suffix == ".md":
        return path.read_text(encoding="utf-8-sig")
    # Read only bounded Markdown members. Do not extract remote/generated paths.
    with zipfile.ZipFile(path) as archive:
        files = [f for f in archive.infolist() if f.filename.lower().endswith(".md")]
        if not files:
            raise ValueError("MinerU 的结果中没有 Markdown 文字。")
        files.sort(key=lambda f: (Path(f.filename).name != "full.md", f.filename))
        if files[0].file_size > 20 * 1024 * 1024:
            raise ValueError("文字结果过大，请拆分原稿后重试。")
        return archive.read(files[0]).decode("utf-8-sig")


class LocalEngine:
    def __init__(self, library: Library):
        self.library = library
        self.pending = queue.Queue()
        self.stop_event = threading.Event()
        self.jobs = {}
        self.guard = threading.RLock()
        self.process = None
        self.active_id = None
        self.active_token = None
        self.worker = None
        self.phase = '等待任务'

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        self.stop_event.clear()
        self.worker = threading.Thread(target=self.run, name="shijian-local-ocr", daemon=True)
        self.worker.start()

    def config(self):
        return self.library.setting(
            "engine", {"executable": "", "tier": "standard", "model_source": "modelscope"}
        )

    def info(self):
        config = self.config()
        executable = find_engine(config.get("executable", ""))
        with self.guard:
            active = self.active_id if self.jobs.get(self.active_id) == self.active_token else None
            pending = len(self.jobs) - int(active is not None)
        return {
            **config,
            "available": bool(executable),
            "resolved_executable": executable,
            "mode": "local",
            "active_id": active,
            "data_dir": str(self.library.root),
            "pending": pending,
            "phase": self.phase,
            "readiness": '程序已找到；模型与离线运行需通过实际识别验证' if executable else '尚未找到程序',
        }

    def diagnose(self):
        executable = find_engine(self.config().get('executable', ''))
        if not executable:
            raise ValueError('尚未找到 MinerU 程序，请检查安装或路径。')
        try:
            result = subprocess.run([executable, '--version'], capture_output=True, text=True,
                                    encoding='utf-8', errors='replace', timeout=20,
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ValueError('程序诊断未完成，请检查安装或稍后重试。') from exc
        return {'executable': executable, 'version_output': (result.stdout + result.stderr)[-2000:],
                'program_ok': result.returncode == 0,
                'models': '尚未验证；请先用一份代表性手稿识别。离线模式需相关模型全部缓存。'}

    def enqueue(self, ids, force=False):
        if not self.info()["available"]:
            raise ValueError(
                "尚未找到本地 MinerU。请在识别设置中填写 mineru-kit.exe 路径，或先运行本地引擎安装脚本。"
            )
        accepted = []
        with self.guard, self.library.lock:
            if self.stop_event.is_set():
                raise ValueError("识别引擎已停止，请重新启动拾笺。")
            for doc_id in dict.fromkeys(ids):
                doc = self.library.get(doc_id)
                if doc["demo"] or doc["trashed"] or doc["status"] in ("running", "queued"):
                    continue
                if doc["text"].strip() and not force:
                    continue
                token = uuid.uuid4().hex
                self.jobs[doc_id] = token
                self.library.update(doc_id, status="queued", error="")
                self.pending.put((doc_id, dict(self.config()), token))
                accepted.append(doc_id)
        return accepted

    def cancel(self, ids):
        with self.guard:
            for doc_id in ids:
                doc = self.library.get(doc_id)
                if doc["status"] in ("queued", "running"):
                    self.jobs.pop(doc_id, None)
                    self.library.update(
                        doc_id, status="interrupted", error="识别已停止，原图和已有文字已保留。"
                    )
            if self.active_id in ids and self.process and self.process.poll() is None:
                terminate_process(self.process)

    def stop(self):
        self.stop_event.set()
        with self.guard:
            self.cancel(list(self.jobs))
            if self.process and self.process.poll() is None:
                terminate_process(self.process)
        if self.worker:
            self.worker.join(timeout=4)

    def run(self):
        while not self.stop_event.is_set():
            try:
                doc_id, config, token = self.pending.get(timeout=0.4)
            except queue.Empty:
                continue
            try:
                with self.guard:
                    if self.jobs.get(doc_id) != token:
                        continue
                    self.active_id = doc_id
                    self.active_token = token
                    self.phase = '准备原稿'
                self.parse(doc_id, config, token)
            except Exception as exc:
                with self.guard:
                    if self.is_current(doc_id, token):
                        self.library.update(doc_id, status="failed", error=str(exc)[-1800:])
            finally:
                with self.guard:
                    self.process = None
                    self.active_id = None
                    self.active_token = None
                    self.phase = '等待任务'
                    if self.jobs.get(doc_id) == token:
                        self.jobs.pop(doc_id, None)
                self.pending.task_done()

    def is_current(self, doc_id, token):
        with self.guard:
            return not self.stop_event.is_set() and self.jobs.get(doc_id) == token

    def parse(self, doc_id, config, token):
        with self.guard:
            if not self.is_current(doc_id, token):
                return
            doc = self.library.get(doc_id)
            # Check ownership and change state together, before slow image preparation.
            self.library.update(doc_id, status="running", error="")
        executable = find_engine(config.get("executable", ""))
        if not executable:
            raise ValueError("找不到本地 MinerU 程序，请检查识别设置。")
        with self.library.result_attempt(doc_id) as work:
            text = self.parse_attempt(doc, config, token, executable, work)
        # Files are closed and immutable before their completed text enters the database.
        if text is not None:
            with self.guard:
                if self.is_current(doc_id, token):
                    self.phase = '保存识别文字'
                    self.library.finish_recognition(doc_id, text)

    def parse_attempt(self, doc, config, token, executable, work):
        doc_id = doc["id"]
        original = self.library.root / doc["source"]
        source = original
        if original.suffix != ".pdf":
            # Honor camera orientation without changing the preserved original.
            source = work / "input.png"
            with Image.open(original) as image:
                manuscript_rgb(image).save(source)
        output = work / "result.zip"
        command = [
            executable,
            "parse",
            str(source),
            "-o",
            str(output),
            "--format",
            "zip",
            "--tier",
            config.get("tier", "standard"),
            "--ocr-mode",
            "ocr",
            "--disable-image-analysis",
        ]
        env = os.environ.copy()
        for key in ("MINERU_API_URL", "MINERU_API_KEY", "MINERU_CONFIG"):
            env.pop(key, None)
        env["MINERU_HOME"] = str(self.library.root.parent / "mineru-local")
        env["MINERU_MODEL_SOURCE"] = config.get("model_source", "modelscope")
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        env["HF_HUB_DISABLE_TELEMETRY"] = "1"
        with (work / "engine.log").open("w", encoding="utf-8") as log:
            with self.guard:
                if not self.is_current(doc_id, token):
                    return
                process = subprocess.Popen(
                    command,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    cwd=work,
                    env=env,
                    start_new_session=os.name != "nt",
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
                self.process = process
                self.phase = '引擎运行中（首次可能准备模型，可停止）'
            deadline = time.monotonic() + 7200
            while process.poll() is None:
                if self.stop_event.wait(0.5) or not self.is_current(doc_id, token):
                    terminate_process(process)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
                    return
                if time.monotonic() > deadline:
                    terminate_process(process)
                    process.wait(timeout=5)
                    raise ValueError(
                        "单份诗稿识别超过两小时，请拆分页数，或改用基础档。原稿已保存。"
                    )
            returncode = process.returncode
        if not self.is_current(doc_id, token):
            return
        if returncode != 0:
            tail = (work / "engine.log").read_text(encoding="utf-8", errors="replace")[-1400:]
            raise ValueError(
                "本地 MinerU 未完成识别。首次使用可能需要下载模型；请检查网络、模型或内存。\n"
                + tail
            )
        if not output.exists():
            raise ValueError("MinerU 没有生成结果文件，请检查版本是否为 4.x。")
        self.phase = '读取识别结果'
        text = read_result(output)
        if not text.strip():
            raise ValueError("未识别到文字。请尝试更清晰的照片或更高的识别档位。")
        return text
