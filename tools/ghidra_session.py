#!/usr/bin/env python3
"""One long-running Ghidra analyzeHeadless process per decomp project.

The first call analyzes the executable. After that, each address is decompiled
over a local socket. No Ghidra window is opened.
"""

import os
import re
import socket
import subprocess
import threading
import time

ANALYZING = (
    "Ghidra is analyzing this executable. The first function takes a few minutes.\n"
    "Later functions reuse that analysis."
)

_SESSIONS = {}
_SESSIONS_LOCK = threading.Lock()
_CREATE_NO_WINDOW = 0x08000000


def decompile(project_root, exe, addr):
    return _session(project_root, exe).decompile(addr)


def split_function(project_root, exe, start, end):
    return _session(project_root, exe).split(start, end)


def _session(project_root, exe):
    with _SESSIONS_LOCK:
        session = _SESSIONS.get(project_root)
        if session is None or session.exe != exe:
            session = GhidraSession(project_root, exe)
            _SESSIONS[project_root] = session
        return session


class GhidraSession:
    def __init__(self, project_root, exe):
        self.root = project_root
        self.exe = os.path.abspath(exe)
        self.home = os.path.join(project_root, "build", "ghidra")
        self.port_file = os.path.join(self.home, "port")
        self.pid_file = os.path.join(self.home, "pid")
        self.log_path = os.path.join(self.home, "analyze.log")
        self.stamp_file = os.path.join(self.home, "script.stamp")
        self.lock = threading.Lock()
        self.proc = None
        self.failed = ""

    def decompile(self, addr):
        with self.lock:
            message = self._ensure()
            if message:
                return message
            port = self._port()
        try:
            return _ask(port, "0x%X" % addr)
        except (OSError, ValueError) as exc:
            return "Ghidra did not answer for this function.\n(%s)" % exc

    def split(self, start, end):
        with self.lock:
            message = self._wait_ready()
            if message:
                return message
            port = self._port()
        try:
            return _ask(port, "SPLIT %X %X" % (start, end))
        except (OSError, ValueError) as exc:
            return "Ghidra did not apply the split.\n(%s)" % exc

    def _wait_ready(self):
        deadline = time.time() + 120
        while time.time() < deadline:
            message = self._ensure()
            if not message and self._ready():
                return ""
            if self.failed:
                return self.failed
            time.sleep(1)
        return "Ghidra is still starting. Try this function again in a moment."

    def _ensure(self):
        if self._script_stale():
            self._stop()
        elif self.failed:
            return self.failed
        if self.proc is not None and self.proc.poll() is not None:
            self.failed = _failure(self.log_path)
            return self.failed
        if self._ready():
            return ""
        if self._running():
            return ANALYZING
        headless, java_home = _install()
        if not headless or not java_home:
            return (
                "Ghidra is not installed yet.\n"
                "Run tools/setup.py in the builder. It downloads Ghidra and JDK 21."
            )
        os.makedirs(self.home, exist_ok=True)
        if os.path.isfile(self.port_file):
            os.remove(self.port_file)
        lock = os.path.join(self.home, "game.lock")
        if os.path.isfile(lock):
            os.remove(lock)
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ghidra_scripts")
        cmd = [headless, self.home, "game"]
        if os.path.isfile(os.path.join(self.home, "game.gpr")):
            cmd.extend(["-process", os.path.basename(self.exe), "-noanalysis"])
        else:
            cmd.extend(["-import", self.exe, "-analysisTimeoutPerFile", "3600"])
        cmd.extend(["-scriptPath", script, "-postScript", "DecompileServer.java", self.port_file])
        env = os.environ.copy()
        env["JAVA_HOME"] = java_home
        env["PATH"] = os.path.join(java_home, "bin") + os.pathsep + env.get("PATH", "")
        log = open(self.log_path, "w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(
            cmd,
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=self.home,
            creationflags=_CREATE_NO_WINDOW,
        )
        with open(self.pid_file, "w", encoding="utf-8") as handle:
            handle.write(str(self.proc.pid))
        with open(self.stamp_file, "w", encoding="utf-8") as handle:
            handle.write("ok\n")
        return ANALYZING

    def _script_stale(self):
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ghidra_scripts", "DecompileServer.java")
        if not os.path.isfile(script) or not os.path.isfile(self.stamp_file):
            return os.path.isfile(script) and self._running()
        return os.path.getmtime(script) > os.path.getmtime(self.stamp_file)

    def _stop(self):
        pid = None
        if self.proc is not None and self.proc.poll() is None:
            pid = self.proc.pid
        else:
            pid = _read_pid(self.pid_file)
        if pid and _alive(pid):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
        self.proc = None
        self.failed = ""
        for path in (self.port_file, self.pid_file, os.path.join(self.home, "game.lock")):
            if os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError:
                    pass

    def _ready(self):
        return self._running() and self._port() > 0

    def _port(self):
        if not os.path.isfile(self.port_file):
            return 0
        text = open(self.port_file, "r", encoding="utf-8", errors="replace").read().strip()
        if not text.isdigit():
            return 0
        return int(text)

    def _running(self):
        if self.proc is not None and self.proc.poll() is None:
            return True
        if self.proc is not None and self.proc.poll() is not None:
            self.failed = _failure(self.log_path)
            return False
        pid = _read_pid(self.pid_file)
        if pid and _alive(pid):
            return True
        return False

def _ask(port, line):
    with socket.create_connection(("127.0.0.1", port), timeout=90) as sock:
        sock.settimeout(90)
        sock.sendall((line + "\n").encode("ascii"))
        header = b""
        while b"\n" not in header:
            chunk = sock.recv(4096)
            if not chunk:
                raise OSError("closed")
            header += chunk
            if len(header) > 64 and b"\n" not in header:
                raise ValueError("bad response")
        line, rest = header.split(b"\n", 1)
        length = int(line.strip())
        body = rest
        while len(body) < length:
            chunk = sock.recv(length - len(body))
            if not chunk:
                break
            body += chunk
    return body.decode("utf-8", "replace")


def _install():
    roots = []
    env_ghidra = os.environ.get("GHIDRA_INSTALL_DIR") or ""
    if env_ghidra:
        roots.append(env_ghidra)
    here = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    roots.append(here)
    headless = ""
    for root in roots:
        found = _headless_under(os.path.join(root, "tools", "ghidra"))
        if not found and os.path.isfile(os.path.join(root, "support", "analyzeHeadless.bat")):
            found = os.path.join(root, "support", "analyzeHeadless.bat")
        if found:
            headless = found
            break
    java_home = _jdk(os.environ.get("JAVA_HOME") or "")
    if not java_home:
        java_home = _jdk_under(os.path.join(here, "tools", "jdk"))
    return headless, java_home


def _headless_under(path):
    if not os.path.isdir(path):
        return ""
    for dirpath, _dirs, files in os.walk(path):
        if "analyzeHeadless.bat" in files and os.path.basename(dirpath).lower() == "support":
            return os.path.join(dirpath, "analyzeHeadless.bat")
    return ""


def _jdk(home):
    java = os.path.join(home, "bin", "java.exe") if home else ""
    if not java or not os.path.isfile(java):
        return ""
    proc = subprocess.run([java, "-version"], capture_output=True, text=True)
    match = re.search(r'version "(\d+)', (proc.stderr or "") + (proc.stdout or ""))
    if match and int(match.group(1)) >= 21:
        return home
    return ""


def _jdk_under(path):
    if not os.path.isdir(path):
        return ""
    for dirpath, _dirs, files in os.walk(path):
        if "java.exe" in files and os.path.basename(dirpath).lower() == "bin":
            home = _jdk(os.path.dirname(dirpath))
            if home:
                return home
    return ""


def _read_pid(path):
    if not os.path.isfile(path):
        return 0
    text = open(path, "r", encoding="utf-8", errors="replace").read().strip()
    return int(text) if text.isdigit() else 0


def _alive(pid):
    import ctypes
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def _failure(log_path):
    text = ""
    if os.path.isfile(log_path):
        with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    errors = [line.strip() for line in text.splitlines() if "error:" in line.lower() or line.startswith("ERROR REPORT")]
    if errors:
        return "Ghidra stopped.\n" + "\n".join(errors[-6:])
    tail = text[-1200:].strip()
    if not tail:
        tail = "analyzeHeadless exited before it published a port."
    return "Ghidra stopped.\n" + tail

