import ipaddress
import socket


def validate_plan(plan, devices, check_reachability=False):
    """Check the AI plan against the real router data.

    Returns {"errors": [...], "warnings": [...]}.
    Errors block approval. Warnings are things the AI flagged as unclear.
    """
    errors = []
    warnings = [str(a) for a in plan.get("ambiguities", [])]
    routers = devices["routers"]
    addresses = plan.get("addresses", [])
    plan_names = {d["name"] for d in plan.get("devices", [])}

    # 1. every address must be valid and sit inside its network
    for a in addresses:
        try:
            ip = ipaddress.ip_address(a["ip"])
            net = ipaddress.ip_network(a["network"])
            if ip not in net:
                errors.append(a["device"] + ": " + a["ip"] + " is not inside " + a["network"])
        except (ValueError, KeyError):
            errors.append("invalid address entry in the plan: " + str(a))
    if errors:
        return {"errors": errors, "warnings": warnings}

    # 2. no duplicate IP addresses
    ips = [a["ip"] for a in addresses]
    if len(ips) != len(set(ips)):
        errors.append("duplicate IP address in the plan")

    # 3. router IDs must be valid IPv4 addresses and unique
    ids = []
    for name, router in routers.items():
        try:
            ipaddress.IPv4Address(str(router["router_id"]))
            ids.append(str(router["router_id"]))
        except ValueError:
            errors.append(name + ": router_id is not a valid IPv4 address")
    if len(ids) != len(set(ids)):
        errors.append("router IDs are not unique")

    for name, router in routers.items():
        # 4. the router must appear in the plan
        if name not in plan_names:
            errors.append(name + ": router not found in the AI plan")
            continue

        # 5. the interface map must match the links in the plan
        neighbors_in_plan = set()
        for link in plan.get("links", []):
            if link["from"] == name:
                neighbors_in_plan.add(link["to"])
            if link["to"] == name:
                neighbors_in_plan.add(link["from"])
        mapped = set(router["interfaces"])
        for n in sorted(neighbors_in_plan - mapped):
            errors.append(name + ": no interface mapped for the link to " + n)
        for n in sorted(mapped - neighbors_in_plan):
            errors.append(name + ": interface mapped to " + n + " but the plan has no such link")

        # 6. real interface names, and each link shares exactly one network
        my_nets = {a["network"] for a in addresses if a["device"] == name}
        for neighbor, if_name in router["interfaces"].items():
            if "<" in str(if_name) or not str(if_name).strip():
                errors.append(name + ": interface name for " + neighbor + " is not set")
            neighbor_nets = {a["network"] for a in addresses if a["device"] == neighbor}
            shared = my_nets & neighbor_nets
            if len(shared) == 0:
                errors.append(name + " and " + neighbor + ": the plan has no network that both share (does the diagram show their IP addresses?)")
            elif len(shared) > 1:
                errors.append(name + " and " + neighbor + ": the plan gives them more than one shared network")

        # 7. management access
        host = str(router.get("management_host", ""))
        if "<" in host or not host.strip():
            errors.append(name + ": management_host not set")
        elif check_reachability:
            port = int(router.get("ssh_port", 22))
            try:
                socket.create_connection((host, port), timeout=3).close()
            except OSError:
                errors.append(name + ": management host " + host + " is not reachable on port " + str(port))

    return {"errors": errors, "warnings": warnings}


if __name__ == "__main__":
    import json
    import yaml

    devices = yaml.safe_load(open("configs/devices.yaml", encoding="utf-8"))
    plan = json.load(open("tests/sample_plan.json", encoding="utf-8"))
    result = validate_plan(plan, devices)
    print("ERRORS:")
    for e in result["errors"]:
        print("  -", e)
    print("WARNINGS (flagged by the AI):")
    for w in result["warnings"]:
        print("  -", w)
