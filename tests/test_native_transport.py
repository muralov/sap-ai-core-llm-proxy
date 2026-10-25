"""Real loopback HTTP and threaded WSGI lifecycle checks; no SAP credentials."""

import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import requests
from werkzeug.serving import make_server

import proxy_server as proxy
from tests.test_routes import TOKEN, _make_config


class NativeTransportTests(unittest.TestCase):
    def setUp(self):
        self.release = threading.Event()
        self.closed = []
        self.attempts = []
        case = self

        class Upstream(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                case.attempts.append((self.path, body, dict(self.headers)))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()
                def send(event):
                    raw = (f"event: {event['type']}\ndata: {json.dumps(event)}\n\n").encode()
                    self.wfile.write(f"{len(raw):x}\r\n".encode() + raw + b"\r\n")
                    self.wfile.flush()
                try:
                    send({"type": "response.created", "response": {"id": body["model"]}})
                    if body["input"] == "stall":
                        case.release.wait(2)
                    elif body["input"] == "disconnect":
                        for index in range(100):
                            send({"type": "response.output_text.delta", "delta": "x" * 1024,
                                  "sequence_number": index})
                            if case.release.wait(0.01):
                                break
                    send({"type": "response.completed", "response": {
                        "id": body["model"], "object": "response", "status": "completed",
                        "output": [], "usage": {"input_tokens": 1, "output_tokens": 1}}})
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                self.close_connection = True

        self.backend = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        self.backend.daemon_threads = True
        self.backend_thread = threading.Thread(target=self.backend.serve_forever, daemon=True)
        self.backend_thread.start()
        self.cfg = _make_config()
        root = f"http://127.0.0.1:{self.backend.server_port}"
        self.cfg.subaccounts["S"].normalized_models.update({
            "gpt-6-astra": [root + "/astra"], "gpt-6.1-sol": [root + "/sol"]})
        self.cfg.build_model_mapping()
        self.session = proxy.create_native_http_session()
        original_post = self.session.post
        def tracked_post(*args, **kwargs):
            response = original_post(*args, **kwargs)
            original_close = response.close
            def close():
                case.closed.append(response.url)
                original_close()
            response.close = close
            return response
        self.session.post = tracked_post
        self.patches = [patch.object(proxy, "proxy_config", self.cfg),
                        patch.object(proxy, "fetch_token", return_value="loopback-token"),
                        patch.object(proxy, "_native_http_session", self.session),
                        patch.object(proxy, "_NATIVE_RESPONSES_TIMEOUT", (1, 0.2))]
        for item in self.patches:
            item.start()
        self.wsgi = make_server("127.0.0.1", 0, proxy.app, threaded=True)
        self.wsgi_thread = threading.Thread(target=self.wsgi.serve_forever, daemon=True)
        self.wsgi_thread.start()
        self.url = f"http://127.0.0.1:{self.wsgi.server_port}/v1/responses"
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.release.set()
        self.wsgi.shutdown()
        self.wsgi.server_close()
        self.backend.shutdown()
        self.backend.server_close()
        self.session.close()
        for item in reversed(self.patches):
            item.stop()

    def post(self, mode="normal", model="gpt-6-astra"):
        return requests.post(self.url, headers={"Authorization": "Bearer " + TOKEN},
                             json={"model": model, "input": mode, "stream": True},
                             stream=True, timeout=(1, 2))

    def wait_closed(self, count, bound=1):
        deadline = time.monotonic() + bound
        while len(self.closed) < count and time.monotonic() < deadline:
            threading.Event().wait(0.01)
        self.assertEqual(len(self.closed), count)

    def test_real_http_normal_close_and_model_isolation(self):
        responses = []
        threads = [threading.Thread(target=lambda m=model: responses.append((m, self.post(model=m))))
                   for model in ("gpt-6-astra", "gpt-6.1-sol")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
        for model, response in responses:
            with response:
                self.assertEqual(response.status_code, 200)
                self.assertIn('"id": "' + model + '"', response.text)
                self.assertIn("response.completed", response.text)
        self.assertEqual(len(responses), 2)
        self.wait_closed(2)
        self.assertEqual({path for path, _, _ in self.attempts},
                         {"/astra/responses?api-version=2025-03-01-preview",
                          "/sol/responses?api-version=2025-03-01-preview"})

    def test_close_of_unconsumed_wsgi_response_closes_open_upstream(self):
        with proxy.app.test_request_context("/v1/responses", method="POST",
                headers={"Authorization": "Bearer " + TOKEN},
                json={"model": "gpt-6-astra", "input": "stall", "stream": True}):
            response = proxy.proxy_responses_request()
            response.close()
            response.close()
        self.wait_closed(1)

    def test_client_disconnect_releases_upstream_and_other_model_works(self):
        response = self.post("disconnect")
        next(response.iter_content(chunk_size=1))
        response.close()
        self.wait_closed(1)
        with self.post(model="gpt-6.1-sol") as subsequent:
            self.assertIn("response.completed", subsequent.text)
        self.wait_closed(2)

    def test_silent_read_is_bounded_after_disconnect(self):
        start = time.monotonic()
        response = self.post("stall")
        next(response.iter_content(chunk_size=1))
        response.close()
        self.wait_closed(1)
        self.assertLess(time.monotonic() - start, 1)
        with self.post(model="gpt-6.1-sol") as subsequent:
            self.assertIn("response.completed", subsequent.text)
        self.wait_closed(2)
