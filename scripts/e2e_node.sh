#!/bin/bash
# scripts/e2e_node.sh <service id> <label> "<prompt>"  — one run on a nodo node, as a client:
# execute (the node's free credit), converse with client/chat_client.py, kill, read the
# ledger (read-only; bench/ledger.py of celaut-basics/distributed-llama). Run as root on the
# node host; the client runs as the user nrtest. KEEP=1 keeps an instance whose run failed.
# Paths are the ones of the test host (/srv/om); change them for another host.
# A socat forwarder lets the guest reach the host Ollama (127.0.0.1:11434) at
# 192.168.200.1:11435 for the run; Ollama config is not changed.
set -u
SID=$1; LABEL=$2; PROMPT=$3
OUT=/srv/om/node-$LABEL; rm -rf $OUT; mkdir -p $OUT; chown nrtest $OUT
socat TCP-LISTEN:11435,bind=192.168.200.1,reuseaddr,fork TCP:127.0.0.1:11434 & FWD=$!
field() { python3 -c "
import json, re, sys
d = json.load(sys.stdin); d = d if isinstance(d, list) else d.get(\"instances\", [])
for i in d:
    if i.get(\"name\") == \"$LABEL\":
        if \"$1\" == \"ip\":
            m = re.search(r\"(\d+\.\d+\.\d+\.\d+):(\d+)\", json.dumps(i.get(\"ip\") or i)); print(m.group(1) + \":\" + m.group(2) if m else \"\")
        else: print(i.get(\"$1\", \"\"))
        break"; }
T0=$(date +%s.%N)
nodo execute --name "$LABEL" --no-input -e OPENMONTAGE_LLM_BASE_URL http://192.168.200.1:11435/v1 \
    -e OPENMONTAGE_LLM_MODEL qwen2.5:14b-instruct "$SID" < /dev/null > $OUT/exec.log 2>&1
T1=$(date +%s.%N)
TOKEN=$(nodo instances --json 2>/dev/null | field id)
ADDR=""; for i in $(seq 1 120); do ADDR=$(nodo instances --json 2>/dev/null | field ip); [ -n "$ADDR" ] && break; sleep 1; done
echo "token=$TOKEN addr=$ADDR exec_s=$(python3 -c "print(round($T1-$T0,1))")" > $OUT/info
# Memory of the VM process while the run lasts.
( while sleep 5; do P=$(pgrep -f "nodo-ch-${TOKEN:0:8}" | head -1); [ -n "$P" ] && ps -o rss= -p $P; done ) > $OUT/rss.txt & SAMPLER=$!
su nrtest -s /bin/bash -c "cd /srv/om/pack && /srv/om/venv/bin/python client/chat_client.py $ADDR \"$PROMPT\" --out $OUT/files" > $OUT/client.log 2>&1
echo client_rc=$? >> $OUT/info
kill $SAMPLER
nodo instances --json > $OUT/instances.json 2>&1
if grep -q client_rc=0 $OUT/info || [ -z "${KEEP:-}" ]; then nodo kill "$TOKEN" --json > $OUT/kill.log 2>&1; else echo kept >> $OUT/info; fi
sleep 3
python3 /srv/dl/repo/bench/ledger.py /nodo/storage/database.sqlite "$TOKEN" > $OUT/ledger.json 2>&1
kill $FWD
echo "max_rss_kb=$(sort -n $OUT/rss.txt | tail -1)" >> $OUT/info
echo DONE >> $OUT/info
