# 把本机权重上传到 Cloudflare R2（免费额度内，断点续传）
# 前置（一次性）：
#   1) 安装 rclone：winget install Rclone.Rclone   （或 scoop install rclone）
#   2) 配置 R2 远端，把下面 <...> 换成你的 Cloudflare 账号信息：
#      rclone config create lumen-r2 s3 provider Cloudflare `
#        access_key_id "<R2_ACCESS_KEY_ID>" `
#        secret_access_key "<R2_SECRET_ACCESS_KEY>" `
#        endpoint "<ACCOUNT_ID>.r2.cloudflarestorage.com" region auto
#
# 运行本脚本即开始上传（已存在且大小一致的会自动跳过）：
rclone copy "F:/LumenAI/models" "lumen-r2:lumen-weights" `
  --include "*.gguf" `
  --progress --transfers 4 --check-first --retries 5

# 上传完成后，在 Cloudflare 控制台给 Worker 设置密钥：
#   wrangler secret put LUMEN_TOKEN
#   值填安装器内 WEIGHTS_TOKEN 明文：koCVPXeGIgAoUXJq7QTrjb6JN1xjEx3t36rmuJdY894
#
# 部署 Worker： wrangler deploy
# 安装器 WEIGHTS_ENDPOINT 改为： https://lumen-weights.<你的子域>.workers.dev/w
