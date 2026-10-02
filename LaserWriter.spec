from pathlib import Path

base = Path(SPECPATH)
a = Analysis(
    [str(base / 'start_laser_writer.py')],
    pathex=[str(base)],
    binaries=[],
    datas=[(str(base / 'LICENSE'), '.'),
           (str(base / 'THIRD_PARTY_NOTICES.md'), '.'),
           (str(base / 'third_party_licenses'), 'third_party_licenses')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={'matplotlib': {'backends': ['TkAgg', 'Agg']}},
    runtime_hooks=[],
    excludes=['pytest','IPython','notebook','black'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [],
          name='Laser Writer Open', icon='NONE',
          console=False, debug=False, strip=False, upx=False,
          bootloader_ignore_signals=False, disable_windowed_traceback=False)
