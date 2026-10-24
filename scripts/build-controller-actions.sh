#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
mkdir -p "$ROOT/build"
python3 "$ROOT/scripts/generate-controller-action-pins.py" "$ROOT/build/controller_action_pins.hpp"
"${CXX:-g++}" -std=c++17 -O2 -Wall -Wextra -Wpedantic -I "$ROOT/build" \
    "$ROOT/native/controller_actions.cpp" -ldl -pthread -o "$ROOT/build/frame-controller-actions"
