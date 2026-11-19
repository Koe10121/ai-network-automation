"""Check that OSPF really works on a router after the config was deployed.

Runs  show ip ospf neighbor / show ip route ospf / show ip protocols  over SSH
and checks:
  * the OSPF process is running with the right process ID and router ID
  * the right networks are advertised, and the LAN interfaces are passive
  * every expected neighbor is in the FULL state
  * every remote LAN is learned through OSPF (an 'O' route)

OSPF needs time to converge (neighbors reach FULL after about 40 seconds on
Ethernet), so the checks repeat until they pass or VERIFY_TIMEOUT_SECONDS runs out.
"""
import os
import re
import time

import paramiko

from worker.deployer import (
    DeploymentError,
    get_target,
    open_router_session,
    run,
    translate_ssh_error,
)

COMMANDS = ["show ip ospf neighbor", "show ip route ospf", "show ip protocols"]

NEIGHBOR_RE = re.compile(
    r"^(?P<id>\d+\.\d+\.\d+\.\d+)\s+(?P<pri>\d+)\s+(?P<state>[A-Za-z0-9]+)(?:/\s*(?P<role>\S+))?"
    r"\s+(?P<dead>\S+)\s+(?P<addr>\d+\.\d+\.\d+\.\d+)\s+(?P<iface>\S+)",
    re.MULTILINE,
)
ROUTE_RE = re.compile(
    r"^\s*O(?:\s+(?:IA|E1|E2|N1|N2))?\*?\s+(?P<net>\d+\.\d+\.\d+\.\d+)(?:/(?P<len>\d+))?\s+\[\d+/\d+\]",
    re.MULTILINE,
)
PROCESS_RE = re.compile(r'Routing Protocol is "ospf (?P<pid>\d+)"')
ROUTER_ID_RE = re.compile(r"Router ID (?P<id>\d+\.\d+\.\d+\.\d+)")
NETWORK_RE = re.compile(r"^\s+(?P<net>\d+\.\d+\.\d+\.\d+)\s+(?P<wild>\d+\.\d+\.\d+\.\d+)\s+area\s+(?P<area>\S+)", re.MULTILINE)


# ---------- parsers ----------

def parse_neighbors(text):
    return [
        {"router_id": m["id"], "state": m["state"].upper(), "role": m["role"] or "", "address": m["addr"], "interface": m["iface"]}
        for m in NEIGHBOR_RE.finditer(text)
    ]


def parse_ospf_routes(text):
    """Return the networks learned through OSPF as 'a.b.c.d/len' (or just 'a.b.c.d' if no length is shown)."""
    routes = []
    for m in ROUTE_RE.finditer(text):
        routes.append(m["net"] + ("/" + m["len"] if m["len"] else ""))
    return routes


def parse_protocols(text):
    info = {"process_ids": PROCESS_RE.findall(text), "router_id": None, "networks": [], "passive": []}

    m = ROUTER_ID_RE.search(text)
    if m:
        info["router_id"] = m["id"]

    for m in NETWORK_RE.finditer(text):
        info["networks"].append({"network": m["net"], "wildcard": m["wild"], "area": m["area"]})

    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith("Passive Interface"):
            for follow in lines[i + 1:]:
                if not follow.startswith(" ") or not follow.strip() or follow.strip().endswith(":"):
                    break
                info["passive"].append(follow.strip())
            break
    return info


# ---------- the judgement ----------

def _check(name, ok, detail):
    return {"name": name, "ok": bool(ok), "detail": detail}


def evaluate(expect, neighbor_text, route_text, protocols_text):
    """Compare the router output with what the plan says should be true.

    expect = {"process_id", "area", "router_id", "neighbors": [router ids],
              "advertised": [{"network", "wildcard"}], "passive": [names], "active": [names],
              "remote_networks": ["a.b.c.d/len"]}
    """
    checks = []
    proto = parse_protocols(protocols_text)
    neighbors = {n["router_id"]: n for n in parse_neighbors(neighbor_text)}
    routes = parse_ospf_routes(route_text)

    # OSPF process and router ID
    running = str(expect["process_id"]) in proto["process_ids"]
    checks.append(_check(
        "OSPF process " + str(expect["process_id"]) + " is running", running,
        "found" if running else "show ip protocols does not list OSPF process " + str(expect["process_id"]),
    ))
    checks.append(_check(
        "Router ID is " + expect["router_id"], proto["router_id"] == expect["router_id"],
        "Router ID " + str(proto["router_id"]),
    ))

    # advertised networks, in the right area
    for adv in expect["advertised"]:
        match = [n for n in proto["networks"] if n["network"] == adv["network"] and n["wildcard"] == adv["wildcard"]]
        in_area = bool(match) and str(match[0]["area"]) == str(expect["area"])
        checks.append(_check(
            "Advertises " + adv["network"] + " " + adv["wildcard"] + " in area " + str(expect["area"]), in_area,
            "found" if in_area else ("wrong area " + str(match[0]["area"]) if match else "not in 'Routing for Networks'"),
        ))

    # passive interfaces
    for name in expect["passive"]:
        ok = name in proto["passive"]
        checks.append(_check(name + " is passive", ok, "passive" if ok else "not in the passive interface list"))
    for name in expect["active"]:
        ok = name not in proto["passive"]
        checks.append(_check(name + " is not passive", ok, "active" if ok else "wrongly passive, no adjacency can form"))

    # neighbors must be FULL
    for rid in expect["neighbors"]:
        n = neighbors.get(rid)
        if n is None:
            checks.append(_check("FULL adjacency with " + rid, False, "neighbor not seen"))
        else:
            full = n["state"] == "FULL"
            checks.append(_check(
                "FULL adjacency with " + rid, full,
                n["state"] + ("/" + n["role"] if n["role"] else "") + " on " + n["interface"],
            ))

    # remote LANs learned through OSPF
    for net in expect["remote_networks"]:
        base = net.split("/")[0]
        learned = net in routes or base in routes
        checks.append(_check("Learns " + net + " through OSPF", learned, "O route present" if learned else "no OSPF route for it"))

    return {"passed": all(c["ok"] for c in checks), "checks": checks}


# ---------- the SSH part ----------

def verify_ospf(device_id, router_info, expect):
    """Log in, run the three commands, and repeat until OSPF has converged or the time is up.

    Returns {"status": "PASSED"/"FAILED", "checks": [...], "attempts": n, "outputs": {...}}.
    Raises DeploymentError if the router cannot be reached.
    """
    cfg, host, port = get_target(device_id, router_info)
    where = host + ":" + str(port)
    cmd_timeout = max(cfg["timeout"] * 2, 15)

    try:
        total = float(os.getenv("VERIFY_TIMEOUT_SECONDS") or 120)
        poll = float(os.getenv("VERIFY_POLL_SECONDS") or 10)
    except ValueError:
        total, poll = 120.0, 10.0

    deadline = time.time() + total
    attempts = 0
    client = None
    try:
        client, channel = open_router_session(host, port, cfg, cmd_timeout)
        while True:
            attempts += 1
            outputs = {command: run(channel, command, cmd_timeout) for command in COMMANDS}
            result = evaluate(expect, outputs[COMMANDS[0]], outputs[COMMANDS[1]], outputs[COMMANDS[2]])
            if result["passed"] or time.time() + poll > deadline:
                break
            time.sleep(poll)
    except DeploymentError:
        raise
    except (paramiko.SSHException, OSError) as e:
        raise translate_ssh_error(e, cfg, where)
    finally:
        if client is not None:
            client.close()

    return {
        "status": "PASSED" if result["passed"] else "FAILED",
        "checks": result["checks"],
        "attempts": attempts,
        "outputs": {command: text.strip()[:4000] for command, text in outputs.items()},
    }
