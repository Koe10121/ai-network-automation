"""Deploy a configuration to a Cisco IOS router over SSH with Paramiko.

Credentials are never in the code. They come from the environment (.env):
    SSH_USERNAME, SSH_KEY_PATH (preferred) or SSH_PASSWORD,
    SSH_KEY_PASSPHRASE and SSH_ENABLE_PASSWORD (optional), SSH_TIMEOUT_SECONDS.

Safety:
  * config lines that could lock you out of the router are refused before connecting
  * interfaces listed in MGMT_INTERFACES are never touched
  * the running-config is backed up to BACKUP_DIR before any change
  * 'write memory' only runs if every line was accepted, so a half-applied
    change can be undone by reloading the router (see the recovery notes in the README)
"""
import os
import re
import socket
import time
from datetime import datetime, timezone

import paramiko
from dotenv import load_dotenv

load_dotenv()


class DeploymentError(Exception):
    """A deployment problem. The message is shown to the administrator, so it never contains secrets."""


PROMPT_RE = re.compile(r"(?:^|\n)[^\s]+[>#]\s?$")
MORE_RE = re.compile(r"--\s?More\s?--\s*$")
# Command errors start with '% ' (percent and a space). Syslog lines start with '*' or '%FACILITY-'.
ERROR_RE = re.compile(r"^%\s(?!Warning|OSPF: Reload|OSPF: Router-ID)", re.MULTILINE)

BLOCKED_PATTERNS = [
    (r"^no\s+ip\s+ssh", "it would disable SSH and could lock you out of the router"),
    (r"^crypto\s+key", "it would change the SSH keys and could lock you out of the router"),
    (r"^line\s+", "it would change the login lines and could lock you out of the router"),
    (r"^transport\s+", "it would change the allowed login protocols and could lock you out of the router"),
    (r"^(no\s+)?username\s", "it would change user accounts and could lock you out of the router"),
    (r"^enable\s", "it would change the enable password and could lock you out of the router"),
    (r"^no\s+ip\s+address", "it would remove an IP address and could cut the management connection"),
    (r"^shutdown\b", "it would shut an interface down and could cut the management connection"),
    (r"^no\s+interface\b", "it would remove an interface and could cut the management connection"),
    (r"^no\s+router\s+ospf", "it would remove the OSPF routing process"),
    (r"^ip\s+route\b", "static routes are not allowed in this lab (OSPF must provide the routes)"),
    (r"^(reload|erase|delete|format|write\s+erase)\b", "it is a destructive command"),
]


def settings():
    try:
        timeout = float(os.getenv("SSH_TIMEOUT_SECONDS") or 10)
    except ValueError:
        timeout = 10.0
    return {
        "username": os.getenv("SSH_USERNAME", ""),
        "key_path": os.getenv("SSH_KEY_PATH", ""),
        "key_passphrase": os.getenv("SSH_KEY_PASSPHRASE") or None,
        "password": os.getenv("SSH_PASSWORD", ""),
        "enable_password": os.getenv("SSH_ENABLE_PASSWORD", ""),
        "timeout": timeout,
        "mgmt_interfaces": {x.strip().lower() for x in os.getenv("MGMT_INTERFACES", "").split(",") if x.strip()},
        "known_hosts": os.getenv("KNOWN_HOSTS_FILE", os.path.join("worker", "known_hosts")),
        "backup_dir": os.getenv("BACKUP_DIR", "backups"),
    }


def config_lines(config_text):
    """The commands to send: stripped, without blank lines and '!' separators."""
    return [line.strip() for line in config_text.splitlines() if line.strip() and line.strip() != "!"]


def check_config_safety(lines, mgmt_interfaces):
    for line in lines:
        for pattern, reason in BLOCKED_PATTERNS:
            if re.match(pattern, line, re.IGNORECASE):
                raise DeploymentError("Refused to deploy: the line '" + line + "' is blocked because " + reason + ".")
        match = re.match(r"^interface\s+(\S+)", line, re.IGNORECASE)
        if match and match.group(1).lower() in mgmt_interfaces:
            raise DeploymentError("Refused to deploy: the config touches " + match.group(1) + ", which is listed as a management interface (MGMT_INTERFACES).")


# ---------- talking to the router ----------

def read_until_prompt(channel, timeout, extra_stop=None):
    """Read router output until an IOS prompt appears. Handles '--More--' paging."""
    buffer = ""
    end = time.time() + timeout
    while time.time() < end:
        if channel.recv_ready():
            buffer += channel.recv(65535).decode("utf-8", errors="replace").replace("\r", "")
            if MORE_RE.search(buffer):
                channel.send(" ")
                buffer = MORE_RE.sub("", buffer)
                continue
            time.sleep(0.05)
            if channel.recv_ready():
                continue
            if extra_stop and extra_stop.search(buffer):
                return buffer
            if PROMPT_RE.search(buffer):
                return buffer
        else:
            if channel.closed:
                raise DeploymentError("The router closed the SSH session unexpectedly. Output so far: " + repr(buffer[-200:]))
            time.sleep(0.05)
    raise DeploymentError("Timed out waiting for the router prompt. Last output: " + repr(buffer[-200:]))


def run(channel, command, timeout):
    channel.send(command + "\n")
    return read_until_prompt(channel, timeout)


def last_prompt(output):
    lines = [line for line in output.splitlines() if line.strip()]
    return lines[-1].strip() if lines else ""


def connect(host, port, cfg):
    if cfg["key_path"] and not os.path.isfile(cfg["key_path"]):
        raise DeploymentError("The file in SSH_KEY_PATH does not exist.")

    client = paramiko.SSHClient()
    if os.path.exists(cfg["known_hosts"]):
        client.load_host_keys(cfg["known_hosts"])
    # Trust on first use: the first key seen is remembered, and a later change is refused.
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    kwargs = {
        "hostname": host,
        "port": port,
        "username": cfg["username"],
        "timeout": cfg["timeout"],
        "banner_timeout": cfg["timeout"],
        "auth_timeout": cfg["timeout"],
        "allow_agent": False,
        "look_for_keys": False,
    }
    if cfg["key_path"]:
        kwargs["key_filename"] = cfg["key_path"]
        if cfg["key_passphrase"]:
            kwargs["passphrase"] = cfg["key_passphrase"]
    if cfg["password"]:
        kwargs["password"] = cfg["password"]

    client.connect(**kwargs)

    folder = os.path.dirname(cfg["known_hosts"])
    if folder:
        os.makedirs(folder, exist_ok=True)
    client.save_host_keys(cfg["known_hosts"])
    return client


def save_backup(device_id, text, cfg, job_id):
    os.makedirs(cfg["backup_dir"], exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    name = device_id + "_" + (job_id[:8] + "_" if job_id else "") + stamp + ".cfg"
    path = os.path.join(cfg["backup_dir"], name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def get_target(device_id, router_info):
    """Check the credentials and management address. Returns (settings, host, port)."""
    cfg = settings()
    router_info = router_info or {}
    host = str(router_info.get("management_host") or "")
    port = int(router_info.get("ssh_port") or 22)

    if not cfg["username"] or not (cfg["key_path"] or cfg["password"]):
        raise DeploymentError("SSH credentials are not configured. Set SSH_USERNAME and SSH_KEY_PATH (or SSH_PASSWORD) in .env.")
    if not host or "<" in host:
        raise DeploymentError("No management address is set for " + device_id + " in configs/devices.yaml.")
    return cfg, host, port


def open_router_session(host, port, cfg, cmd_timeout):
    """Connect, open a shell, turn paging off and enter privileged mode. Returns (client, channel)."""
    client = connect(host, port, cfg)
    try:
        channel = client.invoke_shell(width=200, height=1000)
        channel.settimeout(cfg["timeout"])
        first = read_until_prompt(channel, cmd_timeout)

        run(channel, "terminal length 0", cmd_timeout)

        if last_prompt(first).endswith(">"):
            channel.send("enable\n")
            out = read_until_prompt(channel, cmd_timeout, extra_stop=re.compile(r"[Pp]assword:\s*$"))
            if re.search(r"[Pp]assword:\s*$", out):
                if not cfg["enable_password"]:
                    raise DeploymentError("The router asks for the enable password, but SSH_ENABLE_PASSWORD is not set in .env.")
                channel.send(cfg["enable_password"] + "\n")
                out = read_until_prompt(channel, cmd_timeout)
            if not last_prompt(out).endswith("#"):
                raise DeploymentError("Could not enter privileged mode (enable). Check SSH_ENABLE_PASSWORD or the user's privilege level.")
        return client, channel
    except Exception:
        client.close()
        raise


def translate_ssh_error(e, cfg, where):
    """Turn a Paramiko or socket error into a DeploymentError with a plain explanation."""
    if isinstance(e, paramiko.AuthenticationException):
        return DeploymentError("SSH authentication failed for user '" + cfg["username"] + "' on " + where + ". Check SSH_USERNAME and the key or password in .env.")
    if isinstance(e, paramiko.BadHostKeyException):
        return DeploymentError("The SSH host key of " + where + " changed since it was first seen. This could be an attack, or the router was rebuilt. If you trust the change, remove that host from " + cfg["known_hosts"] + ".")
    if isinstance(e, (socket.timeout, TimeoutError)):
        return DeploymentError("SSH connection to " + where + " timed out after " + str(cfg["timeout"]) + " seconds. Check the management address and that SSH is enabled on the router.")
    if isinstance(e, ConnectionRefusedError):
        return DeploymentError("SSH connection to " + where + " was refused. Check that SSH is enabled on the router.")
    if isinstance(e, paramiko.SSHException):
        text = str(e)
        if "banner" in text.lower() or "no existing session" in text.lower() or "timed out" in text.lower():
            return DeploymentError("The device at " + where + " did not complete the SSH handshake within " + str(cfg["timeout"]) + " seconds. The router may be hung, or that port is not an SSH server.")
        if "no acceptable" in text.lower() or "incompatible" in text.lower():
            return DeploymentError("SSH algorithm mismatch with " + where + " (" + text + "). The router may need different SSH algorithm settings.")
        return DeploymentError("SSH error talking to " + where + ": " + text)
    return DeploymentError("Could not connect to " + where + ": " + str(e))


# ---------- the deployment ----------

def deploy_config(device_id, config_text, router_info=None, job_id=None):
    """Push the config to one router and return the transcript.

    Raises DeploymentError with a clear message on any failure.
    The worker reports that message as the FAILED output.
    """
    lines = config_lines(config_text)

    # Optional dry run: nothing is sent anywhere.
    if os.getenv("DRY_RUN", "0") == "1":
        return "[DRY RUN] Nothing was sent to " + device_id + ". It would have applied " + str(len(lines)) + " config lines."

    cfg, host, port = get_target(device_id, router_info)

    check_config_safety(lines, cfg["mgmt_interfaces"])

    where = host + ":" + str(port)
    cmd_timeout = max(cfg["timeout"] * 2, 15)
    transcript = []
    client = None
    channel = None
    applied = 0

    try:
        client, channel = open_router_session(host, port, cfg, cmd_timeout)

        # back up the current configuration before changing anything
        backup_text = run(channel, "show running-config", cmd_timeout)
        backup_path = save_backup(device_id, backup_text, cfg, job_id)

        run(channel, "configure terminal", cmd_timeout)
        for line in lines:
            out = run(channel, line, cmd_timeout)
            transcript.append(out.strip())
            if ERROR_RE.search(out):
                try:
                    run(channel, "end", cmd_timeout)
                except Exception:
                    pass
                error_line = [l for l in out.splitlines() if ERROR_RE.match(l)][0]
                raise DeploymentError(
                    "The router rejected the line '" + line + "': " + error_line + " "
                    + str(applied) + " earlier line(s) are active in the running-config but were NOT saved to startup-config. "
                    + "To undo them, reload the router without saving, or restore the backup " + backup_path + "."
                )
            applied += 1

        run(channel, "end", cmd_timeout)
        saved = run(channel, "write memory", cmd_timeout)
        transcript.append(saved.strip())

        return (
            "Deployed " + str(applied) + " config lines to " + device_id + " (" + where + "). "
            + "Saved to startup-config. Backup of the previous running-config: " + backup_path + "\n"
            + "--- router transcript ---\n" + "\n".join(transcript)
        )[:20000]

    except DeploymentError:
        raise
    except (paramiko.SSHException, OSError) as e:
        raise translate_ssh_error(e, cfg, where)
    finally:
        if client is not None:
            client.close()
