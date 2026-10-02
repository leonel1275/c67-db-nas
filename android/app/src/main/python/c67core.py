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
import logging
import errno
from logging.handlers import RotatingFileHandler
import os
import shutil
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

PG_PORT = 5432
CHUNK = 1 << 20
DEFAULT_MAX_UPLOAD_BYTES = 1 << 30
MIN_FREE_BYTES = 64 << 20
LOG_MAX_BYTES = 512 << 10
LOG_BACKUP_COUNT = 3
_WEB_LOGIN = b"""<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>C67 NAS</title><style>body{font:16px system-ui;max-width:32rem;margin:12vh auto;padding:1rem;background:#f3f5f8;color:#17202a}main{background:white;padding:2rem;border-radius:1rem;box-shadow:0 4px 24px #0001}input,button{font:inherit;padding:.7rem;width:100%;box-sizing:border-box;margin:.4rem 0}button{cursor:pointer}#err{color:#a00}</style><main><h1>C67 NAS</h1><p>Ingresa el token NAS que muestra la aplicación Android.</p><form id="login"><input id="token" type="password" autocomplete="current-password" required placeholder="Token NAS"><button>Entrar</button></form><p id="err" role="alert"></p></main><script>document.querySelector('#login').onsubmit=async e=>{e.preventDefault();let r=await fetch('/ui/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:document.querySelector('#token').value})});if(r.ok)location.replace('/');else document.querySelector('#err').textContent='Token no válido o acceso no permitido.'}</script></html>"""
_WEB_APP = b"""<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>C67 NAS</title><style>body{font:16px system-ui;max-width:64rem;margin:auto;padding:1rem;background:#f3f5f8;color:#17202a}header,.toolbar,.item{display:flex;gap:.6rem;align-items:center;justify-content:space-between;flex-wrap:wrap}button{cursor:pointer;padding:.55rem .8rem}main{background:white;padding:1rem;border-radius:1rem}#items{list-style:none;padding:0}.item{border-bottom:1px solid #ddd;padding:.6rem 0}.name{overflow-wrap:anywhere;flex:1}#msg{white-space:pre-wrap;color:#8a2d00}.item button{margin-left:.25rem}</style><header><h1>C67 NAS</h1><button id="logout">Cerrar sesión</button></header><main><p id="where"></p><div class="toolbar"><button id="up">↑ Subir</button><button id="mkdir">Nueva carpeta</button><button id="refresh">Actualizar</button><input id="files" type="file" multiple hidden></div><p id="msg" role="status"></p><ul id="items"></ul></main><script>
let path='',maxUpload=1073741824;const $=s=>document.querySelector(s), enc=s=>encodeURIComponent(s), msg=s=>$('#msg').textContent=s;
async function api(method,url,body){let r=await fetch('/api/nas/'+url,{method,body});let data;if(r.status===204)data={};else{try{data=await r.json()}catch(_){data={}}}if(!r.ok)throw Error(data.error?.message||data.error||('HTTP '+r.status));return data}
async function list(){msg('Cargando…');$('#where').textContent='/'+path;try{let d=await api('GET','list?path='+enc(path));$('#items').replaceChildren();for(let x of d.items){let li=document.createElement('li');li.className='item';let n=document.createElement('span');n.className='name';n.textContent=(x.is_dir?'📁 ':'📄 ')+x.name+(x.is_dir?'':' · '+x.size+' bytes');if(x.is_dir)n.onclick=()=>{path=path?path+'/'+x.name:x.name;list()};li.append(n);if(!x.is_dir){let b=document.createElement('button');b.textContent='Descargar';b.onclick=()=>download(x.name);li.append(b)}let del=document.createElement('button');del.textContent='Borrar';del.onclick=()=>remove(x.name,x.is_dir);li.append(del);$('#items').append(li)}if(path){let li=document.createElement('li'),b=document.createElement('button');b.textContent='← Carpeta superior';b.onclick=()=>{path=path.includes('/')?path.slice(0,path.lastIndexOf('/')):'';list()};li.append(b);$('#items').prepend(li)}msg(d.items.length?'':'Carpeta vacía')}catch(e){msg(e.message)}}
async function download(name){try{msg('Descargando…');let r=await fetch('/api/nas/download?path='+enc(path?path+'/'+name:name));if(!r.ok){let x=await r.json();throw Error(x.error?.message||x.error||('HTTP '+r.status))}let a=document.createElement('a'),u=URL.createObjectURL(await r.blob());a.href=u;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(u),1000);msg('Descarga iniciada')}catch(e){msg(e.message)}}
async function remove(name,dir){if(!confirm('¿Eliminar '+(dir?'la carpeta vacía ':'el archivo ')+name+'?'))return;try{await api('DELETE','delete?path='+enc(path?path+'/'+name:name));await list()}catch(e){msg(e.message)}}
$('#refresh').onclick=list;$('#up').onclick=()=>$('#files').click();$('#mkdir').onclick=async()=>{let name=prompt('Nombre de la carpeta');if(!name)return;if(name.includes('/')||name==='.'||name==='..'){msg('Nombre no válido');return}try{await api('POST','mkdir?path='+enc(path?path+'/'+name:name));list()}catch(e){msg(e.message)}};
$('#files').onchange=async e=>{for(let f of e.target.files){try{if(f.size>maxUpload)throw Error('El archivo supera el límite configurado de '+maxUpload+' bytes');msg('Subiendo '+f.name+'…');await api('PUT','upload?path='+enc(path?path+'/'+f.name:f.name),f)}catch(x){msg(x.message);break}}e.target.value='';list()};
$('#logout').onclick=async()=>{await fetch('/ui/logout',{method:'POST'});location.replace('/')};fetch('/api/health').then(r=>r.json()).then(x=>maxUpload=x.max_upload_bytes||maxUpload);list();
</script></html>"""

ALLOWED_NETS = [
    ipaddress.ip_network(n)
    for n in (
        "127.0.0.0/8", "::1/128",
        "100.64.0.0/10", "fd7a:115c:a1e0::/48",
        "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
    )
]

_state = {
    "root": None, "files_dir": None, "token": "", "server": None, "port": 0,
    "started": 0.0, "max_upload_bytes": DEFAULT_MAX_UPLOAD_BYTES,
    "min_free_bytes": MIN_FREE_BYTES, "logger": None,
}
_mutation_lock = threading.Lock()
_logger_lock = threading.Lock()


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


def _configure_logging(files_dir):
    os.makedirs(files_dir, exist_ok=True)
    logger = logging.getLogger("c67.nas")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    with _logger_lock:
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
        handler = RotatingFileHandler(
            os.path.join(files_dir, "nas-api.log"),
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    _state["logger"] = logger


def _log(event, **fields):
    logger = _state.get("logger")
    if logger is not None:
        details = " ".join("{}={}".format(k, v) for k, v in fields.items())
        logger.info("%s%s", event, " " + details if details else "")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "c67-nas/2"

    def log_message(self, fmt, *args):
        pass

    # ---- helpers ----
    def _send(self, code, obj=None, raw=None, ctype="application/json", extra=None):
        body = raw if raw is not None else json.dumps(obj if obj is not None else {}).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                _log("response_disconnected", status=code)

    def _err(self, code, msg, error_code=None):
        self.close_connection = True  # puede haber cuerpo sin leer
        if error_code is None:
            error_code = {
                400: "bad_request", 401: "unauthorized", 403: "forbidden",
                404: "not_found", 409: "conflict", 411: "length_required",
                413: "file_too_large", 500: "internal_error", 507: "insufficient_storage",
            }.get(code, "request_failed")
        _log("request_error", status=code, error=error_code)
        self._send(code, {"error": {"code": error_code, "message": msg}})

    def _cookie_token(self):
        for item in self.headers.get("Cookie", "").split(";"):
            name, sep, value = item.strip().partition("=")
            if sep and name == "c67_session":
                return value
        return ""

    def _gate(self, need_token):
        if not _client_allowed(self.client_address[0]):
            self._err(403, "client not allowed")
            return False
        if need_token:
            got = ""
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                got = auth[7:].strip()
            got = got or self.headers.get("X-Token", "") or self._cookie_token()
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
            cookie = self._cookie_token()
            authenticated = bool(_state["token"]) and hmac.compare_digest(
                cookie.encode(), _state["token"].encode()
            )
            page = _WEB_APP if authenticated else _WEB_LOGIN
            self._send(200, raw=page, ctype="text/html; charset=utf-8", extra={
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'self' blob:; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            })
        elif path == "/api/health":
            if not self._gate(False):
                return
            root = _state["root"]
            self._send(200, {
                "status": "ok",
                "postgres": _pg_up(),
                "nas_dir": root,
                "nas_writable": bool(root) and os.access(root, os.W_OK),
                "uptime_s": int(time.time() - _state["started"]),
                "max_upload_bytes": _state["max_upload_bytes"],
            })
        elif path == "/api/nas/list":
            if not self._gate(True):
                return
            target = self._resolve(q.get("path", ""))
            if target is None:
                return self._err(400, "invalid path", "invalid_path")
            if not os.path.isdir(target):
                return self._err(404, "directory not found", "not_found")
            try:
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
            except OSError as exc:
                _log("list_failed", errno=exc.errno)
                return self._err(500, "could not list directory", "storage_error")
            rel = os.path.relpath(target, os.path.realpath(_state["root"]))
            self._send(200, {"path": "" if rel == "." else rel, "items": items})
        elif path == "/api/nas/download":
            if not self._gate(True):
                return
            target = self._resolve(q.get("path", ""))
            if target is None:
                return self._err(400, "invalid path", "invalid_path")
            if not os.path.isfile(target):
                return self._err(404, "file not found", "not_found")
            size = os.path.getsize(target)
            _log("download_started", size=size)
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Disposition",
                "attachment; filename*=UTF-8''" + quote(os.path.basename(target)),
            )
            self.end_headers()
            try:
                with open(target, "rb") as f:
                    shutil.copyfileobj(f, self.wfile, CHUNK)
                _log("download_completed", size=size)
            except (BrokenPipeError, ConnectionResetError):
                _log("download_disconnected", size=size)
            except OSError as exc:
                _log("download_failed", errno=exc.errno, size=size)
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
            return self._err(400, "invalid path", "invalid_path")
        if self.headers.get("Transfer-Encoding"):
            return self._err(400, "chunked requests are not supported", "invalid_transfer_encoding")
        lengths = self.headers.get_all("Content-Length", [])
        if not lengths:
            return self._err(411, "Content-Length required", "length_required")
        if len(lengths) != 1:
            return self._err(400, "exactly one Content-Length is required", "invalid_content_length")
        length = lengths[0]
        if not length.isascii() or not length.isdigit() or len(length) > 20:
            return self._err(400, "invalid Content-Length", "invalid_content_length")
        try:
            length = int(length)
        except ValueError:
            return self._err(400, "invalid Content-Length", "invalid_content_length")
        if length > _state["max_upload_bytes"]:
            return self._err(413, "upload exceeds the configured size limit", "file_too_large")

        parent = os.path.dirname(target)
        tmp = None
        with _mutation_lock:
            try:
                os.makedirs(parent, exist_ok=True)
                if not os.path.isdir(parent):
                    return self._err(409, "parent path is not a directory", "invalid_path")
                if os.path.isdir(target):
                    return self._err(409, "destination is a directory", "destination_is_directory")
                if q.get("overwrite") == "0" and os.path.exists(target):
                    return self._err(409, "destination already exists", "file_exists")
                free = shutil.disk_usage(parent).free
                if free < length + _state["min_free_bytes"]:
                    return self._err(507, "insufficient free space for upload", "insufficient_storage")
                fd, tmp = tempfile.mkstemp(prefix=".c67-upload-", dir=parent)
                remaining = length
                _log("upload_started", size=length, overwrite=int(q.get("overwrite") != "0"))
                try:
                    with os.fdopen(fd, "wb") as output:
                        while remaining:
                            chunk = self.rfile.read(min(CHUNK, remaining))
                            if not chunk:
                                raise EOFError("incomplete upload body")
                            output.write(chunk)
                            remaining -= len(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                except (BrokenPipeError, ConnectionResetError, EOFError) as exc:
                    _log("upload_failed", status=400, error="incomplete_body", size=length)
                    return self._err(400, "upload body ended before Content-Length", "incomplete_upload")
                except OSError as exc:
                    if exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)):
                        return self._err(507, "storage ran out of space during upload", "insufficient_storage")
                    _log("upload_failed", status=500, errno=exc.errno, size=length)
                    return self._err(500, "could not write uploaded file", "storage_error")
                os.replace(tmp, target)
                tmp = None
                _log("upload_completed", status=201, size=length)
            except OSError as exc:
                if exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)):
                    return self._err(507, "insufficient free space for upload", "insufficient_storage")
                _log("upload_failed", status=500, errno=exc.errno, size=length)
                return self._err(500, "could not store uploaded file", "storage_error")
            finally:
                if tmp is not None:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
        self._send(201, {"path": rel, "size": length})

    do_PUT = _upload

    def do_POST(self):
        path, q = self._query()
        if path == "/ui/login":
            return self._ui_login()
        if path == "/ui/logout":
            if not self._gate(True):
                return
            return self._send(200, {"ok": True}, extra={
                "Set-Cookie": "c67_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0",
            })
        if path != "/api/nas/mkdir":
            return self._upload()
        if not self._gate(True):
            return
        rel = q.get("path", "")
        target = self._resolve(rel)
        if not rel or target is None or target == os.path.realpath(_state["root"]):
            return self._err(400, "invalid path")
        with _mutation_lock:
            try:
                os.mkdir(target)
            except FileExistsError:
                return self._err(409, "already exists", "already_exists")
            except FileNotFoundError:
                return self._err(404, "parent directory not found", "parent_not_found")
            except OSError as exc:
                _log("mkdir_failed", errno=exc.errno)
                return self._err(500, "could not create directory", "storage_error")
        _log("mkdir_completed")
        self._send(201, {"path": rel})

    def _ui_login(self):
        if not self._gate(False):
            return
        length = self.headers.get("Content-Length", "")
        if not length.isascii() or not length.isdigit() or len(length) > 4 or int(length) > 4096:
            return self._err(400, "invalid login request", "invalid_login")
        try:
            payload = json.loads(self.rfile.read(int(length)))
            token = payload.get("token", "") if isinstance(payload, dict) else ""
        except (ValueError, TypeError):
            return self._err(400, "invalid login request", "invalid_login")
        if not isinstance(token, str) or not _state["token"] or not hmac.compare_digest(
            token.encode(), _state["token"].encode()
        ):
            _log("ui_login_failed")
            return self._err(401, "invalid token", "unauthorized")
        _log("ui_login_completed")
        self._send(200, {"ok": True}, extra={
            "Set-Cookie": "c67_session={}; HttpOnly; SameSite=Strict; Path=/; Max-Age=604800".format(token),
        })

    def do_DELETE(self):
        path, q = self._query()
        if path != "/api/nas/delete":
            return self._err(404, "unknown endpoint")
        if not self._gate(True):
            return
        target = self._resolve(q.get("path", ""))
        if target is None or target == os.path.realpath(_state["root"]):
            return self._err(400, "invalid path", "invalid_path")
        with _mutation_lock:
            try:
                if os.path.isdir(target):
                    os.rmdir(target)
                else:
                    os.unlink(target)
            except FileNotFoundError:
                return self._err(404, "not found", "not_found")
            except OSError as exc:
                if exc.errno in (errno.ENOTEMPTY, errno.EEXIST):
                    return self._err(409, "directory is not empty", "directory_not_empty")
                if exc.errno == errno.EACCES:
                    return self._err(403, "permission denied", "storage_permission_denied")
                _log("delete_failed", errno=exc.errno)
                return self._err(500, "could not delete item", "storage_error")
        _log("delete_completed")
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
    try:
        max_upload = int(os.environ.get("C67_MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES))
        min_free = int(os.environ.get("C67_MIN_FREE_BYTES", MIN_FREE_BYTES))
    except ValueError as exc:
        raise ValueError("C67_MAX_UPLOAD_BYTES and C67_MIN_FREE_BYTES must be integers") from exc
    if max_upload <= 0 or min_free < 0:
        raise ValueError("upload limit must be positive and free-space reserve nonnegative")
    _configure_logging(files_dir)
    _state.update(
        root=str(nas_dir), files_dir=str(files_dir), token=str(token), started=time.time(),
        port=int(port), max_upload_bytes=max_upload, min_free_bytes=min_free,
    )
    server = _Server(("0.0.0.0", int(port)), Handler)
    _state["server"] = server
    threading.Thread(target=server.serve_forever, name="c67-nas", daemon=True).start()
    _log("server_started", port=int(port), max_upload_bytes=max_upload)
    return "NAS API listening on 0.0.0.0:{} root={}".format(port, nas_dir)
