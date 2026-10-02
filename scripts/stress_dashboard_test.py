import json
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import psycopg
import requests


HOST = os.environ.get("C67_HOST", "192.168.0.229")
BASE = f"http://{HOST}:8000"
PG_PASSWORD = os.environ.get("C67_PG_PASSWORD", "")
NAS_TOKEN = os.environ.get("C67_NAS_TOKEN", "")

if not PG_PASSWORD:
    raise SystemExit("Missing C67_PG_PASSWORD")
if not NAS_TOKEN:
    raise SystemExit("Missing C67_NAS_TOKEN")

PG_DSN = (
    f"host={HOST} port=5432 dbname=postgres user=postgres "
    f"password={PG_PASSWORD} connect_timeout=4"
)
HEADERS = {"X-Token": NAS_TOKEN}


def pctl(data, p):
    if not data:
        return None
    arr = sorted(data)
    idx = int(len(arr) * p)
    idx = max(0, min(len(arr) - 1, idx))
    return round(arr[idx], 2)


def main():
    result = {}

    # Phase 0 smoke
    smoke = {}
    for path, hdr in (("/api/health", None), ("/api/nas/list?path=", HEADERS), ("/", None)):
        t0 = time.perf_counter()
        r = requests.get(BASE + path, headers=hdr, timeout=8)
        smoke[path] = {"status": r.status_code, "ms": round((time.perf_counter() - t0) * 1000, 2)}
    result["smoke"] = smoke

    # Phase 1: /api/health load
    n = 3000
    workers = 60
    lat = []
    status_counts = {}

    def hit_health(_):
        t0 = time.perf_counter()
        try:
            r = requests.get(BASE + "/api/health", timeout=6)
            return r.status_code, (time.perf_counter() - t0) * 1000
        except Exception:
            return "EXC", None

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(hit_health, i) for i in range(n)]
        for f in as_completed(futures):
            st, ms = f.result()
            status_counts[str(st)] = status_counts.get(str(st), 0) + 1
            if st == 200 and ms is not None:
                lat.append(ms)
    elapsed = time.perf_counter() - start
    result["health_load"] = {
        "requests": n,
        "workers": workers,
        "elapsed_s": round(elapsed, 2),
        "rps": round(n / elapsed, 2),
        "ok": status_counts.get("200", 0),
        "fail": n - status_counts.get("200", 0),
        "status_counts": status_counts,
        "lat_ms": {
            "p50": round(statistics.median(lat), 2) if lat else None,
            "p95": pctl(lat, 0.95),
            "p99": pctl(lat, 0.99),
            "max": round(max(lat), 2) if lat else None,
        },
    }

    # Phase 2: SQL load
    nq = 2000
    wq = 40
    sql_ok = 0
    sql_fail = 0
    sql_lat = []
    sql_err = {}

    def sql_query(_):
        t0 = time.perf_counter()
        try:
            with psycopg.connect(PG_DSN) as conn:
                with conn.cursor() as cur:
                    cur.execute("select 1")
                    v = cur.fetchone()[0]
            if v != 1:
                return "BAD", None
            return "OK", (time.perf_counter() - t0) * 1000
        except Exception as exc:
            return type(exc).__name__, None

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=wq) as ex:
        futures = [ex.submit(sql_query, i) for i in range(nq)]
        for f in as_completed(futures):
            st, ms = f.result()
            if st == "OK" and ms is not None:
                sql_ok += 1
                sql_lat.append(ms)
            else:
                sql_fail += 1
                sql_err[st] = sql_err.get(st, 0) + 1
    elapsed = time.perf_counter() - start
    result["sql_load"] = {
        "queries": nq,
        "workers": wq,
        "elapsed_s": round(elapsed, 2),
        "qps": round(nq / elapsed, 2),
        "ok": sql_ok,
        "fail": sql_fail,
        "errors": sql_err,
        "lat_ms": {
            "p50": round(statistics.median(sql_lat), 2) if sql_lat else None,
            "p95": pctl(sql_lat, 0.95),
            "p99": pctl(sql_lat, 0.99),
            "max": round(max(sql_lat), 2) if sql_lat else None,
        },
    }

    # Phase 3: NAS upload/download/delete stress
    size = 1024 * 1024
    cycles = 80
    wn = 12
    payload = os.urandom(size)
    nas_ok = 0
    nas_fail = 0
    nas_err = {}

    def nas_cycle(i):
        name = f"opencode_stress_{i}.bin"
        up = f"{BASE}/api/nas/upload?path=stress/{name}"
        dl = f"{BASE}/api/nas/download?path=stress/{name}"
        de = f"{BASE}/api/nas/delete?path=stress/{name}"
        try:
            ru = requests.put(up, headers=HEADERS, data=payload, timeout=25)
            if ru.status_code not in (200, 201):
                return f"UP_{ru.status_code}"
            rd = requests.get(dl, headers=HEADERS, timeout=25)
            if rd.status_code != 200:
                return f"DL_{rd.status_code}"
            if len(rd.content) != size:
                return "DL_SIZE"
            rdel = requests.delete(de, headers=HEADERS, timeout=25)
            if rdel.status_code != 200:
                return f"DEL_{rdel.status_code}"
            return "OK"
        except Exception as exc:
            return type(exc).__name__

    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=wn) as ex:
        futures = [ex.submit(nas_cycle, i) for i in range(cycles)]
        for f in as_completed(futures):
            st = f.result()
            if st == "OK":
                nas_ok += 1
            else:
                nas_fail += 1
                nas_err[st] = nas_err.get(st, 0) + 1
    elapsed = time.perf_counter() - start
    result["nas_rw_stress"] = {
        "cycles": cycles,
        "workers": wn,
        "file_size_bytes": size,
        "elapsed_s": round(elapsed, 2),
        "ok": nas_ok,
        "fail": nas_fail,
        "errors": nas_err,
    }

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
