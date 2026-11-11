# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-dir candidate (plan 1.1, section 12.3): roast.exe (CLI/worker) + roast-gui.exe share one _internal.

UPX is off. Models, third-party engines, fonts and user data are never collected. Build with:
    python -m PyInstaller build/roast-onedir.spec --distpath dist --workpath build/work
The legacy single-file spec (build/windows.spec) is kept for the old candidate.
"""

from pathlib import Path

root = Path(SPECPATH).parent
tools = root / "app" / "tools"
datas = [
    (str(root / "app" / "config" / "roast_pipeline.example.json"), "defaults"),
    (str(root / "app" / "config" / "translation_style_prompt.txt"), "defaults"),
    (str(root / "app" / "config" / "translation_glossary.json"), "defaults"),
    (str(root / "app" / "config" / "dict_pre.txt"), "defaults"),
    (str(root / "app" / "config" / "dict_gpt.txt"), "defaults"),
    (str(root / "app" / "config" / "dict_after.txt"), "defaults"),
    (str(root / "schemas"), "schemas"),
]
hidden = ["upgrade", "upgrade.contracts", "upgrade.config", "upgrade.storage", "upgrade.resource_catalog", "upgrade.cli"]
excludes = ["torch", "paddle", "qwen_asr", "pywinauto", "win32gui", "PIL"]

cli_analysis = Analysis(
    [str(tools / "upgrade" / "cli.py")],
    pathex=[str(tools)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
gui_analysis = Analysis(
    [str(tools / "one_click_roast.py")],
    pathex=[str(tools)],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["torch", "paddle", "qwen_asr"],
    noarchive=False,
)

cli_pyz = PYZ(cli_analysis.pure)
gui_pyz = PYZ(gui_analysis.pure)

cli_exe = EXE(
    cli_pyz, cli_analysis.scripts, [],
    exclude_binaries=True, name="roast", console=True, debug=False, strip=False, upx=False,
    disable_windowed_traceback=False, argv_emulation=False,
)
gui_exe = EXE(
    gui_pyz, gui_analysis.scripts, [],
    exclude_binaries=True, name="roast-gui", console=False, debug=False, strip=False, upx=False,
    disable_windowed_traceback=False, argv_emulation=False,
)

COLLECT(
    cli_exe, cli_analysis.binaries, cli_analysis.datas,
    gui_exe, gui_analysis.binaries, gui_analysis.datas,
    strip=False, upx=False, name="AutoRoast",
)
