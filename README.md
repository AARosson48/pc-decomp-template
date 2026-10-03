# PC decomp template

A blank matching-decomp shell for a Windows game. Fill in the game, then the scripts find the install and download the tools.

This repository is a [GitHub template](https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-repository-from-a-template). Use it to start a new project. It does not contain a game, a split, or decompiled source.

## 1. Name the executable

Edit `config/project.json`:

| Field | Example |
| --- | --- |
| `exe_names` | `["Sum.exe", "Summ.exe"]` |
| `steam_app_id` | `"2750"` |
| `gog_game_id` | `"1442823793"` |
| `steam_install_folder` | `"Summoner"` |
| `sha1` | leave empty until `find_game.py` prints the hash you want to pin |

Set the Steam app id, the GOG id, or the folder name under `steamapps/common`. `GAME_DIR` or `--game-dir` skips the search.

## 2. Install the tools

Python 3.10 or newer, on Windows, with Git available if you want the compiler clone.

```sh
python tools/setup.py
```

That installs Ninja and Capstone with pip, and downloads:

- decomp-toolkit 0.0.27 from [openblack/decomp-toolkit](https://github.com/openblack/decomp-toolkit). This is the build that splits a Windows PE. The GameCube decomp-toolkit does not.
- objdiff 3.8.2, both `objdiff-cli.exe` and the GUI, from [encounter/objdiff](https://github.com/encounter/objdiff).
- [Binary Ninja Free](https://binary.ninja/free/) for Windows. The license is non-commercial. The script downloads the installer and leaves it at `tools/bin/binaryninja_free_win64.exe` for you to run.
- [itsmattkc/MSVC600](https://github.com/itsmattkc/MSVC600) into `msvc6/`. `cl` 12.00.8804 with `/O2` is the usual match compiler for a late-90s or early-2000s MSVC 6 game. Confirm the Rich header of your executable before you treat that version as the one.

Downloaded tools and `msvc6/` stay out of git.

## 3. Copy the executable

```sh
python tools/find_game.py
```

The script looks at the Steam uninstall key, Steam's `libraryfolders.vdf`, each library's `appmanifest_<appid>.acf`, and the GOG registry key. It copies the executable to `orig/` and writes `project.local.json`. Archives stay in the install. If `sha1` is empty, the script prints the hash so you can pin it.

Passing `--game-dir` uses that folder when the search does not find the game.

`src/` is empty on purpose. Splits, symbols, and compiled units belong to the project you start from here, after the executable in `orig/` is the build you mean to match.
