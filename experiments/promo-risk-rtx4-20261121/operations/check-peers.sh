set -u
for peer in 172.31.42.209 172.31.37.239; do
 ssh -i /opt/karpo/.ssh/promo-rtx21 -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=/opt/karpo/.ssh/promo-known-hosts ubuntu@$peer 'ps -eo pid,etime,pcpu,pmem,comm,args | grep -E "[p]ython|[v]llm" | head -10; sudo tail -10 /opt/karpo/promo-validation-v1/experiments/promo-risk-rtx4-20261121/results/setup/*adapter-tests.log; sudo systemctl list-units "karpo-promo*" --all --no-pager'
done
