# ALB连接空闲期限排查

API到GPU连接池调整后，本轮风控仍出现一次非JSON的502，API未记录上游异常；CloudWatch对应分钟记录ALB生成的502。检查发现ALB空闲超时180秒，而API Uvicorn采用默认5秒keep-alive，存在负载均衡器复用已关闭连接的风险。已将API启动参数改为`--timeout-keep-alive 210`，保留原始失败记录，不自动重试。最新完整复测见[验证记录](../demo-rps-20261121/validation.json)。
