set -eu
tail -65 /opt/karpo/promo-opt-20261121/experiments/promo-opt-20261121/results/nodes/original-b8-powers-peer3/replica-3/server.log
journalctl -u karpo-opt-original-b8-powers-peer3 --no-pager -n 15 -o cat
ps -eo pid,ppid,stat,args | rg 'promo_opt|EngineCore|7412|8288' || true
