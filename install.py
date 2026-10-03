#!/usr/bin/env python3
"""Ask which game this is, install the tools, and copy the executable.

Double-click install.bat. This writes config/project.json, downloads the
tools, copies the executable into orig/, and records its SHA1 in config/dtk.yml.
Splits and source are still written by hand. See docs/walkthrough.md.
"""

import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.join(ROOT, "config", "project.json")
DTK = os.path.join(ROOT, "config", "dtk.yml")
LOCAL = os.path.join(ROOT, "project.local.json")


def ask(label):
    return input(label + ": ").strip()


def write_project(exe_name, steam_app_id, gog_game_id, folder):
    data = {
        "exe_names": [exe_name],
        "steam_app_id": steam_app_id,
        "gog_game_id": gog_game_id,
        "steam_install_folder": folder,
        "sha1": "",
    }
    with open(PROJECT, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")


def pin_hash(exe_name, digest):
    with open(PROJECT, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    data["sha1"] = digest
    with open(PROJECT, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, indent=2)
        handle.write("\n")

    stem = os.path.splitext(exe_name)[0]
    with open(DTK, "r", encoding="utf-8") as handle:
        text = handle.read()
    text = re.sub(r"(?m)^name:.*$", "name: %s" % stem, text, count=1)
    text = re.sub(r"(?m)^object:.*$", "object: %s" % exe_name, text, count=1)
    text = re.sub(r'(?m)^hash:.*$', 'hash: "%s"' % digest, text, count=1)
    with open(DTK, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def run(script, *extra):
    subprocess.check_call([sys.executable, os.path.join(ROOT, "tools", script), *extra], cwd=ROOT)


def main():
    if sys.version_info < (3, 10):
        sys.exit("Python 3.10 or newer is required. This is %s." % sys.version.split()[0])
    if os.name != "nt":
        sys.exit("This installer is for Windows.")

    print("This sets up a matching decomp of one Windows executable.")
    print("Leave a line blank to skip it. You need the executable name and one way to find the install.")
    print("")
    exe_name = ask("Executable file name (Sum.exe)")
    steam_app_id = ask("Steam app id")
    gog_game_id = ask("GOG game id")
    folder = ask("Folder name under steamapps/common")
    game_dir = ask("Install folder, if you already know it")
    if not exe_name:
        sys.exit("The executable file name is required.")
    if not steam_app_id and not gog_game_id and not folder and not game_dir:
        sys.exit("Give a Steam app id, a GOG id, a steamapps/common folder, or an install folder.")

    write_project(exe_name, steam_app_id, gog_game_id, folder)
    print("")
    print("Downloading tools. The compiler clone and Binary Ninja installer are large.")
    run("setup.py")
    print("")
    print("Looking for %s." % exe_name)
    if game_dir:
        run("find_game.py", "--game-dir", game_dir)
    else:
        run("find_game.py")

    with open(LOCAL, "r", encoding="utf-8") as handle:
        saved = json.load(handle)
    pin_hash(exe_name, saved["sha1"])
    print("Recorded sha1 %s in config/project.json and config/dtk.yml." % saved["sha1"])

    installer = os.path.join(ROOT, "tools", "bin", "binaryninja_free_win64.exe")
    if os.path.isfile(installer):
        print("")
        print("Opening the Binary Ninja Free installer. That edition is free for non-commercial use.")
        os.startfile(installer)

    print("")
    print("The executable is in orig/. Open it in Binary Ninja.")
    print("Splits, symbols, and C are not written for you. Continue at docs/walkthrough.md from step 4.")


if __name__ == "__main__":
    main()
