"""Build the pinned x64 Opus DLL with VS 2022 C++ tools and uv-managed CMake."""

import hashlib
from pathlib import Path
import shutil
import subprocess
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.5.2"
SHA256 = "65c1d2f78b9f2fb20082c38cbe47c951ad5839345876e46941612ee87f9a7ce1"
URL = f"https://downloads.xiph.org/releases/opus/opus-{VERSION}.tar.gz"


def main() -> None:
	cache = ROOT / ".validation" / "opus"
	cache.mkdir(parents=True, exist_ok=True)
	archive = cache / f"opus-{VERSION}.tar.gz"
	if not archive.is_file():
		urllib.request.urlretrieve(URL, archive)
	if hashlib.sha256(archive.read_bytes()).hexdigest() != SHA256:
		raise ValueError(f"Opus source checksum mismatch: {archive}")
	with tarfile.open(archive) as sourceArchive:
		sourceArchive.extractall(cache, filter="data")
	source = cache / f"opus-{VERSION}"
	build = cache / "build-x64"
	cmake = ["uv", "tool", "run", "--from", "cmake==3.31.6", "cmake"]
	subprocess.run(
		cmake
		+ [
			"-S",
			str(source),
			"-B",
			str(build),
			"-G",
			"Visual Studio 17 2022",
			"-A",
			"x64",
			"-DOPUS_BUILD_SHARED_LIBRARY=ON",
			"-DOPUS_STATIC_RUNTIME=ON",
			"-DOPUS_BUILD_TESTING=OFF",
			"-DOPUS_BUILD_PROGRAMS=OFF",
			"-DOPUS_DRED=OFF",
			"-DOPUS_OSCE=OFF",
			"-DOPUS_HARDENING=ON",
		],
		check=True,
	)
	subprocess.run(cmake + ["--build", str(build), "--config", "Release", "--parallel"], check=True)
	destination = ROOT / "addon" / "globalPlugins" / "remotePlusPlus" / "lib"
	destination.mkdir(exist_ok=True)
	shutil.copyfile(build / "Release" / "opus.dll", destination / "opus.dll")
	shutil.copyfile(source / "COPYING", destination / "COPYING.opus")
	print(f"Built Opus {VERSION}: {destination / 'opus.dll'}")


if __name__ == "__main__":
	main()
