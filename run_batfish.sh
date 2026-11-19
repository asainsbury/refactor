#!/bin/zsh
# Run real Batfish locally in Docker and collect the reference answers for the test bed.
#
#   run_batfish.sh            (from the repo root, or anywhere)
#
# Needs Docker Desktop (installed in /Applications). Batfish wants about 4 GB of RAM: in Docker Desktop
# > Settings > Resources, give it at least 6 GB. The image is about 1.5 GB to pull the first time.
# Nothing is written outside this folder: snapshot in testbed/, answers in out/batfish/.
set -euo pipefail
HERE="${0:A:h}"
cd "$HERE"
IMAGE="batfish/allinone:latest"
NAME="bflite-batfish"

# 1. Docker running?
if ! docker info >/dev/null 2>&1; then
  echo "Starting Docker Desktop..."
  open -a Docker
  for i in {1..60}; do docker info >/dev/null 2>&1 && break; sleep 5; done
  docker info >/dev/null 2>&1 || { echo "Docker did not start"; exit 1; }
fi

# 2. Test bed configs (generated, deterministic)
"${PYTHON:-python3}" testbed/generate.py

# 3. Batfish container (service on 9996/9997; the repo folder is mounted at /work)
docker pull "$IMAGE"
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" -p 9996:9996 -p 9997:9997 -v "$HERE":/work "$IMAGE" >/dev/null
echo "Batfish container started; waiting for the service..."

# 4. Collect the ten questions with pybatfish inside the container (allinone ships pybatfish)
PY=$(docker exec "$NAME" sh -c 'command -v python3 || command -v python')
docker exec "$NAME" "$PY" -c "import pybatfish" 2>/dev/null || docker exec "$NAME" "$PY" -m pip install -q pybatfish
docker exec "$NAME" "$PY" -c "import pybatfish, pandas; print('pybatfish', pybatfish.__version__, 'pandas', pandas.__version__)"
rm -rf out/batfish
docker exec "$NAME" "$PY" /work/collector/pybatfish_collect.py \
  --host localhost --snapshot /work/testbed/snapshot --out /work/out/batfish

# 5. Batfish's own view of memory while it held the snapshot
docker stats --no-stream --format '{{.Name}} mem={{.MemUsage}} cpu={{.CPUPerc}}' "$NAME" | tee out/batfish/docker_stats.txt

echo
echo "Reference answers in out/batfish. Leave the container running for more questions,"
echo "or stop it with: docker rm -f $NAME"
