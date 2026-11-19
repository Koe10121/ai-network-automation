import logging
import os
import sys
import tempfile
import time
import warnings

warnings.filterwarnings("ignore")
logging.getLogger("paramiko").setLevel(logging.CRITICAL)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRATCH = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, SCRATCH)

import copy

import paramiko

from backend.app import verification_expect
from backend.generator import load_devices, load_plan
from fake_ios import FakeIOS
from worker.deployer import DeploymentError
from worker.verifier import evaluate, parse_neighbors, parse_ospf_routes, parse_protocols, verify_ospf

results = []


def check(label, ok, extra=""):
    ok = bool(ok)
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + label + ((" | " + str(extra)[:170]) if extra else ""))


# ---------- realistic Cisco IOS 15.2 output (R1, after OSPF converged) ----------
NEIGHBOR_OK = """
Neighbor ID     Pri   State           Dead Time   Address         Interface
2.2.2.2           1   FULL/DR         00:00:33    10.1.0.2        FastEthernet1/0
"""
NEIGHBOR_INIT = NEIGHBOR_OK.replace("FULL/DR        ", "INIT/DROTHER   ")
NEIGHBOR_2WAY = NEIGHBOR_OK.replace("FULL/DR        ", "2WAY/DROTHER   ")
NEIGHBOR_P2P = "2.2.2.2           0   FULL/  -        00:00:36    10.1.0.2        Serial0/0\n"
NEIGHBOR_NONE = "\n"

ROUTE_OK = """Codes: L - local, C - connected, S - static, R - RIP, M - mobile, B - BGP
       D - EIGRP, EX - EIGRP external, O - OSPF, IA - OSPF inter area
       N1 - OSPF NSSA external type 1, N2 - OSPF NSSA external type 2
       E1 - OSPF external type 1, E2 - OSPF external type 2

Gateway of last resort is not set

      10.0.0.0/8 is variably subnetted, 5 subnets, 2 masks
O        10.2.0.0/24 [110/20] via 10.1.0.2, 00:01:10, FastEthernet1/0
"""
ROUTE_NONE = ROUTE_OK.replace("O        10.2.0.0/24 [110/20] via 10.1.0.2, 00:01:10, FastEthernet1/0\n", "")
ROUTE_STATIC = ROUTE_OK.replace("O        10.2.0.0/24 [110/20]", "S        10.2.0.0/24 [1/0]")

PROTOCOLS_OK = """*** IP Routing is NSF aware ***

Routing Protocol is "ospf 1"
  Outgoing update filter list for all interfaces is not set
  Incoming update filter list for all interfaces is not set
  Router ID 1.1.1.1
  Number of areas in this router is 1. 1 normal 0 stub 0 nssa
  Maximum path: 4
  Routing for Networks:
    10.0.0.0 0.0.0.255 area 0
    10.1.0.0 0.0.0.255 area 0
  Passive Interface(s):
    FastEthernet0/0
  Routing Information Sources:
    Gateway         Distance      Last Update
    2.2.2.2              110      00:01:20
  Distance: (default is 110)
"""
PROTOCOLS_NO_OSPF = "*** IP Routing is NSF aware ***\n\n"
PROTOCOLS_BAD_ID = PROTOCOLS_OK.replace("Router ID 1.1.1.1", "Router ID 10.1.0.1")
PROTOCOLS_NOT_PASSIVE = PROTOCOLS_OK.replace("  Passive Interface(s):\n    FastEthernet0/0\n", "")
PROTOCOLS_MISSING_NET = PROTOCOLS_OK.replace("    10.1.0.0 0.0.0.255 area 0\n", "")
PROTOCOLS_WRONG_AREA = PROTOCOLS_OK.replace("10.1.0.0 0.0.0.255 area 0", "10.1.0.0 0.0.0.255 area 1")
PROTOCOLS_WRONG_PASSIVE = PROTOCOLS_OK.replace("    FastEthernet0/0\n", "    FastEthernet0/0\n    FastEthernet1/0\n")

# ---------- what the backend says R1 should show (from the real plan and devices.yaml) ----------
devices = load_devices()
plan = load_plan()
EXPECT = verification_expect(plan, devices, "R1")
check("backend expectation for R1", EXPECT == {
    "process_id": 1, "area": 0, "router_id": "1.1.1.1", "neighbors": ["2.2.2.2"],
    "advertised": [{"network": "10.0.0.0", "wildcard": "0.0.0.255"}, {"network": "10.1.0.0", "wildcard": "0.0.0.255"}],
    "passive": ["FastEthernet0/0"], "active": ["FastEthernet1/0"], "remote_networks": ["10.2.0.0/24"],
}, EXPECT)
check("backend expectation for R2 (neighbor R1, remote LAN 10.0.0.0/24)", (lambda e: e["neighbors"] == ["1.1.1.1"] and e["remote_networks"] == ["10.0.0.0/24"] and e["passive"] == ["FastEthernet1/0"])(verification_expect(plan, devices, "R2")))

# ---------- parsers ----------
n = parse_neighbors(NEIGHBOR_OK)
check("parse neighbor FULL/DR", len(n) == 1 and n[0]["router_id"] == "2.2.2.2" and n[0]["state"] == "FULL" and n[0]["role"] == "DR" and n[0]["interface"] == "FastEthernet1/0", n)
check("parse neighbor INIT", parse_neighbors(NEIGHBOR_INIT)[0]["state"] == "INIT")
check("parse point-to-point 'FULL/  -'", parse_neighbors(NEIGHBOR_P2P)[0]["state"] == "FULL")
check("no neighbors -> empty list", parse_neighbors(NEIGHBOR_NONE) == [])
check("parse OSPF route", parse_ospf_routes(ROUTE_OK) == ["10.2.0.0/24"], parse_ospf_routes(ROUTE_OK))
check("static route is NOT an OSPF route", parse_ospf_routes(ROUTE_STATIC) == [])
check("legend line 'O - OSPF' is not a route", "O - OSPF" not in str(parse_ospf_routes(ROUTE_NONE)) and parse_ospf_routes(ROUTE_NONE) == [])
check("parse inter-area route", parse_ospf_routes("O IA     10.9.0.0/24 [110/30] via 10.1.0.2, 00:00:05, Fa1/0") == ["10.9.0.0/24"])
p = parse_protocols(PROTOCOLS_OK)
check("parse protocols", p["process_ids"] == ["1"] and p["router_id"] == "1.1.1.1" and len(p["networks"]) == 2 and p["passive"] == ["FastEthernet0/0"], p)
check("passive list stops at the next heading", "Gateway" not in " ".join(p["passive"]))

# ---------- judgement ----------
def judge(nt, rt, pt, expect=EXPECT):
    r = evaluate(expect, nt, rt, pt)
    return r["passed"], [c["name"] + " -> " + c["detail"] for c in r["checks"] if not c["ok"]]

ok, fails = judge(NEIGHBOR_OK, ROUTE_OK, PROTOCOLS_OK)
check("converged router PASSES every check", ok and fails == [], fails)
check("number of checks (process, id, 2 nets, 2 interfaces, neighbor, route)", len(evaluate(EXPECT, NEIGHBOR_OK, ROUTE_OK, PROTOCOLS_OK)["checks"]) == 8)

ok, fails = judge(NEIGHBOR_INIT, ROUTE_OK, PROTOCOLS_OK)
check("neighbor stuck in INIT fails", not ok and any("INIT" in f for f in fails), fails)
ok, fails = judge(NEIGHBOR_2WAY, ROUTE_OK, PROTOCOLS_OK)
check("neighbor in 2WAY fails (must be FULL)", not ok and any("2WAY" in f for f in fails), fails)
ok, fails = judge(NEIGHBOR_NONE, ROUTE_NONE, PROTOCOLS_OK)
check("no neighbor and no route fails both", not ok and any("neighbor not seen" in f for f in fails) and any("no OSPF route" in f for f in fails), fails)
ok, fails = judge(NEIGHBOR_OK, ROUTE_STATIC, PROTOCOLS_OK)
check("a static route does not count as learning the LAN through OSPF", not ok and any("no OSPF route" in f for f in fails), fails)
ok, fails = judge(NEIGHBOR_OK, ROUTE_OK, PROTOCOLS_NO_OSPF)
check("OSPF not running fails", not ok and any("process 1" in f for f in fails), fails)
ok, fails = judge(NEIGHBOR_OK, ROUTE_OK, PROTOCOLS_BAD_ID)
check("wrong router ID fails", not ok and any("Router ID" in f for f in fails), fails)
ok, fails = judge(NEIGHBOR_OK, ROUTE_OK, PROTOCOLS_NOT_PASSIVE)
check("LAN interface not passive fails", not ok and any("is passive" in f for f in fails), fails)
ok, fails = judge(NEIGHBOR_OK, ROUTE_OK, PROTOCOLS_MISSING_NET)
check("network not advertised fails", not ok and any("10.1.0.0" in f for f in fails), fails)
ok, fails = judge(NEIGHBOR_OK, ROUTE_OK, PROTOCOLS_WRONG_AREA)
check("network in the wrong area fails", not ok and any("wrong area" in f for f in fails), fails)
ok, fails = judge(NEIGHBOR_OK, ROUTE_OK, PROTOCOLS_WRONG_PASSIVE)
check("link interface wrongly passive fails", not ok and any("wrongly passive" in f for f in fails), fails)

# ---------- over SSH against a fake router, including slow convergence ----------
TMP = tempfile.mkdtemp(prefix="netauto_verify_")
os.environ.update({
    "KNOWN_HOSTS_FILE": os.path.join(TMP, "known_hosts"), "BACKUP_DIR": os.path.join(TMP, "b"),
    "SSH_TIMEOUT_SECONDS": "5", "SSH_USERNAME": "admin", "SSH_PASSWORD": "s3cret-pw",
    "VERIFY_POLL_SECONDS": "1", "VERIFY_TIMEOUT_SECONDS": "30",
})
host_key = paramiko.RSAKey.generate(2048)


def fake(neighbor, route, protocols, **kw):
    return FakeIOS(host_key, users={"admin": "s3cret-pw"}, show_outputs={
        "show ip ospf neighbor": neighbor, "show ip route ospf": route, "show ip protocols": protocols}, **kw)


def info(s):
    return {"management_host": "127.0.0.1", "ssh_port": s.port}


s = fake(NEIGHBOR_OK, ROUTE_OK, PROTOCOLS_OK)
r = verify_ospf("R1", info(s), EXPECT)
check("SSH: converged router PASSED on the first attempt", r["status"] == "PASSED" and r["attempts"] == 1, r["attempts"])
check("SSH: the three commands were really run", [c for c in s.commands if c.startswith("show ip")] == ["show ip ospf neighbor", "show ip route ospf", "show ip protocols"], s.commands)
check("SSH: raw output kept as evidence", "FULL/DR" in r["outputs"]["show ip ospf neighbor"])
s.stop()

# neighbor comes up after a few polls, like a real router
calls = {"n": 0}
def slow_neighbors():
    calls["n"] += 1
    return NEIGHBOR_INIT if calls["n"] <= 3 else NEIGHBOR_OK
def slow_routes():
    return ROUTE_NONE if calls["n"] <= 3 else ROUTE_OK
s = fake(slow_neighbors, slow_routes, PROTOCOLS_OK)
t0 = time.time()
r = verify_ospf("R1", info(s), EXPECT)
check("SSH: waits for OSPF to converge, then PASSES", r["status"] == "PASSED" and r["attempts"] == 4, (r["status"], r["attempts"], round(time.time() - t0, 1)))
s.stop()

# never converges -> FAILED after the timeout with the reasons
os.environ["VERIFY_TIMEOUT_SECONDS"] = "4"
s = fake(NEIGHBOR_INIT, ROUTE_NONE, PROTOCOLS_OK)
t0 = time.time()
r = verify_ospf("R1", info(s), EXPECT)
failed = [c["name"] for c in r["checks"] if not c["ok"]]
check("SSH: never converges -> FAILED, not an endless wait", r["status"] == "FAILED" and time.time() - t0 < 15, round(time.time() - t0, 1))
check("SSH: FAILED result says which checks failed", failed == ["FULL adjacency with 2.2.2.2", "Learns 10.2.0.0/24 through OSPF"], failed)
s.stop()

# OSPF configured wrongly
s = fake(NEIGHBOR_OK, ROUTE_OK, PROTOCOLS_NOT_PASSIVE)
r = verify_ospf("R1", info(s), EXPECT)
check("SSH: wrong passive config is caught", r["status"] == "FAILED" and any("FastEthernet0/0 is passive" == c["name"] and not c["ok"] for c in r["checks"]))
s.stop()

# router unreachable / bad login
os.environ["SSH_PASSWORD"] = "wrong-pw"
s = fake(NEIGHBOR_OK, ROUTE_OK, PROTOCOLS_OK)
try:
    verify_ospf("R1", info(s), EXPECT)
    check("SSH: wrong password raises", False)
except DeploymentError as e:
    check("SSH: wrong password -> clear error", "authentication failed" in str(e) and "wrong-pw" not in str(e), e)
s.stop()
os.environ["SSH_PASSWORD"] = "s3cret-pw"

print("\nRESULT:", sum(results), "passed,", len(results) - sum(results), "failed")
import shutil
shutil.rmtree(TMP, ignore_errors=True)
