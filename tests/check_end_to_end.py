"""Whole chain including OSPF verification:
backend -> Kafka -> worker -> Paramiko deploy -> worker verifies OSPF -> backend, with two fake Cisco routers."""
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import warnings

warnings.filterwarnings("ignore")
logging.getLogger("paramiko").setLevel(logging.CRITICAL)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRATCH = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(os.environ["USERPROFILE"], "venvs", "netauto-exam", "Scripts", "python.exe")
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, SCRATCH)

import paramiko
import yaml

from backend.generator import generate_config, load_devices, load_plan
from fake_ios import FakeIOS

PORT = 8767
BASE = "http://127.0.0.1:" + str(PORT)
TOPIC = "netauto-test-" + uuid.uuid4().hex[:8]
TMP = tempfile.mkdtemp(prefix="netauto_e2e2_")

# ---- what each fake router says once OSPF is up (flags let a test break one thing) ----
flags = {"r2_has_route": True}

R1_PROTOCOLS = 'Routing Protocol is "ospf 1"\n  Router ID 1.1.1.1\n  Routing for Networks:\n    10.0.0.0 0.0.0.255 area 0\n    10.1.0.0 0.0.0.255 area 0\n  Passive Interface(s):\n    FastEthernet0/0\n  Routing Information Sources:\n'
R2_PROTOCOLS = 'Routing Protocol is "ospf 1"\n  Router ID 2.2.2.2\n  Routing for Networks:\n    10.1.0.0 0.0.0.255 area 0\n    10.2.0.0 0.0.0.255 area 0\n  Passive Interface(s):\n    FastEthernet1/0\n  Routing Information Sources:\n'
R1_NEIGHBOR = "Neighbor ID     Pri   State           Dead Time   Address         Interface\n2.2.2.2           1   FULL/DR         00:00:33    10.1.0.2        FastEthernet1/0\n"
R2_NEIGHBOR = "Neighbor ID     Pri   State           Dead Time   Address         Interface\n1.1.1.1           1   FULL/BDR        00:00:31    10.1.0.1        FastEthernet0/0\n"
R1_ROUTE = "      10.0.0.0/8 is variably subnetted, 5 subnets, 2 masks\nO        10.2.0.0/24 [110/20] via 10.1.0.2, 00:01:10, FastEthernet1/0\n"
R2_ROUTE_OK = "      10.0.0.0/8 is variably subnetted, 5 subnets, 2 masks\nO        10.0.0.0/24 [110/20] via 10.1.0.1, 00:01:10, FastEthernet0/0\n"
R2_ROUTE_NONE = "      10.0.0.0/8 is variably subnetted, 3 subnets, 2 masks\n"

host_key = paramiko.RSAKey.generate(2048)
r1 = FakeIOS(host_key, users={"admin": "s3cret-pw"}, hostname="R1", show_outputs={
    "show ip ospf neighbor": R1_NEIGHBOR, "show ip route ospf": R1_ROUTE, "show ip protocols": R1_PROTOCOLS})
r2 = FakeIOS(host_key, users={"admin": "s3cret-pw"}, hostname="R2", show_outputs={
    "show ip ospf neighbor": R2_NEIGHBOR,
    "show ip route ospf": lambda: R2_ROUTE_OK if flags["r2_has_route"] else R2_ROUTE_NONE,
    "show ip protocols": R2_PROTOCOLS})

devices = load_devices()
devices["routers"]["R1"]["management_host"] = "127.0.0.1"
devices["routers"]["R1"]["ssh_port"] = r1.port
devices["routers"]["R2"]["management_host"] = "127.0.0.1"
devices["routers"]["R2"]["ssh_port"] = r2.port
DEVICES_FILE = os.path.join(TMP, "devices.yaml")
yaml.safe_dump(devices, open(DEVICES_FILE, "w"))

plan = load_plan()
CONFIGS = {name: generate_config(plan, devices, name) for name in ("R1", "R2")}
CONFIGS_BAD = dict(CONFIGS)
CONFIGS_BAD["R2"] = CONFIGS["R2"].replace("router ospf 1", "bogus command\nrouter ospf 1")


def make_job(job_id, configs):
    return {"job_id": job_id, "upload_id": "x", "status": "APPROVED", "approved": True, "created_at": "t",
            "approved_at": "t", "plan": plan, "configs": configs, "errors": [], "warnings": []}


JOB_OK, JOB_BAD, JOB_NOROUTE, JOB_DRY = (uuid.uuid4().hex for _ in range(4))
JOBS_FILE = os.path.join(TMP, "jobs.json")
json.dump({JOB_OK: make_job(JOB_OK, CONFIGS), JOB_BAD: make_job(JOB_BAD, CONFIGS_BAD),
           JOB_NOROUTE: make_job(JOB_NOROUTE, CONFIGS)}, open(JOBS_FILE, "w"))

env = dict(os.environ)
for k in ("DRY_RUN", "SSH_KEY_PATH", "SSH_ENABLE_PASSWORD", "MGMT_INTERFACES"):
    env.pop(k, None)
env.update({
    "JOBS_FILE": JOBS_FILE, "DEVICES_FILE": DEVICES_FILE, "KAFKA_TOPIC": TOPIC, "BACKEND_URL": BASE,
    "WORKER_GROUP": "e2e-" + uuid.uuid4().hex[:8], "PYTHONUNBUFFERED": "1",
    "SSH_USERNAME": "admin", "SSH_PASSWORD": "s3cret-pw", "SSH_TIMEOUT_SECONDS": "5",
    "VERIFY_POLL_SECONDS": "1", "VERIFY_TIMEOUT_SECONDS": "4",
    "KNOWN_HOSTS_FILE": os.path.join(TMP, "known_hosts"), "BACKUP_DIR": os.path.join(TMP, "backups"),
})


def http(method, path):
    req = urllib.request.Request(BASE + path, method=method, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def wait_for(fn, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(1)
    return False


def vstatus(job_id):
    return (http("GET", "/jobs/" + job_id)[1].get("verification") or {}).get("status")


results = []


def check(label, ok, extra=""):
    ok = bool(ok)
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + label + ((" | " + str(extra)[:200]) if extra else ""))


server_log = open(os.path.join(TMP, "server.log"), "w")
server = subprocess.Popen([PY, "-m", "uvicorn", "backend.app:app", "--port", str(PORT)], cwd=ROOT, env=env, stdout=server_log, stderr=subprocess.STDOUT)
worker = None
try:
    def backend_up():
        try:
            return http("GET", "/jobs")[0] == 200
        except Exception:
            return False

    check("backend up", wait_for(backend_up, 40))

    # before any deployment: verification is not available
    code, body = http("POST", "/jobs/" + JOB_OK + "/verify/start")
    check("verification cannot start before deployment", code == 409, body.get("detail"))

    worker_log = open(os.path.join(TMP, "worker.log"), "w")
    worker = subprocess.Popen([PY, "-m", "worker.worker"], cwd=ROOT, env=env, stdout=worker_log, stderr=subprocess.STDOUT)
    time.sleep(3)

    # ---- 1. everything correct ----
    http("POST", "/jobs/" + JOB_OK + "/deploy")
    check("1. deployed, then OSPF verification PASSED", wait_for(lambda: vstatus(JOB_OK) == "PASSED", 90), vstatus(JOB_OK))
    job = http("GET", "/jobs/" + JOB_OK)[1]
    check("1. job status SUCCESS", job["status"] == "SUCCESS", job["status"])
    for dev in ("R1", "R2"):
        v = job["deployments"][dev]["verification"]
        check("1. " + dev + " verification PASSED with all checks ok", v["status"] == "PASSED" and v["checks"] and all(c["ok"] for c in v["checks"]), v["summary"])
        check("1. " + dev + " raw show output kept as evidence", "show ip ospf neighbor" in v["outputs"] and "FULL" in v["outputs"]["show ip ospf neighbor"])
    check("1. verification summary names both routers", "R1: PASSED" in job["verification"]["summary"] and "R2: PASSED" in job["verification"]["summary"], job["verification"]["summary"][:120])

    # ---- 2. a deployment fails -> verification skipped ----
    http("POST", "/jobs/" + JOB_BAD + "/deploy")
    check("2. a failed deployment SKIPS verification", wait_for(lambda: vstatus(JOB_BAD) == "SKIPPED", 60), vstatus(JOB_BAD))
    check("2. job status FAILED", http("GET", "/jobs/" + JOB_BAD)[1]["status"] == "FAILED")

    # ---- 3. deployment fine, but R2 never learns R1's LAN ----
    flags["r2_has_route"] = False
    http("POST", "/jobs/" + JOB_NOROUTE + "/deploy")
    check("3. missing OSPF route -> verification FAILED", wait_for(lambda: vstatus(JOB_NOROUTE) == "FAILED", 90), vstatus(JOB_NOROUTE))
    job = http("GET", "/jobs/" + JOB_NOROUTE)[1]
    check("3. deployment itself still SUCCESS (routers configured)", job["status"] == "SUCCESS" and all(d["status"] == "SUCCESS" for d in job["deployments"].values()))
    check("3. R1 passed but R2 failed", job["deployments"]["R1"]["verification"]["status"] == "PASSED" and job["deployments"]["R2"]["verification"]["status"] == "FAILED")
    failed = [c["name"] for c in job["deployments"]["R2"]["verification"]["checks"] if not c["ok"]]
    check("3. the failed check is the missing LAN route", failed == ["Learns 10.0.0.0/24 through OSPF"], failed)
    check("3. the summary points at the problem", "Learns 10.0.0.0/24 through OSPF" in job["verification"]["summary"], job["verification"]["summary"][-120:])

    # ---- 4. the claim endpoints cannot be abused ----
    code, body = http("POST", "/jobs/" + JOB_OK + "/verify/start")
    check("4. verification cannot be started twice", code == 409, body.get("detail"))
    code, body = http("POST", "/jobs/" + JOB_BAD + "/verify/finish")
    check("4. finish without a running verification is refused", code in (409, 422), code)
finally:
    for p in (worker, server):
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(10)
            except Exception:
                p.kill()
    server_log.close()
    r1.stop(); r2.stop()
    print("topic used:", TOPIC)
    print("RESULT:", sum(results), "passed,", len(results) - sum(results), "failed")
