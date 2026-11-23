#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
# Runs both implementations against spec/fixtures, plus the Python unit tests.
#   ./tests.sh
set -u
cd "$(dirname "$0")"
rc=0
echo "== Python (python/tests/test_parity.py)"
PYTHONPATH=python python3 -m unittest -v python/tests/test_parity.py 2>&1 | grep -E "^test_|^(OK|FAILED)" || rc=1
PYTHONPATH=python python3 -m unittest python/tests/test_parity.py >/dev/null 2>&1 || rc=1
echo "== Java (android/test/adshut/core/ParityTest.java)"
mkdir -p android/build/test
javac -nowarn -source 8 -target 8 -Xlint:-options -d android/build/test android/src/adshut/core/*.java android/test/adshut/core/*.java || exit 1
java -cp android/build/test adshut.core.ParityTest spec/fixtures || rc=1
echo "== $([ $rc = 0 ] && echo "all passed" || echo "FAILURES")"
exit $rc
