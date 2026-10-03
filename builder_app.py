#!/usr/bin/env python3
"""PC Decomp Project Builder.

One window: install the shared tools, pick a Steam or GOG game, and create a
project folder for it. Splits and C are still written in that project.
"""

import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import urllib.parse
import webbrowser
import tkinter as tk
from tkinter import filedialog, messagebox

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import banks  # noqa: E402
import library  # noqa: E402

BG = "#1b1e27"
PANEL = "#252836"
TEXT = "#e8e6e3"
MUTED = "#9aa0ad"
ACCENT = "#7aa2f7"
FIELD = "#12141a"

SKELETON = (
    "config/dtk.yml",
    "config/splits.txt",
    "config/dtk_symbols.txt",
    "config/modules.txt",
    "docs/walkthrough.md",
    ".gitignore",
    "tools/find_game.py",
    "tools/setup.py",
    "tools/library.py",
    "tools/banks.py",
    "src/.gitkeep",
    "include/.gitkeep",
    "notes/.gitkeep",
    ".github/workflows/.gitkeep",
    "orig/.gitkeep",
    "tools/bin/.gitkeep",
)


def _first_file(paths):
    for path in paths:
        if path and os.path.isfile(path):
            return path
    return ""


def find_tools():
    bin_dir = os.path.join(ROOT, "tools", "bin")
    dtk = _first_file([os.path.join(bin_dir, "dtk.exe")])
    objdiff = _first_file([os.path.join(bin_dir, "objdiff-cli.exe")])
    objdiff_gui = _first_file([os.path.join(bin_dir, "objdiff.exe")])
    msvc_roots = [
        os.environ.get("MSVC6_ROOT", ""),
        r"C:\projects\MSVC600",
        os.path.join(ROOT, "msvc6"),
    ]
    cl = _first_file([os.path.join(path, "VC98", "Bin", "cl.exe") for path in msvc_roots if path])
    program_files = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ]
    binary_ninja = _first_file(
        [os.path.join(path, "Vector35", "BinaryNinja", "binaryninja.exe") for path in program_files]
        + [os.path.join(bin_dir, "binaryninja_free_win64.exe")]
    )
    return {
        "dtk": dtk,
        "objdiff": objdiff,
        "objdiff_gui": objdiff_gui,
        "msvc": cl,
        "binary_ninja": binary_ninja,
    }


def slug(name):
    cleaned = re.sub(r'[<>:"/\\|?*]', "", name).strip().rstrip(".")
    return cleaned or "project"


def write_project(dest, game, exe_name):
    data = {
        "exe_names": [exe_name],
        "steam_app_id": game.get("app_id") or "",
        "gog_game_id": game.get("gog_id") or "",
        "steam_install_folder": game.get("folder") or "",
        "sha1": "",
    }
    path = os.path.join(dest, "config", "project.json")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


def pin_hash(dest, exe_name, digest):
    project = os.path.join(dest, "config", "project.json")
    with open(project, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    data["sha1"] = digest
    with open(project, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")
    dtk = os.path.join(dest, "config", "dtk.yml")
    with open(dtk, "r", encoding="utf-8") as handle:
        text = handle.read()
    stem = os.path.splitext(exe_name)[0]
    text = re.sub(r"(?m)^name:.*$", "name: %s" % stem, text, count=1)
    text = re.sub(r"(?m)^object:.*$", "object: %s" % exe_name, text, count=1)
    text = re.sub(r'(?m)^hash:.*$', 'hash: "%s"' % digest, text, count=1)
    with open(dtk, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def copy_skeleton(dest):
    for rel in SKELETON:
        src = os.path.join(ROOT, rel.replace("/", os.sep))
        target = os.path.join(dest, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.isfile(src):
            shutil.copy2(src, target)


def write_readme(dest, title, exe_name, digest):
    path = os.path.join(dest, "README.md")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("# %s\n\n" % title)
        handle.write("Created by PC Decomp Project Builder.\n\n")
        handle.write("The executable is `orig/%s`.\n\n" % exe_name)
        handle.write("SHA1 `%s`.\n\n" % digest)
        handle.write("Shared tools stay in the builder at `%s`.\n\n" % ROOT)
        handle.write("Open the executable in Binary Ninja, then follow `docs/walkthrough.md` from step 2.\n")


def sha1_of(path):
    import hashlib
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def copy_chosen_exe(dest, exe_path):
    orig = os.path.join(dest, "orig")
    os.makedirs(orig, exist_ok=True)
    for name in os.listdir(orig):
        if name.lower().endswith(".exe"):
            os.remove(os.path.join(orig, name))
    target = os.path.join(orig, os.path.basename(exe_path))
    shutil.copy2(exe_path, target)
    digest = sha1_of(target)
    with open(os.path.join(dest, "project.local.json"), "w", encoding="utf-8", newline="\n") as handle:
        json.dump({"game_dir": os.path.dirname(exe_path), "exe": target, "sha1": digest}, handle, indent=2)
        handle.write("\n")
    return digest


def write_tools_json(dest):
    found = find_tools()
    msvc_root = os.path.dirname(os.path.dirname(os.path.dirname(found["msvc"]))) if found["msvc"] else ""
    data = {
        "builder": ROOT,
        "dtk": found["dtk"],
        "objdiff_cli": found["objdiff"],
        "objdiff_gui": found["objdiff_gui"],
        "msvc_root": msvc_root,
        "binary_ninja": found["binary_ninja"],
    }
    with open(os.path.join(dest, "tools.json"), "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


class Builder(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PC Decomp Project Builder")
        self.geometry("820x640")
        self.minsize(720, 560)
        self.configure(bg=BG)
        self.games = []
        self.picked = None
        self.events = queue.Queue()
        self.busy = False
        self._build()
        self.after(100, self._drain)
        self.refresh_games()
        self.refresh_tools()

    def _build(self):
        header = tk.Frame(self, bg=BG)
        header.pack(fill="x", padx=20, pady=(16, 8))
        tk.Label(header, text="PC Decomp Project Builder", bg=BG, fg=TEXT, font=("Segoe UI", 18)).pack(anchor="w")
        tk.Label(
            header,
            text="Pick a game already on this PC. The builder makes a matching-decomp project for it.",
            bg=BG,
            fg=MUTED,
            font=("Segoe UI", 10),
        ).pack(anchor="w")

        tools = tk.Frame(self, bg=PANEL)
        tools.pack(fill="x", padx=20, pady=8)
        self.tools_label = tk.Label(tools, text="Tools", bg=PANEL, fg=TEXT, font=("Segoe UI", 10), anchor="w")
        self.tools_label.pack(side="left", fill="x", expand=True, padx=12, pady=10)
        self.tools_button = tk.Button(
            tools, text="Install tools", command=self.install_tools, bg=ACCENT, fg="#111", relief="flat", padx=12, pady=6
        )
        self.tools_button.pack(side="right", padx=12, pady=8)

        body = tk.Frame(self, bg=BG)
        body.pack(fill="both", expand=True, padx=20)
        left = tk.Frame(body, bg=BG)
        left.pack(side="left", fill="both", expand=True)
        tk.Label(left, text="Installed games", bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w")
        self.game_list = tk.Listbox(
            left, bg=FIELD, fg=TEXT, selectbackground=ACCENT, selectforeground="#111",
            highlightthickness=0, relief="flat", font=("Segoe UI", 10), activestyle="none",
            exportselection=False,
        )
        self.game_list.pack(fill="both", expand=True, pady=(4, 0))
        self.game_list.bind("<<ListboxSelect>>", self._on_select)

        right = tk.Frame(body, bg=BG, width=280)
        right.pack(side="right", fill="y", padx=(16, 0))
        tk.Label(right, text="Executable", bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w")
        exe_row = tk.Frame(right, bg=BG)
        exe_row.pack(fill="x", pady=(4, 10))
        self.exe_var = tk.StringVar()
        tk.Entry(exe_row, textvariable=self.exe_var, bg=FIELD, fg=TEXT, insertbackground=TEXT, relief="flat").pack(side="left", fill="x", expand=True, ipady=4)
        tk.Button(exe_row, text="...", command=self._browse_exe, bg=PANEL, fg=TEXT, relief="flat").pack(side="right", padx=(6, 0))
        tk.Label(right, text="Project name", bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w")
        self.name_var = tk.StringVar()
        tk.Entry(right, textvariable=self.name_var, bg=FIELD, fg=TEXT, insertbackground=TEXT, relief="flat").pack(fill="x", ipady=4, pady=(4, 10))
        tk.Label(right, text="Projects folder", bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w")
        folder_row = tk.Frame(right, bg=BG)
        folder_row.pack(fill="x", pady=(4, 10))
        self.folder_var = tk.StringVar(value=os.path.dirname(ROOT))
        tk.Entry(folder_row, textvariable=self.folder_var, bg=FIELD, fg=TEXT, insertbackground=TEXT, relief="flat").pack(side="left", fill="x", expand=True, ipady=4)
        tk.Button(folder_row, text="...", command=self._browse, bg=PANEL, fg=TEXT, relief="flat").pack(side="right", padx=(6, 0))
        self.path_label = tk.Label(right, text="", bg=BG, fg=MUTED, wraplength=250, justify="left", font=("Segoe UI", 8))
        self.path_label.pack(anchor="w", pady=(0, 12))
        self.create_button = tk.Button(
            right, text="Create project", command=self.create_project, bg=ACCENT, fg="#111", relief="flat", padx=12, pady=8
        )
        self.create_button.pack(fill="x")
        tk.Button(right, text="Open workbench", command=self.open_workbench, bg=PANEL, fg=TEXT, relief="flat", padx=12, pady=6).pack(fill="x", pady=(8, 0))
        tk.Button(right, text="Refresh games", command=self.refresh_games, bg=PANEL, fg=TEXT, relief="flat", padx=12, pady=6).pack(fill="x")

        tk.Label(self, text="Log", bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", padx=20, pady=(8, 0))
        self.log = tk.Text(self, height=8, bg=FIELD, fg=TEXT, relief="flat", font=("Consolas", 9), wrap="word")
        self.log.pack(fill="x", padx=20, pady=(4, 16))
        self.log.configure(state="disabled")

    def _log(self, line):
        self.log.configure(state="normal")
        self.log.insert("end", line.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _drain(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "log":
                    self._log(payload)
                elif kind == "tools":
                    self.refresh_tools()
                elif kind == "idle":
                    self.busy = False
                    self.create_button.configure(state="normal")
                    self.refresh_tools()
                elif kind == "workbench":
                    launch_workbench(payload)
                elif kind == "error":
                    messagebox.showerror("PC Decomp Project Builder", payload)
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def _busy(self):
        self.busy = True
        self.tools_button.configure(state="disabled")
        self.create_button.configure(state="disabled")

    def refresh_tools(self):
        found = find_tools()
        labels = (
            ("dtk", "dtk"),
            ("objdiff", "objdiff"),
            ("msvc", "MSVC 6"),
            ("binary_ninja", "Binary Ninja"),
        )
        missing = [label for key, label in labels if not found[key]]
        if not missing:
            self.tools_label.configure(text="Tools found on this PC.")
            self.tools_button.configure(text="Tools found", state="disabled")
        else:
            self.tools_label.configure(text="Still needed: %s" % ", ".join(missing))
            self.tools_button.configure(text="Install tools", state="normal")

    def refresh_games(self):
        self.games = library.installed_games()
        self.game_list.delete(0, "end")
        for game in self.games:
            self.game_list.insert("end", "%s    %s" % (game["name"], game["store"]))
        if not self.games:
            self._log("No Steam or GOG games were found.")

    def _selected(self):
        picked = self.game_list.curselection()
        if not picked:
            return None
        return self.games[picked[0]]

    def _on_select(self, _event):
        game = self._selected()
        if not game:
            return
        self.picked = game
        chosen = game["exe"]
        if chosen:
            self.exe_var.set(os.path.join(game["directory"], chosen.replace("/", os.sep)))
        else:
            self.exe_var.set("")
        self.path_label.configure(text=game["directory"])

    def open_workbench(self):
        title = self.name_var.get().strip()
        parent = self.folder_var.get().strip()
        dest = os.path.join(parent, slug(title)) if title and parent else ""
        if not os.path.isdir(dest):
            dest = filedialog.askdirectory(title="Decomp project")
        if dest:
            launch_workbench(dest)

    def _browse(self):
        chosen = filedialog.askdirectory(initialdir=self.folder_var.get() or os.path.dirname(ROOT))
        if chosen:
            self.folder_var.set(chosen)

    def _browse_exe(self):
        start = self.path_label.cget("text") or self.folder_var.get() or os.path.dirname(ROOT)
        chosen = filedialog.askopenfilename(
            initialdir=start,
            filetypes=(("Executable", "*.exe"), ("All files", "*.*")),
        )
        if chosen:
            self.exe_var.set(chosen)

    def install_tools(self):
        if self.busy:
            return
        self._busy()
        self._log("Installing tools. The compiler and Binary Ninja download are large.")

        def work():
            try:
                process = subprocess.Popen(
                    [sys.executable, os.path.join(ROOT, "tools", "setup.py")],
                    cwd=ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                for line in process.stdout:
                    self.events.put(("log", line))
                code = process.wait()
                if code != 0:
                    self.events.put(("error", "Tool install exited with code %s." % code))
                else:
                    installer = os.path.join(ROOT, "tools", "bin", "binaryninja_free_win64.exe")
                    if os.path.isfile(installer):
                        self.events.put(("log", "Opening the Binary Ninja Free installer."))
                        os.startfile(installer)
            except Exception as exc:
                self.events.put(("error", str(exc)))
            finally:
                self.events.put(("tools", None))
                self.events.put(("idle", None))

        threading.Thread(target=work, daemon=True).start()

    def create_project(self):
        if self.busy:
            return
        game = self._selected() or self.picked
        if not game:
            messagebox.showinfo("PC Decomp Project Builder", "Pick a game from the list.")
            return
        exe_path = self.exe_var.get().strip()
        if not os.path.isfile(exe_path):
            messagebox.showinfo("PC Decomp Project Builder", "Choose the game executable. You can type the path or use ....")
            return
        exe_name = os.path.basename(exe_path)
        title = self.name_var.get().strip()
        if not title:
            messagebox.showinfo("PC Decomp Project Builder", "Type a project name. The store title is not used.")
            return
        parent = self.folder_var.get().strip()
        if not parent:
            messagebox.showinfo("PC Decomp Project Builder", "Choose a projects folder.")
            return
        dest = os.path.join(parent, slug(title))
        updating = os.path.isdir(dest)
        self._busy()
        self._log("Updating %s" % dest if updating else "Creating %s" % dest)

        def work():
            try:
                os.makedirs(dest, exist_ok=True)
                if not updating:
                    copy_skeleton(dest)
                write_project(dest, game, exe_name)
                digest = copy_chosen_exe(dest, exe_path)
                pin_hash(dest, exe_name, digest)
                count = banks.write_splits(exe_path, os.path.join(dest, "config", "splits.txt"))
                self.events.put(("log", "Wrote %s banks to config/splits.txt" % count))
                dtk = find_tools()["dtk"]
                if dtk:
                    self.events.put(("log", "Splitting the executable"))
                    proc = subprocess.run(
                        [dtk, "-C", dest, "coff", "split", "--no-update", "config/dtk.yml", "build/base"],
                        capture_output=True,
                        text=True,
                    )
                    if proc.returncode != 0:
                        detail = (proc.stderr or proc.stdout or "dtk split failed").strip()
                        self.events.put(("log", detail))
                    else:
                        self.events.put(("log", "Split objects are in build/base"))
                write_tools_json(dest)
                write_readme(dest, title, exe_name, digest)
                self.events.put(("log", "Project ready at %s" % dest))
                self.events.put(("log", "Executable %s" % exe_name))
                self.events.put(("log", "SHA1 %s" % digest))
                os.startfile(dest)
                self.events.put(("workbench", dest))
            except Exception as exc:
                self.events.put(("error", str(exc)))
            finally:
                self.events.put(("idle", None))

        threading.Thread(target=work, daemon=True).start()


def launch_workbench(project):
    script = os.path.join(ROOT, "tools", "workbench.py")
    sock = socket.socket()
    running = False
    try:
        sock.connect(("127.0.0.1", 8765))
        running = True
    except OSError:
        running = False
    finally:
        sock.close()
    if not running:
        subprocess.Popen([sys.executable, script, project], cwd=ROOT)
    webbrowser.open("http://127.0.0.1:8765/?project=" + urllib.parse.quote(os.path.abspath(project)))


def main():
    if sys.version_info < (3, 10):
        sys.exit("Python 3.10 or newer is required.")
    if os.name != "nt":
        sys.exit("PC Decomp Project Builder runs on Windows.")
    app = Builder()
    app.mainloop()


if __name__ == "__main__":
    main()
