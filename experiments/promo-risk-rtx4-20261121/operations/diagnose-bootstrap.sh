set -u
journalctl -u karpo-promo-bootstrap-peer2-v1 -u karpo-promo-bootstrap-peer3-v1 -n 35 --no-pager
for peer in 172.31.42.209 172.31.37.239; do
 ssh -i /opt/karpo/.ssh/promo-rtx21 -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/opt/karpo/.ssh/promo-known-hosts ubuntu@$peer 'sudo ls -la /opt/karpo; sudo systemctl list-unit-files --no-legend "karpo*.timer"; echo list_rc=$?; df -h /; command -v rsync'
done
