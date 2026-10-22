#!/usr/bin/env bash
# Build the WiCAN PRO bitz fork firmware. Runs ON FORGE (CT 110).
# v6 = wican-fw (jtruland fork) + meatpi-components (jtruland fork), both on
# the SAME branch name: BITZ_BRANCH (default bitz/v6-beta2; superseded ports
# are bitz/v6-alfa04 and bitz/v6). The components commit is the PIN in the
# top-level CMakeLists.txt (_meatpi_pin), never the branch tip.
# ESP-IDF 6.0.2 is REQUIRED for v6 (v5.5.2 is only for the old v4.51 tree).
set -euo pipefail
BRANCH="${BITZ_BRANCH:-bitz/v6-beta2}"
REPO="${BITZ_REPO:-/opt/builds/wican-fw-v6}"   # a fresh dir per base keeps the old build/
LOG="${BITZ_LOG:-/tmp/wican-v6-build.log}"
exec >>"$LOG" 2>&1
echo "=== $(date -Is) start ==="
if [[ ! -d $REPO/.git ]]; then
  git clone -q -b "$BRANCH" https://github.com/jtruland/wican-fw.git "$REPO"
fi
cd "$REPO"
git fetch -q origin "$BRANCH" && git checkout -q -B "$BRANCH" origin/"$BRANCH"
git fetch -q --tags https://github.com/meatpiHQ/wican-fw.git   # describe needs the v6 tags
git fetch -q --tags origin
# clone the components BEFORE anything else: a docker -w into a missing path
# creates it empty, and CMake then accepts the empty dir and skips its clone
if [[ ! -d components/meatpi/.git ]]; then
  rm -rf components/meatpi
  git clone -q -b "$BRANCH" https://github.com/jtruland/meatpi-components.git components/meatpi
fi
git -C components/meatpi fetch -q origin "$BRANCH"
PIN=$(sed -n 's/^ *set(_meatpi_pin "\([0-9a-f]*\)").*/\1/p' CMakeLists.txt | head -1)
if [[ -n $PIN ]]; then
  git -C components/meatpi checkout -q "$PIN"   # CMake refuses any other commit
else
  git -C components/meatpi checkout -q -B "$BRANCH" origin/"$BRANCH"
fi
echo "=== describe: $(git describe --tags --always --dirty) / components $(git -C components/meatpi rev-parse --short HEAD 2>/dev/null || echo not-yet-cloned) (pin ${PIN:-none}) ==="
RUN="git config --global --add safe.directory '*'"
echo "=== STEP host-tests ==="
for t in sleep_manager vpn_manager; do
  docker run --rm -v "$REPO":/project -w /project/components/meatpi/components/$t/host_test espressif/idf:v6.0.2 \
    bash -c "$RUN; rm -rf build sdkconfig; idf.py --preview set-target linux >/dev/null && idf.py build >/dev/null && timeout 60 ./build/${t}_host_test.elf; true" \
    > /tmp/hosttest-$t.log 2>&1
  # the linux-target ELF keeps FreeRTOS running after app_main: it never exits
  # on its own, so the timeout ends it and the summary is read from the log
  grep -E '^[0-9]+ Tests|:FAIL|error:' /tmp/hosttest-$t.log | sed "s/^/HOSTTEST $t: /" || echo "HOSTTEST $t: no summary (see /tmp/hosttest-$t.log)"
done
echo "=== STEP firmware ==="
docker run --rm -v "$REPO":/project -w /project espressif/idf:v6.0.2 bash -c "$RUN; idf.py build" | grep -E 'error|warning: .*bitz|FAILED|Project build complete|binary|\.bin' | tail -40
echo "=== BUILD EXIT ${PIPESTATUS[0]} ==="
ls -la build/*.bin 2>/dev/null | tail -3
echo "=== $(date -Is) done ==="
