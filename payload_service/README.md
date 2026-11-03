# Lumen 分发后端

对外只暴露安装器；AI 主体（core）需令牌才能取回。

- `GET /`                 引导页
- `GET /Lumen-Setup.exe`  安装器本体
- `GET /core`             AI 主体，**必须**带 `X-Lumen-Token` 头，否则 403

令牌明文只存在于安装器内；服务端仅持有 sha256 摘要（`token_sha.txt`），
通过环境变量 `LUMEN_TOKEN_SHA256` 注入。
