"""C67 DB+NAS: API HTTP minima (solo libreria estandar, sin wheels nativos).

Endpoints (JSON):
  GET    /api/health                      sin token (solo clientes permitidos)
  GET    /api/nas/list?path=sub/dir       token
  GET    /api/nas/download?path=a/b.txt   token
  PUT    /api/nas/upload?path=a/b.txt     token, cuerpo crudo (Content-Length obligatorio)
  POST   /api/nas/mkdir?path=a/b           token
  DELETE /api/nas/delete?path=a/b.txt     token (archivo o directorio vacio)

Token: cabecera "Authorization: Bearer <token>" o "X-Token: <token>".
Solo acepta clientes de loopback, Tailscale (100.64.0.0/10, fd7a:115c:a1e0::/48)
y redes privadas (10/8, 172.16/12, 192.168/16).
"""
import hmac
import ipaddress
import json
import os
import shutil
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

PG_PORT = 5432
CHUNK = 1 << 20

ALLOWED_NETS = [
    ipaddress.ip_network(n)
    for n in (
        "127.0.0.0/8", "::1/128",
        "100.64.0.0/10", "fd7a:115c:a1e0::/48",
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    )
]

_state = {"root": None, "token": "", "server": None, "port": 0, "started": 0.0}


def _pg_up():
    try:
        with socket.create_connection(("127.0.0.1", PG_PORT), timeout=0.5):
            return True
    except OSError:
        return False


def _client_allowed(addr):
    try:
        ip = ipaddress.ip_address(addr.split("%")[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return any(ip in net for net in ALLOWED_NETS)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "c67-nas/2"

    def log_message(self, fmt, *args):  # silencio: Postgres/Chaquopy ya inundan logcat
        pass

    # ---- helpers ----
    def _send(self, code, obj=None, raw=None, ctype="application/json", extra=None):
        body = raw if raw is not None else json.dumps(obj if obj is not None else {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _err(self, code, msg):
        self.close_connection = True  # puede haber cuerpo sin leer
        self._send(code, {"error": msg})

    def _gate(self, need_token):
        if not _client_allowed(self.client_address[0]):
            self._err(403, "client not allowed")
            return False
        if need_token:
            got = ""
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                got = auth[7:].strip()
            got = got or self.headers.get("X-Token", "")
            if not _state["token"] or not hmac.compare_digest(got.encode(), _state["token"].encode()):
                self._err(401, "invalid token")
                return False
        return True

    def _query(self):
        parts = urlsplit(self.path)
        q = parse_qs(parts.query, keep_blank_values=True)
        return parts.path, {k: v[0] for k, v in q.items()}

    def _resolve(self, rel):
        """Ruta dentro de la raiz NAS o None (anti path-traversal, sigue symlinks)."""
        if "\x00" in rel:
            return None
        root = os.path.realpath(_state["root"])
        target = os.path.realpath(os.path.join(root, rel.lstrip("/")))
        if target == root or target.startswith(root + os.sep):
            return target
        return None

    # ---- verbos ----
    def do_GET(self):
        path, q = self._query()
        if path == "/api/health":
            if not self._gate(False):
                return
            root = _state["root"]
            self._send(200, {
                "status": "ok",
                "postgres": _pg_up(),
                "nas_dir": root,
                "nas_writable": bool(root) and os.access(root, os.W_OK),
                "uptime_s": int(time.time() - _state["started"]),
            })
        elif path == "/api/nas/list":
            if not self._gate(True):
                return
            target = self._resolve(q.get("path", ""))
            if target is None or not os.path.isdir(target):
                return self._err(404, "not a directory")
            items = []
            with os.scandir(target) as it:
                for e in sorted(it, key=lambda x: x.name.lower()):
                    try:
                        st = e.stat()
                    except OSError:
                        continue
                    items.append({
                        "name": e.name,
                        "is_dir": e.is_dir(),
                        "size": 0 if e.is_dir() else st.st_size,
                        "mtime": int(st.st_mtime),
                    })
            rel = os.path.relpath(target, os.path.realpath(_state["root"]))
            self._send(200, {"path": "" if rel == "." else rel, "items": items})
        elif path == "/api/nas/download":
            if not self._gate(True):
                return
            target = self._resolve(q.get("path", ""))
            if target is None or not os.path.isfile(target):
                return self._err(404, "file not found")
            size = os.path.getsize(target)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header(
                "Content-Disposition",
                "attachment; filename*=UTF-8''" + quote(os.path.basename(target)),
            )
            self.end_headers()
            with open(target, "rb") as f:
                shutil.copyfileobj(f, self.wfile, CHUNK)
        else:
            self._err(404, "unknown endpoint")

    def _upload(self):
        path, q = self._query()
        if path != "/api/nas/upload":
            return self._err(404, "unknown endpoint")
        if not self._gate(True):
            return
        rel = q.get("path", "")
        target = self._resolve(rel)
        if not rel or target is None or target == os.path.realpath(_state["root"]):
            return self._err(400, "invalid path")
        if os.path.isdir(target):
            return self._err(400, "path is a directory")
        if q.get("overwrite") == "0" and os.path.exists(target):
            return self._err(409, "exists")
        length = self.headers.get("Content-Length")
        if length is None or not length.isdigit():
            return self._err(411, "Content-Length required")
        remaining = int(length)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = os.path.join(os.path.dirname(target), "." + os.path.basename(target) + ".part")
        try:
            with open(tmp, "wb") as f:
                while remaining > 0:
                    chunk = self.rfile.read(min(CHUNK, remaining))
                    if not chunk:
                        raise ConnectionError("client closed early")
                    f.write(chunk)
                    remaining -= len(chunk)
            os.replace(tmp, target)
        except Exception as exc:  # noqa: BLE001
            try:
                os.unlink(tmp)
            except OSError:
                pass
            return self._err(500, "upload failed: {}".format(exc))
        self._send(201, {"path": rel, "size": os.path.getsize(target)})

    do_PUT = _upload

    def do_POST(self):
        path, q = self._query()
        if path != "/api/nas/mkdir":
            return self._upload()
        if not self._gate(True):
            return
        rel = q.get("path", "")
        target = self._resolve(rel)
        if not rel or target is None or target == os.path.realpath(_state["root"]):
            return self._err(400, "invalid path")
        try:
            os.mkdir(target)
        except FileExistsError:
            return self._err(409, "already exists")
        except FileNotFoundError:
            return self._err(404, "parent directory not found")
        except OSError as exc:
            return self._err(409, str(exc))
        self._send(201, {"path": rel})

    def do_DELETE(self):
        path, q = self._query()
        if path != "/api/nas/delete":
            return self._err(404, "unknown endpoint")
        if not self._gate(True):
            return
        target = self._resolve(q.get("path", ""))
        if target is None or target == os.path.realpath(_state["root"]):
            return self._err(400, "invalid path")
        try:
            if os.path.isdir(target):
                os.rmdir(target)
            else:
                os.unlink(target)
        except FileNotFoundError:
            return self._err(404, "not found")
        except OSError as exc:
            return self._err(409, str(exc))
        self._send(200, {"deleted": q.get("path", "")})


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32


def start(files_dir, nas_dir, token, port=8000):
    """Llamado desde Java (Chaquopy). Idempotente. Devuelve texto de estado."""
    if _state["server"] is not None:
        return "already running on {}".format(_state["port"])
    os.makedirs(nas_dir, exist_ok=True)
    _state.update(root=str(nas_dir), token=str(token), started=time.time(), port=int(port))
    server = _Server(("0.0.0.0", int(port)), Handler)
    _state["server"] = server
    threading.Thread(target=server.serve_forever, name="c67-nas", daemon=True).start()
    return "NAS API listening on 0.0.0.0:{} root={}".format(port, nas_dir)
