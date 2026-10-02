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
import errno
import logging
from logging.handlers import RotatingFileHandler
import os
import shutil
import secrets
import socket
import tempfile
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlsplit

PG_PORT = 5432
CHUNK = 1 << 20
DEFAULT_MAX_UPLOAD_BYTES = 1 << 30
MIN_FREE_BYTES = 64 << 20
UPLOAD_IDLE_TIMEOUT_SECONDS = 60
LIST_DEFAULT_LIMIT = 100
LIST_MAX_LIMIT = 500
TRASH_DIRNAME = ".c67-trash"
TRASH_RETENTION_SECONDS = 30 * 24 * 60 * 60
UI_SESSION_TTL_SECONDS = 7 * 24 * 60 * 60
UI_SESSION_LIMIT = 64
LOG_MAX_BYTES = 512 << 10
LOG_BACKUP_COUNT = 3
_WEB_LOGIN = """<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>C67 NAS</title><style>body{font:16px system-ui;max-width:32rem;margin:12vh auto;padding:1rem;background:#f3f5f8;color:#17202a}main{background:white;padding:2rem;border-radius:1rem;box-shadow:0 4px 24px #0001}input,button{font:inherit;padding:.7rem;width:100%;box-sizing:border-box;margin:.4rem 0}button{cursor:pointer}#err{color:#a00}</style><main><h1>C67 NAS</h1><p>Ingresa el token NAS que muestra la aplicación Android.</p><form id="login"><input id="token" type="password" autocomplete="current-password" required placeholder="Token NAS"><button>Entrar</button></form><p id="err" role="alert"></p></main><script>document.querySelector('#login').onsubmit=async e=>{e.preventDefault();let r=await fetch('/ui/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:document.querySelector('#token').value})});if(r.ok)location.replace('/');else document.querySelector('#err').textContent='Token no válido o acceso no permitido.'}</script></html>"""
_WEB_APP = """<!doctype html><html lang="es"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>C67 NAS</title><style>
body{font:16px system-ui;max-width:70rem;margin:auto;padding:1rem;background:#f3f5f8;color:#17202a}header,.toolbar,.item,.transfer{display:flex;gap:.6rem;align-items:center;justify-content:space-between;flex-wrap:wrap}button,select,input{font:inherit;padding:.55rem}button{cursor:pointer}main{background:white;padding:1rem;border-radius:1rem;margin:.7rem 0}#items,#queue{list-style:none;padding:0}.item,.transfer{border-bottom:1px solid #ddd;padding:.65rem 0}.name{overflow-wrap:anywhere;flex:1}.item button{margin-left:.25rem}.name.dir{cursor:pointer;color:#075d9b}#msg{white-space:pre-wrap;color:#8a2d00}progress{width:min(20rem,55vw)}#drop{padding:1.2rem;border:2px dashed #8aa;border-radius:.7rem;text-align:center}#drop.over{background:#e3f3ff}
</style><header><h1>C67 NAS</h1><button id="logout">Cerrar sesión</button></header>
<main><p id="where"></p><div class="toolbar"><button id="up">Subir archivos</button><button id="mkdir">Nueva carpeta</button><button id="refresh">Actualizar</button><button id="trash">Papelera</button><button id="emptyTrash" hidden>Vaciar papelera</button><select id="sort"><option value="name">Nombre</option><option value="size">Tamaño</option><option value="mtime">Fecha</option></select><select id="direction"><option value="asc">Ascendente</option><option value="desc">Descendente</option></select><input id="search" type="search" placeholder="Buscar en esta carpeta"><input id="files" type="file" multiple hidden></div>
<div id="drop" tabindex="0">Arrastra archivos aquí para subirlos. Máximo por archivo: <span id="limit"></span>. Espacio libre aprox.: <span id="free"></span>.</div><p>El espacio disponible puede cambiar durante la subida.</p><p id="msg" role="status"></p><ul id="items"></ul><button id="more" hidden>Mostrar más</button></main>
<main><h2>Transferencias</h2><ul id="queue"></ul></main><script>
let path='',maxUpload=1073741824,page=0,hasMore=false,inTrash=false,queueId=0,listing=[],searchTimer;
const $=s=>document.querySelector(s),enc=s=>encodeURIComponent(s),msg=s=>$('#msg').textContent=s;
const size=n=>n<1024?`${n} B`:n<1048576?`${(n/1024).toFixed(1)} KiB`:n<1073741824?`${(n/1048576).toFixed(1)} MiB`:`${(n/1073741824).toFixed(2)} GiB`;
async function api(method,url,body){let r=await fetch('/api/nas/'+url,{method,body});let d={};try{d=await r.json()}catch(_){}if(!r.ok)throw Error(d.error?.message||d.error||('HTTP '+r.status));return d}
function query(){return `path=${enc(path)}&limit=100&offset=${page*100}&sort=${$('#sort').value}&direction=${$('#direction').value}&search=${enc($('#search').value)}`}
async function list(reset=true){if(reset){page=0;listing=[]}inTrash=false;$('#emptyTrash').hidden=true;msg('Cargando…');renderCrumbs();try{let d=await api('GET','list?'+query());listing=reset?d.items:listing.concat(d.items);hasMore=d.has_more;render();$('#more').hidden=!hasMore;msg(listing.length?'':'Carpeta vacía')}catch(e){msg(e.message)}}
function renderCrumbs(){let el=$('#where');el.replaceChildren();let root=document.createElement('button');root.textContent='NAS';root.onclick=()=>{path='';list()};el.append(root);let sofar='';for(let part of path.split('/').filter(Boolean)){sofar=sofar?sofar+'/'+part:part;let current=sofar,b=document.createElement('button');b.textContent=' / '+part;b.onclick=()=>{path=current;list()};el.append(b)}}
function render(){let ul=$('#items');ul.replaceChildren();for(let x of listing){let li=document.createElement('li');li.className='item';let n=document.createElement('span');n.className='name '+(x.is_dir?'dir':'');n.textContent=(x.is_dir?'📁 ':'📄 ')+x.name+(x.is_dir?'':` · ${size(x.size)}`);if(x.is_dir)n.onclick=()=>{path=path?path+'/'+x.name:x.name;list()};li.append(n);if(!x.is_dir){let b=document.createElement('button');b.textContent='Descargar';b.onclick=()=>download(x.name);li.append(b)}let del=document.createElement('button');del.textContent='Papelera';del.setAttribute('aria-label','Mover a papelera: '+x.name);del.onclick=()=>remove(x.name);li.append(del);ul.append(li)}if(path){let li=document.createElement('li'),b=document.createElement('button');b.textContent='← Carpeta superior';b.onclick=()=>{path=path.includes('/')?path.slice(0,path.lastIndexOf('/')):'';list()};ul.prepend(li);li.append(b)}}
function download(name){let a=document.createElement('a');a.href='/api/nas/download?path='+enc(path?path+'/'+name:name);a.download=name;a.click();msg('Descarga iniciada: '+name)}
function xhrUpload(file,retry=false){return new Promise((resolve,reject)=>{let id=++queueId,li=document.createElement('li');li.className='transfer';let label=document.createElement('span');label.textContent=file.name;let progress=document.createElement('progress');progress.max=100;progress.value=0;let state=document.createElement('span');state.textContent='Preparando';let cancel=document.createElement('button');cancel.textContent='Cancelar';let again=document.createElement('button');again.textContent='Reintentar';again.hidden=true;li.append(label,progress,state,cancel,again);$('#queue').prepend(li);let xhr=new XMLHttpRequest(),dest=path?path+'/'+file.name:file.name,cancelled=false;
const start=overwrite=>{if(file.size>maxUpload){state.textContent='Excede límite '+size(maxUpload);cancel.hidden=true;reject(Error(state.textContent));return}state.textContent=retry?'Reintentando':'Subiendo';cancel.hidden=false;again.hidden=true;xhr=new XMLHttpRequest();xhr.open('PUT','/api/nas/upload?path='+enc(dest)+'&overwrite='+(overwrite?'1':'0'));xhr.withCredentials=true;xhr.upload.onprogress=e=>{if(e.lengthComputable){progress.value=e.loaded/e.total*100;state.textContent=`${size(e.loaded)} / ${size(e.total)} (${Math.round(progress.value)}%)`}};xhr.onload=()=>{cancel.hidden=true;if(xhr.status>=200&&xhr.status<300){state.textContent='Completado';progress.value=100;resolve()}else{let d={};try{d=JSON.parse(xhr.responseText)}catch(_){}let error=d.error?.message||'HTTP '+xhr.status;if(xhr.status===409){state.textContent='Ya existe';if(confirm(file.name+' ya existe. ¿Reemplazarlo?'))start(true);else if(confirm('¿Guardar como otro nombre?')){let name=prompt('Nuevo nombre',file.name);if(name){dest=path?path+'/'+name:name;start(false)}else{state.textContent='Cancelado';resolve()}}else{state.textContent='Cancelado';resolve()}}else{state.textContent=error;again.hidden=false;reject(Error(error))}}};xhr.onerror=()=>failed('Conexión interrumpida');xhr.onabort=()=>{state.textContent=cancelled?'Cancelado':'Cancelado';cancel.hidden=true;if(!cancelled)again.hidden=false;cancelled=false;resolve()};xhr.send(file)};
function failed(text){state.textContent=text;cancel.hidden=true;again.hidden=false;reject(Error(text))}
cancel.onclick=()=>{cancelled=true;xhr.abort()};again.onclick=()=>{retry=true;start(false)};start(false)})}
async function uploadFiles(files){for(const f of files){try{await xhrUpload(f)}catch(e){msg(f.name+': '+e.message)}}await list()}
async function remove(name){if(!confirm('Mover '+name+' a la papelera? Se podrá restaurar durante 30 días.'))return;try{await api('POST','trash?path='+enc(path?path+'/'+name:name));await list()}catch(e){msg(e.message)}}
async function showTrash(){inTrash=true;path='';$('#where').textContent='Papelera (retención 30 días)';$('#items').replaceChildren();$('#emptyTrash').hidden=false;$('#more').hidden=true;try{let d=await api('GET','trash');$('#where').textContent=`Papelera (retención ${d.retention_days} días) · espacio usado aprox.: ${size(d.total_bytes)}`;for(let x of d.items){let li=document.createElement('li');li.className='item';let n=document.createElement('span');n.className='name';n.textContent=(x.is_dir?'📁 ':'📄 ')+x.name+' — '+x.original_path+' — '+size(x.size)+' — eliminado '+new Date(x.deleted_at*1000).toLocaleString();li.append(n);let r=document.createElement('button');r.textContent='Restaurar';r.onclick=async()=>{try{await api('POST','trash/restore?id='+enc(x.id));showTrash()}catch(e){msg(e.message)}};li.append(r);$('#items').append(li)}msg(d.items.length?'':'Papelera vacía')}catch(e){msg(e.message)}}
async function emptyTrash(){if(!confirm('Vaciar la papelera permanentemente?'))return;try{await api('PATCH','trash');showTrash()}catch(e){msg(e.message)}}
$('#refresh').onclick=()=>inTrash?showTrash():list();$('#more').onclick=()=>{page++;list(false)};$('#up').onclick=()=>$('#files').click();$('#files').onchange=e=>uploadFiles([...e.target.files]);$('#trash').onclick=showTrash;$('#emptyTrash').onclick=emptyTrash;
$('#mkdir').onclick=async()=>{let n=prompt('Nombre de la carpeta');if(!n)return;if(n.includes('/')||n==='.'||n==='..'){msg('Nombre no válido');return}try{await api('POST','mkdir?path='+enc(path?path+'/'+n:n));list()}catch(e){msg(e.message)}};
$('#sort').onchange=()=>list();$('#direction').onchange=()=>list();$('#search').oninput=()=>{clearTimeout(searchTimer);searchTimer=setTimeout(()=>list(),250)};
const drop=$('#drop');drop.ondragover=e=>{e.preventDefault();drop.classList.add('over')};drop.ondragleave=()=>drop.classList.remove('over');drop.ondrop=e=>{e.preventDefault();drop.classList.remove('over');uploadFiles([...e.dataTransfer.files])};
$('#logout').onclick=async()=>{await fetch('/ui/logout',{method:'POST',credentials:'same-origin'});location.replace('/')};
fetch('/api/health').then(r=>r.json()).then(x=>{maxUpload=x.max_upload_bytes||maxUpload;$('#limit').textContent=size(maxUpload);$('#free').textContent=size(x.free_bytes||0)});list();
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
    "min_free_bytes": MIN_FREE_BYTES, "logger": None, "sessions": {},
}
_logger_lock = threading.Lock()
_session_lock = threading.Lock()
_path_locks_guard = threading.Lock()
_path_locks = {}
_tree_activity_lock = threading.Lock()
_active_uploads = set()
_moving_paths = set()


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
    logger = logging.getLogger("c67.nas")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    with _logger_lock:
        for handler in logger.handlers[:]:
            logger.removeHandler(handler)
            handler.close()
        try:
            os.makedirs(files_dir, exist_ok=True)
            handler = RotatingFileHandler(
                os.path.join(files_dir, "nas-api.log"),
                maxBytes=LOG_MAX_BYTES,
                backupCount=LOG_BACKUP_COUNT,
                encoding="utf-8",
            )
        except OSError:
            handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    _state["logger"] = logger


def _log(event, **fields):
    logger = _state.get("logger")
    if logger is not None:
        details = " ".join("{}={}".format(k, v) for k, v in fields.items())
        logger.info("%s%s", event, " " + details if details else "")


@contextmanager
def _path_lock(path):
    key = os.path.abspath(path)
    with _path_locks_guard:
        entry = _path_locks.get(key)
        if entry is None:
            entry = [threading.Lock(), 0]
            _path_locks[key] = entry
        entry[1] += 1
    entry[0].acquire()
    try:
        yield
    finally:
        entry[0].release()
        with _path_locks_guard:
            entry[1] -= 1
            if entry[1] == 0:
                _path_locks.pop(key, None)


def _remove_tree(path):
    if os.path.isdir(path) and not os.path.islink(path):
        for name in os.listdir(path):
            _remove_tree(os.path.join(path, name))
        os.rmdir(path)
    else:
        os.unlink(path)


def _path_size(path):
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for current, dirs, files in os.walk(path, followlinks=False):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(current, d))]
        for name in files:
            try:
                total += os.path.getsize(os.path.join(current, name))
            except OSError:
                continue
    return total


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
                404: "not_found", 408: "request_timeout", 409: "conflict", 411: "length_required",
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

    def _session_authenticated(self, session_id):
        if not session_id:
            return False
        now = time.time()
        with _session_lock:
            expired = [key for key, expiry in _state["sessions"].items() if expiry <= now]
            for key in expired:
                _state["sessions"].pop(key, None)
            return _state["sessions"].get(session_id, 0) > now

    def _new_session(self):
        now = time.time()
        session_id = secrets.token_urlsafe(32)
        with _session_lock:
            expired = [key for key, expiry in _state["sessions"].items() if expiry <= now]
            for key in expired:
                _state["sessions"].pop(key, None)
            if len(_state["sessions"]) >= UI_SESSION_LIMIT:
                oldest = min(_state["sessions"], key=_state["sessions"].get)
                _state["sessions"].pop(oldest, None)
            _state["sessions"][session_id] = now + UI_SESSION_TTL_SECONDS
        return session_id

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
            token_valid = bool(_state["token"]) and hmac.compare_digest(
                got.encode(), _state["token"].encode()
            )
            if not token_valid and not self._session_authenticated(self._cookie_token()):
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
        if any(part == TRASH_DIRNAME for part in rel.replace("\\", "/").split("/")):
            return None
        root = os.path.realpath(_state["root"])
        target = os.path.realpath(os.path.join(root, rel.lstrip("/")))
        if target == root or target.startswith(root + os.sep):
            return target
        return None

    def _trash_root(self):
        return os.path.join(os.path.realpath(_state["root"]), TRASH_DIRNAME)

    def _purge_expired_trash(self):
        root = self._trash_root()
        if not os.path.isdir(root):
            return
        cutoff = time.time() - TRASH_RETENTION_SECONDS
        with os.scandir(root) as entries:
            for entry in entries:
                if not entry.is_dir(follow_symlinks=False):
                    continue
                try:
                    with open(os.path.join(entry.path, "metadata.json"), encoding="utf-8") as f:
                        meta = json.load(f)
                    if float(meta["deleted_at"]) < cutoff:
                        with _path_lock(entry.path):
                            _remove_tree(entry.path)
                        _log("trash_expired")
                except (OSError, ValueError, KeyError, TypeError):
                    continue

    def _trash_items(self):
        self._purge_expired_trash()
        root = self._trash_root()
        items = []
        if os.path.isdir(root):
            with os.scandir(root) as entries:
                for entry in entries:
                    if not entry.is_dir(follow_symlinks=False):
                        continue
                    try:
                        with open(os.path.join(entry.path, "metadata.json"), encoding="utf-8") as f:
                            meta = json.load(f)
                        meta["id"] = entry.name
                        items.append(meta)
                    except (OSError, ValueError):
                        continue
        items.sort(key=lambda x: x.get("deleted_at", 0), reverse=True)
        return items

    def _move_to_trash(self, rel):
        self._purge_expired_trash()
        target = self._resolve(rel)
        if not rel or target is None or target == os.path.realpath(_state["root"]):
            return self._err(400, "invalid path", "invalid_path")
        root = os.path.realpath(_state["root"])
        original = os.path.relpath(target, root)
        trash = self._trash_root()
        with _path_lock(target):
            if not os.path.lexists(target):
                return self._err(404, "item not found", "not_found")
            with _tree_activity_lock:
                if any(p == target or p.startswith(target + os.sep) for p in _active_uploads):
                    return self._err(409, "item has an active upload", "operation_in_progress")
                _moving_paths.add(target)
            entry_id = secrets.token_hex(16)
            entry = os.path.join(trash, entry_id)
            payload = os.path.join(entry, "payload")
            try:
                os.makedirs(entry)
                meta = {
                    "name": os.path.basename(target), "original_path": original,
                    "deleted_at": time.time(), "is_dir": os.path.isdir(target),
                    "size": _path_size(target),
                }
                os.replace(target, payload)
                try:
                    with open(os.path.join(entry, "metadata.json"), "w", encoding="utf-8") as f:
                        json.dump(meta, f, ensure_ascii=False)
                        f.flush()
                        os.fsync(f.fileno())
                except OSError:
                    os.replace(payload, target)
                    raise
            except OSError as exc:
                if os.path.isdir(entry) and not os.listdir(entry):
                    os.rmdir(entry)
                if exc.errno in (errno.EACCES, errno.EPERM):
                    return self._err(403, "permission denied", "storage_permission_denied")
                _log("trash_move_failed", errno=exc.errno)
                return self._err(500, "could not move item to trash", "storage_error")
            finally:
                with _tree_activity_lock:
                    _moving_paths.discard(target)
        _log("trash_item_added")
        self._send(200, {"id": entry_id, "original_path": original})

    def _restore_from_trash(self, entry_id):
        if not entry_id or any(c not in "0123456789abcdef" for c in entry_id):
            return self._err(400, "invalid trash item id", "invalid_id")
        entry = os.path.join(self._trash_root(), entry_id)
        with _path_lock(entry):
            try:
                with open(os.path.join(entry, "metadata.json"), encoding="utf-8") as f:
                    meta = json.load(f)
                target = self._resolve(meta["original_path"])
                payload = os.path.join(entry, "payload")
                if target is None:
                    return self._err(400, "invalid original path", "invalid_path")
                if not os.path.isdir(os.path.dirname(target)):
                    return self._err(404, "original parent directory no longer exists", "parent_not_found")
                with _path_lock(target):
                    if os.path.lexists(target):
                        return self._err(409, "restore destination already exists", "file_exists")
                    os.replace(payload, target)
                _remove_tree(entry)
            except FileNotFoundError:
                return self._err(404, "trash item not found", "not_found")
            except (OSError, ValueError, KeyError, TypeError) as exc:
                _log("trash_restore_failed", error=type(exc).__name__)
                return self._err(500, "could not restore item", "storage_error")
        _log("trash_item_restored")
        self._send(200, {"restored": meta["original_path"]})

    # ---- verbos ----
    def do_GET(self):
        path, q = self._query()
        if path == "/":
            if not self._gate(False):
                return
            authenticated = self._session_authenticated(self._cookie_token())
            page = _WEB_APP if authenticated else _WEB_LOGIN
            self._send(200, raw=page.encode("utf-8"), ctype="text/html; charset=utf-8", extra={
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
                "free_bytes": shutil.disk_usage(root).free if root and os.path.isdir(root) else 0,
            })
        elif path == "/api/nas/trash":
            if not self._gate(True):
                return
            try:
                items = self._trash_items()
                self._send(200, {
                    "items": items,
                    "retention_days": TRASH_RETENTION_SECONDS // 86400,
                    "total_bytes": sum(item.get("size", 0) for item in items),
                })
            except OSError as exc:
                _log("trash_list_failed", errno=exc.errno)
                self._err(500, "could not list trash", "storage_error")
        elif path == "/api/nas/list":
            if not self._gate(True):
                return
            target = self._resolve(q.get("path", ""))
            if target is None:
                return self._err(400, "invalid path", "invalid_path")
            if not os.path.isdir(target):
                return self._err(404, "directory not found", "not_found")
            try:
                limit = int(q.get("limit", LIST_DEFAULT_LIMIT))
                offset = int(q.get("offset", "0"))
            except ValueError:
                return self._err(400, "invalid pagination values", "invalid_pagination")
            if limit < 1 or limit > LIST_MAX_LIMIT or offset < 0:
                return self._err(400, "pagination values out of range", "invalid_pagination")
            sort_by = q.get("sort", "name")
            direction = q.get("direction", "asc")
            if sort_by not in ("name", "size", "mtime") or direction not in ("asc", "desc"):
                return self._err(400, "invalid sort options", "invalid_sort")
            search = q.get("search", "").casefold()
            try:
                items = []
                with os.scandir(target) as it:
                    for e in it:
                        if e.name == TRASH_DIRNAME or (search and search not in e.name.casefold()):
                            continue
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
                items.sort(
                    key=lambda item: item[sort_by].casefold() if sort_by == "name" else item[sort_by],
                    reverse=direction == "desc",
                )
                items.sort(key=lambda item: not item["is_dir"])
            except OSError as exc:
                _log("list_failed", errno=exc.errno)
                return self._err(500, "could not list directory", "storage_error")
            rel = os.path.relpath(target, os.path.realpath(_state["root"]))
            total = len(items)
            page_items = items[offset:offset + limit]
            self._send(200, {
                "path": "" if rel == "." else rel, "items": page_items,
                "offset": offset, "limit": limit, "total": total,
                "has_more": offset + limit < total,
            })
        elif path == "/api/nas/download":
            if not self._gate(True):
                return
            target = self._resolve(q.get("path", ""))
            if target is None:
                return self._err(400, "invalid path", "invalid_path")
            if not os.path.isfile(target):
                return self._err(404, "file not found", "not_found")
            try:
                download = open(target, "rb")
                size = os.fstat(download.fileno()).st_size
            except FileNotFoundError:
                return self._err(404, "file not found", "not_found")
            except OSError as exc:
                _log("download_failed", errno=exc.errno)
                if exc.errno in (errno.EACCES, errno.EPERM):
                    return self._err(403, "permission denied", "storage_permission_denied")
                return self._err(500, "could not read file", "storage_error")
            _log("download_started", size=size)
            try:
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
                with download:
                    shutil.copyfileobj(download, self.wfile, CHUNK)
                _log("download_completed", size=size)
            except (BrokenPipeError, ConnectionResetError):
                _log("download_disconnected", size=size)
            except OSError as exc:
                _log("download_failed", errno=exc.errno, size=size)
            finally:
                download.close()
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
        active_upload = False
        with _path_lock(target):
            try:
                with _tree_activity_lock:
                    if any(target == p or target.startswith(p + os.sep) for p in _moving_paths):
                        return self._err(409, "parent item is being moved", "operation_in_progress")
                    _active_uploads.add(target)
                    active_upload = True
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
                    previous_timeout = self.connection.gettimeout()
                    self.connection.settimeout(UPLOAD_IDLE_TIMEOUT_SECONDS)
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
                    finally:
                        self.connection.settimeout(previous_timeout)
                except socket.timeout:
                    _log("upload_failed", status=408, error="body_timeout", size=length)
                    return self._err(408, "upload body timed out", "upload_timeout")
                except (ConnectionError, EOFError):
                    _log("upload_failed", status=400, error="incomplete_body", size=length)
                    return self._err(400, "upload body ended before Content-Length", "incomplete_upload")
                except OSError as exc:
                    if exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)):
                        return self._err(507, "storage ran out of space during upload", "insufficient_storage")
                    if exc.errno in (errno.EACCES, errno.EPERM):
                        return self._err(403, "permission denied while writing file", "storage_permission_denied")
                    _log("upload_failed", status=500, errno=exc.errno, size=length)
                    return self._err(500, "could not write uploaded file", "storage_error")
                os.replace(tmp, target)
                tmp = None
                _log("upload_completed", status=201, size=length)
            except OSError as exc:
                if exc.errno in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)):
                    return self._err(507, "insufficient free space for upload", "insufficient_storage")
                if exc.errno in (errno.EACCES, errno.EPERM):
                    return self._err(403, "permission denied while storing file", "storage_permission_denied")
                _log("upload_failed", status=500, errno=exc.errno, size=length)
                return self._err(500, "could not store uploaded file", "storage_error")
            finally:
                if tmp is not None:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                if active_upload:
                    with _tree_activity_lock:
                        _active_uploads.discard(target)
        self._send(201, {"path": rel, "size": length})

    do_PUT = _upload

    def do_POST(self):
        path, q = self._query()
        if path == "/ui/login":
            return self._ui_login()
        if path == "/ui/logout":
            if not self._gate(True):
                return
            with _session_lock:
                _state["sessions"].pop(self._cookie_token(), None)
            return self._send(200, {"ok": True}, extra={
                "Set-Cookie": "c67_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0",
            })
        if path == "/api/nas/trash/restore":
            if not self._gate(True):
                return
            return self._restore_from_trash(q.get("id", ""))
        if path == "/api/nas/trash":
            if not self._gate(True):
                return
            return self._move_to_trash(q.get("path", ""))
        if path != "/api/nas/mkdir":
            return self._upload()
        if not self._gate(True):
            return
        rel = q.get("path", "")
        target = self._resolve(rel)
        if not rel or target is None or target == os.path.realpath(_state["root"]):
            return self._err(400, "invalid path")
        with _path_lock(target):
            try:
                os.mkdir(target)
            except FileExistsError:
                return self._err(409, "already exists", "already_exists")
            except FileNotFoundError:
                return self._err(404, "parent directory not found", "parent_not_found")
            except OSError as exc:
                if exc.errno in (errno.EACCES, errno.EPERM):
                    return self._err(403, "permission denied", "storage_permission_denied")
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
        session_id = self._new_session()
        _log("ui_login_completed")
        self._send(200, {"ok": True}, extra={
            "Set-Cookie": "c67_session={}; HttpOnly; SameSite=Strict; Path=/; Max-Age={}".format(
                session_id, UI_SESSION_TTL_SECONDS
            ),
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
        with _path_lock(target):
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

    def do_PATCH(self):
        path, _ = self._query()
        if path != "/api/nas/trash":
            return self._err(404, "unknown endpoint")
        if not self._gate(True):
            return
        root = self._trash_root()
        try:
            if os.path.isdir(root):
                with _path_lock(root):
                    _remove_tree(root)
            _log("trash_emptied")
            self._send(200, {"emptied": True})
        except OSError as exc:
            _log("trash_empty_failed", errno=exc.errno)
            self._err(500, "could not empty trash", "storage_error")


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
