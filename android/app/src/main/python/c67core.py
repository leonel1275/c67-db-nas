"""C67 DB+NAS: API HTTP minima (solo libreria estandar, sin wheels nativos).

Endpoints (JSON):
  GET    /api/health                      sin token (solo clientes permitidos)
  GET    /api/nas/list?path=sub/dir       token
  GET    /api/nas/download?path=a/b.txt   token
  PUT    /api/nas/upload?path=a/b.txt     token, cuerpo crudo (Content-Length obligatorio)
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
import struct
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

_state = {
    "root": None,
    "token": "",
    "server": None,
    "port": 0,
    "started": 0.0,
    "health": {},
    "history": [],
    "lock": threading.Lock(),
}


def _uptime_s():
    return int(time.time() - _state["started"]) if _state["started"] else 0


def _tcp_up(host, port, timeout=0.7):
    t0 = time.time()
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True, int((time.time() - t0) * 1000), "ok"
    except OSError as exc:
        return False, int((time.time() - t0) * 1000), str(exc)


def _pg_protocol_ping(timeout=0.7):
    """Ping PostgreSQL by speaking the initial wire protocol.

    Sends SSLRequest packet and expects one-byte answer:
      S -> SSL supported, N -> SSL not supported.
    Either response proves postgres accepted and parsed startup bytes.
    """
    t0 = time.time()
    try:
        with socket.create_connection(("127.0.0.1", PG_PORT), timeout=timeout) as s:
            s.settimeout(timeout)
            ssl_request = struct.pack("!II", 8, 80877103)
            s.sendall(ssl_request)
            b = s.recv(1)
            if b in (b"S", b"N"):
                return True, int((time.time() - t0) * 1000), b.decode("ascii")
            if b:
                return False, int((time.time() - t0) * 1000), "unexpected response {}".format(b.hex())
            return False, int((time.time() - t0) * 1000), "empty response"
    except OSError as exc:
        return False, int((time.time() - t0) * 1000), str(exc)


def _disk_usage(path):
    try:
        u = shutil.disk_usage(path)
        total_gb = round(u.total / (1024 ** 3), 3)
        free_gb = round(u.free / (1024 ** 3), 3)
        used_gb = round(u.used / (1024 ** 3), 3)
        return {
            "ok": True,
            "total_gb": total_gb,
            "used_gb": used_gb,
            "free_gb": free_gb,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def _wal_state(root):
    wal_dir = os.path.join(root, "pg_wal")
    if not os.path.isdir(wal_dir):
        return {"ok": False, "exists": False, "dir": wal_dir}
    try:
        files = [
            f for f in os.listdir(wal_dir)
            if f and not f.startswith(".")
        ]
        files.sort(reverse=True)
        latest = files[0] if files else ""
        return {
            "ok": True,
            "exists": True,
            "dir": wal_dir,
            "segments": len(files),
            "latest": latest,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "exists": True, "dir": wal_dir, "error": str(exc)}


def _collect_health():
    root = _state["root"]
    pg_tcp_ok, pg_tcp_ms, pg_tcp_msg = _tcp_up("127.0.0.1", PG_PORT)
    api_tcp_ok, api_tcp_ms, api_tcp_msg = _tcp_up("127.0.0.1", _state["port"])
    pg_wire_ok, pg_wire_ms, pg_wire_msg = _pg_protocol_ping()
    disk = _disk_usage(root)
    wal = _wal_state(root)

    checks = {
        "postgres_tcp": {
            "ok": pg_tcp_ok,
            "latency_ms": pg_tcp_ms,
            "detail": pg_tcp_msg,
        },
        "postgres_wire": {
            "ok": pg_wire_ok,
            "latency_ms": pg_wire_ms,
            "detail": pg_wire_msg,
        },
        "nas_api_tcp": {
            "ok": api_tcp_ok,
            "latency_ms": api_tcp_ms,
            "detail": api_tcp_msg,
        },
        "nas_dir_writable": {
            "ok": bool(root) and os.access(root, os.W_OK),
            "detail": root,
        },
        "nas_disk": disk,
        "wal_archive": wal,
    }

    overall_ok = all(v.get("ok") is True for v in checks.values())
    payload = {
        "status": "ok" if overall_ok else "degraded",
        "overall_ok": overall_ok,
        "postgres": _pg_up(),
        "nas_dir": root,
        "nas_writable": bool(root) and os.access(root, os.W_OK),
        "uptime_s": _uptime_s(),
        "checks": checks,
        "ts": int(time.time()),
        "version": "health-v2",
    }

    with _state["lock"]:
        _state["health"] = payload
        _state["history"].append({
            "ts": payload["ts"],
            "overall_ok": overall_ok,
            "postgres_tcp": pg_tcp_ok,
            "postgres_wire": pg_wire_ok,
            "nas_api_tcp": api_tcp_ok,
        })
        if len(_state["history"]) > 120:
            _state["history"] = _state["history"][-120:]
    return payload


def _health_snapshot():
    with _state["lock"]:
        if _state["health"]:
            return _state["health"]
    return _collect_health()


def _health_loop():
    while _state["server"] is not None:
        try:
            _collect_health()
        except Exception:
            pass
        time.sleep(10)


def _dashboard_html():
    return b"""<!doctype html>
<html lang='en'>
<head>
  <meta charset='utf-8'/>
  <meta name='viewport' content='width=device-width, initial-scale=1'/>
  <title>C67 NAS Dashboard</title>
  <style>
    body { font-family: Arial, sans-serif; margin: 16px; background: #f5f7fb; color: #111827; }
    .card { background: #fff; border: 1px solid #e5e7eb; border-radius: 10px; padding: 12px; margin-bottom: 12px; }
    .ok { color: #166534; font-weight: 700; }
    .bad { color: #991b1b; font-weight: 700; }
    table { width: 100%; border-collapse: collapse; }
    th, td { border-bottom: 1px solid #e5e7eb; text-align: left; padding: 6px; font-size: 13px; }
    .mono { font-family: Consolas, monospace; font-size: 12px; }
  </style>
</head>
<body>
  <div class='card'>
    <h2 style='margin:0 0 8px 0'>C67 DB+NAS Health</h2>
    <div>Overall: <span id='ov' class='bad'>loading...</span></div>
    <div class='mono'>Uptime: <span id='up'>-</span>s</div>
    <div class='mono'>NAS dir: <span id='dir'>-</span></div>
  </div>
  <div class='card'>
    <h3 style='margin:0 0 8px 0'>Checks</h3>
    <table><thead><tr><th>Check</th><th>Status</th><th>Detail</th></tr></thead><tbody id='checks'></tbody></table>
  </div>
  <script>
    async function tick(){
      try {
        const r = await fetch('/api/health');
        const j = await r.json();
        const ov = document.getElementById('ov');
        ov.textContent = j.overall_ok ? 'HEALTHY' : 'DEGRADED';
        ov.className = j.overall_ok ? 'ok' : 'bad';
        document.getElementById('up').textContent = j.uptime_s;
        document.getElementById('dir').textContent = j.nas_dir || '-';

        const tbody = document.getElementById('checks');
        tbody.innerHTML = '';
        for (const [k, v] of Object.entries(j.checks || {})) {
          const tr = document.createElement('tr');
          const detail = (v && (v.detail || v.error || JSON.stringify(v))) || '';
          tr.innerHTML = `<td class='mono'>${k}</td><td class='${v.ok ? 'ok':'bad'}'>${v.ok ? 'OK':'FAIL'}</td><td class='mono'>${detail}</td>`;
          tbody.appendChild(tr);
        }
      } catch (e) {}
    }
    setInterval(tick, 5000);
    tick();
  </script>
</body>
</html>
"""


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
        if path == "/":
            if not self._gate(False):
                return
            self._send(200, raw=_dashboard_html(), ctype="text/html; charset=utf-8")
        elif path == "/api/health":
            if not self._gate(False):
                return
            self._send(200, _health_snapshot())
        elif path == "/api/metrics":
            if not self._gate(False):
                return
            with _state["lock"]:
                hist = list(_state["history"][-60:])
            self._send(200, {"history": hist, "count": len(hist)})
        elif path == "/api/checks/run":
            if not self._gate(True):
                return
            self._send(200, _collect_health())
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
    do_POST = _upload

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
    _collect_health()
    threading.Thread(target=_health_loop, name="c67-health", daemon=True).start()
    return "NAS API listening on 0.0.0.0:{} root={}".format(port, nas_dir)
