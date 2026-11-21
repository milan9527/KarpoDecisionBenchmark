set -u
for peer in 172.31.42.209 172.31.37.239; do
 ssh -i /opt/karpo/.ssh/promo-rtx21 -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/opt/karpo/.ssh/promo-known-hosts ubuntu@$peer 'nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv,noheader; ps -eo pid,etime,pcpu,pmem,stat,comm --sort=-pcpu | head -8; sudo tail -5 /opt/karpo/promo-validation-v1/experiments/promo-risk-rtx4-20261121/results/nodes/peer*/replica-*/server.log'
done
