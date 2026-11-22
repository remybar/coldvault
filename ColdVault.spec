# Build natif : uv run --extra gui --group build pyinstaller ColdVault.spec
import sys
from pathlib import Path

root = Path(SPECPATH)
a = Analysis(
    [str(root / "tools/gui_entry.py")],
    pathex=[str(root / "src")],
    binaries=[],
    datas=[
        (str(root / "docs"), "docs"),
        (str(root / "tools/verify_manifest.py"), "tools"),
        (str(root / "LICENSE"), "."),
        (str(root / "packaging/licenses"), "licenses"),
    ],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ColdVault",
    debug=False,
    strip=False,
    upx=False,
    console=False,
)
collection = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="ColdVault")
if sys.platform == "darwin":
    app = BUNDLE(collection, name="ColdVault.app", bundle_identifier="org.coldvault.desktop")
