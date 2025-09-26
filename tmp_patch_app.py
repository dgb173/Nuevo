from pathlib import Path
path = Path('app.py')
text = path.read_text(encoding='utf-8')

if 'from pathlib import Path' not in text[:200]:
    text = text.replace('from bs4 import BeautifulSoup\r\nimport datetime', 'from bs4 import BeautifulSoup\r\nfrom pathlib import Path\r\nimport datetime', 1)

start_token = 'def _resolve_chromium_executable() -> str | None:'
end_token = '\n\n\ndef _get_shared_requests_session():'
start_idx = text.find(start_token)
end_idx = text.find(end_token)
if start_idx == -1 or end_idx == -1:
    raise SystemExit('Unable to locate helper function block')

new_helper = '''def _resolve_chromium_executable() -> str | None:\r\n    candidates = [\r\n        os.environ.get('CHROME_BINARY'),\r\n        shutil.which('chromium'),\r\n        shutil.which('chromium-browser'),\r\n        shutil.which('google-chrome'),\r\n        '/usr/bin/chromium',\r\n        '/usr/bin/chromium-browser',\r\n        '/usr/bin/google-chrome',\r\n    ]\r\n    for candidate in candidates:\r\n        if candidate and os.path.exists(candidate):\r\n            return candidate\r\n\r\n    pw_path = os.environ.get('PLAYWRIGHT_BROWSERS_PATH')\r\n    if pw_path:\r\n        base = Path(pw_path)\r\n        if base.exists():\r\n            patterns = ('chromium-*', 'chrome-*', 'chromium', 'chrome', 'ms-playwright/chromium-*')\r\n            for pattern in patterns:\r\n                for item in sorted(base.glob(pattern), reverse=True):\r\n                    if item.is_file() and os.access(item, os.X_OK):\r\n                        return str(item)\r\n                    if item.is_dir():\r\n                        for candidate in [\r\n                            item / 'chrome-linux' / 'chrome',\r\n                            item / 'chrome-linux' / 'chromium',\r\n                            item / 'chrome-linux' / 'headless_shell',\r\n                            item / 'chrome-linux' / 'chrome-wrapper',\r\n                            item / 'chrome-linux' / 'chrome-sandbox',\r\n                        ]:\r\n                            if candidate.exists() and os.access(candidate, os.X_OK):\r\n                                return str(candidate)\r\n\r\n    cache_base = Path.home() / '.cache' / 'ms-playwright'\r\n    if cache_base.exists():\r\n        for item in sorted(cache_base.glob('chromium-*'), reverse=True):\r\n            candidate = item / 'chrome-linux' / 'chrome'\r\n            if candidate.exists() and os.access(candidate, os.X_OK):\r\n                return str(candidate)\r\n\r\n    return None\r\n\r\n'''

text = text[:start_idx] + new_helper + text[end_idx:]
path.write_text(text, encoding='utf-8')
