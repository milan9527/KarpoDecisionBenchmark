set -eu
systemctl is-active karpo-rtx6-bootstrap-peer4 karpo-rtx6-bootstrap-peer5 || true
tail -n 4 /opt/karpo/rtx6-bootstrap/peer4.log
tail -n 4 /opt/karpo/rtx6-bootstrap/peer5.log
ps -eo pid,etime,pcpu,args | awk '/[r]sync .*karpo/ {print}'
