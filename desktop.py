"""Windows app entry point and optional loopback-only browser preview."""
from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from app import create_app
from storage import default_data_dir


def main():
    parser = argparse.ArgumentParser(description='拾笺 · 家藏诗词')
    parser.add_argument('--browser', action='store_true')
    parser.add_argument('--server', action='store_true', help='Run without opening a window')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--data-dir', type=Path)
    args = parser.parse_args()
    data_dir = args.data_dir or default_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    log = (data_dir/'app.log').open('a', encoding='utf-8', buffering=1)
    if sys.stdout is None:
        sys.stdout = log
    if sys.stderr is None:
        sys.stderr = log
    logging.basicConfig(filename=data_dir/'app.log', level=logging.INFO, encoding='utf-8')
    # Prevent a second app instance from resetting another instance's live queue.
    lockfile = (data_dir/'instance.lock').open('a+b')
    if os.name == 'nt':
        import msvcrt
        lockfile.seek(0)
        if not lockfile.read(1):
            lockfile.write(b'0'); lockfile.flush()
        lockfile.seek(0)
        try:
            msvcrt.locking(lockfile.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, '拾笺已在运行，请切换到已有窗口。', '拾笺', 0)
            return
    app = create_app(data_dir)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(('127.0.0.1',args.port))
    port = sock.getsockname()[1]
    config = uvicorn.Config(app, host='127.0.0.1', port=port, log_level='warning', access_log=False)
    server = uvicorn.Server(config)
    url = f'http://127.0.0.1:{port}'
    if args.server:
        print('拾笺本地地址：'+url, flush=True)
        server.run(sockets=[sock])
        return
    thread = threading.Thread(target=server.run, kwargs={'sockets':[sock]}, daemon=True)
    thread.start()
    for _ in range(150):
        if server.started:
            break
        time.sleep(.1)
    if not server.started:
        raise RuntimeError('本地诗库服务未能启动，请查看 '+str(data_dir/'app.log'))
    if args.browser:
        webbrowser.open(url)
        thread.join()
        return
    try:
        import webview
        webview.settings['ALLOW_DOWNLOADS'] = True
        webview.create_window('拾笺 · 家藏诗词', url, width=1440, height=960,
            min_size=(900,650), background_color='#F8F7F3', confirm_close=True)
        webview.start(gui='edgechromium' if os.name=='nt' else None,
            localization={'global.quitConfirmation':'关闭拾笺？请先保存正在校对的文字。进行中的识别将停止，已保存内容会保留。'})
    except Exception:
        logging.exception('Desktop window could not open; opening a browser')
        webbrowser.open(url)
        thread.join()
    finally:
        app.state.engine.stop()
        server.should_exit = True
        thread.join(timeout=5)
        lockfile.close()


if __name__=='__main__':
    main()
