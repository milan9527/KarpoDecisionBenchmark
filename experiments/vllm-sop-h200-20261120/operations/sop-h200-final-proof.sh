set -u
date -u --iso-8601=seconds
nvidia-smi --query-gpu=index,name,memory.used,utilization.gpu,power.draw --format=csv
systemctl show karpo-sop-h200-v2.service --property=ActiveState,SubState,ExecMainStatus --no-pager
systemctl list-timers --all --no-pager
if test -e /run/systemd/shutdown/scheduled; then cat /run/systemd/shutdown/scheduled; else echo no_scheduled_shutdown; fi
cat /opt/karpo/experiments/vllm-sop-h200-20261120/results/setup/h200-suite-v2-exit.txt
