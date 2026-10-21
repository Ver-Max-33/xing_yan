# PyInstaller spec of the backend (spec §3.3, §3.5): one directory, the `wb` command line.
#   cd backend && uv run --group build pyinstaller packaging/wb.spec --noconfirm --distpath dist --workpath build
# Registries fill themselves by importing every module of their package (wb.registry.autoload), which PyInstaller
# cannot see: every wb module is collected. Modules with numba's cache=True need their source on disk, so wb is kept
# as .py next to its bytecode. Data files: migrations, market profiles, built-in rules; time zones, pinyin, calendars.
# PA_Agent's vendored core (pa/NOTICE.md, AGPL) reads its prompts and our patch from beside its package at run time:
# pa_agent/config/paths.py climbs three levels from its own file, which in the bundle is the bundle's root.
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules, copy_metadata

PA = Path(SPECPATH).parent / "pa"

hidden = (collect_submodules("wb") + collect_submodules("uvicorn") + collect_submodules("pa_agent")
          + ["keyring.backends.macOS", "keyring.backends.Windows", "keyring.backends.null", "keyring.backends.chainer"])
datas = (collect_data_files("wb", excludes=["**/__pycache__/**"]) + collect_data_files("tzdata")
         + collect_data_files("pypinyin") + collect_data_files("exchange_calendars") + collect_data_files("curl_cffi")
         + copy_metadata("keyring") + copy_metadata("wb")
         + [(str(PA / "prompt_engineering"), "prompt_engineering"), (str(PA / "a_share.patch"), ".")])
binaries = collect_dynamic_libs("curl_cffi")

a = Analysis(
    ["wb_backend.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "hypothesis", "PyInstaller"],
    module_collection_mode={"wb": "pyz+py"},
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="wb-backend", console=True, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="wb-backend", upx=False)
