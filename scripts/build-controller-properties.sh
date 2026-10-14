#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
mkdir -p "$ROOT/build"
# Separate observer executable: does not rebuild, install or restart the input proxy.
"${CXX:-g++}" -std=c++17 -O2 -Wall -Wextra -Wpedantic \
    "$ROOT/native/controller_properties.cpp" -ldl -o "$ROOT/build/frame-controller-properties"
