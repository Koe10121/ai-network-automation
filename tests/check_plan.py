import copy
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

from backend.generator import generate_config, load_devices, load_plan
from backend.planning import normalize_plan, resolve_endpoint
from backend.validator import validate_plan

results = []


def check(label, ok, extra=""):
    ok = bool(ok)
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + label + ((" | " + str(extra)[:200]) if extra else ""))


devices = load_devices()
sample = load_plan()
names = ["PC1", "R1", "R2", "PC2", "R10"]

check("plain name unchanged", resolve_endpoint("R1", names) == ("R1", ""))
check("'R1:f0/0' -> R1 + port", resolve_endpoint("R1:f0/0", names) == ("R1", "f0/0"))
check("'PC1 e0' -> PC1 + port", resolve_endpoint("PC1 e0", names) == ("PC1", "e0"))
check("lowercase 'r2' -> R2", resolve_endpoint("r2", names)[0] == "R2")
check("'R10:g0/1' is R10, not R1", resolve_endpoint("R10:g0/1", names) == ("R10", "g0/1"))
check("unknown text left alone", resolve_endpoint("Switch9", names) == ("Switch9", ""))

# a valid plan whose links carry port labels now validates and generates
labelled = copy.deepcopy(sample)
labelled["links"] = [{"from": "PC1:e0", "to": "R1:f0/0"}, {"from": "R1:f1/0", "to": "R2:f0/0"}, {"from": "R2:f1/0", "to": "PC2:e0"}]
before = validate_plan(labelled, devices)["errors"]
after_plan = normalize_plan(labelled)
after = validate_plan(after_plan, devices)["errors"]
check("labelled links WITHOUT normalizing are rejected (the old bug)", any("no such link" in e for e in before))
check("labelled links WITH normalizing validate cleanly", [e for e in after if "management_host" not in e] == [], after)
check("port labels are kept for information", after_plan["links"][0].get("to_port") == "f0/0")
check("configs are identical to the unlabelled plan", all(generate_config(after_plan, devices, r) == generate_config(sample, devices, r) for r in ("R1", "R2")))

# the original input is not modified
check("original plan not modified", labelled["links"][0]["from"] == "PC1:e0")

# your real saved jobs
jobs = json.load(open("jobs.json", encoding="utf-8"))
for job in jobs.values():
    plan = normalize_plan(job["plan"])
    errs = [e for e in validate_plan(plan, devices)["errors"] if "management_host" not in e]
    print("   saved job", job["job_id"][:8], "(was " + job["status"] + ") ->", errs if errs else "no errors")

# a diagram with no IP addresses: the errors now say what is really wrong
noip = {"devices": [{"name": n} for n in ("PC1", "R1", "R2", "PC2")], "links": labelled["links"], "networks": [], "addresses": [], "ambiguities": []}
errs = validate_plan(normalize_plan(noip), devices)["errors"]
check("no-IP diagram: links accepted, error explains the IPs are missing", errs and all("no such link" not in e for e in errs) and any("IP addresses" in e for e in errs), errs[:2])

# junk input does not crash
for junk in ({}, {"links": "nope"}, {"devices": [1, 2], "links": [None, 5]}):
    try:
        normalize_plan(junk)
        check("junk input handled: " + json.dumps(junk)[:40], True)
    except Exception as e:
        check("junk input handled: " + json.dumps(junk)[:40], False, type(e).__name__ + ": " + str(e))

print("\nRESULT:", sum(results), "passed,", len(results) - sum(results), "failed")
