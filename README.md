# PC decomp template

A blank matching-decomp shell for a Windows game. The steps are in [docs/walkthrough.md](docs/walkthrough.md).

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
tools/setup.py            downloads Ninja, objdiff, decomp-toolkit, Binary Ninja Free, MSVC 6
tools/find_game.py        copies the executable out of Steam or GOG
.github/workflows/        empty until a split builds
```

`python tools/setup.py` then `python tools/find_game.py`. Fill `config/project.json` first.
