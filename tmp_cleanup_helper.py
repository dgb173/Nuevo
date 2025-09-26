from pathlib import Path
path = Path('app.py')
text = path.read_text(encoding='utf-8')
start_token = 'def _resolve_chromium_executable() -> str | None:'
end_token = '\n\n\ndef _get_shared_requests_session():'
start_idx = text.find(start_token)
end_idx = text.find(end_token)
if start_idx == -1 or end_idx == -1:
    raise SystemExit('Unable to locate helper block for cleanup')

helper_lines = [
    'def _resolve_chromium_executable() -> str | None:',
    '    candidates = [',
    '        os.environ.get("CHROME_BINARY"),',
    '        shutil.which("chromium"),',
    '        shutil.which("chromium-browser"),',
    '        shutil.which("google-chrome"),',
    '        "/usr/bin/chromium",',
    '        "/usr/bin/chromium-browser",',
    '        "/usr/bin/google-chrome",',
    '    ]',
    '    for candidate in candidates:',
    '        if candidate and os.path.exists(candidate):',
    '            return candidate',
    '',
    '    pw_path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")',
    '    if pw_path:',
    '        base = Path(pw_path)',
    '        if base.exists():',
    '            patterns = ("chromium-*", "chrome-*", "chromium", "chrome")',
    '            for pattern in patterns:',
    '                for item in sorted(base.glob(pattern), reverse=True):',
    '                    if item.is_file() and os.access(item, os.X_OK):',
    '                        return str(item)',
    '                    if item.is_dir():',
    '                        for candidate in [',
    '                            item / "chrome-linux" / "chrome",',
    '                            item / "chrome-linux" / "chromium",',
    '                            item / "chrome-linux" / "headless_shell",',
    '                        ]:',
    '                            if candidate.exists() and os.access(candidate, os.X_OK):',
    '                                return str(candidate)',
    '',
    '    cache_base = Path.home() / ".cache" / "ms-playwright"',
    '    if cache_base.exists():',
    '        for item in sorted(cache_base.glob("chromium-*"), reverse=True):',
    '            candidate = item / "chrome-linux" / "chrome"',
    '            if candidate.exists() and os.access(candidate, os.X_OK):',
    '                return str(candidate)',
    '',
    '    return None',
    '',
]
new_helper = '\r\n'.join(helper_lines) + '\r\n'

text = text[:start_idx] + new_helper + text[end_idx:]
path.write_text(text, encoding='utf-8')
