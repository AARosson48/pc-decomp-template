#!/usr/bin/env python3
"""List Windows games installed through Steam or GOG."""

import os
import re

try:
    import winreg
except ImportError:
    winreg = None

SKIP_NAMES = {
    "steamworks common redistributables",
    "steamworks shared",
}
SKIP_EXE = (
    "unins",
    "uninstall",
    "unitycrashhandler",
    "crashreport",
    "crashpad",
    "vc_redist",
    "dxsetup",
    "installscript",
    "dgvoodoo",
    "voodoo",
)


def _reg_open(hive, subkey):
    if winreg is None:
        return None
    try:
        return winreg.OpenKey(hive, subkey)
    except OSError:
        return None


def _reg_value(key, name):
    try:
        value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return value or None


def _steam_roots():
    found = []
    if winreg is not None:
        key = _reg_open(winreg.HKEY_CURRENT_USER, r"SOFTWARE\Valve\Steam")
        if key is not None:
            steam = _reg_value(key, "SteamPath")
            winreg.CloseKey(key)
            if steam:
                found.append(steam)
    for env_name in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(env_name)
        if base:
            found.append(os.path.join(base, "Steam"))
    seen = set()
    for root in found:
        if not root or not os.path.isdir(root):
            continue
        norm = os.path.normcase(os.path.normpath(root))
        if norm in seen:
            continue
        seen.add(norm)
        yield root


def _library_roots():
    seen = set()
    for steam in _steam_roots():
        yield steam
        for rel in ("steamapps/libraryfolders.vdf", "config/libraryfolders.vdf"):
            vdf_path = os.path.join(steam, rel)
            if not os.path.isfile(vdf_path):
                continue
            with open(vdf_path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
            for match in re.finditer(r'"path"\s+"([^"]+)"', text):
                library = match.group(1).replace("\\\\", "\\")
                norm = os.path.normcase(os.path.normpath(library))
                if norm in seen or not os.path.isdir(library):
                    continue
                seen.add(norm)
                yield library


def exes_in(directory):
    if not directory or not os.path.isdir(directory):
        return []
    found = []
    for name in os.listdir(directory):
        if not name.lower().endswith(".exe"):
            continue
        lowered = name.lower()
        if any(skip in lowered for skip in SKIP_EXE):
            continue
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            found.append(name)
    found.sort(key=str.lower)
    return found


def _prefer_exe(names, folder_name, directory):
    if not names:
        return ""
    stem = folder_name.lower().replace(" ", "").replace("-", "")
    for name in names:
        base = os.path.basename(name.replace("/", os.sep))
        if os.path.splitext(base)[0].lower().replace(" ", "") == stem:
            return name

    def size(name):
        path = os.path.join(directory, name.replace("/", os.sep))
        try:
            return os.path.getsize(path)
        except OSError:
            return 0

    return max(names, key=size)


def exes_near(directory):
    """Executables in the install folder, or one directory down when the root has none."""
    names = exes_in(directory)
    if names:
        return names
    skip_dirs = {"_commonredist", "redist", "directx", "dotnet", "support", "easyanticheat"}
    found = []
    if not os.path.isdir(directory):
        return found
    for entry in os.listdir(directory):
        if entry.lower() in skip_dirs:
            continue
        sub = os.path.join(directory, entry)
        if not os.path.isdir(sub):
            continue
        for name in exes_in(sub):
            found.append(entry + "/" + name)
    found.sort(key=str.lower)
    return found


def steam_games():
    games = []
    seen = set()
    for library in _library_roots():
        steamapps = os.path.join(library, "steamapps")
        if not os.path.isdir(steamapps):
            continue
        for entry in os.listdir(steamapps):
            if not (entry.startswith("appmanifest_") and entry.endswith(".acf")):
                continue
            path = os.path.join(steamapps, entry)
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
            name = re.search(r'"name"\s+"([^"]+)"', text)
            installdir = re.search(r'"installdir"\s+"([^"]+)"', text)
            app_id = re.search(r'"appid"\s+"(\d+)"', text)
            if not name or not installdir:
                continue
            title = name.group(1)
            if title.lower() in SKIP_NAMES:
                continue
            directory = os.path.join(steamapps, "common", installdir.group(1))
            norm = os.path.normcase(os.path.normpath(directory))
            if norm in seen or not os.path.isdir(directory):
                continue
            seen.add(norm)
            names = exes_near(directory)
            games.append({
                "name": title,
                "store": "Steam",
                "directory": directory,
                "app_id": app_id.group(1) if app_id else "",
                "gog_id": "",
                "folder": installdir.group(1),
                "exes": names,
                "exe": _prefer_exe(names, installdir.group(1), directory),
            })
    return games


def gog_games():
    if winreg is None:
        return []
    games = []
    seen = set()
    roots = (
        r"SOFTWARE\GOG.com\Games",
        r"SOFTWARE\WOW6432Node\GOG.com\Games",
    )
    for root in roots:
        key = _reg_open(winreg.HKEY_LOCAL_MACHINE, root)
        if key is None:
            continue
        try:
            count = winreg.QueryInfoKey(key)[0]
            for index in range(count):
                subname = winreg.EnumKey(key, index)
                sub = _reg_open(winreg.HKEY_LOCAL_MACHINE, root + "\\" + subname)
                if sub is None:
                    continue
                try:
                    directory = _reg_value(sub, "path") or _reg_value(sub, "PATH")
                    title = _reg_value(sub, "gameName") or _reg_value(sub, "GAMENAME") or subname
                finally:
                    winreg.CloseKey(sub)
                if not directory or not os.path.isdir(directory):
                    continue
                norm = os.path.normcase(os.path.normpath(directory))
                if norm in seen:
                    continue
                seen.add(norm)
                names = exes_near(directory)
                folder = os.path.basename(directory.rstrip("\\/"))
                games.append({
                    "name": title,
                    "store": "GOG",
                    "directory": directory,
                    "app_id": "",
                    "gog_id": subname,
                    "folder": folder,
                    "exes": names,
                    "exe": _prefer_exe(names, folder, directory),
                })
        finally:
            winreg.CloseKey(key)
    return games


def installed_games():
    games = steam_games() + gog_games()
    games.sort(key=lambda game: game["name"].lower())
    return games
