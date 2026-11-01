"""Local static preview. Accepts the standard sites-preview host/port flags."""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock

from build import ROOT, normalize_base


def source_signature(root: Path = ROOT) -> tuple:
    files = [root / 'build.py', root / 'content.py', root.parent / 'chelus/config.py',
             root.parent / 'assets/chelus-header.svg', *list((root / 'templates').rglob('*')),
             *list((root / 'assets').rglob('*'))]
    return tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size)
                 for p in sorted(files) if p.is_file())


def serve():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=4173)
    parser.add_argument('--strictPort', action='store_true')
    parser.add_argument('--base-path', default='/chelus/')
    args = parser.parse_args()
    base = normalize_base(args.base_path)
    preview = ROOT / '.preview'
    destination = preview / base.strip('/')
    lock = Lock()
    stamp = None

    def refresh():
        nonlocal stamp
        current = source_signature()
        if current != stamp:
            # Reload authored content while keeping a dependency-free preview.
            import importlib
            import content
            import build as builder
            importlib.reload(content)
            importlib.reload(builder)
            builder.build(destination, base)
            stamp = current

    class Handler(SimpleHTTPRequestHandler):
        def do_GET(self):
            with lock:
                refresh()
            if self.path == '/' and base != '/':
                self.send_response(302)
                self.send_header('Location', base)
                self.end_headers()
                return
            super().do_GET()

        def end_headers(self):
            self.send_header('Cache-Control', 'no-store')
            super().end_headers()

    refresh()
    server = ThreadingHTTPServer((args.host, args.port), partial(Handler, directory=str(preview)))
    print(f'Preview on port {args.port}, base {base}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()


if __name__ == '__main__':
    serve()
