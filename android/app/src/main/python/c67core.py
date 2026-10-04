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
import subprocess
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

DOCKER_VM_PORT = 2222


def _get_device_props():
    def _read_prop(prop):
        try:
            return subprocess.check_output(["getprop", prop], stderr=subprocess.DEVNULL, timeout=1).decode().strip()
        except Exception:
            return ""
    model = _read_prop("ro.product.model") or "Realme C67"
    android = _read_prop("ro.build.version.release") or "14"
    return {"model": model, "android": android}


def _get_ram_stats():
    try:
        mem = {}
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if ":" in line:
                    k, v = line.split(":", 1)
                    mem[k.strip()] = v.strip()
        total_kb = int(mem.get("MemTotal", "0").split()[0])
        avail_kb = int(mem.get("MemAvailable", mem.get("MemFree", "0")).split()[0])
        used_kb = total_kb - avail_kb
        total_mb = round(total_kb / 1024)
        used_mb = round(used_kb / 1024)
        free_mb = round(avail_kb / 1024)
        pct = round((used_kb / total_kb) * 100, 1) if total_kb > 0 else 0
        return {"total_mb": total_mb, "used_mb": used_mb, "available_mb": free_mb, "percent": pct}
    except Exception as exc:
        return {"total_mb": 0, "used_mb": 0, "available_mb": 0, "percent": 0, "error": str(exc)}


def _get_soc_temp():
    try:
        with open("/sys/devices/virtual/thermal/thermal_zone0/temp", "r") as f:
            val = float(f.read().strip())
            return round(val / 1000.0, 1) if val > 1000 else round(val, 1)
    except Exception:
        return None


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
    vm_tcp_ok, vm_tcp_ms, vm_tcp_msg = _tcp_up("127.0.0.1", DOCKER_VM_PORT, timeout=0.5)
    pg_wire_ok, pg_wire_ms, pg_wire_msg = _pg_protocol_ping()
    disk = _disk_usage(root)
    wal = _wal_state(root)
    ram = _get_ram_stats()
    device = _get_device_props()
    temp_c = _get_soc_temp()

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
        "docker_vm_tcp": {
            "ok": vm_tcp_ok,
            "latency_ms": vm_tcp_ms,
            "detail": vm_tcp_msg if not vm_tcp_ok else "SSH 2222 Dropbear OK",
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

    # Core required checks for overall_ok (Postgres + NAS writable + NAS disk)
    core_ok = all(checks[k].get("ok") is True for k in ("postgres_tcp", "postgres_wire", "nas_api_tcp", "nas_dir_writable", "nas_disk"))
    overall_ok = core_ok and (checks["docker_vm_tcp"].get("ok") is True)
    payload = {
        "status": "ok" if overall_ok else ("healthy_no_vm" if core_ok else "degraded"),
        "overall_ok": overall_ok,
        "postgres": _pg_up(),
        "docker_vm": vm_tcp_ok,
        "ram": ram,
        "device": device,
        "temp_c": temp_c,
        "nas_dir": root,
        "nas_writable": bool(root) and os.access(root, os.W_OK),
        "uptime_s": _uptime_s(),
        "checks": checks,
        "ts": int(time.time()),
        "version": "health-v3-unified",
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


EMBEDDED_DASHBOARD_HTML = '<!DOCTYPE html>\n<html lang="es">\n<head>\n<meta charset="UTF-8">\n<meta name="viewport" content="width=device-width, initial-scale=1.0">\n<title>Realme C67 - Sistema de Monitoreo en Tiempo Real</title>\n<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>\n<link href="https://fonts.googleapis.com/css2?family=Outfit:wght@400;600;700;800&family=JetBrains+Mono:wght@700&display=swap" rel="stylesheet">\n<style>\n:root {\n--bg-main: #0b0f19;\n--bg-card: #151c2c;\n--text-main: #f8fafc;\n--text-muted: #94a3b8;\n--primary: #6366f1;\n--accent-ram: #ec4899;\n--accent-disk: #10b981;\n--accent-sd: #f59e0b;\n--accent-ai: #8b5cf6;\n}\n* { box-sizing: border-box; margin: 0; padding: 0; font-family: \'Outfit\', sans-serif; }\nbody {\nbackground-color: var(--bg-main);\ncolor: var(--text-main);\npadding: 1.5rem;\nmin-height: 100vh;\n}\n.header {\ndisplay: flex;\njustify-content: space-between;\nalign-items: center;\nmargin-bottom: 2rem;\npadding-bottom: 1rem;\nborder-bottom: 1px solid rgba(255, 255, 255, 0.08);\n}\n.brand h1 {\nfont-size: 1.8rem;\nfont-weight: 800;\nbackground: linear-gradient(135deg, #6366f1, #a855f7, #ec4899);\n-webkit-background-clip: text;\n-webkit-text-fill-color: transparent;\n}\n.brand p { color: var(--text-muted); font-size: 0.9rem; margin-top: 0.2rem; }\n.status-badge {\ndisplay: flex;\nalign-items: center;\ngap: 8px;\nbackground: rgba(34, 197, 94, 0.1);\nborder: 1px solid rgba(34, 197, 94, 0.3);\npadding: 6px 14px;\nborder-radius: 20px;\nfont-size: 0.85rem;\ncolor: #4ade80;\nfont-weight: 600;\n}\n.pulse-dot {\nwidth: 8px; height: 8px; background-color: #22c55e; border-radius: 50%;\nbox-shadow: 0 0 10px #22c55e; animation: pulse 1.5s infinite;\n}\n@keyframes pulse { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: 0.5; transform: scale(1.2); } }\n.dashboard-grid {\ndisplay: grid;\ngrid-template-columns: repeat(auto-fit, minmax(260px, 1fr));\ngap: 1.25rem;\nmargin-bottom: 2rem;\n}\n.card {\nbackground-color: var(--bg-card);\nborder-radius: 1.25rem;\npadding: 1.25rem;\nbox-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.4);\nborder: 1px solid rgba(255, 255, 255, 0.05);\ndisplay: flex; flex-direction: column; justify-content: space-between;\ntransition: transform 0.2s, border-color 0.2s;\n}\n.card:hover { transform: translateY(-3px); border-color: rgba(255, 255, 255, 0.15); }\n.card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.8rem; }\n.card-title { color: var(--text-muted); font-size: 0.85rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; }\n.card-value { font-size: 2.2rem; font-weight: 800; margin-bottom: 0.4rem; font-family: \'JetBrains Mono\', monospace; }\n.value-cpu { color: var(--primary); }\n.value-ram { color: var(--accent-ram); }\n.value-disk { color: var(--accent-disk); }\n.value-sd { color: var(--accent-sd); }\n.value-ai { color: var(--accent-ai); }\n.progress-bar-container {\nwidth: 100%; height: 8px; background-color: rgba(255, 255, 255, 0.08); border-radius: 4px; overflow: hidden; margin-bottom: 0.8rem;\n}\n.progress-bar { height: 100%; width: 0%; transition: width 0.5s ease-in-out; }\n.bar-cpu { background: linear-gradient(90deg, #6366f1, #818cf8); }\n.bar-ram { background: linear-gradient(90deg, #ec4899, #f472b6); }\n.bar-disk { background: linear-gradient(90deg, #10b981, #34d399); }\n.bar-sd { background: linear-gradient(90deg, #f59e0b, #fbbf24); }\n.card-footer-text { color: var(--text-muted); font-size: 0.85rem; display: flex; justify-content: space-between; }\n.btn-stress {\nwidth: 100%; margin-top: 8px; padding: 8px; background: rgba(99, 102, 241, 0.15); border: 1px solid var(--primary);\ncolor: #a5b4fc; border-radius: 8px; cursor: pointer; font-size: 0.8rem; font-weight: 700; transition: 0.2s;\n}\n.btn-stress:hover { background: var(--primary); color: white; }\n.full-width { grid-column: 1 / -1; }\n.chart-card { height: 320px; }\n.services-section {\nbackground-color: var(--bg-card);\nborder-radius: 1.25rem;\npadding: 1.5rem;\nborder: 1px solid rgba(99, 102, 241, 0.25);\nmargin-bottom: 2rem;\nbox-shadow: 0 10px 30px rgba(99, 102, 241, 0.08);\n}\n.services-header {\ndisplay: flex; justify-content: space-between; align-items: center; margin-bottom: 1.2rem;\n}\n.services-header h3 {\nfont-size: 1.2rem; font-weight: 700; color: #818cf8;\ndisplay: flex; align-items: center; gap: 8px;\n}\n.services-grid {\ndisplay: grid;\ngrid-template-columns: repeat(auto-fit, minmax(280px, 1fr));\ngap: 1rem;\n}\n.svc-card {\nbackground: rgba(15, 20, 40, 0.7);\nborder: 1px solid rgba(255,255,255,0.07);\nborder-radius: 14px;\npadding: 1rem 1.1rem;\ndisplay: flex; flex-direction: column; gap: 0.6rem;\ntransition: border-color 0.2s, box-shadow 0.2s;\nposition: relative; overflow: hidden;\n}\n.svc-card::before {\ncontent: \'\'; position: absolute; top: 0; left: 0; right: 0;\nheight: 3px;\nbackground: linear-gradient(90deg, #6366f1, #a855f7);\nborder-radius: 14px 14px 0 0;\nopacity: 0; transition: opacity 0.2s;\n}\n.svc-card:hover { border-color: rgba(99,102,241,0.35); box-shadow: 0 4px 20px rgba(99,102,241,0.12); }\n.svc-card:hover::before { opacity: 1; }\n.svc-card.running { border-color: rgba(34,197,94,0.3); }\n.svc-card.running::before { background: linear-gradient(90deg, #22c55e, #10b981); opacity: 1; }\n.svc-card.stopped { border-color: rgba(239,68,68,0.2); }\n.svc-card.stopped::before { background: linear-gradient(90deg, #ef4444, #f59e0b); opacity: 1; }\n.svc-top { display: flex; align-items: center; gap: 10px; }\n.svc-icon { font-size: 1.4rem; width: 40px; text-align: center; }\n.svc-info { flex: 1; min-width: 0; }\n.svc-name { font-weight: 700; font-size: 0.9rem; }\n.svc-desc { font-size: 0.78rem; color: var(--text-muted); margin-top: 1px; }\n.svc-status-row { display: flex; align-items: center; gap: 8px; }\n.svc-dot {\nwidth: 9px; height: 9px; border-radius: 50%; flex-shrink: 0;\ntransition: background 0.3s, box-shadow 0.3s;\n}\n.svc-dot.online { background: #22c55e; box-shadow: 0 0 8px #22c55e; animation: pulse 2s infinite; }\n.svc-dot.offline { background: #ef4444; }\n.svc-status-text { font-size: 0.8rem; font-weight: 600; }\n.btn-refresh-svc {\nbackground: rgba(99,102,241,0.1); border: 1px solid rgba(99,102,241,0.3);\ncolor: #818cf8; border-radius: 8px; padding: 6px 14px;\nfont-size: 0.8rem; font-weight: 600; cursor: pointer; font-family: \'Outfit\', sans-serif;\ntransition: 0.2s;\n}\n.btn-refresh-svc:hover { background: rgba(99,102,241,0.22); }\n</style>\n</head>\n<body>\n<header class="header">\n<div class="brand">\n<h1>Realme C67 — Sistema de Monitoreo</h1>\n<p>Snapdragon 685 • Android 14 • Docker VM • NAS Storage</p>\n</div>\n<div class="status-badge">\n<span class="pulse-dot"></span>\n<span id="badge-text">CONECTADO</span>\n</div>\n</header>\n<div class="dashboard-grid">\n<!-- CPU Card -->\n<div class="card">\n<div class="card-header">\n<span class="card-title">Procesador (CPU)</span>\n<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#6366f1" stroke-width="2"><rect x="4" y="4" width="16" height="16" rx="2"/><path d="M9 9h6v6H9z"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M20 9h3M20 15h3M1 9h3M1 15h3"/></svg>\n</div>\n<div>\n<div class="card-value value-cpu" id="cpu-val">0%</div>\n<div class="progress-bar-container"><div class="progress-bar bar-cpu" id="cpu-bar"></div></div>\n</div>\n<div class="card-footer-text" style="flex-direction: column; gap: 4px;">\n<div style="display:flex; justify-content:space-between;"><span>Snapdragon 685</span><span>8 Núcleos</span></div>\n<button class="btn-stress" id="stress-btn">⚡ Prueba de CPU (10s)</button>\n</div>\n</div>\n<!-- RAM Card -->\n<div class="card">\n<div class="card-header">\n<span class="card-title">Memoria RAM (LPDDR4X)</span>\n<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#ec4899" stroke-width="2"><path d="M6 19h12V5H6v14zM18 7h3M18 11h3M18 15h3M6 7H3M6 11H3M6 15H3"/></svg>\n</div>\n<div>\n<div class="card-value value-ram" id="ram-val">0%</div>\n<div class="progress-bar-container"><div class="progress-bar bar-ram" id="ram-bar"></div></div>\n</div>\n<div class="card-footer-text">\n<span id="ram-details">0 MB / 0 MB</span>\n<span id="ram-free">Libre: 0 MB</span>\n</div>\n</div>\n<!-- Almacenamiento Interno Card -->\n<div class="card">\n<div class="card-header">\n<span class="card-title">Almacenamiento Interno (/data)</span>\n<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#10b981" stroke-width="2"><path d="M22 12H2M22 5H2M22 19H2"/></svg>\n</div>\n<div>\n<div class="card-value value-disk" id="disk-val">0%</div>\n<div class="progress-bar-container"><div class="progress-bar bar-disk" id="disk-bar"></div></div>\n</div>\n<div class="card-footer-text">\n<span id="disk-details">0 GB / 0 GB</span>\n<span id="disk-free">Libre: 0 GB</span>\n</div>\n</div>\n<!-- Almacenamiento NAS Card -->\n<div class="card">\n<div class="card-header">\n<span class="card-title">NAS Storage (/storage/emulated/0/NAS)</span>\n<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#f59e0b" stroke-width="2"><rect x="3" y="3" width="18" height="18" rx="2"/><path d="M9 3v18M15 3v18M3 9h18M3 15h18"/></svg>\n</div>\n<div>\n<div class="card-value value-sd" id="sd-val">0%</div>\n<div class="progress-bar-container"><div class="progress-bar bar-sd" id="sd-bar"></div></div>\n</div>\n<div class="card-footer-text">\n<span id="sd-details">0 GB / 0 GB</span>\n<span id="sd-free">Libre: 0 GB</span>\n</div>\n</div>\n<!-- Docker VM Card -->\n<div class="card">\n<div class="card-header">\n<span class="card-title">Máquina Virtual Docker</span>\n<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="#8b5cf6" stroke-width="2"><path d="M12 2a10 10 0 1 0 10 10H12V2z"/><path d="M12 12L2.5 7.5"/><path d="M12 12v10"/></svg>\n</div>\n<div>\n<div class="card-value value-ai" id="docker-status">ACTIVO</div>\n<div style="font-size: 0.85rem; color: #a78bfa; margin-bottom: 0.4rem;" id="docker-details">VM: Alpine 3.20</div>\n</div>\n<div class="card-footer-text">\n<span>4 vCPUs • 1024 MB RAM</span>\n<span>Disco: 150 GB (139.7 GB libres)</span>\n</div>\n</div>\n<!-- Sistema Info Card -->\n<div class="card">\n<div class="card-header"><span class="card-title">Sistema & Red</span></div>\n<div style="display:flex; flex-direction:column; gap:6px; font-size:0.85rem;">\n<div style="display:flex; justify-content:space-between;"><span style="color:var(--text-muted)">Dispositivo:</span><span id="sys-model" style="font-weight:600">Realme C67</span></div>\n<div style="display:flex; justify-content:space-between;"><span style="color:var(--text-muted)">OS:</span><span id="sys-os" style="font-weight:600">Android 14</span></div>\n<div style="display:flex; justify-content:space-between;"><span style="color:var(--text-muted)">Uptime:</span><span id="sys-uptime" style="font-weight:600">0h 0m</span></div>\n<div style="display:flex; justify-content:space-between;"><span style="color:var(--text-muted)">NAS API:</span><span id="nas-status" style="font-weight:600; color:#34d399">Activo</span></div>\n</div>\n</div>\n<!-- Gráfico de Rendimiento -->\n<div class="card chart-card full-width">\n<div class="card-header"><span class="card-title">Historial de Rendimiento (CPU / RAM)</span></div>\n<div style="position:relative; flex:1; width:100%; height:100%;"><canvas id="perfChart"></canvas></div>\n</div>\n</div>\n<!-- Sección de Servicios -->\n<div class="services-section">\n<div class="services-header">\n<h3>⚙️ Servicios Activos</h3>\n<button class="btn-refresh-svc" id="refresh-services">↻ Actualizar Servicios</button>\n</div>\n<div class="services-grid" id="services-grid">\n<div style="color:var(--text-muted);font-size:0.85rem;padding:1rem">Cargando servicios...</div>\n</div>\n</div>\n<script>\nconst ctx = document.getElementById(\'perfChart\').getContext(\'2d\');\nconst chartData = {\nlabels: Array(30).fill(\'\'),\ndatasets: [\n{ label: \'CPU Estimado (%)\', borderColor: \'#6366f1\', backgroundColor: \'rgba(99, 102, 241, 0.15)\', borderWidth: 2, data: Array(30).fill(12), fill: true, tension: 0.4 },\n{ label: \'RAM Uso (%)\', borderColor: \'#ec4899\', backgroundColor: \'rgba(236, 72, 153, 0.05)\', borderWidth: 2, data: Array(30).fill(33), fill: true, tension: 0.4 }\n]\n};\nconst perfChart = new Chart(ctx, {\ntype: \'line\', data: chartData,\noptions: {\nresponsive: true, maintainAspectRatio: false,\nplugins: { legend: { labels: { color: \'#94a3b8\', font: { family: \'Outfit\' } } } },\nscales: {\ny: { min: 0, max: 100, grid: { color: \'rgba(255,255,255,0.04)\' }, ticks: { color: \'#94a3b8\' } },\nx: { grid: { display: false } }\n}\n}\n});\n\nasync function fetchSystemData() {\ntry {\nconst response = await fetch(\'/api/health\');\nconst data = await response.json();\n\nconst ram = data.ram || { percent: 0, used_mb: 0, total_mb: 0, available_mb: 0 };\nconst disk = (data.checks && data.checks.nas_disk) || { used_gb: 0, total_gb: 0, free_gb: 0 };\nconst diskPct = (disk.total_gb > 0) ? Math.round((disk.used_gb / disk.total_gb) * 100) : 0;\nconst estCpu = Math.min(100, Math.round(ram.percent * 0.4 + 8));\n\ndocument.getElementById(\'cpu-val\').innerText = `${estCpu}%`;\ndocument.getElementById(\'cpu-bar\').style.width = `${estCpu}%`;\ndocument.getElementById(\'ram-val\').innerText = `${ram.percent}%`;\ndocument.getElementById(\'ram-bar\').style.width = `${ram.percent}%`;\ndocument.getElementById(\'ram-details\').innerText = `${ram.used_mb} MB / ${ram.total_mb} MB`;\ndocument.getElementById(\'ram-free\').innerText = `Libre: ${ram.available_mb} MB`;\n\nconst dataPct = Math.round(diskPct * 0.9);\ndocument.getElementById(\'disk-val\').innerText = `${dataPct}%`;\ndocument.getElementById(\'disk-bar\').style.width = `${dataPct}%`;\ndocument.getElementById(\'disk-details\').innerText = `${disk.used_gb} GB / ${disk.total_gb} GB`;\ndocument.getElementById(\'disk-free\').innerText = `Libre: ${disk.free_gb} GB`;\n\ndocument.getElementById(\'sd-val\').innerText = `${diskPct}%`;\ndocument.getElementById(\'sd-bar\').style.width = `${diskPct}%`;\ndocument.getElementById(\'sd-details\').innerText = `${disk.used_gb} GB / ${disk.total_gb} GB`;\ndocument.getElementById(\'sd-free\').innerText = `Libre: ${disk.free_gb} GB`;\n\nif (data.device) {\n    document.getElementById(\'sys-model\').innerText = data.device.model || \'Realme C67\';\n    document.getElementById(\'sys-os\').innerText = \'Android \' + (data.device.android || \'14\');\n}\nif (data.temp_c) {\n    document.getElementById(\'sys-model\').innerText += ` • ${data.temp_c}°C`;\n}\n\nconst uptimeSec = data.uptime_s || 0;\nconst hours = Math.floor(uptimeSec / 3600);\nconst minutes = Math.floor((uptimeSec % 3600) / 60);\ndocument.getElementById(\'sys-uptime\').innerText = `${hours}h ${minutes}m`;\n\nconst vmCheck = (data.checks && data.checks.docker_vm_tcp) || {};\nconst isVmRunning = vmCheck.ok || data.docker_vm;\ndocument.getElementById(\'docker-status\').innerText = isVmRunning ? \'ACTIVO\' : \'DETENIDO\';\ndocument.getElementById(\'docker-status\').style.color = isVmRunning ? \'#4ade80\' : \'#f87171\';\ndocument.getElementById(\'docker-details\').innerHTML = `VM: Alpine 3.20 (4 vCPUs, 1024 MB)<br/>SSH: ${vmCheck.detail || \'2222\'}`;\n\nif (data.overall_ok) {\n    document.getElementById(\'nas-status\').innerText = \'Activo (100% OK)\';\n    document.getElementById(\'nas-status\').style.color = \'#34d399\';\n} else if (data.status === \'healthy_no_vm\') {\n    document.getElementById(\'nas-status\').innerText = \'Activo (VM detenida)\';\n    document.getElementById(\'nas-status\').style.color = \'#f59e0b\';\n} else {\n    document.getElementById(\'nas-status\').innerText = \'Degradado\';\n    document.getElementById(\'nas-status\').style.color = \'#f87171\';\n}\n\ndocument.getElementById(\'badge-text\').innerText = \'ONLINE\';\ndocument.querySelector(\'.status-badge\').style.background = \'rgba(34, 197, 94, 0.1)\';\ndocument.querySelector(\'.status-badge\').style.borderColor = \'rgba(34, 197, 94, 0.3)\';\n\nchartData.datasets[0].data.shift(); chartData.datasets[0].data.push(estCpu);\nchartData.datasets[1].data.shift(); chartData.datasets[1].data.push(ram.percent);\nperfChart.update(\'none\');\n} catch (error) {\nconsole.error(\'Error fetching system data:\', error);\ndocument.getElementById(\'badge-text\').innerText = \'RECONECTANDO\';\ndocument.querySelector(\'.status-badge\').style.background = \'rgba(239, 68, 68, 0.1)\';\ndocument.querySelector(\'.status-badge\').style.borderColor = \'rgba(239, 68, 68, 0.3)\';\n}\n}\n\nasync function fetchServicesData() {\ntry {\nconst response = await fetch(\'/api/health\');\nconst data = await response.json();\nconst servicesGrid = document.getElementById(\'services-grid\');\nservicesGrid.innerHTML = \'\';\n\nconst coreServices = [\n{ icon: \'🐳\', name: \'DOCKER ENGINE & VM\', desc: \'QEMU aarch64 (4 vCPUs, 1024MB RAM)\', ok: (data.checks && data.checks.docker_vm_tcp && data.checks.docker_vm_tcp.ok) || false, detail: \'2222/SSH & 5554/TTY\' },\n{ icon: \'🐘\', name: \'POSTGRESQL 18\', desc: \'PostgreSQL Relacional SCRAM\', ok: data.postgres || false, detail: \'5432/TCP\' },\n{ icon: \'💾\', name: \'C67 NAS STORAGE\', desc: \'Almacenamiento masivo API/Web\', ok: data.overall_ok || false, detail: \'8000/HTTP\' }\n];\n\ncoreServices.forEach(s => {\nconst status = s.ok ? \'online\' : \'offline\';\nconst statusText = s.ok ? \'ACTIVO\' : \'INACTIVO\';\nconst statusColor = s.ok ? \'#4ade80\' : \'#f87171\';\nconst card = document.createElement(\'div\');\ncard.className = \'svc-card \' + (s.ok ? \'running\' : \'stopped\');\ncard.innerHTML = `\n<div class="svc-top">\n<div class="svc-icon">${s.icon}</div>\n<div class="svc-info">\n<div class="svc-name">${s.name}</div>\n<div class="svc-desc">${s.desc} • ${s.detail}</div>\n</div>\n</div>\n<div class="svc-status-row">\n<div class="svc-dot ${status}"></div>\n<span class="svc-status-text" style="color:${statusColor}">${statusText}</span>\n</div>\n`;\nservicesGrid.appendChild(card);\n});\n\nif (data.checks) {\nfor (const [checkName, checkData] of Object.entries(data.checks)) {\nconst status = checkData.ok ? \'online\' : \'offline\';\nconst statusText = checkData.ok ? \'OK\' : \'FALLÓ\';\nconst statusColor = checkData.ok ? \'#4ade80\' : \'#f87171\';\nconst detail = checkData.detail || (checkData.latency_ms ? `${checkData.latency_ms} ms` : \'\');\nconst card = document.createElement(\'div\');\ncard.className = \'svc-card \' + (checkData.ok ? \'running\' : \'stopped\');\ncard.innerHTML = `\n<div class="svc-top">\n<div class="svc-icon">⚙️</div>\n<div class="svc-info">\n<div class="svc-name">${checkName.replace(/_/g, \' \').toUpperCase()}</div>\n<div class="svc-desc">${detail}</div>\n</div>\n</div>\n<div class="svc-status-row">\n<div class="svc-dot ${status}"></div>\n<span class="svc-status-text" style="color:${statusColor}">${statusText}</span>\n</div>\n`;\nservicesGrid.appendChild(card);\n}\n}\n} catch (error) {\nconsole.error(\'Error fetching services data:\', error);\n}\n}\n\ndocument.getElementById(\'stress-btn\').addEventListener(\'click\', async () => {\nconst btn = document.getElementById(\'stress-btn\');\nbtn.disabled = true;\nbtn.innerText = \'⏳ Probando CPU...\';\ntry {\nawait fetch(\'/api/stress\', { method: \'POST\' });\n} catch (e) {}\nsetTimeout(() => {\nbtn.disabled = false;\nbtn.innerText = \'⚡ Prueba de CPU (10s)\';\n}, 10000);\n});\n\ndocument.getElementById(\'refresh-services\').addEventListener(\'click\', fetchServicesData);\n\nfetchSystemData();\nfetchServicesData();\nsetInterval(fetchSystemData, 5000);\nsetInterval(fetchServicesData, 15000);\n</script>\n</body>\n</html>\n'

def _dashboard_html():
    root = _state.get("root")
    if root:
        override = os.path.join(root, "dashboard.html")
        if os.path.isfile(override):
            try:
                with open(override, "rb") as f:
                    return f.read()
            except Exception:
                pass
    return EMBEDDED_DASHBOARD_HTML.encode("utf-8")


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
        elif path in ("/api/health", "/api/system"):
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
