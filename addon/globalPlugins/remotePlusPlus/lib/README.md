# Opus

This directory contains the x64 libopus 1.5.2 library at packaging time.
Source: https://downloads.xiph.org/releases/opus/opus-1.5.2.tar.gz
SHA-256: `65c1d2f78b9f2fb20082c38cbe47c951ad5839345876e46941612ee87f9a7ce1`

Run `uv run python tools/build_opus.py` from the repository root to build it
with Visual Studio 2022 C++ tools and the Windows SDK. The script obtains
CMake 3.31.6 through uv, verifies the source archive, and builds a Release DLL
with the static C runtime, hardening enabled, and DRED/OSCE disabled. It also
copies the upstream license into `COPYING.opus`.

The generated DLL is not committed. No additional runtime installation is
required. The upstream license and patent-license references are included in `COPYING.opus`.
