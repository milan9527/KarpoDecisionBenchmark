set -u
systemctl show karpo-promo-dual-v1 -p ActiveState -p SubState
nvidia-smi --query-gpu=index,name,memory.used,utilization.gpu --format=csv,noheader
for peer in 172.31.42.209 172.31.37.239; do
 ssh -i /opt/karpo/.ssh/promo-rtx21 -o BatchMode=yes -o ConnectTimeout=8 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/opt/karpo/.ssh/promo-known-hosts ubuntu@$peer 'nvidia-smi --query-gpu=name,memory.used,utilization.gpu --format=csv,noheader; sudo systemctl list-units "karpo-promo*" --no-pager'
done
