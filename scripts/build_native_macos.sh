#!/bin/sh
set -eu
TASK_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
mkdir -p "$TASK_ROOT/scripts/native/bin"
xcrun swiftc -O "$TASK_ROOT/scripts/native/macos_probe.swift" -o "$TASK_ROOT/scripts/native/bin/macos-probe"
xcrun swiftc -O "$TASK_ROOT/scripts/native/macos_ocr.swift" -o "$TASK_ROOT/scripts/native/bin/macos-ocr"
codesign --force --sign - "$TASK_ROOT/scripts/native/bin/macos-probe"
codesign --force --sign - "$TASK_ROOT/scripts/native/bin/macos-ocr"

xcrun swiftc -O "$TASK_ROOT/scripts/native/macos_watch.swift" -o "$TASK_ROOT/scripts/native/bin/macos-watch"
codesign --force --sign - "$TASK_ROOT/scripts/native/bin/macos-watch"

xcrun swiftc -O "$TASK_ROOT/scripts/native/macos_crop.swift" -o "$TASK_ROOT/scripts/native/bin/macos-crop"
codesign --force --sign - "$TASK_ROOT/scripts/native/bin/macos-crop"

xcrun swiftc -O "$TASK_ROOT/scripts/native/macos_dock.swift" -o "$TASK_ROOT/scripts/native/bin/macos-dock"
codesign --force --sign - "$TASK_ROOT/scripts/native/bin/macos-dock"
