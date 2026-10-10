#!/usr/bin/env bash
# Build the WiCAN PRO bitz/v6 fork firmware. Runs ON FORGE (CT 110).
# v6 = wican-fw (jtruland fork, branch bitz/v6) + meatpi-components
# (jtruland fork, branch bitz/v6, auto-cloned by CMake into components/meatpi).
# ESP-IDF 6.0.2 is REQUIRED for v6 (v5.5.2 is only for the old v4.51 tree).
set -euo pipefail
REPO=/opt/builds/wican-fw-v6
LOG=/tmp/wican-v6-build.log
exec >>"$LOG" 2>&1
echo "=== $(date -Is) start ==="
if [[ ! -d $REPO/.git ]]; then
  git clone -q -b bitz/v6 https://github.com/jtruland/wican-fw.git "$REPO"
fi
cd "$REPO"
git fetch -q origin bitz/v6 && git checkout -q -B bitz/v6 origin/bitz/v6
git fetch -q --tags https://github.com/meatpiHQ/wican-fw.git   # describe needs the v6 tags
git fetch -q --tags origin
# clone the components BEFORE anything else: a docker -w into a missing path
# creates it empty, and CMake then accepts the empty dir and skips its clone
if [[ ! -d components/meatpi/.git ]]; then
  rm -rf components/meatpi
  git clone -q -b bitz/v6 https://github.com/jtruland/meatpi-components.git components/meatpi
fi
git -C components/meatpi fetch -q origin bitz/v6
git -C components/meatpi checkout -q -B bitz/v6 origin/bitz/v6
echo "=== describe: $(git describe --tags --always --dirty) / components $(git -C components/meatpi rev-parse --short HEAD 2>/dev/null || echo not-yet-cloned) ==="
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
