#!/usr/bin/env python3
"""TYPERINO: tiny, local-only essay editor. Python standard library only."""
from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent
ESSAYS = ROOT / 'essays'
ESSAYS.mkdir(exist_ok=True)
ID_PATTERN = re.compile(r'\w[\w-]{0,99}\Z')
MAX_BYTES = 2 * 1024 * 1024
WRITE_LOCK = threading.RLock()


IDLE_SECONDS = 180


class ActivityTracker:
    """Open tabs renew a lease; abandoned tabs expire without a close event."""
    def __init__(self, timeout=IDLE_SECONDS, clock=time.monotonic):
        self.timeout = timeout
        self.clock = clock
        self.lock = threading.Lock()
        self.last_seen = clock()

    def touch(self):
        with self.lock:
            self.last_seen = self.clock()

    def expired(self):
        with self.lock:
            return self.clock() - self.last_seen >= self.timeout


def stop_when_idle(httpd, stopped):
    while not stopped.wait(2):
        if httpd.activity.expired():
            print('No tab heartbeat for three minutes; stopping Typerino.', flush=True)
            httpd.shutdown()
            return


def clean_title(title: object) -> str:
    if not isinstance(title, str):
        raise ValueError('Title must be text')
    return re.sub(r'[\r\n\t]+', ' ', title).strip().upper()[:150] or 'UNTITLED ESSAY'


def validate_document(data: object) -> tuple[str, str]:
    if not isinstance(data, dict):
        raise ValueError('Expected an essay object')
    title = clean_title(data.get('title', ''))
    body = data.get('body', '')
    if not isinstance(body, str):
        raise ValueError('Essay body must be text')
    if '\x00' in body:
        raise ValueError('Essay body cannot contain NUL characters')
    return title, body


def path_for(essay_id: str) -> Path:
    if not ID_PATTERN.fullmatch(essay_id):
        raise ValueError('Invalid essay ID')
    path = ESSAYS / (essay_id + '.txt')
    if path.is_symlink():
        raise ValueError('Symbolic links are not supported')
    return path


def title_path(title: str, current: Path | None = None) -> Path:
    slug = re.sub(r'[\W_]+', '-', title.lower()).strip('-')[:80] or 'untitled'
    candidate = ESSAYS / (slug + '.txt')
    number = 2
    while candidate != current and (candidate.exists() or candidate.is_symlink()):
        candidate = ESSAYS / f'{slug}-{number}.txt'
        number += 1
    return candidate


def rename_existing_essays() -> None:
    with WRITE_LOCK:
        for path in sorted(ESSAYS.glob('*.txt')):
            if path.is_symlink() or not ID_PATTERN.fullmatch(path.stem):
                continue
            title = read_essay(path)['title']
            target = title_path(title, path)
            if target != path:
                path.rename(target)


def read_essay(path: Path) -> dict:
    raw = path.read_text(encoding='utf-8')
    if '\n\n' in raw:
        title, body = raw.split('\n\n', 1)
    elif '\n' in raw:
        title, body = raw.split('\n', 1)
    else:
        title, body = raw, ''
    stat = path.stat()
    created = getattr(stat, 'st_birthtime', stat.st_ctime)
    return {
        'id': path.stem,
        'title': clean_title(title),
        'body': body,
        'createdAt': int(created * 1000),
        'updatedAt': int(stat.st_mtime * 1000),
        'file': 'essays/' + path.name,
    }


def save_essay(path: Path, title: str, body: str) -> None:
    # Atomic write: prevents a partially written file if the app stops mid-save.
    file_data = (title + '\n\n' + body).encode('utf-8')
    fd, tmp = tempfile.mkstemp(prefix='.typerino-', suffix='.tmp', dir=ESSAYS)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(file_data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def list_essays() -> list[dict]:
    with WRITE_LOCK:
        items = []
        for path in ESSAYS.glob('*.txt'):
            if path.is_symlink() or not ID_PATTERN.fullmatch(path.stem):
                continue
            try:
                essay = read_essay(path)
            except (UnicodeError, OSError, ValueError):
                continue
            body = essay.pop('body')
            text = ' '.join(body.split())
            first_sentence, dot, _ = text.partition('.')
            essay['excerpt'] = first_sentence + dot + '...' if text else ''
            essay['words'] = len(body.split())
            items.append(essay)
        items.sort(key=lambda a: (a['updatedAt'], a['id']), reverse=True)
        return items


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        print(f'[{self.log_date_time_string()}] {format % args}', flush=True)

    def json_response(self, status: int, payload: object) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

    def error_json(self, status: int, message: str) -> None:
        self.json_response(status, {'error': message})

    def read_payload(self) -> object:
        length = int(self.headers.get('Content-Length', '-1'))
        if length < 0 or length > MAX_BYTES:
            raise ValueError('Invalid essay size (maximum 2 MB)')
        return json.loads(self.rfile.read(length).decode('utf-8'))

    def essay_id_from_path(self) -> str | None:
        match = re.fullmatch(r'/api/essays/(\w[\w-]{0,99})', unquote(urlsplit(self.path).path))
        return match.group(1) if match else None

    def do_GET(self) -> None:
        self.server.activity.touch()
        route = urlsplit(self.path).path
        if route in ('/', '/index.html', '/style.css', '/icon-no-background.png'):
            asset, content_type = {
                '/style.css': ('style.css', 'text/css; charset=utf-8'),
                '/icon-no-background.png': ('icon-no-background.png', 'image/png'),
            }.get(route, ('index.html', 'text/html; charset=utf-8'))
            data = (ROOT / asset).read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(data)
        elif route == '/favicon.ico':
            self.send_response(204)
            self.end_headers()
        elif route == '/api/heartbeat':
            self.json_response(200, {'alive': True})
        elif route == '/api/info':
            self.json_response(200, {'essaysDirectory': str(ESSAYS)})
        elif route == '/api/essays':
            self.json_response(200, {'essays': list_essays()})
        else:
            essay_id = self.essay_id_from_path()
            if essay_id is None:
                return self.error_json(404, 'Not found')
            try:
                with WRITE_LOCK:
                    path = path_for(essay_id)
                    if not path.is_file():
                        return self.error_json(404, 'Essay not found')
                    essay = read_essay(path)
                self.json_response(200, essay)
            except (OSError, ValueError, UnicodeError) as e:
                self.error_json(400, str(e))

    def do_POST(self) -> None:
        self.server.activity.touch()
        if urlsplit(self.path).path != '/api/essays':
            return self.error_json(404, 'Not found')
        try:
            title, body = validate_document(self.read_payload())
            with WRITE_LOCK:
                path = title_path(title)
                save_essay(path, title, body)
                essay = read_essay(path)
            self.json_response(201, essay)
        except (ValueError, UnicodeError, json.JSONDecodeError) as e:
            self.error_json(400, str(e))
        except OSError as e:
            self.error_json(500, 'Could not write essay: ' + str(e))

    def do_PUT(self) -> None:
        self.server.activity.touch()
        essay_id = self.essay_id_from_path()
        if not essay_id:
            return self.error_json(404, 'Not found')
        try:
            title, body = validate_document(self.read_payload())
            with WRITE_LOCK:
                path = path_for(essay_id)
                if not path.is_file():
                    return self.error_json(404, 'Essay not found')
                target = title_path(title, path)
                save_essay(path, title, body)
                if target != path:
                    path.rename(target)
                essay = read_essay(target)
            self.json_response(200, essay)
        except (ValueError, UnicodeError, json.JSONDecodeError) as e:
            self.error_json(400, str(e))
        except OSError as e:
            self.error_json(500, 'Could not save essay: ' + str(e))

    def do_DELETE(self) -> None:
        self.server.activity.touch()
        essay_id = self.essay_id_from_path()
        if not essay_id:
            return self.error_json(404, 'Not found')
        try:
            with WRITE_LOCK:
                path = path_for(essay_id)
                if not path.is_file():
                    return self.error_json(404, 'Essay not found')
                path.unlink()
            self.json_response(200, {'deleted': essay_id})
        except ValueError as e:
            self.error_json(400, str(e))
        except OSError as e:
            self.error_json(500, 'Could not delete essay: ' + str(e))


def main() -> None:
    parser = argparse.ArgumentParser(description='Run TYPERINO locally')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    rename_existing_essays()
    try:
        server = ThreadingHTTPServer(('127.0.0.1', args.port), Handler)
    except OSError as exc:
        raise SystemExit(f'Could not start TYPERINO on port {args.port}: {exc}')
    server.activity = ActivityTracker()
    stopped = threading.Event()
    monitor = threading.Thread(target=stop_when_idle, args=(server, stopped), daemon=True)
    monitor.start()
    url = f'http://127.0.0.1:{server.server_address[1]}/'
    print('\n  TYPERINO', flush=True)
    print(f'  Open:    {url}', flush=True)
    print(f'  Essays:  {ESSAYS}', flush=True)
    print('  Stop:    Ctrl+C\n', flush=True)
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nTYPERINO stopped.')
    finally:
        stopped.set()
        monitor.join(timeout=3)
        server.server_close()


if __name__ == '__main__':
    main()
