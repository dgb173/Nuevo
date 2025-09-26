from pathlib import Path
path = Path('app.py')
lines = path.read_text(encoding='utf-8').splitlines()
if 'from pathlib import Path' not in lines:
    for idx, line in enumerate(lines):
        if line.strip() == 'from bs4 import BeautifulSoup':
            lines.insert(idx + 1, 'from pathlib import Path')
            break
    else:
        raise SystemExit('Could not find insertion point for Path import')
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
