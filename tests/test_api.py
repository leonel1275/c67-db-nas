#!/usr/bin/env python3
import http.client
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import patch

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "android", "app", "src", "main", "python"))
import c67core


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.temp.name, "nas")
        os.mkdir(self.root)
        c67core._configure_logging(self.temp.name)
        c67core._state.update(
            root=self.root, files_dir=self.temp.name, token="test-token-not-for-logs",
            started=time.time(), max_upload_bytes=16, min_free_bytes=0,
        )
        self.server = c67core.ThreadingHTTPServer(("127.0.0.1", 0), c67core.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        for handler in c67core._state["logger"].handlers[:]:
            handler.close()
            c67core._state["logger"].removeHandler(handler)
        self.temp.cleanup()

    def request(self, method, path, body=None, token=True, cookie=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        headers = {}
        if token:
            headers["Authorization"] = "Bearer " + c67core._state["token"]
        if cookie:
            headers["Cookie"] = cookie
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
            headers["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=headers)
        response = conn.getresponse()
        data = response.read()
        result = (response.status, response.getheaders(), data)
        conn.close()
        return result

    def json_request(self, *args, **kwargs):
        status, headers, body = self.request(*args, **kwargs)
        return status, dict(headers), json.loads(body or b"{}")

    def test_upload_limit_validation_and_atomic_publish(self):
        self.assertEqual(self.json_request("PUT", "/api/nas/upload?path=a", b"x" * 17)[0], 413)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        conn.putrequest("PUT", "/api/nas/upload?path=missing-length")
        conn.putheader("Authorization", "Bearer " + c67core._state["token"])
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 411)
        conn.close()

        status, _, result = self.json_request("PUT", "/api/nas/upload?path=small.txt", b"hello")
        self.assertEqual(status, 201)
        self.assertEqual(result["size"], 5)
        with open(os.path.join(self.root, "small.txt"), "rb") as uploaded:
            self.assertEqual(uploaded.read(), b"hello")
        self.assertFalse(any(name.startswith(".c67-upload-") for name in os.listdir(self.root)))

    def test_free_space_error_and_path_traversal(self):
        with patch.object(c67core.shutil, "disk_usage", return_value=SimpleNamespace(free=3)):
            status, _, body = self.json_request("PUT", "/api/nas/upload?path=full.txt", b"1234")
        self.assertEqual(status, 507)
        self.assertEqual(body["error"]["code"], "insufficient_storage")
        self.assertEqual(self.json_request("PUT", "/api/nas/upload?path=../escape", b"x")[0], 400)
        self.assertEqual(self.json_request("GET", "/api/nas/list?path=../escape")[0], 400)
        self.assertFalse(os.path.exists(os.path.join(self.temp.name, "escape")))

    def test_mkdir_list_delete_and_structured_auth_errors(self):
        status, _, unauthorized = self.json_request(
            "GET", "/api/nas/list", token=False
        )
        self.assertEqual(status, 401)
        self.assertEqual(unauthorized["error"]["code"], "unauthorized")
        self.assertEqual(self.request("POST", "/api/nas/mkdir?path=docs")[0], 201)
        self.assertEqual(self.request("POST", "/api/nas/mkdir?path=docs")[0], 409)
        self.assertEqual(self.request("PUT", "/api/nas/upload?path=docs%2Fnote.txt", b"note")[0], 201)
        self.assertEqual(self.request("DELETE", "/api/nas/delete?path=docs")[0], 409)
        self.assertEqual(self.request("DELETE", "/api/nas/delete?path=docs%2Fnote.txt")[0], 200)
        self.assertEqual(self.request("DELETE", "/api/nas/delete?path=docs")[0], 200)

    def test_interrupted_upload_removes_temporary_file(self):
        protected_path = os.path.join(self.root, "protected.txt")
        with open(protected_path, "wb") as protected:
            protected.write(b"previous-good-content")
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        sock.sendall(
            b"PUT /api/nas/upload?path=protected.txt HTTP/1.1\r\n"
            b"Host: localhost\r\nAuthorization: Bearer "
            + c67core._state["token"].encode()
            + b"\r\nContent-Length: 12\r\n\r\nabc"
        )
        sock.shutdown(socket.SHUT_WR)
        sock.recv(4096)
        sock.close()
        deadline = time.time() + 2
        while time.time() < deadline and any(n.startswith(".c67-upload-") for n in os.listdir(self.root)):
            time.sleep(0.02)
        with open(protected_path, "rb") as protected:
            self.assertEqual(protected.read(), b"previous-good-content")
        self.assertFalse(any(n.startswith(".c67-upload-") for n in os.listdir(self.root)))

    def test_idle_upload_times_out_and_releases_temporary_file(self):
        with patch.object(c67core, "UPLOAD_IDLE_TIMEOUT_SECONDS", 0.1):
            sock = socket.create_connection(("127.0.0.1", self.port), timeout=3)
            sock.settimeout(3)
            sock.sendall(
                b"PUT /api/nas/upload?path=idle HTTP/1.1\r\n"
                b"Host: localhost\r\nAuthorization: Bearer "
                + c67core._state["token"].encode()
                + b"\r\nContent-Length: 1\r\n\r\n"
            )
            response = sock.recv(4096)
            sock.close()
        self.assertIn(b"408", response)
        self.assertFalse(os.path.exists(os.path.join(self.root, "idle")))
        self.assertFalse(any(n.startswith(".c67-upload-") for n in os.listdir(self.root)))

    def test_no_overwrite_is_atomic_for_concurrent_requests(self):
        def upload():
            return self.request("PUT", "/api/nas/upload?path=shared.txt&overwrite=0", b"replacement")[0]

        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses = list(pool.map(lambda _: upload(), range(2)))
        self.assertEqual(sorted(statuses), [201, 409])
        with open(os.path.join(self.root, "shared.txt"), "rb") as uploaded:
            self.assertEqual(uploaded.read(), b"replacement")

    def test_login_protects_web_app_and_reuses_cookie_for_api(self):
        status, _, body = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b"Ingresa el token", body)
        self.assertNotIn(b"let path=", body)
        self.assertEqual(self.request("POST", "/ui/login", {"token": "wrong"}, token=False)[0], 401)
        status, headers, _ = self.request(
            "POST", "/ui/login", {"token": "test-token-not-for-logs"}, token=False
        )
        self.assertEqual(status, 200)
        cookie = dict(headers)["Set-Cookie"].split(";", 1)[0]
        status, _, body = self.request("GET", "/", token=False, cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn(b"let path=", body)
        self.assertEqual(self.request("GET", "/api/nas/list", token=False, cookie=cookie)[0], 200)
        self.assertEqual(self.request("POST", "/ui/logout", token=False, cookie=cookie)[0], 200)

    def test_rotating_logs_never_include_token_or_file_content(self):
        old_max = c67core.LOG_MAX_BYTES
        try:
            c67core.LOG_MAX_BYTES = 128
            c67core._configure_logging(self.temp.name)
            for _ in range(30):
                c67core._log("test_event", result="x" * 80)
            files = [n for n in os.listdir(self.temp.name) if n.startswith("nas-api.log")]
            self.assertLessEqual(len(files), c67core.LOG_BACKUP_COUNT + 1)
            chunks = []
            for name in files:
                with open(os.path.join(self.temp.name, name), "rb") as log_file:
                    chunks.append(log_file.read())
            logs = b"".join(chunks)
            self.assertNotIn(b"test-token-not-for-logs", logs)
            self.assertNotIn(b"file-content", logs)
        finally:
            c67core.LOG_MAX_BYTES = old_max


if __name__ == "__main__":
    unittest.main()
