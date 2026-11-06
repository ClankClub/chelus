"""Build a dependency-free, directory-based static site for GitHub Pages."""
from __future__ import annotations

import argparse
import ast
from html import escape
from pathlib import Path
import re
import shutil
from string import Template

from content import PAGES, code

ROOT = Path(__file__).resolve().parent
REPO = 'https://github.com/ClankClub/chelus'
ORIGIN = 'https://chelus.clankclub.site'


def normalize_base(value: str) -> str:
    if not re.fullmatch(r'/?(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]*/?', value):
        raise ValueError('Base path must contain only URL path segments, e.g. /chelus/')
    return '/' + value.strip('/') + '/' if value.strip('/') else '/'


def settings_table() -> str:
    """Read public config defaults without importing Chelus or touching user data."""
    tree = ast.parse((ROOT.parent / 'chelus/config.py').read_text(encoding='utf-8'))
    settings = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'Settings')
    rows = []
    for node in settings.body:
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            if node.target.id == 'workdir':
                value = '~ (your home directory)'
            else:
                continue  # Private directory overrides use environment variables.
        rendered = str(value).lower() if isinstance(value, bool) else str(value)
        rows.append(f'<tr><td><code>{escape(node.target.id)}</code></td><td><code>{escape(rendered)}</code></td></tr>')
    return '<div class="table-scroll"><table><thead><tr><th scope="col">Setting</th><th scope="col">Default</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div>'


def build(output: Path, base_path: str = '/chelus/') -> None:
    base = normalize_base(base_path)
    output = output.resolve()
    sources = [ROOT / x for x in ('assets', 'templates', 'tests')]
    if output == ROOT or output in ROOT.parents or any(output == p or p in output.parents for p in sources):
        raise ValueError('Build destination must not overwrite site source')
    output.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / 'assets', output / 'assets', dirs_exist_ok=True)
    # Reuse the repository's exact existing symbol, with a cropped SVG viewBox.
    original = (ROOT.parent / 'assets/chelus-header.svg').read_text(encoding='utf-8')
    symbol = re.search(r'(<rect x="108".*?</path>)', original, flags=re.S)
    if not symbol:
        # The original path is self-closing; preserve both exact source elements.
        symbol = re.search(r'(<rect x="108"[^>]+/>\s*<path [^>]+/>)', original)
    if not symbol:
        raise ValueError('The repository brand symbol has changed; review the site asset extraction')
    mark = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="108 84 278 265">' + symbol.group(1) + '</svg>'
    (output / 'assets/chelus-mark.svg').write_text(mark, encoding='utf-8')
    template = Template((ROOT / 'templates/base.html').read_text(encoding='utf-8'))
    home = (ROOT / 'templates/home.html').read_text(encoding='utf-8')
    all_pages = {'': {'title': 'Small footprint. Real agency.', 'description': 'A persistent AI agent with tools, memory, and skills. Standard-library Python, demonstrated on 32-bit Termux.', 'body': home}, **PAGES}
    all_pages['404.html'] = {'title': 'Page not found', 'description': 'Find your way back to Chelus.', 'body': '<section class="not-found"><p class="eyebrow">404 / A WRONG TURN</p><h1>This path ends here.</h1><p>Let’s get you back to something useful.</p><a class="button" href="{{base}}">Back to Chelus <img src="{{base}}assets/icons/arrow-right.svg" alt="" width="22" height="22"></a></section>'}
    for route, page in all_pages.items():
        section = 'start' if route == 'start' else 'guide' if route.startswith('guide') else 'reference' if route == 'reference' else ''
        def nav(path, label):
            current = ' aria-current="page"' if section == path else ''
            return f'<a href="{base}{path}/"{current}>{label}</a>'
        body = page['body'].replace('{{settings}}', settings_table() if route == 'reference' else '')
        body = body.replace('{{base}}', base)
        toc = ''
        if route and route != '404.html':
            links = [('', 'Overview'), ('telegram', 'Telegram'), ('memory-and-skills', 'Memory & skills'), ('notes', 'Notes'), ('tools-and-approvals', 'Tools & approvals'), ('scheduling', 'Scheduled jobs')]
            sidebar = f'<a class="doc-top" href="{base}start/">Get started</a><p class="sidebar-label">GUIDE</p>'
            for slug, label in links:
                target = 'guide' + ('/' + slug if slug else '')
                current = ' aria-current="page"' if route == target else ''
                sidebar += f'<a href="{base}{target}/"{current}>{label}</a>'
            sidebar += f'<p class="sidebar-label">DETAILS</p><a href="{base}reference/"' + (' aria-current="page"' if route == 'reference' else '') + '>Reference</a>'
            headings = re.findall(r'<h2 id="([^"]+)">(.+?)</h2>', body)
            toc = '<nav class="page-toc" aria-label="On this page"><p class="sidebar-label">ON THIS PAGE</p>' + ''.join(f'<a href="{base}{route}/#{anchor}">{title}</a>' for anchor, title in headings) + '</nav>'
            body = f'<div class="docs-layout"><aside class="doc-sidebar"><nav aria-label="Documentation">{sidebar}</nav></aside><article class="prose"><p class="eyebrow">{escape(page["kicker"])}</p><h1>{escape(page["heading"])}</h1><p class="lead">{escape(page["intro"])}</p>{body}<div class="doc-source"><span>Found a gap?</span> <a href="{REPO}/issues">Open an issue</a> <span>or consult the</span> <a href="{REPO}/blob/main/README.md">source documentation</a>.</div></article>{toc}</div>'
        canonical = ORIGIN + base + (route + '/' if route and route != '404.html' else route)
        html = template.substitute(title=escape(page['title']), description=escape(page['description'], quote=True), base=base, canonical=canonical, body=body, page_class='homepage' if not route else 'documentation', nav_start=nav('start', 'Get started'), nav_guide=nav('guide', 'Guide'), repo=REPO, robots='<meta name="robots" content="noindex">' if route == '404.html' else '')
        target = output / route if route == '404.html' else output / route / 'index.html'
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(html, encoding='utf-8')
    (output / '.nojekyll').write_text('')
    sitemap = '<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">' + ''.join(f'<url><loc>{ORIGIN}{base}{route + "/" if route else ""}</loc></url>' for route in all_pages if route != '404.html') + '</urlset>'
    (output / 'sitemap.xml').write_text(sitemap, encoding='utf-8')
    (output / 'robots.txt').write_text(f'User-agent: *\nAllow: {base}\nSitemap: {ORIGIN}{base}sitemap.xml\n')
    print(f'Built {len(all_pages)} pages at {output} (base {base})')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-path', default='/chelus/')
    parser.add_argument('--output', type=Path, default=ROOT / 'dist')
    args = parser.parse_args()
    build(args.output, args.base_path)
