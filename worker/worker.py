"""Kafka worker.

Run from the project folder:
    python -m worker.worker

For every message on the jobs topic it:
  1. claims the device job from the backend (so it can never run twice)
  2. re-checks that the job is still approved and fetches the config
  3. deploys the config
  4. reports SUCCESS or FAILED, with the output, back to the backend
"""
import json
import os
import time
import urllib.error
import urllib.request
import warnings
from datetime import datetime

from dotenv import load_dotenv
from kafka import KafkaConsumer

load_dotenv()
warnings.filterwarnings("ignore", category=DeprecationWarning)

from worker.deployer import DeploymentError, deploy_config  # noqa: E402  (must come after load_dotenv)
from worker.verifier import verify_ospf  # noqa: E402

BACKEND_URL = os.getenv("BACKEND_URL", "http://127.0.0.1:8000")
BOOTSTRAP = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
TOPIC = os.getenv("KAFKA_TOPIC", "network-config-jobs")
GROUP_ID = os.getenv("WORKER_GROUP", "config-worker")


def log(text):
    print(datetime.now().strftime("%H:%M:%S"), text, flush=True)


class BackendError(Exception):
    def __init__(self, status, detail):
        super().__init__(str(status) + " " + str(detail))
        self.status = status
        self.detail = detail


def call_backend(method, path, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        BACKEND_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8")).get("detail", "")
        except Exception:
            detail = ""
        raise BackendError(e.code, detail)
    # urllib.error.URLError / timeouts are OSError: they mean the backend is unreachable


def call_backend_retry(method, path, body=None, attempts=5):
    """Like call_backend, but waits and retries if the backend is unreachable.

    Used after a job is claimed: the job must never be left stuck in RUNNING.
    """
    for attempt in range(1, attempts + 1):
        try:
            return call_backend(method, path, body)
        except OSError as e:
            log("backend unreachable (attempt " + str(attempt) + "/" + str(attempts) + "): " + str(e))
            if attempt == attempts:
                raise
            time.sleep(3)


def report_result(job_id, device_id, status, output):
    path = "/jobs/" + job_id + "/devices/" + device_id + "/result"
    call_backend_retry("POST", path, {"status": status, "output": output}, attempts=10)


def verify_device(job_id, device_id):
    """Run the OSPF checks on one router. Always returns a result, never raises for router problems."""
    if os.getenv("DRY_RUN", "0") == "1":
        return {"status": "SKIPPED", "summary": "Dry run: OSPF was not checked.", "checks": [], "outputs": {}, "attempts": 0}

    try:
        config = call_backend_retry("GET", "/jobs/" + job_id + "/configs/" + device_id)
        if not config.get("verification_expect"):
            return {"status": "FAILED", "summary": "The backend could not work out what OSPF should look like for this router.", "checks": [], "outputs": {}, "attempts": 0}
        result = verify_ospf(device_id, config.get("router"), config["verification_expect"])
        passed = sum(1 for c in result["checks"] if c["ok"])
        result["summary"] = str(passed) + " of " + str(len(result["checks"])) + " checks passed after " + str(result["attempts"]) + " attempt(s)"
        return result
    except DeploymentError as e:
        return {"status": "FAILED", "summary": str(e), "checks": [], "outputs": {}, "attempts": 0}
    except BackendError as e:
        return {"status": "FAILED", "summary": "Could not get the verification plan: " + str(e), "checks": [], "outputs": {}, "attempts": 0}


def maybe_verify(job_id):
    """Try to claim the OSPF verification. It only succeeds for the worker that finished the last router."""
    try:
        job = call_backend_retry("POST", "/jobs/" + job_id + "/verify/start")
    except BackendError as e:
        if e.status not in (404, 409):
            log("could not start verification for " + job_id[:8] + ": " + str(e))
        return

    log("verifying OSPF for job " + job_id[:8])
    lines = []
    statuses = []
    for device_id in job["deployments"]:
        result = verify_device(job_id, device_id)
        call_backend_retry("POST", "/jobs/" + job_id + "/devices/" + device_id + "/verification", result)
        statuses.append(result["status"])
        line = device_id + ": " + result["status"] + " (" + result["summary"] + ")"
        failed = [c["name"] for c in result.get("checks", []) if not c["ok"]]
        if failed:
            line += " failed: " + "; ".join(failed)
        lines.append(line)
        log("verification " + line[:160])

    if all(s == "SKIPPED" for s in statuses):
        final = "SKIPPED"
    elif all(s == "PASSED" for s in statuses):
        final = "PASSED"
    else:
        final = "FAILED"
    call_backend_retry("POST", "/jobs/" + job_id + "/verify/finish", {"status": final, "summary": " | ".join(lines)})
    log("verification finished for job " + job_id[:8] + ": " + final)


def handle(message):
    """Process one Kafka message. Raises OSError only if the backend is unreachable."""
    if not isinstance(message, dict):
        log("skip: message is not valid JSON")
        return

    job_id = message.get("job_id")
    device_id = message.get("device_id")
    config_ref = message.get("config_ref")
    if not (job_id and device_id and config_ref):
        log("skip: message is missing job_id, device_id or config_ref")
        return
    if config_ref != "/jobs/" + job_id + "/configs/" + device_id:
        log("skip: config_ref does not match job_id and device_id (" + str(config_ref) + ")")
        return

    label = job_id[:8] + "/" + device_id

    # 1. claim the job. This also re-checks approval and blocks duplicates.
    try:
        call_backend("POST", "/jobs/" + job_id + "/devices/" + device_id + "/start")
    except BackendError as e:
        if e.status == 404:
            log("skip " + label + ": the backend does not know this job")
        elif e.status == 403:
            log("skip " + label + ": job is not approved")
        elif e.status == 409:
            log("skip " + label + ": duplicate, " + str(e.detail))
        else:
            log("skip " + label + ": " + str(e))
        return

    log("RUNNING " + label)

    # 2. re-check approval and fetch the config
    config = None
    try:
        config = call_backend_retry("GET", config_ref)
        status, output = None, None
    except BackendError as e:
        status, output = "FAILED", "Approval re-check failed: " + str(e)
    except OSError as e:
        status, output = "FAILED", "Could not reach the backend to re-check approval: " + str(e)

    # 3. deploy. Any error here, even a network timeout, is a FAILED deployment.
    if config is not None:
        try:
            output = deploy_config(device_id, config["config"], config.get("router"), job_id)
            status = "SUCCESS"
        except DeploymentError as e:
            status, output = "FAILED", str(e)
        except Exception as e:
            status, output = "FAILED", type(e).__name__ + ": " + str(e)

    # 4. report back
    report_result(job_id, device_id, status, output)
    log(status + " " + label + ": " + output.splitlines()[0][:100])

    # 5. if that was the last router, check that OSPF really works
    try:
        maybe_verify(job_id)
    except Exception as e:
        log("verification error for " + job_id[:8] + ": " + type(e).__name__ + ": " + str(e))


def parse_json(raw):
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


def main():
    consumer = KafkaConsumer(
        TOPIC,
        bootstrap_servers=BOOTSTRAP,
        group_id=GROUP_ID,
        enable_auto_commit=False,
        auto_offset_reset="earliest",
        value_deserializer=parse_json,
        max_poll_interval_ms=900000,  # verification can take a few minutes
    )
    log("worker started. topic=" + TOPIC + " group=" + GROUP_ID + " backend=" + BACKEND_URL)

    try:
        for message in consumer:
            while True:
                try:
                    handle(message.value)
                    break
                except OSError as e:
                    log("backend unreachable, retrying in 5 seconds: " + str(e))
                    time.sleep(5)
                except Exception as e:
                    log("unexpected error, skipping this message: " + type(e).__name__ + ": " + str(e))
                    break
            consumer.commit()
    except KeyboardInterrupt:
        log("stopping the worker")
    finally:
        consumer.close()


if __name__ == "__main__":
    main()
