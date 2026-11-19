import logging
import os
import socket
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

import paramiko

from backend.generator import generate_config, load_devices, load_plan
from fake_ios import Blackhole, FakeIOS
from worker import deployer
from worker.deployer import DeploymentError, deploy_config

TMP = tempfile.mkdtemp(prefix="netauto_ssh_")
KNOWN = os.path.join(TMP, "known_hosts")
BACKUPS = os.path.join(TMP, "backups")
os.environ.update({"KNOWN_HOSTS_FILE": KNOWN, "BACKUP_DIR": BACKUPS, "SSH_TIMEOUT_SECONDS": "5"})
for name in ("SSH_USERNAME", "SSH_PASSWORD", "SSH_KEY_PATH", "SSH_ENABLE_PASSWORD", "SSH_KEY_PASSPHRASE", "MGMT_INTERFACES", "DRY_RUN"):
    os.environ.pop(name, None)

CONFIG = generate_config(load_plan(), load_devices(), "R1")
SECRETS = ["s3cret-pw", "wrong-pw", "en-pw-1", "wrong-enable"]

host_key = paramiko.RSAKey.generate(2048)
client_key = paramiko.RSAKey.generate(2048)
key_file = os.path.join(TMP, "id_test")
client_key.write_private_key_file(key_file)

results = []


def check(label, ok, extra=""):
    ok = bool(ok)
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + label + ((" | " + str(extra)[:150]) if extra else ""))


def set_creds(user="admin", password="s3cret-pw", key=None, enable=None):
    for n in ("SSH_USERNAME", "SSH_PASSWORD", "SSH_KEY_PATH", "SSH_ENABLE_PASSWORD"):
        os.environ.pop(n, None)
    os.environ["SSH_USERNAME"] = user
    if password:
        os.environ["SSH_PASSWORD"] = password
    if key:
        os.environ["SSH_KEY_PATH"] = key
    if enable:
        os.environ["SSH_ENABLE_PASSWORD"] = enable


def info(server, host="127.0.0.1"):
    return {"management_host": host, "ssh_port": server.port}


def expect_error(label, fn, *needles):
    start = time.time()
    try:
        out = fn()
        check(label, False, "no error, got: " + str(out)[:80])
        return None
    except DeploymentError as e:
        msg = str(e)
        ok = all(n.lower() in msg.lower() for n in needles) and not any(s in msg for s in SECRETS)
        check(label, ok, msg)
        return msg
    except Exception as e:
        check(label, False, "unexpected " + type(e).__name__ + ": " + str(e))
        return None
    finally:
        pass


# 1. password login
s = FakeIOS(host_key, users={"admin": "s3cret-pw"})
set_creds()
out = deploy_config("R1", CONFIG, info(s), "abcdef123456")
expected = [l.strip() for l in CONFIG.splitlines() if l.strip() and l.strip() != "!"]
check("password login: deployed and saved", "Deployed" in out and "Saved to startup-config" in out and s.saved, out.splitlines()[0])
check("every config line reached the router in order", s.config_lines[:len(expected)] == expected and s.config_lines[-1] == "end")
check("password never appears in the output", "s3cret-pw" not in out)
backups = os.listdir(BACKUPS)
check("running-config was backed up first", len(backups) == 1 and "hostname R1" in open(os.path.join(BACKUPS, backups[0])).read(), backups)
check("host key remembered (known_hosts written)", os.path.exists(KNOWN) and os.path.getsize(KNOWN) > 0)
s.stop()

# 2. key login (no password at all)
s = FakeIOS(host_key, users={"admin": "s3cret-pw"}, authorized_keys=[client_key])
set_creds(password=None, key=key_file)
out = deploy_config("R1", CONFIG, info(s), None)
check("key login works without a password", "Deployed" in out and s.saved)
s.stop()

# 3. wrong password
s = FakeIOS(host_key, users={"admin": "s3cret-pw"})
set_creds(password="wrong-pw")
expect_error("wrong password -> clear auth error, password not leaked", lambda: deploy_config("R1", CONFIG, info(s), None), "authentication failed", "admin")
check("nothing was configured after the auth failure", s.config_lines == [] and not s.saved)
s.stop()

# 4. connection refused (nothing listening)
probe = socket.socket(); probe.bind(("127.0.0.1", 0)); closed_port = probe.getsockname()[1]; probe.close()
set_creds()
expect_error("connection refused", lambda: deploy_config("R1", CONFIG, {"management_host": "127.0.0.1", "ssh_port": closed_port}, None), "127.0.0.1:" + str(closed_port))

# 5. router accepts TCP but never answers (hung)
bh = Blackhole()
os.environ["SSH_TIMEOUT_SECONDS"] = "2"
t0 = time.time()
expect_error("hung router -> timeout error, not a hang", lambda: deploy_config("R1", CONFIG, {"management_host": "127.0.0.1", "ssh_port": bh.port}, None), "127.0.0.1:" + str(bh.port))
check("hung router returned quickly", time.time() - t0 < 20, round(time.time() - t0, 1))
os.environ["SSH_TIMEOUT_SECONDS"] = "5"
bh.stop()

# 6. router rejects a line in the middle
s = FakeIOS(host_key, users={"admin": "s3cret-pw"})
set_creds()
bad = CONFIG.replace("router ospf 1", "bogus command\nrouter ospf 1")
msg = expect_error("router rejects a line -> FAILED with explanation", lambda: deploy_config("R1", bad, info(s), "abcdef123456"), "rejected", "bogus command", "NOT saved", "backup")
check("rejected deployment was NOT saved to startup-config", not s.saved)
check("session left config mode cleanly after the rejection", s.config_lines[-1] == "end")
s.stop()

# 7. dangerous lines are refused before any connection
s = FakeIOS(host_key, users={"admin": "s3cret-pw"})
before = s.connections
for dangerous in ["line vty 0 4", "ip route 10.2.0.0 255.255.255.0 10.1.0.2", "no ip ssh version 2", "crypto key zeroize rsa", "username x password y", "interface FastEthernet0/0\n shutdown"]:
    expect_error("refuses: " + dangerous.splitlines()[-1], lambda d=dangerous: deploy_config("R1", CONFIG + "\n" + d, info(s), None), "refused")
time.sleep(0.5)
check("no connection was made for refused configs", s.connections == before)

# 8. management interface protection
os.environ["MGMT_INTERFACES"] = "FastEthernet1/0, GigabitEthernet9/9"
expect_error("config touching a management interface is refused", lambda: deploy_config("R1", CONFIG, info(s), None), "management interface")
os.environ.pop("MGMT_INTERFACES")
s.stop()

# 9. misconfiguration messages
for n in ("SSH_USERNAME", "SSH_PASSWORD", "SSH_KEY_PATH"):
    os.environ.pop(n, None)
expect_error("no credentials set", lambda: deploy_config("R1", CONFIG, {"management_host": "127.0.0.1", "ssh_port": 22}, None), "credentials are not configured")
set_creds()
expect_error("placeholder management host", lambda: deploy_config("R1", CONFIG, {"management_host": "<R1 management IP>", "ssh_port": 22}, None), "management address")
set_creds(password=None, key=os.path.join(TMP, "missing_key"))
expect_error("missing key file", lambda: deploy_config("R1", CONFIG, {"management_host": "127.0.0.1", "ssh_port": 22}, None), "SSH_KEY_PATH")

# 10. user EXEC mode that needs 'enable'
s = FakeIOS(host_key, users={"admin": "s3cret-pw"}, unprivileged=True, enable_password="en-pw-1")
set_creds(enable="en-pw-1")
out = deploy_config("R1", CONFIG, info(s), None)
check("enable password handled (user mode -> privileged)", "Deployed" in out and s.saved)
set_creds(enable=None)
expect_error("enable password missing in .env", lambda: deploy_config("R1", CONFIG, info(s), None), "SSH_ENABLE_PASSWORD")
set_creds(enable="wrong-enable")
expect_error("wrong enable password", lambda: deploy_config("R1", CONFIG, info(s), None), "privileged mode")
s.stop()

# 11. old-IOS-style server: SHA-1 key exchange and ssh-rsa only
s = FakeIOS(host_key, users={"admin": "s3cret-pw"}, legacy=True)
set_creds()
os.remove(KNOWN)
out = deploy_config("R1", CONFIG, info(s), None)
check("legacy-only server (SHA-1 kex, ssh-rsa) connects", "Deployed" in out and s.saved, paramiko.__version__)
s.stop()

# 12. host key changed since first use
port = 0
s1 = FakeIOS(host_key, users={"admin": "s3cret-pw"})
port = s1.port
set_creds()
os.remove(KNOWN)
deploy_config("R1", CONFIG, info(s1), None)
s1.stop(); time.sleep(1)
s2 = FakeIOS(paramiko.RSAKey.generate(2048), port=port, users={"admin": "s3cret-pw"})
expect_error("changed host key is refused", lambda: deploy_config("R1", CONFIG, info(s2), None), "host key", "changed")
check("nothing configured on the router with the changed key", s2.config_lines == [])
s2.stop()

# 13. dry run
os.environ["DRY_RUN"] = "1"
out = deploy_config("R1", CONFIG, {"management_host": "203.0.113.9", "ssh_port": 22}, None)
check("DRY_RUN sends nothing", out.startswith("[DRY RUN]"), out)
os.environ.pop("DRY_RUN")

print("\nRESULT:", sum(results), "passed,", len(results) - sum(results), "failed")
import shutil
shutil.rmtree(TMP, ignore_errors=True)
