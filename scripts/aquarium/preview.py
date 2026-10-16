"""Loopback-only static engineering preview with a canvas-video export sink.

This serves public static assets, not Vira APIs or owner data. Recordings are
saved in the git-ignored authoring directory, never in the product tree.
"""
import argparse
import json
import os
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=8399)
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[2]
    captures=root/'.aquarium-build'/'recordings'
    captures.mkdir(parents=True,exist_ok=True)
    capture=captures/'latest.webm'
    origin=f'http://localhost:{args.port}'
    allowed_origins={origin,f'http://127.0.0.1:{args.port}'}

    class Handler(SimpleHTTPRequestHandler):
        def __init__(self,*a,**kw):
            super().__init__(*a,directory=str(root/'static'),**kw)

        def do_POST(self):
            if urlsplit(self.path).path!='/__recording' or self.headers.get('Origin') not in allowed_origins:
                self.send_error(403);return
            size=int(self.headers.get('Content-Length','0'))
            if not 0<size<=64*1024*1024 or not self.headers.get('Content-Type','').startswith('video/webm'):
                self.send_error(400);return
            data=self.rfile.read(size)
            if len(data)!=size:
                self.send_error(400);return
            temporary=captures/'incoming.webm'
            temporary.write_bytes(data);os.chmod(temporary,0o600);temporary.replace(capture)
            body=json.dumps({'saved':True,'url':'/__recording','bytes':size}).encode('utf-8')
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)

        def do_GET(self):
            if urlsplit(self.path).path!='/__recording':
                return super().do_GET()
            if not capture.exists():
                self.send_error(404);return
            self.send_response(200);self.send_header('Content-Type','video/webm');self.send_header('Content-Length',str(capture.stat().st_size));self.end_headers()
            with capture.open('rb') as stream:
                while block:=stream.read(1024*1024):self.wfile.write(block)

    print(f'Engineering preview: {origin}/aquarium-benchmark.html',flush=True)
    ThreadingHTTPServer(('127.0.0.1',args.port),Handler).serve_forever()


if __name__=='__main__':main()
