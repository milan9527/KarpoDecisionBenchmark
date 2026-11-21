set -u
for peer in 172.31.42.209 172.31.37.239; do
 ssh -i /opt/karpo/.ssh/promo-rtx21 -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/opt/karpo/.ssh/promo-known-hosts ubuntu@$peer 'sudo systemctl show karpo-promo-weight-proof-v1 -p ActiveState -p MainPID; for pid in $(pgrep -f "[p]romo-hash-model.py"); do sudo cat /proc/$pid/io; done; vmstat 1 2'
done
