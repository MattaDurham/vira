#!/bin/zsh
set -eu
cd "${0:A:h}/.."
# Standalone Command Line Tools ship Testing.framework outside Swift's default search path.
TEST_FRAMEWORKS="$(xcode-select -p)/Library/Developer/Frameworks"
if [[ -d "$TEST_FRAMEWORKS/Testing.framework" ]]; then
  swift test --disable-xctest -Xswiftc -F -Xswiftc "$TEST_FRAMEWORKS" -Xlinker "-F$TEST_FRAMEWORKS" -Xlinker -rpath -Xlinker "$TEST_FRAMEWORKS" -Xlinker -rpath -Xlinker "$TEST_FRAMEWORKS/../usr/lib"
else
  swift test --disable-xctest
fi
