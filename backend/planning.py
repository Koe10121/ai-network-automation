"""Clean up the plan returned by the AI before it is validated.

Diagrams often show port labels next to a device, so the AI may write a link
endpoint as "R1:f0/0" or "PC1 e0" instead of "R1". This turns every endpoint
back into a plain device name and keeps the port label for information.
"""


def device_names(plan):
    return [d["name"] for d in plan.get("devices", []) if isinstance(d, dict) and isinstance(d.get("name"), str)]


def resolve_endpoint(text, names):
    """Return (device_name, port_label). Unknown text is returned unchanged."""
    text = str(text).strip()
    if text in names:
        return text, ""

    best = None
    for name in names:
        if text.lower().startswith(name.lower()):
            rest = text[len(name):]
            # the device name must end here: "R1:f0/0" matches R1, but "R10" must not match R1
            if rest == "" or not rest[0].isalnum():
                if best is None or len(name) > len(best):
                    best = name
    if best is None:
        return text, ""
    return best, text[len(best):].lstrip(" :-_/.").strip()


def normalize_plan(plan):
    """Return a copy of the plan whose links and addresses use plain device names."""
    names = device_names(plan)
    result = dict(plan)

    links = []
    for link in plan.get("links", []):
        if not isinstance(link, dict) or "from" not in link or "to" not in link:
            links.append(link)
            continue
        a, a_port = resolve_endpoint(link["from"], names)
        b, b_port = resolve_endpoint(link["to"], names)
        fixed = {"from": a, "to": b}
        if a_port:
            fixed["from_port"] = a_port
        if b_port:
            fixed["to_port"] = b_port
        links.append(fixed)
    result["links"] = links

    addresses = []
    for entry in plan.get("addresses", []):
        if isinstance(entry, dict) and isinstance(entry.get("device"), str):
            device, port = resolve_endpoint(entry["device"], names)
            entry = dict(entry, device=device)
            if port and "port" not in entry:
                entry["port"] = port
        addresses.append(entry)
    result["addresses"] = addresses

    return result
