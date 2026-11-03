# Build with: pyinstaller energy_monitor.spec
from pathlib import Path

project_root = Path(SPECPATH)
src_root = project_root / "src"

a = Analysis(
    [str(src_root / "energy_monitor" / "__main__.py")],
    pathex=[str(src_root)],
    datas=[
        (str(project_root / "config" / "sources.json"), "config"),
        (str(src_root / "energy_monitor" / "assets"), "energy_monitor/assets"),
    ],
    hiddenimports=[],
    name="energy_monitor",
)

pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="energy_monitor",
    console=False,
)
