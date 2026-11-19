"""A tiny fake Cisco IOS SSH server for testing the deployer."""
import socket
import threading

import paramiko

FAKE_RUNNING_CONFIG = "Building configuration...\n\nCurrent configuration : 500 bytes\n!\nhostname R1\n!\ninterface FastEthernet0/0\n no ip address\n shutdown\n!\nend"


class FakeIOS(threading.Thread):
    def __init__(self, host_key, port=0, users=None, authorized_keys=None, hostname="R1",
                 unprivileged=False, enable_password=None, legacy=False, show_outputs=None):
        super().__init__(daemon=True)
        self.show_outputs = show_outputs or {}   # command -> text, or a function returning text
        self.legacy = legacy
        self.host_key = host_key
        self.users = users or {}
        self.authorized = {k.get_base64() for k in (authorized_keys or [])}
        self.hostname = hostname
        self.unprivileged = unprivileged
        self.enable_password = enable_password
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(5)
        self.sock.settimeout(0.5)
        self.port = self.sock.getsockname()[1]
        self.running = True
        self.connections = 0
        self.commands = []      # every command line received
        self.config_lines = []  # lines received while in config mode
        self.saved = False
        self.start()

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except Exception:
            pass

    def run(self):
        while self.running:
            try:
                client, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.connections += 1
            threading.Thread(target=self.handle, args=(client,), daemon=True).start()

    def handle(self, client):
        transport = paramiko.Transport(client)
        if self.legacy:
            # behave like an old IOS: only SHA-1 key exchange and an ssh-rsa host key
            transport._preferred_kex = ("diffie-hellman-group14-sha1",)
            transport._preferred_keys = ("ssh-rsa",)
            transport._preferred_ciphers = ("aes128-ctr",)
            transport._preferred_macs = ("hmac-sha1",)
        transport.add_server_key(self.host_key)
        server = _Server(self)
        try:
            transport.start_server(server=server)
            channel = transport.accept(20)
            if channel is None:
                return
            server.shell_event.wait(10)
            self.shell(channel)
        except Exception:
            pass
        finally:
            try:
                transport.close()
            except Exception:
                pass

    def shell(self, channel):
        mode = "user" if self.unprivileged else "exec"
        context = ""  # "", "if", "router"
        awaiting_enable_password = False

        def prompt():
            if mode == "user":
                return self.hostname + ">"
            if mode == "exec":
                return self.hostname + "#"
            return self.hostname + "(config" + ("-" + context if context else "") + ")#"

        channel.send("\r\nFake IOS\r\n" + prompt())
        buffer = ""
        while True:
            data = channel.recv(1024)
            if not data:
                return
            buffer += data.decode("utf-8", errors="replace")
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip("\r").strip()

                if awaiting_enable_password:
                    awaiting_enable_password = False
                    if line == self.enable_password:
                        mode = "exec"
                        channel.send("\r\n" + prompt())
                    else:
                        channel.send("\r\n% Access denied\r\n" + prompt())
                    continue

                self.commands.append(line)
                out = line + "\r\n"  # echo, like a real terminal

                if mode == "user":
                    if line == "enable":
                        if self.enable_password:
                            channel.send(out + "Password: ")
                            awaiting_enable_password = True
                            continue
                        mode = "exec"
                    elif line == "terminal length 0":
                        pass
                    else:
                        out += "% Invalid input detected at '^' marker.\r\n"
                elif mode == "exec":
                    if line == "terminal length 0":
                        pass
                    elif line == "show running-config":
                        out += FAKE_RUNNING_CONFIG.replace("\n", "\r\n") + "\r\n"
                    elif line == "configure terminal":
                        mode, context = "config", ""
                        out += "Enter configuration commands, one per line.  End with CNTL/Z.\r\n"
                    elif line == "write memory":
                        self.saved = True
                        out += "Building configuration...\r\n[OK]\r\n"
                    elif line in self.show_outputs:
                        value = self.show_outputs[line]
                        text = value() if callable(value) else value
                        out += text.replace("\n", "\r\n") + "\r\n"
                    else:
                        out += "% Invalid input detected at '^' marker.\r\n"
                else:  # config modes
                    self.config_lines.append(line)
                    if line == "end":
                        mode, context = "exec", ""
                    elif line.startswith("interface "):
                        context = "if"
                    elif line.startswith("router ospf "):
                        context = "router"
                    elif line == "no shutdown":
                        out += "*Nov 13 10:00:00.000: %LINK-3-UPDOWN: Interface FastEthernet0/0, changed state to up\r\n"
                    elif line.startswith(("ip address ", "router-id ", "network ", "passive-interface ")):
                        pass
                    else:
                        out += "% Invalid input detected at '^' marker.\r\n"

                channel.send(out + prompt())


class _Server(paramiko.ServerInterface):
    def __init__(self, outer):
        self.outer = outer
        self.shell_event = threading.Event()

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def get_allowed_auths(self, username):
        return "password,publickey"

    def check_auth_password(self, username, password):
        if self.outer.users.get(username) == password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        if username in self.outer.users and key.get_base64() in self.outer.authorized:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_pty_request(self, channel, term, width, height, pixelwidth, pixelheight, modes):
        return True

    def check_channel_shell_request(self, channel):
        self.shell_event.set()
        return True


class Blackhole(threading.Thread):
    """Accepts TCP connections but never speaks SSH, to simulate a hung router."""

    def __init__(self):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.sock.settimeout(0.5)
        self.port = self.sock.getsockname()[1]
        self.running = True
        self.held = []
        self.start()

    def run(self):
        while self.running:
            try:
                client, _ = self.sock.accept()
                self.held.append(client)
            except socket.timeout:
                continue
            except OSError:
                return

    def stop(self):
        self.running = False
        for c in self.held:
            try:
                c.close()
            except Exception:
                pass
        self.sock.close()
