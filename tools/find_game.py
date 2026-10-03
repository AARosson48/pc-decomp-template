#!/usr/bin/env python3
"""Copy the retail executable out of a Steam or GOG install.

Reads config/project.json. Does not copy archives, movies, or audio.
The executable lands in orig/ and is gitignored.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys

try:
    import winreg
except ImportError:
    winreg = None

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CONFIG = os.path.join(ROOT, "config", "project.json")
LOCAL = os.path.join(ROOT, "project.local.json")


def load_config():
    with open(CONFIG, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    names = data.get("exe_names") or []
    if isinstance(names, str):
        names = [names]
    data["exe_names"] = [name for name in names if name]
    data["steam_app_id"] = str(data.get("steam_app_id") or "").strip()
    data["gog_game_id"] = str(data.get("gog_game_id") or "").strip()
    data["steam_install_folder"] = str(data.get("steam_install_folder") or "").strip()
    data["sha1"] = str(data.get("sha1") or "").strip().lower()
    if not data["exe_names"]:
        sys.exit("Set exe_names in config/project.json to the executable file name.")
    untouched = (
        data["exe_names"] == ["Game.exe"]
        and not data["steam_app_id"]
        and not data["gog_game_id"]
        and not data["steam_install_folder"]
    )
    if untouched:
        sys.exit("Edit config/project.json before searching. exe_names is still the placeholder.")
    return data


def _reg(hive, subkey, value_name):
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(hive, subkey) as key:
            value, _ = winreg.QueryValueEx(key, value_name)
    except OSError:
        return None
    return value or None


def _steam_roots():
    found = []
    steam = _reg(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Valve\Steam", "SteamPath") if winreg else None
    if steam:
        found.append(steam)
    for env_name in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(env_name)
        if base:
            found.append(os.path.join(base, "Steam"))
    seen = set()
    for root in found:
        norm = os.path.normcase(os.path.normpath(root))
        if norm in seen or not os.path.isdir(root):
            continue
        seen.add(norm)
        yield root


def _library_roots():
    for steam in _steam_roots():
        yield steam
        for rel in ("steamapps/libraryfolders.vdf", "config/libraryfolders.vdf"):
            vdf_path = os.path.join(steam, rel)
            if not os.path.isfile(vdf_path):
                continue
            with open(vdf_path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
            for match in re.finditer(r'"path"\s+"([^"]+)"', text):
                yield match.group(1).replace("\\\\", "\\")


def _has_exe(directory, names):
    if not directory:
        return None
    for name in names:
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            return path
    return None


def _manifest_dir(library, app_id):
    manifest = os.path.join(library, "steamapps", "appmanifest_%s.acf" % app_id)
    if not os.path.isfile(manifest):
        return None
    with open(manifest, "r", encoding="utf-8", errors="replace") as handle:
        text = handle.read()
    match = re.search(r'"installdir"\s+"([^"]+)"', text)
    if not match:
        return None
    return os.path.join(library, "steamapps", "common", match.group(1))


def candidates(config):
    names = config["exe_names"]
    app_id = config["steam_app_id"]
    gog_id = config["gog_game_id"]
    folder = config["steam_install_folder"]

    if os.environ.get("GAME_DIR"):
        yield os.environ["GAME_DIR"]
    if os.path.isfile(LOCAL):
        with open(LOCAL, "r", encoding="utf-8") as handle:
            saved = json.load(handle).get("game_dir")
        if saved:
            yield saved

    if winreg and app_id:
        uninstall = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\Steam App " + app_id
        wow = r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\Steam App " + app_id
        for subkey in (uninstall, wow):
            path = _reg(winreg.HKEY_LOCAL_MACHINE, subkey, "InstallLocation")
            if path:
                yield path

    if winreg and gog_id:
        for subkey in (
            "SOFTWARE\\GOG.com\\Games\\" + gog_id,
            "SOFTWARE\\WOW6432Node\\GOG.com\\Games\\" + gog_id,
        ):
            path = _reg(winreg.HKEY_LOCAL_MACHINE, subkey, "path")
            if path:
                yield path

    seen = set()
    for library in _library_roots():
        norm = os.path.normcase(os.path.normpath(library))
        if norm in seen:
            continue
        seen.add(norm)
        if app_id:
            yield _manifest_dir(library, app_id)
        if folder:
            yield os.path.join(library, "steamapps", "common", folder)
        common = os.path.join(library, "steamapps", "common")
        if os.path.isdir(common) and not app_id and not folder:
            for entry in os.listdir(common):
                yield os.path.join(common, entry)


def sha1_of(path):
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--game-dir", help="Install directory, if the search misses")
    args = parser.parse_args()
    config = load_config()
    found = None
    if args.game_dir:
        found = _has_exe(args.game_dir, config["exe_names"])
        if not found:
            sys.exit("None of %s is in %s" % (", ".join(config["exe_names"]), args.game_dir))
    else:
        for directory in candidates(config):
            found = _has_exe(directory, config["exe_names"])
            if found:
                break
    if not found:
        sys.exit(
            "Could not find %s. Set steam_app_id, gog_game_id, or steam_install_folder "
            "in config/project.json, or pass --game-dir." % ", ".join(config["exe_names"])
        )

    digest = sha1_of(found)
    pinned = config["sha1"]
    if pinned and digest != pinned:
        sys.exit("%s sha1 %s does not match config sha1 %s" % (os.path.basename(found), digest, pinned))

    dest_dir = os.path.join(ROOT, "orig")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, os.path.basename(found))
    shutil.copy2(found, dest)
    game_dir = os.path.dirname(found)
    with open(LOCAL, "w", encoding="utf-8") as handle:
        json.dump({"game_dir": game_dir, "exe": dest, "sha1": digest}, handle, indent=2)
        handle.write("\n")
    print("copied %s" % dest)
    print("sha1 %s" % digest)
    if not pinned:
        print("Pin that sha1 in config/project.json when this is the build you are matching.")
    print("game_dir saved to project.local.json")
    return dest, digest


if __name__ == "__main__":
    main()
