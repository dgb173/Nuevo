from pathlib import Path
path = Path('app.py')
text = path.read_text(encoding='utf-8')
start_token = 'def _resolve_chromium_executable() -> str | None:'
end_token = '\n\n\ndef _get_shared_requests_session():'
start_idx = text.find(start_token)
end_idx = text.find(end_token)
if start_idx == -1 or end_idx == -1:
    raise SystemExit('Unable to locate helper block for final formatting')

new_helper = (
    'def _resolve_chromium_executable() -> str | None:\n'
    '    candidates = [\n'
    '        os.environ.get("CHROME_BINARY"),\n'
    '        shutil.which("chromium"),\n'
    '        shutil.which("chromium-browser"),\n'
    '        shutil.which("google-chrome"),\n'
    '        "/usr/bin/chromium",\n'
    '        "/usr/bin/chromium-browser",\n'
    '        "/usr/bin/google-chrome",\n'
    '    ]\n'
    '    for candidate in candidates:\n'
    '        if candidate and os.path.exists(candidate):\n'
    '            return candidate\n'
    '\n'
    '    pw_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")\n'
    '    if pw_path:\n'
    '        base = Path(pw_path)\n'
    '        if base.exists():\n'
    '            patterns = ("chromium-*", "chrome-*", "chromium", "chrome")\n'
    '            for pattern in patterns:\n'
    '                for item in sorted(base.glob(pattern), reverse=True):\n'
    '                    if item.is_file() and os.access(item, os.X_OK):\n'
    '                        return str(item)\n'
    '                    if item.is_dir():\n'
    '                        for candidate in [\n'
    '                            item / "chrome-linux" / "chrome",\n'
    '                            item / "chrome-linux" / "chromium",\n'
    '                            item / "chrome-linux" / "headless_shell",\n'
    '                        ]:\n'
    '                            if candidate.exists() and os.access(candidate, os.X_OK):\n'
    '                                return str(candidate)\n'
    '\n'
    '    cache_base = Path.home() / ".cache" / "ms-playwright"\n'
    '    if cache_base.exists():\n'
    '        for item in sorted(cache_base.glob("chromium-*"), reverse=True):\n'
    '            candidate = item / "chrome-linux" / "chrome"\n'
    '            if candidate.exists() and os.access(candidate, os.X_OK):\n'
    '                return str(candidate)\n'
    '\n'
    '    return None\n'
    '\n'
)

text = text[:start_idx] + new_helper + text[end_idx:]
path.write_text(text, encoding='utf-8')
