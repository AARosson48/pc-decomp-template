# Walkthrough

Do these in order. Each step fills one of the folders below. `build/`, `msvc6/`, `tools/bin/`, and the executable under `orig/` are created on your machine and stay out of git.

```text
config/project.json       Steam app id, GOG id, executable name
config/dtk.yml            executable path and SHA1 for the splitter
config/splits.txt         one address range per unit
config/dtk_symbols.txt    function names the diff can pair
config/modules.txt        original source paths recovered from the executable
src/                      C and C++ you compile
include/                  headers
orig/                     the executable, copied from Steam or GOG, not committed
notes/                    what you compiled and what objdiff scored
docs/                     this walkthrough
install.bat               asks for the game, then runs the two scripts below
tools/setup.py            downloads the tools
tools/find_game.py        finds the install and copies the executable
tools/bin/                dtk, objdiff, and the Binary Ninja installer
msvc6/                    MSVC 6 compiler tree, cloned by setup.py
build/base/obj/           objects split from the executable
build/src/                objects compiled from src/
.github/workflows/        CI, after a split exists
```

## 1. Run the installer

Python 3.10 or newer, on Windows. Git is required for the compiler clone. Double-click `install.bat` (or run `python install.py`).

It asks for the executable file name and one of: a Steam app id, a GOG game id, the folder under `steamapps/common`, or the install path. Then it downloads Ninja, Capstone, decomp-toolkit, objdiff, the Binary Ninja Free installer, and the MSVC 6 tree. It copies the executable into `orig/`, writes the SHA1 into `config/project.json` and `config/dtk.yml`, and opens the Binary Ninja installer. That edition is free for non-commercial use.

Archives, movies, and audio stay in the Steam or GOG install. `tools/setup.py` and `tools/find_game.py` are the same two steps, if you would rather run them yourself after editing `config/project.json`.

## 2. Open the executable

Open the file in `orig/` with Binary Ninja. Read asserts and path strings before naming files. When a string names an original `.cpp`, add that path to `config/modules.txt` and create the same relative path under `src/`. A range with no original path stays a `bank/00xxxxxx` slice of about 16KB in `config/splits.txt`.

`config/dtk_symbols.txt` gets one line per function once you know its start address and size. MSVC names need the leading underscore (`_fn_00401000`).

## 3. Split, compile, diff

`tools/bin/dtk.exe coff split` reads `config/dtk.yml` and writes one object per unit under `build/base/obj/`. That command needs a real `splits.txt`. An empty splits file will not produce objects.

Compile a `.cpp` with `msvc6/VC98/Bin/cl.exe /nologo /O2 /c`. Confirm the Rich header of your executable before you treat `cl` 12.00.8804 as the right compiler. The object you compile is the second input to objdiff. The split object is the first:

```sh
tools/bin/objdiff-cli.exe diff -1 build/base/obj/<unit>.o -2 build/src/<unit>.obj
```

A function counts as matched when that diff reports 100%. Write the percent, the size, and the compiler in `notes/`. A function under 100% can stay in `src/` when it builds. It does not add matched bytes.

Capstone, installed by `setup.py`, is for reading a short instruction sequence when a register or a compare does not match. A decompiler listing, including Binary Ninja, is a hint. The objdiff diff is the result.

## 4. CI

`.github/workflows/` is empty until the split and the compile both work. The workflow should check out the executable from a private repo (do not commit it), clone MSVC600, download `dtk` and `objdiff-cli` into `tools/bin/`, and upload `build/report.json`. decomp.dev reads that artifact. Add the workflow when `objdiff-cli report generate` succeeds locally.
