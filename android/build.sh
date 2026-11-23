#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alpar Sandor
# Builds build/adshut.apk with Ubuntu's packaged Android tools (no Gradle, no Google SDK).
# Needs: openjdk-17-jdk-headless android-sdk android-sdk-platform-23 dalvik-exchange
#
# The APK is signed with keystore/adshut.jks, created on the first build. Keep that file:
# updates of an installed app must be signed with the same key. ADSHUT_KEY_PASS sets its
# password (default: a development password, fine for a key that never leaves your machine).
set -euo pipefail
cd "$(dirname "$0")"

JDK=/usr/lib/jvm/java-17-openjdk-amd64/bin
PLATFORM=/usr/lib/android-sdk/platforms/android-23/android.jar
KEYSTORE=keystore/adshut.jks
PASS=${ADSHUT_KEY_PASS:-adshut}
OUT=build

rm -rf "$OUT"
mkdir -p "$OUT/classes" keystore

if [ ! -f "$KEYSTORE" ]; then
    "$JDK/keytool" -genkeypair -keystore "$KEYSTORE" -storepass "$PASS" -keypass "$PASS" \
        -alias adshut -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=adshut" >/dev/null 2>&1
fi

"$JDK/javac" -nowarn -source 8 -target 8 -bootclasspath "$PLATFORM" -Xlint:-options \
    -d "$OUT/classes" $(find src -name '*.java')
dalvik-exchange --dex --output="$OUT/classes.dex" "$OUT/classes"

aapt package -f -M AndroidManifest.xml -S res -I "$PLATFORM" -F "$OUT/unsigned.apk"
(cd "$OUT" && aapt add unsigned.apk classes.dex >/dev/null)
zipalign -f 4 "$OUT/unsigned.apk" "$OUT/aligned.apk"
apksigner sign --ks "$KEYSTORE" --ks-pass "pass:$PASS" --key-pass "pass:$PASS" \
    --out "$OUT/adshut.apk" "$OUT/aligned.apk"

ls -l "$OUT/adshut.apk"
