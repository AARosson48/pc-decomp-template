#!/usr/bin/env python3
"""Download the tools a matching PC decomp needs.

Ninja and Capstone come from pip. decomp-toolkit is the openblack PE build,
not the GameCube decomp-toolkit. objdiff is the CLI and the GUI. Binary Ninja
Free is the non-commercial installer from Vector35. MSVC 6 is the portable
tree at itsmattkc/MSVC600.
"""

import json
import os
import shutil
import subprocess
import sys
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BIN = os.path.join(ROOT, "tools", "bin")

DTK_URL = "https://github.com/openblack/decomp-toolkit/releases/download/v0.0.27/dtk-windows-x86_64.exe"
OBJDIFF_CLI_URL = "https://github.com/encounter/objdiff/releases/download/v3.8.2/objdiff-cli-windows-x86_64.exe"
OBJDIFF_GUI_URL = "https://github.com/encounter/objdiff/releases/download/v3.8.2/objdiff-windows-x86_64.exe"
MSVC_URL = "https://github.com/itsmattkc/MSVC600.git"


def download(url, dest):
    if os.path.isfile(dest) and os.path.getsize(dest) > 0:
        print("have %s" % os.path.relpath(dest, ROOT))
        return
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    print("get %s" % url)
    urllib.request.urlretrieve(url, dest)


def pip_install(*packages):
    subprocess.check_call([sys.executable, "-m", "pip", "install", *packages])


def binary_ninja_url():
    api = "https://api.github.com/repos/Vector35/binaryninja-api/releases?per_page=10"
    with urllib.request.urlopen(api) as handle:
        releases = json.load(handle)
    for release in releases:
        for asset in release.get("assets") or []:
            if asset.get("name") == "binaryninja_free_win64.exe":
                return asset["browser_download_url"]
    sys.exit("No binaryninja_free_win64.exe on the recent Vector35 releases.")


def clone_msvc():
    dest = os.path.join(ROOT, "msvc6")
    cl = os.path.join(dest, "VC98", "Bin", "cl.exe")
    if os.path.isfile(cl):
        print("have msvc6/VC98/Bin/cl.exe")
        return
    git = shutil.which("git")
    if not git:
        print("git is not on PATH. Clone %s into msvc6/ yourself." % MSVC_URL)
        return
    print("clone %s" % MSVC_URL)
    subprocess.check_call([git, "clone", "--depth", "1", MSVC_URL, dest])


def main():
    if sys.version_info < (3, 10):
        sys.exit("Python 3.10 or newer is required. This is %s." % sys.version.split()[0])
    if os.name != "nt":
        sys.exit("This setup downloads the Windows tools. Run it on Windows.")

    pip_install("ninja", "capstone")
    download(DTK_URL, os.path.join(BIN, "dtk.exe"))
    download(OBJDIFF_CLI_URL, os.path.join(BIN, "objdiff-cli.exe"))
    download(OBJDIFF_GUI_URL, os.path.join(BIN, "objdiff.exe"))
    download(binary_ninja_url(), os.path.join(BIN, "binaryninja_free_win64.exe"))
    clone_msvc()

    installer = os.path.join(BIN, "binaryninja_free_win64.exe")
    print("")
    print("Tools are in tools/bin. Ninja is on the Python Scripts directory.")
    print("MSVC 6 cl.exe is msvc6/VC98/Bin/cl.exe when the clone finishes.")
    print("Binary Ninja Free is non-commercial. Run the installer:")
    print("  %s" % installer)
    print("Then fill config/project.json and run: python tools/find_game.py")


if __name__ == "__main__":
    main()
