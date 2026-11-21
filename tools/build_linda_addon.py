"""Package the Linux companion with the shared codec and transport sources."""

from pathlib import Path
import sys
from zipfile import ZipFile, ZIP_DEFLATED


def build(output):
	root = Path(__file__).resolve().parents[1]
	companion = root / "extras/lindaRemoteAudio"
	plugin = "globalPlugins/lindaRemoteAudio"
	with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
		archive.write(companion / "manifest.ini.tpl", "manifest.ini")
		archive.write(companion / plugin / "__init__.py", f"{plugin}/__init__.py")
		for name in ("audioCodec.py", "audioTransport.py"):
			archive.write(root / "addon/globalPlugins/remotePlusPlus" / name, f"{plugin}/{name}")
		archive.write(root / "COPYING.txt", "COPYING.txt")


if __name__ == "__main__":
	build(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("lindaRemoteAudio-0.1.0.nvda-addon"))
