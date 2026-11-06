#!/usr/bin/env python3
"""NPU monitoring & occupy web board — run on the bastion host.

Endpoints:
  GET  /            -> board.html
  GET  /api/status  -> per-server card/container state (16 machines, ~15s)
  POST /api/occupy  {ip} -> start mockedup container (safety-gated)
  POST /api/release {ip} -> docker rm -f mockedup on that node

Design notes:
  - Query fans out via one ssh MultiplexDaemon? No — simpler: single ssh
    ControlMaster per cycle is slow for 16 hosts, so we fork one thread per
    host (ssh ConnectTimeout 8s; full cycle ~15s worst case).
  - Card state: free (<5GB HBM), ours (mockedup container present),
    busy (>5GB by someone else). Container names distinguish ours.
  - Occupy calls occupy.sh (which enforces the idle gate: procs/cards/others).
    MOCKUP_FORCE is NOT used — the web button must respect the same safety
    rules as the CLI. Ops state kept in memory only.
"""
import json, os, re, subprocess, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MON = os.environ.get("NPU_MON", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KEY = os.environ.get("NPU_KEY", os.path.expanduser("~/.ssh/id_rsa"))
PORT = 8766
# node groups shown as separate stats on the board; configure via env:
#   NPU_GROUP_A / NPU_GROUP_B  (space-separated IPs)
GROUP_A = os.environ.get("NPU_GROUP_A", "10.0.0.1 10.0.0.2").split()
GROUP_B = os.environ.get("NPU_GROUP_B", "10.0.0.3 10.0.0.4").split()
EXCLUDED = set()  # optional: IPs to permanently refuse occupying
SSH = ["ssh", "-i", KEY, "-o", "StrictHostKeyChecking=no",
       "-o", "UserKnownHostsFile=/dev/null", "-o", "ConnectTimeout=8",
       "-o", "BatchMode=yes"]

ops = {}  # ip -> {"op","state","message","ts"}


def ssh_run(ip, script, timeout=25):
    try:
        p = subprocess.run(SSH + [f"root@{ip}", script], capture_output=True,
                           text=True, timeout=timeout)
        return p.stdout, p.returncode
    except Exception as e:
        return str(e), 255


def probe(ip):
    """One ssh probe; returns server dict."""
    script = r'''
npu-smi info 2>/dev/null | awk '
  /0000:/ {
    line = $0
    gsub(/ *\//, "/", line); gsub(/\/ */, "/", line)
    n = 0
    while (match(line, /[0-9]+\/[0-9]+/)) {
      pair = substr(line, RSTART, RLENGTH); split(pair, t, "/")
      n++; used[n] = t[1]+0; total[n] = t[2]+0
      line = substr(line, RSTART + RLENGTH)
    }
    if (n > 0 && total[n] > 0) print used[n]
  }'
echo "---DOCKER---"
docker ps --format "{{.Names}}|{{.Status}}" 2>/dev/null
echo "---HOLDER---"
cat /root/mockup/HOLDER 2>/dev/null; echo ""
echo "---PROCNAMES---"
npu-smi info 2>/dev/null | grep -A200 "Process id" | grep -oE "\| [A-Za-z_][A-Za-z0-9_.-]* +\| [0-9]+ +\| [A-Za-z_][A-Za-z0-9_.-]*" | awk -F"\\|" "{gsub(/ /,\"\",\$4); print \$4}" | sort | uniq -c
echo "---VMEM---"
npu-smi info 2>/dev/null | sed -n "/Process id/,\$p" | grep -E "^\\| [0-9]" | awk -F"\\|" "{gsub(/ /,\"\",\$2); gsub(/ /,\"\",\$4); gsub(/ /,\"\",\$5); print \$2, \$4, \$5}"
'''
    out, rc = ssh_run(ip, script)
    d = {"ip": ip, "group": "a" if ip in GROUP_A else "b",
         "cards": [], "mockedup": None, "holder": "", "others": 0,
         "other_names": [], "verdict": "UNREACHABLE"}
    if rc != 0:
        d["error"] = out.strip()[:200] or "ssh failed"
        return d
    sec = None
    per_card_procs = {}  # card -> set(names)
    for line in out.splitlines():
        line = line.rstrip()
        if line == "---DOCKER---": sec = "docker"; continue
        if line == "---HOLDER---": sec = "holder"; continue
        if line == "---PROCNAMES---": sec = "pn"; continue
        if line == "---VMEM---": sec = "vm"; continue
        if sec is None:
            m = re.match(r"^\s*(\d+)\s*$", line)
            if m: d["cards"].append({"id": len(d["cards"]), "hbm": int(m.group(1)), "state": "busy", "procs": []})
        elif sec == "docker":
            if "|" in line:
                name, status = line.split("|", 1)
                if not status.startswith("Up"): continue
                if name == "mockedup": d["mockedup"] = status
                else: d.setdefault("other_names", []).append(name)
        elif sec == "holder":
            if line.strip(): d["holder"] = line.strip()[:40]
            sec = None  # single-line section; stop consuming on first non-empty line
        elif sec == "vm":
            cells = [c.strip() for c in line.split("|")[1:-1]]
            if len(cells) >= 3:
                try:
                    card = int(cells[0].split()[0]); pname = cells[2]
                    per_card_procs.setdefault(card, set()).add(pname)
                except (ValueError, IndexError):
                    pass
    for c in d["cards"]:
        names = per_card_procs.get(c["id"], set())
        holder = any(n in ("VLLMWorker_FK", "NPUHolder_WC") for n in names)
        if c["hbm"] < 5000:
            c["state"] = "free"
        elif holder and d["mockedup"]:
            c["state"] = "ours"
        else:
            c["state"] = "busy"
        c["procs"] = sorted(n for n in names if n not in ("VLLMWorker_FK", "NPUHolder_WC"))[:3]
    d["others"] = len(d["other_names"])
    if ip in EXCLUDED:
        d["verdict"] = "EXCLUDED"
    elif d["mockedup"]:
        d["verdict"] = "OURS"
    elif not d["cards"]:
        d["verdict"] = "UNREACHABLE"; d["error"] = "npu-smi parse failed"
    elif all(c["state"] == "free" for c in d["cards"]):
        d["verdict"] = "IDLE"
    elif any(c["state"] == "free" for c in d["cards"]):
        d["verdict"] = "PARTIAL"
    else:
        d["verdict"] = "BUSY"
    return d


def query_all():
    results, lock = {}, threading.Lock()

    def work(ip):
        r = probe(ip)
        with lock: results[ip] = r

    ts = [threading.Thread(target=work, args=(ip,)) for ip in GROUP_A + GROUP_B]
    for t in ts: t.start()
    for t in ts: t.join()
    return [results[ip] for ip in GROUP_A + GROUP_B]


def do_occupy(ip, name=""):
    ops[ip] = {"op": "occupy", "state": "running", "message": "", "ts": time.time()}
    env = dict(os.environ)
    if name: env["MOCKUP_HOLDER"] = name
    def run():
        try:
            p = subprocess.run(["bash", f"{MON}/occupy.sh", ip], capture_output=True,
                               text=True, timeout=180, env=env)
            ok = p.returncode == 0
            msg = (p.stdout + p.stderr).strip().splitlines()
            ops[ip] = {"op": "occupy", "state": "ok" if ok else "failed",
                       "message": msg[-1][:150] if msg else ("done" if ok else "failed"),
                       "ts": time.time()}
        except Exception as e:
            ops[ip] = {"op": "occupy", "state": "failed", "message": str(e)[:150], "ts": time.time()}
    threading.Thread(target=run, daemon=True).start()


def do_release(ip):
    ops[ip] = {"op": "release", "state": "running", "message": "", "ts": time.time()}
    def run():
        try:
            p = subprocess.run(["bash", f"{MON}/release.sh", ip], capture_output=True,
                               text=True, timeout=60)
            ok = p.returncode == 0 and "removed" in (p.stdout + p.stderr)
            msg = (p.stdout + p.stderr).strip().splitlines()
            ops[ip] = {"op": "release", "state": "ok" if ok else "failed",
                       "message": msg[-1][:150] if msg else ("done" if ok else "failed"),
                       "ts": time.time()}
        except Exception as e:
            ops[ip] = {"op": "release", "state": "failed", "message": str(e)[:150], "ts": time.time()}
    threading.Thread(target=run, daemon=True).start()


def do_register(ip, name):
    """Write holder name on the node (registry only, no hardware claim)."""
    try:
        p = subprocess.run(SSH + [f"root@{ip}", f"mkdir -p /root/mockup && printf '%s' '{name}' > /root/mockup/HOLDER"],
                           capture_output=True, text=True, timeout=20)
        return p.returncode == 0
    except Exception:
        return False


def do_unregister(ip):
    try:
        p = subprocess.run(SSH + [f"root@{ip}", "rm -f /root/mockup/HOLDER"],
                           capture_output=True, text=True, timeout=20)
        return p.returncode == 0
    except Exception:
        return False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            try:
                with open(f"{MON}/web/board.html", "rb") as f: html = f.read()
                self._send(200, html, "text/html")
            except FileNotFoundError:
                self._send(404, {"error": "board.html missing"})
        elif self.path == "/api/status":
            servers = query_all()
            self._send(200, {"time": time.strftime("%Y-%m-%dT%H:%M:%S"),
                             "servers": servers, "ops": ops})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try: body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError: return self._send(400, {"error": "bad json"})
        ip = body.get("ip", "")
        if self.path == "/api/occupy":
            if ip not in GROUP_A + GROUP_B: return self._send(400, {"error": f"unknown ip {ip}"})
            do_occupy(ip, str(body.get("name", ""))[:40]); self._send(202, {"ok": True, "async": True, "message": f"occupy {ip} started" + (f" ({body.get('name')})" if body.get('name') else "")})
        elif self.path == "/api/release":
            if ip not in GROUP_A + GROUP_B: return self._send(400, {"error": f"unknown ip {ip}"})
            do_release(ip); self._send(202, {"ok": True, "async": True, "message": f"release {ip} started"})
        elif self.path == "/api/register":
            if ip not in GROUP_A + GROUP_B: return self._send(400, {"error": f"unknown ip {ip}"})
            name = re.sub(r"[^\w一-鿿 .-]", "", str(body.get("name", ""))).strip()[:40]
            if not name: return self._send(400, {"error": "名字不能为空"})
            ok = do_register(ip, name)
            self._send(200 if ok else 502, {"ok": ok, "message": (f"{ip} 登记 → {name}" if ok else "ssh failed")})
        elif self.path == "/api/unregister":
            if ip not in GROUP_A + GROUP_B: return self._send(400, {"error": f"unknown ip {ip}"})
            ok = do_unregister(ip)
            self._send(200 if ok else 502, {"ok": ok, "message": (f"{ip} 登记已清除" if ok else "ssh failed")})
        else:
            self._send(404, {"error": "not found"})


if __name__ == "__main__":
    os.makedirs(f"{MON}/web", exist_ok=True)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"board listening on :{PORT}")
    srv.serve_forever()
