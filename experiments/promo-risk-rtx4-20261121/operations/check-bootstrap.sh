set -u
systemctl show karpo-promo-bootstrap-peer2-v1b karpo-promo-bootstrap-peer3-v1b karpo-promo-backup-v1 -p Id -p ActiveState -p SubState -p Result
ps -eo pid,etime,pcpu,comm,args | grep -E '[r]sync|[p]romo-peer.*bootstrap' | head -12
for peer in 172.31.42.209 172.31.37.239; do
 ssh -i /opt/karpo/.ssh/promo-rtx21 -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/opt/karpo/.ssh/promo-known-hosts ubuntu@$peer 'sudo du -sh /opt/karpo/venv-vllm034; nvidia-smi --query-gpu=name,memory.used --format=csv,noheader; sudo systemctl list-units "karpo-promo*" --no-pager'
done
