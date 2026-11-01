"""Deployability checks: real routes and links at both root and project prefixes."""
from html.parser import HTMLParser
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class Document(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.links, self.ids, self.titles = [], set(), 0
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            if attrs['id'] in self.ids:
                raise AssertionError(f"Duplicate id: {attrs['id']}")
            self.ids.add(attrs['id'])
        for key in ('href', 'src'):
            if attrs.get(key):
                self.links.append(attrs[key])
        if tag == 'title':
            self.titles += 1


class BuildTests(unittest.TestCase):
    def build_at(self, base):
        from build import build
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        target = Path(temp.name) / 'output'
        build(target, base_path=base)
        return target

    def test_project_routes_and_all_local_links_resolve(self):
        self.check_links('/chelus/')

    def test_root_deployment_and_all_local_links_resolve(self):
        self.check_links('/')

    def check_links(self, base):
        target = self.build_at(base)
        expected = ['', 'start', 'guide', 'guide/telegram', 'guide/memory-and-skills',
                    'guide/tools-and-approvals', 'guide/scheduling', 'reference']
        for route in expected:
            self.assertTrue((target / route / 'index.html').is_file(), route)
        docs = {p: Document(p.read_text()) for p in target.rglob('*.html')}
        for source, doc in docs.items():
            self.assertEqual(doc.titles, 1, source)
            for link in doc.links:
                url = urlsplit(link)
                if url.scheme or url.netloc:
                    continue
                path = unquote(url.path)
                if not path:
                    dest = source
                else:
                    self.assertTrue(path.startswith(base), (source, link))
                    dest = target / path[len(base):]
                    if path.endswith('/'):
                        dest = dest / 'index.html'
                self.assertTrue(dest.is_file(), (source, link))
                if url.fragment:
                    self.assertIn(url.fragment, docs[dest].ids, (source, link))

    def test_no_unreleased_version_or_offline_claim(self):
        target = self.build_at('/chelus/')
        home = (target / 'index.html').read_text()
        self.assertIn('Inference via DeepSeek', home)
        self.assertIn('Illustrative', home)
        self.assertNotIn('1.0 is here', home)
        self.assertNotIn('runs offline', home.lower())

    def test_prefix_is_normalized(self):
        target = self.build_at('chelus')
        self.assertIn('href="/chelus/start/"', (target / 'index.html').read_text())

    def test_unsafe_prefix_is_rejected(self):
        from build import build
        with tempfile.TemporaryDirectory() as temp:
            for value in ('../secret', '/a/../b', 'https://example.com', '/a?x=1', '/a"b'):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    build(Path(temp) / 'output', base_path=value)

    def test_source_cannot_be_used_as_build_destination(self):
        from build import build
        with self.assertRaises(ValueError):
            build(ROOT / 'assets', base_path='/')

    def test_reference_includes_source_defaults(self):
        target = self.build_at('/chelus/')
        reference = (target / 'reference/index.html').read_text()
        self.assertIn('deepseek-flash', reference)
        self.assertIn('shell_timeout', reference)
        self.assertIn('120.0', reference)
        self.assertIn('memory_char_limit', reference)
        self.assertIn('2200', reference)
        self.assertIn('workdir', reference)
        self.assertIn('~ (your home directory)', reference)

    def test_accessible_progressive_enhancement(self):
        target = self.build_at('/chelus/')
        text = (target / 'start/index.html').read_text()
        self.assertIn('class="skip-link"', text)
        self.assertIn('aria-current="page"', text)
        self.assertIn('data-copy hidden', text)
        self.assertTrue((target / '.nojekyll').exists())
        self.assertTrue((target / '404.html').exists())

    def test_preview_signature_tracks_source_changes_and_removal(self):
        from dev import source_signature
        with tempfile.TemporaryDirectory() as temp:
            parent = Path(temp)
            root = parent / 'website'
            (root / 'assets').mkdir(parents=True)
            (parent / 'chelus').mkdir()
            config = parent / 'chelus/config.py'
            config.write_text('first')
            first = source_signature(root)
            config.write_text('second version')
            second = source_signature(root)
            self.assertNotEqual(first, second)
            asset = root / 'assets/example.txt'
            asset.write_text('asset')
            third = source_signature(root)
            self.assertNotEqual(second, third)
            asset.unlink()
            self.assertEqual(second, source_signature(root))


if __name__ == '__main__':
    unittest.main()
