"""Read-only BRS bridge for the loopback booking simulation."""

import datetime as dt
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


def start_visitor_reader(key):
    from rosapenna_backend.integrations.brs.tee_sheet import BrsReader, COURSES

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if not compare_digest(self.headers.get("X-API-Key", "").encode(), key.encode()):
                return self.send_error(403)
            url = urlsplit(self.path)
            params = parse_qs(url.query)
            try:
                day = dt.date.fromisoformat(params["date"][0])
                course = int(params["course_id"][0])
                if url.path != "/tee-sheet" or course not in COURSES:
                    raise ValueError
            except (ValueError, KeyError):
                return self.send_error(400)
            try:
                with BrsReader(timeout=12) as reader:
                    reader.opener = build_opener(ProxyHandler({}), NoRedirects())
                    sheet = reader.fetch(day.isoformat(), course)
                body = json.dumps(sheet).encode()
            except Exception:
                return self.send_error(502, "Could not read visitor tee times")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server
