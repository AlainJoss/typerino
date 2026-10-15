#!/usr/bin/env python3
"""Start the local app, or reopen this folder's running instance."""
import json
from pathlib import Path
from urllib.request import ProxyHandler, build_opener
import webbrowser
import sys

# The writing app stays beside this launcher bundle.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
import server


def main():
    url = 'http://127.0.0.1:8765'
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(url + '/api/info', timeout=1) as response:
            info = json.load(response)
        if Path(info.get('essaysDirectory', '')).resolve() == server.ESSAYS.resolve():
            webbrowser.open(url)
            print('Opened the running Typerino.')
            return
    except (OSError, ValueError):
        pass
    server.main()


if __name__ == '__main__':
    main()
