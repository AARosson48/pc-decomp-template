# PC decomp template

**Work in progress.** The builder and the local workbench run, and the matching flow is still changing.

A blank matching-decomp shell for a Windows game. Double-click `install.bat` to open PC Decomp Project Builder. The steps after a project exists are in [docs/walkthrough.md](docs/walkthrough.md).

This repository is a [GitHub template](https://docs.github.com/en/repositories/creating-and-managing-repositories/creating-a-repository-from-a-template). Use it to start a new project. It does not contain a game or decompiled source.

```text
config/project.json       which Steam or GOG install to search
config/dtk.yml            executable name and SHA1 for the splitter
config/splits.txt         address range of each unit
config/dtk_symbols.txt    function names
config/modules.txt        original source paths from the executable
src/                      C and C++
include/                  headers
orig/                     local executable, not committed
notes/                    scores
docs/walkthrough.md       the steps, in order
install.bat               opens PC Decomp Project Builder
builder_app.py            the window: tools, installed games, new project
tools/setup.py            downloads Ninja, objdiff, decomp-toolkit, Binary Ninja Free, MSVC 6
tools/find_game.py        copies the executable out of Steam or GOG
.github/workflows/        empty until a split builds
```

Double-click `install.bat`. The window lists Steam and GOG games on this PC. Install tools once, pick a game, and it creates a project folder with the executable and the SHA1 filled in. `install.py` is the same setup as a prompt, if you do not want the window.
