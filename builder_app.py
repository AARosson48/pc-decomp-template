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
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "tools"))

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
    "src/.gitkeep",
    "include/.gitkeep",
    "notes/.gitkeep",
    ".github/workflows/.gitkeep",
    "orig/.gitkeep",
    "tools/bin/.gitkeep",
)


def tool_status():
    cl = os.path.join(ROOT, "msvc6", "VC98", "Bin", "cl.exe")
    checks = (
        ("dtk", os.path.join(ROOT, "tools", "bin", "dtk.exe")),
        ("objdiff", os.path.join(ROOT, "tools", "bin", "objdiff-cli.exe")),
        ("MSVC 6", cl),
        ("Binary Ninja Free", os.path.join(ROOT, "tools", "bin", "binaryninja_free_win64.exe")),
    )
    ready = [name for name, path in checks if os.path.isfile(path)]
    return ready, [name for name, _ in checks]


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


def write_readme(dest, game, exe_name, digest):
    path = os.path.join(dest, "README.md")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("# %s\n\n" % game["name"])
        handle.write("Created by PC Decomp Project Builder.\n\n")
        handle.write("The executable is `orig/%s`.\n\n" % exe_name)
        handle.write("SHA1 `%s`.\n\n" % digest)
        handle.write("Shared tools stay in the builder at `%s`.\n\n" % ROOT)
        handle.write("Open the executable in Binary Ninja, then follow `docs/walkthrough.md` from step 2.\n")


def write_tools_json(dest):
    bin_dir = os.path.join(ROOT, "tools", "bin")
    data = {
        "builder": ROOT,
        "dtk": os.path.join(bin_dir, "dtk.exe"),
        "objdiff_cli": os.path.join(bin_dir, "objdiff-cli.exe"),
        "objdiff_gui": os.path.join(bin_dir, "objdiff.exe"),
        "msvc_root": os.path.join(ROOT, "msvc6"),
        "binary_ninja_installer": os.path.join(bin_dir, "binaryninja_free_win64.exe"),
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
        )
        self.game_list.pack(fill="both", expand=True, pady=(4, 0))
        self.game_list.bind("<<ListboxSelect>>", self._on_select)

        right = tk.Frame(body, bg=BG, width=280)
        right.pack(side="right", fill="y", padx=(16, 0))
        tk.Label(right, text="Executable", bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w")
        self.exe_pick = ttk.Combobox(right, state="readonly")
        self.exe_pick.pack(fill="x", pady=(4, 10))
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
        tk.Button(right, text="Refresh games", command=self.refresh_games, bg=PANEL, fg=TEXT, relief="flat", padx=12, pady=6).pack(fill="x", pady=(8, 0))

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
                    self.tools_button.configure(state="normal")
                    self.create_button.configure(state="normal")
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
        ready, names = tool_status()
        if len(ready) == len(names):
            self.tools_label.configure(text="Tools are installed in this builder.")
        elif ready:
            missing = [name for name in names if name not in ready]
            self.tools_label.configure(text="Still needed: %s" % ", ".join(missing))
        else:
            self.tools_label.configure(text="Tools are not installed yet.")

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
        self.exe_pick["values"] = game["exes"]
        self.exe_pick.set(game["exe"])
        self.name_var.set(game["name"])
        self.path_label.configure(text=game["directory"])

    def _browse(self):
        chosen = filedialog.askdirectory(initialdir=self.folder_var.get() or os.path.dirname(ROOT))
        if chosen:
            self.folder_var.set(chosen)

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
        game = self._selected()
        if not game:
            messagebox.showinfo("PC Decomp Project Builder", "Pick a game from the list.")
            return
        exe_rel = self.exe_pick.get().strip().replace("/", os.sep)
        if not exe_rel:
            messagebox.showinfo("PC Decomp Project Builder", "That install has no executable in its top folder. Pick another game or check the install.")
            return
        exe_name = os.path.basename(exe_rel)
        game_dir = game["directory"]
        nested = os.path.dirname(exe_rel)
        if nested:
            game_dir = os.path.join(game_dir, nested)
        parent = self.folder_var.get().strip()
        if not parent:
            messagebox.showinfo("PC Decomp Project Builder", "Choose a projects folder.")
            return
        dest = os.path.join(parent, slug(self.name_var.get() or game["name"]))
        if os.path.exists(dest):
            messagebox.showerror("PC Decomp Project Builder", "That folder already exists:\n%s" % dest)
            return
        self._busy()
        self._log("Creating %s" % dest)

        def work():
            try:
                os.makedirs(dest)
                copy_skeleton(dest)
                write_project(dest, game, exe_name)
                process = subprocess.Popen(
                    [sys.executable, os.path.join(dest, "tools", "find_game.py"), "--game-dir", game_dir],
                    cwd=dest,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                for line in process.stdout:
                    self.events.put(("log", line))
                code = process.wait()
                if code != 0:
                    self.events.put(("error", "Could not copy the executable. See the log."))
                    return
                local = os.path.join(dest, "project.local.json")
                with open(local, "r", encoding="utf-8") as handle:
                    saved = json.load(handle)
                pin_hash(dest, exe_name, saved["sha1"])
                write_tools_json(dest)
                write_readme(dest, game, exe_name, saved["sha1"])
                self.events.put(("log", "Project ready at %s" % dest))
                self.events.put(("log", "SHA1 %s" % saved["sha1"]))
                os.startfile(dest)
            except Exception as exc:
                self.events.put(("error", str(exc)))
            finally:
                self.events.put(("idle", None))

        threading.Thread(target=work, daemon=True).start()


def main():
    if sys.version_info < (3, 10):
        sys.exit("Python 3.10 or newer is required.")
    if os.name != "nt":
        sys.exit("PC Decomp Project Builder runs on Windows.")
    app = Builder()
    app.mainloop()


if __name__ == "__main__":
    main()
