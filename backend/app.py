import ipaddress
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from backend.analysis import analyze_image
from backend.dispatcher import send_jobs
from backend.generator import build_interfaces, load_devices, generate_config
from backend.planning import normalize_plan
from backend.validator import validate_plan

load_dotenv()

app = FastAPI()

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs("backups", exist_ok=True)
JOBS_FILE = os.getenv("JOBS_FILE", "jobs.json")
CHECK_REACHABILITY = os.getenv("CHECK_REACHABILITY") == "1"
JOBS_LOCK = threading.Lock()


# ---------- job store (kept in memory, saved to jobs.json) ----------

def load_jobs():
    if os.path.exists(JOBS_FILE):
        with open(JOBS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_jobs():
    with open(JOBS_FILE, "w", encoding="utf-8") as f:
        json.dump(JOBS, f, indent=2)


JOBS = load_jobs()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_job_or_404(job_id):
    if not job_id.isalnum() or job_id not in JOBS:
        raise HTTPException(status_code=404, detail="Job not found")
    return JOBS[job_id]


def find_upload(upload_id):
    if not upload_id.isalnum():
        raise HTTPException(status_code=400, detail="Invalid upload id")
    for ext in (".png", ".jpg"):
        candidate = os.path.join(UPLOAD_DIR, upload_id + ext)
        if os.path.exists(candidate):
            return candidate
    raise HTTPException(status_code=404, detail="Upload not found")


# ---------- pages and upload ----------

@app.get("/")
def home():
    return FileResponse("frontend/index.html")


@app.post("/upload")
async def upload(file: UploadFile = File(...)):
    if file.content_type not in ("image/png", "image/jpeg"):
        raise HTTPException(status_code=400, detail="Only PNG or JPEG allowed")

    upload_id = uuid.uuid4().hex
    ext = ".png" if file.content_type == "image/png" else ".jpg"

    path = os.path.join(UPLOAD_DIR, upload_id + ext)
    with open(path, "wb") as f:
        f.write(await file.read())

    return {"upload_id": upload_id, "filename": file.filename}


def parse_ai_json(raw_text: str):
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 2 and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return json.loads(cleaned)


# ---------- AI analysis ----------

@app.post("/analyze/{upload_id}")
def analyze(upload_id: str):
    path = find_upload(upload_id)

    try:
        return parse_ai_json(analyze_image(path))
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="AI did not return valid JSON")
    except Exception as e:
        raise HTTPException(status_code=502, detail="AI call failed: " + str(e))


# ---------- generate, review, approve ----------

@app.post("/generate/{upload_id}")
def generate(upload_id: str):
    path = find_upload(upload_id)

    try:
        plan = parse_ai_json(analyze_image(path))
    except json.JSONDecodeError:
        raise HTTPException(status_code=502, detail="AI did not return valid JSON")
    except Exception as e:
        raise HTTPException(status_code=502, detail="AI call failed: " + str(e))

    if not isinstance(plan, dict):
        raise HTTPException(status_code=502, detail="AI plan has the wrong shape")

    try:
        plan = normalize_plan(plan)
    except (TypeError, AttributeError, KeyError):
        pass  # a malformed plan is reported by the validator below

    devices = load_devices()
    errors = []
    warnings = []
    configs = {}

    try:
        result = validate_plan(plan, devices, check_reachability=CHECK_REACHABILITY)
        errors = list(result["errors"])
        warnings = list(result["warnings"])
        if not errors:
            for name in devices["routers"]:
                configs[name] = generate_config(plan, devices, name)
    except (KeyError, TypeError, ValueError, AttributeError) as e:
        errors.append("the AI plan is malformed: " + repr(e))
        configs = {}

    job_id = uuid.uuid4().hex
    job = {
        "job_id": job_id,
        "upload_id": upload_id,
        "status": "INVALID" if errors else "GENERATED",
        "approved": False,
        "created_at": now(),
        "approved_at": None,
        "plan": plan,
        "configs": configs,
        "errors": errors,
        "warnings": warnings,
    }
    JOBS[job_id] = job
    save_jobs()
    return job


@app.get("/devices")
def devices_info():
    """Router inventory (no secrets) so the web UI can label interfaces in the topology drawing."""
    try:
        return load_devices()
    except Exception:
        return {"ospf": {}, "routers": {}}


@app.get("/jobs")
def list_jobs():
    return list(JOBS.values())


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    return get_job_or_404(job_id)


@app.post("/jobs/{job_id}/approve")
def approve_job(job_id: str):
    job = get_job_or_404(job_id)

    if job["errors"]:
        raise HTTPException(status_code=409, detail="Job has validation errors and cannot be approved")
    if job["approved"]:
        return job

    job["approved"] = True
    job["status"] = "APPROVED"
    job["approved_at"] = now()
    save_jobs()
    return job


# ---------- deploy: send approved configs to Kafka ----------

@app.get("/jobs/{job_id}/configs/{device_id}")
def get_config(job_id: str, device_id: str):
    """Used by the worker: re-check approval and fetch one router's config."""
    job = get_job_or_404(job_id)

    if device_id not in job["configs"]:
        raise HTTPException(status_code=404, detail="No config for this device in this job")
    if not job["approved"]:
        raise HTTPException(status_code=403, detail="Job is not approved")

    devices = load_devices()
    router = devices["routers"].get(device_id, {})
    try:
        expect = verification_expect(job["plan"], devices, device_id)
    except (KeyError, ValueError, TypeError, AttributeError):
        expect = None  # deployment must still work; verification will report that it has no plan
    return {
        "job_id": job_id,
        "device_id": device_id,
        "approved": True,
        "config": job["configs"][device_id],
        "router": {
            "management_host": router.get("management_host"),
            "ssh_port": router.get("ssh_port", 22),
        },
        "verification_expect": expect,
    }


def verification_expect(plan, devices, device_id):
    """What `show` commands on this router must prove after a correct deployment."""
    interfaces = build_interfaces(plan, devices, device_id)
    connected = {i["network"] for i in interfaces}
    all_networks = {a["network"] for a in plan.get("addresses", []) if isinstance(a, dict) and "network" in a}

    neighbor_ids = [
        devices["routers"][name]["router_id"]
        for name in devices["routers"][device_id]["interfaces"]
        if name in devices["routers"]
    ]

    advertised = []
    for i in interfaces:
        net = ipaddress.ip_network(i["network"])
        advertised.append({"network": str(net.network_address), "wildcard": str(net.hostmask)})

    return {
        "process_id": devices["ospf"]["process_id"],
        "area": devices["ospf"]["area"],
        "router_id": devices["routers"][device_id]["router_id"],
        "neighbors": neighbor_ids,
        "advertised": advertised,
        "passive": [i["name"] for i in interfaces if i["passive"]],
        "active": [i["name"] for i in interfaces if not i["passive"]],
        "remote_networks": sorted(all_networks - connected),
    }


@app.post("/jobs/{job_id}/deploy")
def deploy_job(job_id: str):
    # The whole dispatch runs under the lock. The router records are created BEFORE the
    # messages are sent, because a worker that is already running can pick a message up
    # immediately and must find its record. If Kafka fails, everything is rolled back.
    with JOBS_LOCK:
        job = get_job_or_404(job_id)

        if not job["approved"]:
            raise HTTPException(status_code=403, detail="Job is not approved. Approve it before deploying.")
        if job["status"] != "APPROVED":
            raise HTTPException(status_code=409, detail="Job was already dispatched (status " + job["status"] + ")")

        sent_at = now()
        messages = [
            {
                "job_id": job_id,
                "device_id": device_id,
                "config_ref": "/jobs/" + job_id + "/configs/" + device_id,
                "dispatch_id": uuid.uuid4().hex,
                "sent_at": sent_at,
            }
            for device_id in job["configs"]
        ]

        job["status"] = "QUEUED"
        job["deployments"] = {
            m["device_id"]: {
                "status": "PENDING",
                "queued_at": sent_at,
                "started_at": None,
                "finished_at": None,
                "output": "",
                "verification": None,
                "kafka": None,
                "dispatch_id": m["dispatch_id"],
            }
            for m in messages
        }
        save_jobs()

        try:
            results = send_jobs(messages)
        except Exception as e:
            job["status"] = "APPROVED"
            job.pop("deployments", None)
            save_jobs()
            raise HTTPException(status_code=503, detail="Could not send the job to Kafka: " + str(e))

        for message, result in zip(messages, results):
            job["deployments"][message["device_id"]]["kafka"] = result
        save_jobs()
        return job


# ---------- worker callbacks: claim a device job, report the result ----------

class DeviceResult(BaseModel):
    status: Literal["SUCCESS", "FAILED"]
    output: str = ""
    verification: Optional[dict] = None


def refresh_job_status(job):
    states = [d["status"] for d in job["deployments"].values()]
    if all(s == "SUCCESS" for s in states):
        job["status"] = "SUCCESS"
    elif all(s in ("SUCCESS", "FAILED") for s in states):
        job["status"] = "FAILED"
    elif any(s in ("RUNNING", "SUCCESS", "FAILED") for s in states):
        job["status"] = "RUNNING"
    else:
        job["status"] = "QUEUED"


def get_deployment_or_404(job, device_id):
    deployment = job.get("deployments", {}).get(device_id)
    if deployment is None:
        raise HTTPException(status_code=404, detail="No deployment for this device in this job")
    return deployment


@app.post("/jobs/{job_id}/devices/{device_id}/start")
def start_device(job_id: str, device_id: str):
    """The worker claims a device job. Only one worker can claim it, so it never runs twice."""
    with JOBS_LOCK:
        job = get_job_or_404(job_id)
        deployment = get_deployment_or_404(job, device_id)

        if not job["approved"]:
            raise HTTPException(status_code=403, detail="Job is not approved")
        if deployment["status"] != "PENDING":
            raise HTTPException(status_code=409, detail="Device job already " + deployment["status"])

        deployment["status"] = "RUNNING"
        deployment["started_at"] = now()
        refresh_job_status(job)
        save_jobs()
        return deployment


@app.post("/jobs/{job_id}/devices/{device_id}/result")
def report_result(job_id: str, device_id: str, result: DeviceResult):
    with JOBS_LOCK:
        job = get_job_or_404(job_id)
        deployment = get_deployment_or_404(job, device_id)

        if deployment["status"] != "RUNNING":
            raise HTTPException(status_code=409, detail="Device job is " + deployment["status"] + ", not RUNNING")

        deployment["status"] = result.status
        deployment["output"] = result.output
        deployment["verification"] = result.verification
        deployment["finished_at"] = now()
        refresh_job_status(job)

        # when every router is done, OSPF verification can start (or is skipped)
        if job["status"] == "SUCCESS" and not job.get("verification"):
            job["verification"] = {"status": "PENDING"}
        elif job["status"] == "FAILED" and not job.get("verification"):
            job["verification"] = {"status": "SKIPPED", "summary": "A deployment failed, so OSPF was not verified."}

        save_jobs()
        return deployment


# ---------- OSPF verification ----------

class VerificationResult(BaseModel):
    status: Literal["PASSED", "FAILED", "SKIPPED"]
    checks: list[dict] = []
    outputs: dict = {}
    attempts: int = 0
    summary: str = ""


@app.post("/jobs/{job_id}/verify/start")
def start_verification(job_id: str):
    """The worker claims the verification. It only works once all routers are deployed."""
    with JOBS_LOCK:
        job = get_job_or_404(job_id)
        current = (job.get("verification") or {}).get("status", "NOT READY")
        if current != "PENDING":
            raise HTTPException(status_code=409, detail="Verification is not ready to start (" + current + ")")

        job["verification"] = {"status": "RUNNING", "started_at": now()}
        save_jobs()
        return job


@app.post("/jobs/{job_id}/devices/{device_id}/verification")
def report_device_verification(job_id: str, device_id: str, result: VerificationResult):
    with JOBS_LOCK:
        job = get_job_or_404(job_id)
        deployment = get_deployment_or_404(job, device_id)
        if (job.get("verification") or {}).get("status") != "RUNNING":
            raise HTTPException(status_code=409, detail="Verification is not running for this job")

        deployment["verification"] = {
            "status": result.status,
            "checks": result.checks,
            "outputs": result.outputs,
            "attempts": result.attempts,
            "summary": result.summary,
            "finished_at": now(),
        }
        save_jobs()
        return deployment["verification"]


@app.post("/jobs/{job_id}/verify/finish")
def finish_verification(job_id: str, result: VerificationResult):
    with JOBS_LOCK:
        job = get_job_or_404(job_id)
        current = job.get("verification") or {}
        if current.get("status") != "RUNNING":
            raise HTTPException(status_code=409, detail="Verification is not running for this job")

        job["verification"] = {
            "status": result.status,
            "summary": result.summary,
            "started_at": current.get("started_at"),
            "finished_at": now(),
        }
        save_jobs()
        return job
