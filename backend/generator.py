import json
import ipaddress
import os
import yaml


def load_devices(path=None):
    path = path or os.getenv("DEVICES_FILE", "configs/devices.yaml")
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_plan(path="tests/sample_plan.json"):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def build_interfaces(plan, devices, router_name):
    router = devices["routers"][router_name]
    result = []

    my_networks = {a["network"]: a["ip"] for a in plan["addresses"] if a["device"] == router_name}

    for neighbor, if_name in router["interfaces"].items():
        neighbor_networks = {a["network"] for a in plan["addresses"] if a["device"] == neighbor}
        shared = [n for n in my_networks if n in neighbor_networks]
        if len(shared) != 1:
            raise ValueError(router_name + " and " + neighbor + " must share exactly one network")

        network = shared[0]
        result.append({
            "name": if_name,
            "ip": my_networks[network],
            "network": network,
            "passive": neighbor not in devices["routers"],
        })

    return result


def generate_config(plan, devices, router_name):
    router = devices["routers"][router_name]
    ospf = devices["ospf"]
    interfaces = build_interfaces(plan, devices, router_name)
    lines = []

    for item in interfaces:
        net = ipaddress.ip_network(item["network"])
        lines.append("interface " + item["name"])
        lines.append(" ip address " + item["ip"] + " " + str(net.netmask))
        lines.append(" no shutdown")
        lines.append("!")

    lines.append("router ospf " + str(ospf["process_id"]))
    lines.append(" router-id " + router["router_id"])

    for item in interfaces:
        net = ipaddress.ip_network(item["network"])
        lines.append(" network " + str(net.network_address) + " " + str(net.hostmask) + " area " + str(ospf["area"]))

    for item in interfaces:
        if item["passive"]:
            lines.append(" passive-interface " + item["name"])

    return "\n".join(lines)


if __name__ == "__main__":
    devices = load_devices()
    plan = load_plan()
    for name in ["R1", "R2"]:
        print("===== " + name + " =====")
        print(generate_config(plan, devices, name))