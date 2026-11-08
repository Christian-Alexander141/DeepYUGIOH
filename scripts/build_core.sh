#!/usr/bin/env bash
# Builds ygopro-core (third_party/ygopro-core) as build/ocgcore/libocgcore.so
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
git -C "$ROOT" submodule update --init --recursive third_party/ygopro-core
BUILD="$ROOT/build/ocgcore"
if [ ! -f "$BUILD/build.ninja" ]; then
  (cd "$ROOT/third_party/ygopro-core" && uv run --project "$ROOT" meson setup "$BUILD" --buildtype=release -Ddefault_library=shared)
fi
ninja -C "$BUILD"
