# Lumen 分发后端

仿 Visual Studio Installer 的"引导器 + 受控资源"架构：**对外只发一个安装器，AI 主体程序与模型权重都不在网页上暴露直链，只能由安装器带令牌取回**。

## 对外端点

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/`                  | 引导页（只提供安装器下载） |
| GET  | `/Lumen-Setup.exe`   | 安装器本体 |
| GET  | `/w/<file>`          | 模型权重，**必须**带 `X-Lumen-Token` 头，否则 403；支持 `Range` 断点续传；防路径穿越 |
| GET  | `/manifest`          | 带令牌返回可用权重清单(JSON) |
| GET  | `/core` `/core.zip`  | （历史兼容）AI 主体程序，令牌校验 |

全站**不含任何 workbuddy 字样**；部署时域名也必须中性。

## 运行

```bash
# 把权重文件放到一个目录，例如 F:/LumenAI/models
cd payload_service
LUMEN_WEIGHTS_DIR="F:/LumenAI/models" \
LUMEN_TOKEN_SHA256="$(cat token_sha.txt)" \
python server.py            # 默认监听 0.0.0.0:8000
```

环境变量：
- `LUMEN_WEIGHTS_DIR`：权重目录（默认 `./weights`）。安装器只会请求这里存在的 `.gguf` 文件名。
- `LUMEN_TOKEN_SHA256`：令牌 sha256（默认读 `token_sha.txt`）。
- `PORT`：监听端口（默认 8000）。

> 令牌明文只存在于安装器内；服务端仅存 sha256 摘要。技术上令牌可被逆向提取，
> 因此这是**分发隐藏 + 访问控制**，不是绝对保密。

## 安装器如何指向本后端

安装器默认权重端点是占位符 `https://lumen-cdn.example/w`（会失败并回退到公开镜像）。
正式上线时用环境变量重打包，把端点改成你的域名：

```bash
LUMEN_WEIGHTS_ENDPOINT="https://你的域名/w" \
LUMEN_WEIGHTS_TOKEN="和 token_sha.txt 对应的明文令牌" \
pyinstaller --noconfirm --clean --distpath dist_setup --workpath build_setup \
           installer/build_installer.spec
```

> 若你**不想**做令牌后端，也可把 `LUMEN_WEIGHTS_ENDPOINT` 直接指向一个
> **不公开链接的对象存储直链**（国内 COS/OSS 公开桶 URL，网页上不出现），
> 安装器直接拉取即可——简单、免费、国内快，代价是"不可单独下载"靠"链接不公开"保证。

## 国内部署建议

要"国内快 + 中性域名 + 免费/低价"，任选其一（都需你提供账号）：

1. **Cloudflare（推荐，近乎免费、全球 CDN、中性 `*.workers.dev` / 自定义域名）**
   - 用 Cloudflare Workers/Pages Functions 实现 `/w/<file>` 的令牌校验 + R2 存储；
   - 或仅把 `Lumen-Setup.exe` 与权重放 R2 公开桶，安装器直链拉取。
2. **国内对象存储（腾讯云 COS / 阿里云 OSS 公开桶）**
   - 国内最快，通常需实名；把权重设为"私有/不公开列表"，用签名 URL 或直接不公开链接，
     安装器带 URL 拉取。
3. **轻量云主机（腾讯云轻量 / 阿里云 ECS）**
   - 直接 `python server.py` 跑这个后端，域名解析过去；最贴合"令牌受控后端"。

部署完成后：把重打包的 `Lumen-Setup.exe` 放进本目录（覆盖），并把门面 `download_site/index.html`
的下载按钮指向它即可。
