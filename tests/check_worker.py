import json
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

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = os.path.join(os.environ["USERPROFILE"], "venvs", "netauto-exam", "Scripts", "python.exe")
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from kafka import KafkaProducer

PORT = 8765
BASE = "http://127.0.0.1:" + str(PORT)
TOPIC = "netauto-test-" + uuid.uuid4().hex[:8]
GROUP = "test-" + uuid.uuid4().hex[:8]
TMP = tempfile.mkdtemp(prefix="netauto_")
JOBS_FILE = os.path.join(TMP, "jobs.json")

CONFIG = {
    "R1": "interface FastEthernet0/0\n ip address 10.0.0.1 255.255.255.0\n no shutdown\n!\nrouter ospf 1\n router-id 1.1.1.1",
    "R2": "interface FastEthernet0/0\n ip address 10.1.0.2 255.255.255.0\n no shutdown\n!\nrouter ospf 1\n router-id 2.2.2.2",
}


def make_job(job_id, approved, deployments=False):
    job = {
        "job_id": job_id, "upload_id": "x", "status": "APPROVED" if approved else "GENERATED",
        "approved": approved, "created_at": "t", "approved_at": "t" if approved else None,
        "plan": {}, "configs": CONFIG, "errors": [], "warnings": [],
    }
    if deployments:
        job["status"] = "QUEUED"
        job["deployments"] = {
            d: {"status": "PENDING", "queued_at": "t", "started_at": None, "finished_at": None,
                "output": "", "verification": None, "kafka": None, "dispatch_id": "d"}
            for d in CONFIG
        }
    return job


JOB1 = uuid.uuid4().hex   # approved, deployed through the API
JOB2 = uuid.uuid4().hex   # approval revoked after it was queued
JOB3 = uuid.uuid4().hex   # approved, used for the FAILED path
UNKNOWN = uuid.uuid4().hex

json.dump({
    JOB1: make_job(JOB1, True),
    JOB2: make_job(JOB2, False, deployments=True),
    JOB3: make_job(JOB3, True),
}, open(JOBS_FILE, "w"))

env = dict(os.environ)
env.update({
    "JOBS_FILE": JOBS_FILE, "KAFKA_TOPIC": TOPIC, "BACKEND_URL": BASE,
    "WORKER_GROUP": GROUP, "DRY_RUN": "1", "PYTHONUNBUFFERED": "1",
    # Safety: this test must never log in to a real router, even if .env has real credentials.
    # Empty values stay empty because load_dotenv() does not override variables that are already set.
    "SSH_USERNAME": "", "SSH_PASSWORD": "", "SSH_KEY_PATH": "", "SSH_KEY_PASSPHRASE": "", "SSH_ENABLE_PASSWORD": "",
})


def http(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def wait_for(fn, timeout, step=1.0):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(step)
    return False


results = []


def check(label, ok, extra=""):
    ok = bool(ok)
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + label + (" | " + str(extra) if extra else ""))


server_log = open(os.path.join(TMP, "server.log"), "w")
server = subprocess.Popen([PY, "-m", "uvicorn", "backend.app:app", "--port", str(PORT)],
                          cwd=ROOT, env=env, stdout=server_log, stderr=subprocess.STDOUT)
worker = None

try:
    def up_check():
        try:
            return http("GET", "/jobs")[0] == 200
        except Exception:
            return False

    check("backend started on port " + str(PORT), wait_for(up_check, 40))

    # 1. deploy JOB1 through the real API -> two Kafka messages
    code, body = http("POST", "/jobs/" + JOB1 + "/deploy")
    check("deploy through the API", code == 200 and body["status"] == "QUEUED", (code, body.get("status")))

    # 2. extra messages the worker must handle safely
    producer = KafkaProducer(bootstrap_servers="127.0.0.1:9092")
    def send(obj, raw=None):
        producer.send(TOPIC, raw if raw is not None else json.dumps(obj).encode()).get(timeout=10)

    def msg(job, dev, ref=None):
        return {"job_id": job, "device_id": dev, "config_ref": ref or "/jobs/" + job + "/configs/" + dev, "dispatch_id": uuid.uuid4().hex}

    send(msg(JOB1, "R1"))                       # duplicate of an already queued job
    send(msg(UNKNOWN, "R1"))                    # job the backend does not know
    send(None, raw=b"this is not json")         # malformed
    send(msg(JOB2, "R1"))                       # approval was revoked
    send(msg(JOB1, "R2", ref="/jobs/other/configs/R2"))  # config_ref does not match
    producer.flush()
    producer.close()

    # 3. run the real worker
    worker_log = open(os.path.join(TMP, "worker.log"), "w")
    worker = subprocess.Popen([PY, "-m", "worker.worker"], cwd=ROOT, env=env, stdout=worker_log, stderr=subprocess.STDOUT)

    def job1_done():
        c, b = http("GET", "/jobs/" + JOB1)
        return b.get("status") == "SUCCESS"

    check("worker finished JOB1 (status SUCCESS)", wait_for(job1_done, 45))
    time.sleep(6)  # let it get through the extra messages too

    c, j1 = http("GET", "/jobs/" + JOB1)
    for dev in ("R1", "R2"):
        d = j1["deployments"][dev]
        check(dev + " SUCCESS with output and timestamps",
              d["status"] == "SUCCESS" and "DRY RUN" in d["output"] and d["started_at"] and d["finished_at"], d["output"][:60])

    c, j2 = http("GET", "/jobs/" + JOB2)
    check("revoked job stayed PENDING (not executed)", all(d["status"] == "PENDING" for d in j2["deployments"].values()))

    # 4. every message was committed, so a restarted worker has nothing left to run
    worker.terminate(); worker.wait(10); worker_log.close()
    first_log = open(os.path.join(TMP, "worker.log")).read()
    from kafka import KafkaConsumer, TopicPartition
    probe = KafkaConsumer(bootstrap_servers="127.0.0.1:9092", group_id=GROUP, enable_auto_commit=False)
    tp = TopicPartition(TOPIC, 0)
    committed = probe.committed(tp)
    end = probe.end_offsets([tp])[tp]
    probe.close()
    check("all messages committed (restart re-runs nothing)", committed == end, "committed " + str(committed) + " of " + str(end))

    # 5. FAILED path: real deployment is not implemented yet, so DRY_RUN=0 must report FAILED
    code, body = http("POST", "/jobs/" + JOB3 + "/deploy")
    env["DRY_RUN"] = "0"
    env["WORKER_GROUP"] = "test3-" + uuid.uuid4().hex[:8]
    worker_log = open(os.path.join(TMP, "worker3.log"), "w")
    worker = subprocess.Popen([PY, "-m", "worker.worker"], cwd=ROOT, env=env, stdout=worker_log, stderr=subprocess.STDOUT)

    def job3_done():
        c, b = http("GET", "/jobs/" + JOB3)
        return b.get("status") == "FAILED"

    check("failed deployment reported as FAILED", wait_for(job3_done, 90))
    c, j3 = http("GET", "/jobs/" + JOB3)
    check("FAILED output explains why", "credentials are not configured" in j3["deployments"]["R1"]["output"], j3["deployments"]["R1"]["output"][:70])
    worker.terminate(); worker.wait(10); worker_log.close()

    # 6. what the first worker run logged for the awkward messages
    wanted = {
        "duplicate skipped": "duplicate",
        "unknown job skipped": "does not know this job",
        "malformed message skipped": "not valid JSON",
        "revoked approval skipped": "not approved",
        "mismatched config_ref skipped": "does not match",
    }
    for label, needle in wanted.items():
        check(label, needle in first_log)

    print("\n--- first worker log ---")
    print(first_log)
finally:
    for p in (worker, server):
        if p is not None and p.poll() is None:
            p.terminate()
            try:
                p.wait(10)
            except Exception:
                p.kill()
    server_log.close()
    print("topic used:", TOPIC)
    print("RESULT:", sum(results), "passed,", len(results) - sum(results), "failed")
    try:
        import shutil
        shutil.rmtree(TMP, ignore_errors=True)
    except Exception:
        pass
