# Lumen 免费权重分发（Cloudflare R2 + Workers）

免费、全球 CDN、中性域名（`*.workers.dev`）、令牌校验、不暴露任何文件列表或直链。
契合「对外只发安装器、权重不可单独下载、只能用安装器调用」的约束，且**零第三方产品标识**。

## 你需要提供（唯一 inputs）

1. 一个免费的 Cloudflare 账号（注册即用，无需付费）。
2. 在该账号下：
   - 建一个 R2 桶，名 `lumen-weights`；
   - 生成 **R2 API Token**（用于上传 29GB 权重）；
   - 记下 **Account ID**。

把以上三项（Account ID、R2 Access Key/Secret、桶名）交给操作者，即可：
上传权重 → 部署 Worker（令牌校验端点）→ 重打包安装器指向 `https://lumen-weights.<子域>.workers.dev/w`。

## 操作者步骤（已就绪，给 token 即可跑）

```bash
# 1) 上传权重（断点续传，约 29GB）
pwsh cf-dist/upload_weights.ps1        # 先按脚本里注释配置 rclone 的 lumen-r2 远端

# 2) 安装并登录 wrangler
npm i -g wrangler
wrangler login                         # 或 wrangler login --api-token <CF_API_TOKEN>

# 3) 设置 Worker 密钥（值 = 安装器内 WEIGHTS_TOKEN 明文）
cd cf-dist
wrangler secret put LUMEN_TOKEN        # 输入 koCVPXeGIgAoUXJq7QTrjb6JN1xjEx3t36rmuJdY894

# 4) 部署 Worker
wrangler deploy

# 5) 重打包安装器，把端点指向上面的 Worker 地址
LUMEN_WEIGHTS_ENDPOINT="https://lumen-weights.<子域>.workers.dev/w" \
LUMEN_WEIGHTS_TOKEN="koCVPXeGIgAoUXJq7QTrjb6JN1xjEx3t36rmuJdY894" \
pyinstaller --noconfirm --clean --distpath dist_setup --workpath build_setup \
           installer/build_installer.spec
```

## 依赖说明

- Cloudflare 免费额度：R2 10 GB 存储 + 每月千万级请求，足够 29GB 权重分发。
- 中国大陆连通性：走 Cloudflare 全球任播（香港/东京/新加坡等就近节点），比 GitHub 快，但非「境内 PoP」；
  若日后要「完全国内」极致速度，可平移到腾讯云 COS / 阿里云 OSS（需实名/付费），Worker 契约不变。
