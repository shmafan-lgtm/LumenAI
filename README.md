# Lumen · 本地化 AI 桌面端

> 模型推理、文件读写、代码修改，全部在这台电脑上完成。
> 手机端只做三件事：发现你的电脑、看它的会话、在你授权后控制它。

**版本 1.0.0 · Windows · 无云端依赖**（联网搜索除外，且带引用来源）

---

## 0. 从 GitHub 下载

全部产物都挂在 **Releases** 页面，不在仓库正文里：

```
https://github.com/<你的账号>/Lumen/releases
```

| 文件 | 大小 | 用途 |
|---|---|---|
| `Lumen-Setup.exe` | ~136 MB | **安装向导**（推荐，Windows 10+） |
| `Lumen-Windows-x64.zip` | ~2.2 GB | 绿色版主程序，解压即用（含内置 3B 权重） |
| `LumenGuard-Windows-x64.zip` | ~8 MB | 守护服务（崩溃自动拉起） |
| `Lumen-release-v1.0.0.apk` | ~1.2 MB | 手机端 APP |

**关于限速**：GitHub Releases 的文件走 GitHub 自己的 CDN（`objects.githubusercontent.com`），
不是 Google Drive 那种要单独授权的网盘，**国内直连通常不限速**。两个已知例外：

- 单个附件上限 **2 GB** —— 所以模型权重不随包分发；
- 偶尔会被 DNS 污染到 `github.com` 的解析失败 —— 换 DNS（223.5.5.5 / 119.29.29.29）即可。

### 两种安装方式

| 方式 | 适合 | 说明 |
|---|---|---|
| `Lumen-Setup.exe` | 大多数人 | 图形向导：选目录 → 勾组件 → 装完直接用。会自动创建桌面快捷方式、装守护服务、下手机 APP 与模型权重 |
| `Lumen-Windows-x64.zip` | 喜欢绿色版 | 解压到任意目录即用，整个目录可以随时搬走 |

**权重怎么办**：绿色版包里带了 3B 兜底权重（2.0 GB）；更大的档位（H1 Turbo / mini se）
不随包分发 —— 第一次启动后，在「设置 → 模型」里选好档位，客户端会
**自动从国内镜像拉取**（`hf-mirror` 直链 + 官方仓库兜底，支持断点续传），拉到 `models/` 目录。

安装器里的组件是可选的：

- **桌面快捷方式**：默认开；
- **LumenGuard**（开机自启，异常退出自动拉起）：默认开，约 26 MB 常驻内存；
- **手机端 APP**：默认开，装到 `mobile/` 目录，安装器会把 APK 也留在那儿；
- **模型权重**（约 2.0 GB，走 hf-mirror）：默认开，不勾就跳过。

**自己打包**：

```bash
git clone <你的仓库地址> Lumen && cd Lumen
pip install pyinstaller
# 解压内置运行时（仓库里只存了压缩包）
python -c "import zipfile; zipfile.ZipFile('runtime/llamacpp-vulkan.zip').extractall('runtime')"
python -m PyInstaller --noconfirm --clean --distpath dist --workpath build build.spec
```

打 `v*` 标签会自动跑 CI：Windows 桌面端 + 手机 APK 一起构建并挂到 Releases。

### 发布到 GitHub（一条命令）

```bat
push_to_github.bat
```

脚本会引导你填仓库地址，并自动完成：测 SSH → 设远端 → 推代码 → 打 tag 触发 CI。

前提是在浏览器里做两件事：

1. 建一个**空仓库**（<https://github.com/new>），名字填 `Lumen`，
   **不要**勾选 Add a README / .gitignore / license（本地已经有了）；
2. 把部署公钥加到账号（<https://github.com/settings/ssh/new>），脚本里会打印那一整行。

> 这台机器上 `github.com` 主站直连不通，所以 SSH 走 **over 443**
> （`~/.ssh/config` 里把 github.com 指向 `ssh.github.com:443`，已配好）。
> 下载 Release 附件走的是 `objects.githubusercontent.com`，这个域名直连是通的。

### 自己打安装器

安装器是纯 tkinter（标准库），但**要用带 tkinter 的 Python**打包：

```bash
# 1. 不含权重的程序包（权重由安装器联网下载）
set LUMEN_BUNDLE_MODEL=0
python -m PyInstaller --noconfirm --clean --distpath dist_inst --workpath build_inst build.spec
# 2. 打包安装器
python -m PyInstaller --noconfirm --clean --distpath dist_setup --workpath build_setup installer/build_installer.spec
# 产物：dist_setup/Lumen-Setup.exe
```

---

## 1. 现在就能跑

双击 **`Lumen.exe`**（源码环境用 `start_lumen.bat`）。

第一次启动会自动做这几件事：

1. 用 `nvidia-smi`（或 WMI）读取显卡与显存，给三个档位上硬件锁；
2. 扫描本机推理端点：Ollama `11434` / llama.cpp `8080` / LM Studio `1234` / vLLM `8000`；
3. 一个都没有？**自动拉起 Lumen 自带的 llama.cpp 运行时**（Vulkan 后端，直接吃显卡）；
4. 起一个 HTTP 服务，用 Edge/Chrome 的 `--app` 模式开一个无地址栏窗口。

### 内置运行时（开箱即用，不需要装 Ollama）

Lumen 自带一份 llama.cpp 的 **Vulkan 构建**（`runtime/llamacpp/`）。Vulkan 走的是显卡驱动，
不依赖 CUDA Toolkit，也不挑显卡——NVIDIA / AMD / Intel 都能用。

只要 `models/` 目录里有 `.gguf` 权重，Lumen 启动时就会自动把它拉起来：

```
实测 RTX 5060 8GB + Qwen2.5-3B-Instruct Q4_K_M
  预填充 pp128  3733 t/s
  生成     tg64   112 t/s
```

对比纯 CPU 的 ONNX int4 路径（约 0.25 t/s），**快约 450 倍**。

关掉窗口 = 退出 Lumen。进程被强杀时，由 LumenGuard 负责拉起（见第 7 节）。

---

## 2. 界面速览

| 区域 | 能做什么 |
|---|---|
| 左栏 | 会话列表、新对话（`Ctrl+N`）、设置 / 手机 / 关于 |
| 顶栏 | 点模型名切换档位（带硬件锁）、引擎状态、搜索开关 |
| 对话区 | 流式输出、代码块复制、工具调用卡片、引用来源卡片、置信度徽标 |
| 输入框 | `Agent` 开关、`联网` 开关、工作区、发送 / 停止 |
| 右抽屉 | 设置、手机同步、守护服务、关于（含合规声明） |

快捷键：`Ctrl+N` 新对话、`Ctrl+K` 聚焦输入、`Esc` 关闭弹层。

---

## 3. 模型档位与硬件锁

| 档位 | 门槛 | 参数 | 量化 | 运行时 |
|---|---|---|---|---|
| **Lumen V1 Ultra** | 显存 ≥ 24GB | 27B | PTQ1_0 三值 | PrismML 定制 llama.cpp |
| **Lumen H1 Turbo** | 显存 ≥ 8GB | 32B MoE | Q4_K_M | 内置 llama.cpp / Ollama / OpenAI 兼容端点 |
| **Lumen mini se** | 显存 ≥ 12GB | 14B | Q4_K_M | 内置 llama.cpp / Ollama |
| **Lumen Core 3B**（内置兜底） | 无 | 3B | Q4_K_M (GGUF) | 内置 llama.cpp · Vulkan |

**硬件锁逻辑**（`core/models.py` → `decorate()`）：

- 检测显存 ≥ 档位门槛 → 可选；
- 低于门槛 → 档位置灰，鼠标悬停显示「需要 XX GB+ 显存，本机仅 X GB」；
- 低于门槛时可在设置里开「实验性强制加载」，但会明确标注这是非官方路径。

**「置灰」到底指什么（两种情况不是一回事）**：

| 现象 | 含义 | 怎么解 |
|---|---|---|
| 卡片**变灰** + 标「显存不足」 | 本机显存不够，属于硬件门槛 | 换机器，或设置 → 实验性选项里开「强制加载」 |
| 卡片**没变灰**，但标「待放入 models/xxx.gguf」 | 硬件够，只是权重文件没装 | 点卡片旁的**「获取权重」**一键下载，装完这档立刻真正可用 |

**档位 ≠ 实际权重**：档位是「能力定位标签」，真正跑的是某个 `.gguf`。如果你选了
H1 Turbo 却没放 H1 的权重，Lumen 会**诚实地退回 Core 3B 并在界面上说明原因**，
而不是让模型自称高档位却跑着低档权重（`core/models.py` → `effective_tier()`）。
反过来，权重一旦就位，切换档位会自动换成对应权重重启运行时（`/api/tier` → `resolve_weight()`）。

**权重从哪来**（`core/weights.py` · 界面「模型档位 → 获取权重」）：

| 候选（落地文件名） | 对应档位 | 体积 | 许可证 |
|---|---|---|---|
| `H1-Turbo-Q4_K_M.gguf` | H1 Turbo | 10.8GB | Apache-2.0 |
| `mini-se-Q4_K_M.gguf` | mini se | 8.4GB | MIT |
| `Qwen2.5-3B-Instruct-Q4_K_M.gguf` | Core 3B | 2.0GB | Apache-2.0（随包已内置） |

- 走公开镜像 + 官方仓库，**带断点续传**，中断后重试接着下；
- 下载完直接落在 `models/`，Lumen 自动识别，不需要手动配置；
- **权重文件统一以档位名命名**，用户一眼就知道它属于哪一档；
- 各候选的上游来源与许可证义务统一记在 `licenses/H1-Turbo-NOTICE.txt` 等文件中
  （不可删除）；
- 自己下载的 `.gguf` 也可以直接丢进 `models/`，Lumen 会认。

**合规要点**：

- V1 Ultra 与 mini se 在界面上保留底层来源与「修改增强」标注；
- H1 Turbo 的原始来源**不在主界面与关于页展示**，但第三方 MIT 许可证要求的版权与许可声明完整保留在 `licenses/H1-Turbo-NOTICE.txt`（该文件不可删除）；
- **Lumen Core 3B 不是自研模型**：底层是阿里开源的 Qwen2.5-3B-Instruct（Apache-2.0），
  Lumen 只做集成、量化分发与 Vulkan 加速，**没有自行训练或改写权重**；
- 其余三档由「想念也是麻烦」制作。

---

## 4. 智能搜索

模型不知道、或把握度低时自动联网检索，而不是硬编。

- **协议**：系统提示词要求模型在回答末尾输出 `<confidence>0-100</confidence>` 与 `<need_search>true/false</need_search>`；
- **触发**：置信度低于阈值（默认 60）、`need_search=true`、或命中「最新 / 价格 / 版本 / 新闻」等启发式词；
- **后端**：优先自建 SearXNG（`config/search.json` 里配 `searxng_base`），没有就降级到 Bing RSS / DuckDuckGo；
- **RAG**：结果清洗、去重、截断后拼成上下文回灌，模型回答带 `[1] [2]` 编号引用；
- **缓存**：结果本地缓存 24 小时；
- **离线**：检索不通时明确提示「当前无法联网，以下为模型记忆回答」，不假装有资料；
- **开关**：设置里可关联网搜索，或切「始终搜索 / 仅低置信度时搜索」。

---

## 5. Agent 工具循环

本地小模型的 function calling 不稳，所以用**纯文本协议**：模型输出

```xml
<tool name="fs_read">{"path": "src/main.py"}</tool>
```

框架解析后执行，再把结果作为新的 user 消息回灌，最多 6 步（可调）。

### 小模型也能可靠调用（三层保障）

内置的 Core 3B 权重在 function calling 上并不稳定，Lumen 用三层机制兜住：

1. **assistant 侧示范**——判断用户这句话可能要动工具时，往对话历史里插一段
   「用户提问 → assistant 输出工具标签 → tool_result → assistant 给出答案」的
   完整样例。3B 对「assistant 说过什么」的模仿远比读系统提示词里的说明可靠，
   也不会把说明文字当成自己的输出复述一遍；
2. **多格式解析**——除标准的 `<tool name="…">{…}</tool>` 外，
   `<tool>fs_read {…}</tool>`、`<tool_call>`、`<function>`、```tool 代码块、
   以及裸 JSON 都能识别，JSON 用平衡括号扫描避免嵌套被截断；
3. **框架兜底**——模型只说「正在读取文件内容…」却不发标签时，由框架根据
   用户话里点名的文件（且文件在工作区里真实存在）替它跑一遍只读工具，
   把真实结果回灌，杜绝凭空编造文件内容。

同时：相同工具 + 相同参数不会重复执行；模型这一轮已经给出实质回答时
不会再多跑一轮工具（避免同一段回答出现两遍）。

可用工具：

| 工具 | 作用 | 默认是否需要审批 |
|---|---|---|
| `fs_ls` | 浏览工作区目录 | 否 |
| `fs_read` | 读取文件 | 否 |
| `fs_write` / `fs_append` | 写入 / 追加文件 | 工作区内免审批，区域外必须确认 |
| `run_cmd` | 执行命令 | **必须确认**，且有破坏性命令黑名单 |
| `search` | 联网检索 | 否 |
| `fetch` | 抓取网页 | 否 |

审批走界面弹窗，可一键拒绝；超时默认拒绝（fail-closed）。

---

## 6. 手机端与远控

### 6.1 账号与设备发现

- 桌面端默认只监听本机；要在手机访问，需在「设置 → 手机同步」里开启**允许局域网访问**；
- **扫码直连**：桌面端「手机」面板会显示一个二维码，手机相机扫一下就打开
  `http://<电脑局域网IP>:8770/m`，不用手输 IP。换 Wi-Fi 或 IP 变了，重新扫一次即可
  （面板里的码会自动刷新）；
- 也可以用手机打开 `http://<电脑局域网IP>:8770/m`，用账号密码登录，或用桌面端生成的**配对码**登录；
- 登录后自动拿到设备令牌（有效期 30 天），可在「设备」里随时吊销。

> 浏览器出于安全限制无法接收局域网广播，所以「发现」靠二维码而非自动搜索。
> 二维码由 `qrcode-generator`（MIT）生成，只在桌面端前端使用，不参与推理。

### 6.2 零成本联网方案

| 方案 | 用途 | 成本 |
|---|---|---|
| 同一局域网 Wi-Fi | 扫码直连 `http://IP:8770/m` | 0 |
| ZeroTier / Tailscale 免费层 | 组虚拟内网，外网访问 | 0 |
| Cloudflare Tunnel Quick Tunnel | 临时公网测试（URL 会变） | 0 |
| RustDesk 自建中继 | 远程桌面兜底 | 0 |

> 完全零成本且长期稳定的公网同步无法保证，长期用请走虚拟局域网或自建中继。

### 6.3 远程控制（平滑光标 + 画面回传）

手机端发送**目标坐标 + 速度参数**，不是「点哪瞬移哪」。

- 电脑端以 **60Hz**（30–120 可调）插值追踪目标；
- 有**最大速度**（默认 2600 px/s）与**最大加速度**（默认 9000 px/s²）限制；
- 接近目标时软着陆，不会抖；
- 支持移动 / 单击 / 双击 / 右键 / 滚轮 / 横向滚轮 / 键盘输入；
- 指令限流：移动 240 次/秒、点击 12 次/秒、文本 6 次/秒；
- **静止时插值线程退避到 4Hz**，有新指令立刻唤醒回到 60Hz——长期挂着不烧 CPU。

**画面回传（整屏可见）**：授权后手机端能直接看到电脑的**整个屏幕**，
不只是虚拟光标。桌面端用 GDI 抓屏 → 缩放 → GDI+ 编码 JPEG 直推，
不落盘、不转 base64，手机端 `<img>` 直接换帧。

| 项 | 实测 |
|---|---|
| 1280×720 JPEG | **179 KB / 47 ms** |
| 同尺寸 PNG（回退路径） | 1.7 MB / 316 ms |
| 多显示器 | 抓的是**虚拟屏幕**（含副屏），坐标映射按虚拟屏幕原点算 |

**分辨率自适应（网易 UU 式）**：不管电脑是 2K 还是 4K，远控时画面都能降成手机尺寸。

| 档位 | 行为 |
|---|---|
| `auto`（默认） | 按手机**短边**等比缩放，不放大超过电脑原始分辨率。2560×1440 屏幕 + 390×844 手机 → 降到 **692×390** |
| `480` / `720` / `1080` | 固定档位，可手动选 |
| `native` | 电脑原始分辨率 |

点画面时，手机坐标会**反算回电脑真实像素**再点击（画面 692 宽 ↔ 屏幕 2560 宽，比例 3.7×），
所以手指点哪儿就是哪儿。

**安全**：画面接口必须**已授权**才能拉（否则同网段任何人截屏都能看到桌面）。
`<img>` 标签没法带 Authorization 头，所以额外接受**当前远控会话 ID** 作凭证——
它只在授权期间有效，断开立刻失效。

**授权模型**：

1. 手机发起远控请求 → 电脑端弹窗；
2. 本机用户点「允许」后才生效，拒绝则立即失效；
3. 界面上永远有「一键断开」，断开后立刻停止插值线程；
4. 没有授权会话时，所有输入指令一律拒绝；
5. **空闲自动断开**（默认 15 分钟，可在「手机」面板改成 5/15/30/60 分钟或不超时）：
   手机丢了、退出 App、断网了，电脑不会一直停在「可被远程操控」的状态。

**长期使用的稳定性设计**：

| 事项 | 做法 |
|---|---|
| 令牌校验写盘 | 令牌表常驻内存，按 mtime 判断变更；`last_seen` 每 60 秒才落盘一次（手机每 2.5s 轮询一次，否则一天几万次磁盘写） |
| 过期令牌 | 签发新令牌时顺手清理，不会无限堆积 |
| 事件表 | 只保留最近 5000 条同步事件，避免数据库无限膨胀（会话数据不自动删） |
| 搜索缓存 | 24 小时 TTL，自动过期 |
| 空闲巡检 | 1 秒一次，独立线程，不依赖手机端继续发请求 |

### 6.4 Windows 防火墙：为什么老是弹「已阻止此应用」

这是 Windows 的机制问题，不是 Lumen 的 bug，但确实很烦，根因值得写清楚：

1. **入站规则按「程序完整路径」匹配。** Lumen 是绿色版，你把它拷到任何一个目录，
   对应的旧规则就失效了，Windows 监听 8770 时找不到放行规则 → 弹
   「Windows Defender 防火墙已阻止此应用的部分功能」。
2. **在弹窗上点「取消」会留下一条拦截（Block）规则。** Block 的优先级高于 Allow，
   于是出现「明明点了允许也没用、越点越弹」——这正是大多数人遇到的情况。
3. **禁用 Defender 实时保护 ≠ 放行防火墙。** 前者是杀毒，后者是网络过滤，
   关掉实时保护不会影响防火墙弹窗，也不建议关。

Lumen 的处理（`core/firewall.py`）：

- **按端口放行**（TCP 8770 / 8771），规则与 exe 路径无关，**搬到哪都有效**；
- 远程地址限定为 `LocalSubnet`，只有同一局域网能连，不是对公网开放；
- 启动时自动体检，缺规则就幂等补写，并清掉本程序遗留的 Block 规则；
- 需要管理员权限时走标准 UAC 提权（系统会弹权限确认框），**不静默提权**；
- 界面「手机」面板里有实时状态与「一键放行局域网」按钮，也可以手动触发；
- 只改 Lumen 自己的规则，不碰用户其它任何防火墙配置。

### 6.5 手机 APP（APK）

想要「装到手机桌面、有图标、能推送」，就用 APK。构建链是纯 Gradle + Android SDK，
**没有 Node / Cordova / Capacitor**，APK 只有 **1.2 MB**。

**首次使用**：装好后填电脑地址（桌面端「手机」面板里能看到，形如 `192.168.1.10:8770`），
用账号密码或配对码登录一次，之后 APP 会记住这台电脑。

**APP 做什么、不做什么**：

| 能力 | 实现 |
|---|---|
| 桌面图标 | 自适应图标（青碧星球 + 轨道环），5 种密度 + 圆形变体 |
| 系统推送 | 前台服务每 4 秒轮询 `/api/remote/status`，检测到远控请求 / 已连接就发系统通知 |
| 记住电脑 | `SharedPreferences` 存地址与令牌 |
| 界面 | WebView 加载电脑端下发的 `/m` 页面——**和桌面端同一套前端**，桌面端升级功能手机端不用重装 APP |
| 令牌同步 | WebView 的 `localStorage` 原生读不到，页面登录后通过 JS 桥把令牌交给原生 |

**为什么不把前端打包进 APK**：那样每次改前端都要重新发版。做成「壳 + 远端页面」，
桌面端升级了功能，手机端下次打开就是新的。

**权限清单**（都在 `AndroidManifest.xml` 里，没有多余项）：

| 权限 | 用途 |
|---|---|
| `INTERNET` / `ACCESS_NETWORK_STATE` | 连电脑、轮询状态 |
| `POST_NOTIFICATIONS` | 远控请求通知（Android 13+ 需要运行时申请） |
| `FOREGROUND_SERVICE` + `FOREGROUND_SERVICE_DATA_SYNC` | 后台轮询用的前台服务 |

WebView 关掉了 `file://` 和 `content://` 访问，站内跳转留在 WebView，站外链接交给系统浏览器。

**自己构建**：

```bash
cd app
echo "sdk.dir=C\:\\path\\to\\android-sdk" > local.properties
./gradlew assembleRelease        # 产物：app/build/outputs/apk/release/
```

需要 JDK 17 + Android SDK（platform 34 / build-tools 34.0.0）。

---

## 7. LumenGuard 守护服务

轻量常驻程序，只做三件事：**启动 Lumen / 关闭 Lumen / Lumen 异常退出后拉起**。

- 开机自启（计划任务 `LumenGuard`，以当前用户身份运行）；
- 监听本地回环 `127.0.0.1:8771`，用 `X-Lumen-Token` 头做鉴权（令牌存在 `data/ipc.token`，与主程序共用同一个 `data/` 目录）；
- 接受 `START_LUMEN` / `STOP_LUMEN` / `STATUS` / `PING`；
- 资源占用：内存 < 30MB，空闲 CPU 接近 0。

**合规边界（明确不做什么）**：

- ❌ 不做隐蔽进程、不隐藏窗口、不对抗任务管理器；
- ❌ 不做 Rootkit、不做反卸载；
- ✅ 管理员可在 `services.msc` / 任务计划程序里随时停止、禁用、卸载；
- ✅ 安装时必须明确告知并取得用户确认。

安装 / 卸载：`install_guard.bat` / `uninstall_guard.bat`（需管理员权限）。

---

## 8. 目录结构

### 交付包（Windows）

```
Lumen/
  Lumen.exe                主程序
  _internal/               运行依赖 + 随包分发的只读资源
    models/                内置权重 Qwen2.5-3B-Instruct-Q4_K_M.gguf
    runtime/llamacpp/      内置 llama.cpp Vulkan 构建
    licenses/              四份第三方许可声明（不可删除）
    web/                   桌面端 SPA + 手机端页面
    config/                配置模板（首次运行自动复制到同级 config/）
  LumenGuard/              守护服务
    LumenGuard.exe
  licenses/                第三方许可声明（可见副本）
  config/                  settings.json / search.json / sync.json
  logs/                    运行日志
  data/                    账号、会话库、IPC 令牌、运行时状态
  workspace/               Agent 的默认工作目录
  models/                  放你自己的 .gguf（内置权重在 _internal/models）
  README.md
  install_lumen.bat        安装说明
  start_lumen.bat          启动
  install_guard.bat        安装守护服务（开机自启）
  uninstall_guard.bat      卸载守护服务
```

> 整个 `Lumen/` 目录是绿色版：想搬家就把整个目录复制过去，权重、会话、设置都跟着走。
> 删掉 `data/` 就是彻底重置（会话、账号、令牌全在里面）。

### 源码树

```
Lumen/
  main.py                  入口
  core/                    后端（全部标准库，无第三方框架）
    config.py              路径与设置
    hardware.py            nvidia-smi / WMI 硬件探测
    models.py              档位注册表 + 硬件锁 + 档位回退
    engine.py              端点发现 + OpenAI 兼容流式对话
    llamacpp.py            内置 llama.cpp 运行时管理（自动拉起 / 停止）
    onnx_engine.py         可选 ONNX 推理后端（无 GPU 时的 CPU 兜底）
    search.py              SearXNG / Bing / DDG 搜索与缓存
    agent.py               工具循环与审批
    remote.py              光标插值与键鼠注入
    screencap.py           GDI 抓屏 + 缩放 + JPEG/PNG 编码
    screencap_service.py   远控画面服务（分辨率自适应、坐标反算）
    jpegenc.py             GDI+ JPEG 编码器（ordinal 导出）
    auth.py                账号、配对码、设备令牌
    store.py               SQLite 会话存储与增量同步
    firewall.py            防火墙自愈（按端口放行）
    weights.py             权重下载（hf-mirror 直链 + 官方兜底）
    server.py              HTTP / SSE 服务
  guard/lumen_guard.py     守护服务源码
  web/                     桌面端 SPA + 手机端页面
    assets/thinking.css    思考动效「星核」（原创设计）
    assets/qrcode.js       二维码生成（MIT，仅前端用）
  app/                     手机端 Android 工程（Gradle，产出 APK）
    app/src/main/java/com/lumen/remote/
      MainActivity.java    原生首屏 + WebView 壳
      LumenBridge.java     JS 桥（令牌同步、即时通知）
      NotifyService.java   前台服务 + 轮询 + 系统通知
      Prefs.java           地址与令牌本地存储
  runtime/
    llamacpp-vulkan.zip    内置 llama.cpp 压缩包（CI 打包前解压）
    llamacpp/              解压后的运行时（不入库）
  models/                  .gguf 权重与 Modelfile（不入库）
  licenses/                六份第三方许可声明（不可删除）
  tools/                   图标生成、端到端自测脚本
  build.spec / guard.spec  PyInstaller 打包配置
  .github/workflows/       CI：自动构建 EXE + APK 并发布 Release
```

### 依赖的 Python 包

只有两个，且都是可选的：

| 包 | 用途 | 缺失时 |
|---|---|---|
| `onnxruntime` + `onnxruntime-genai` | 无 GPU 时的 CPU 推理兜底 | 自动跳过，用 llama.cpp |
| `pyinstaller` | 打包成 exe | 用 `start_lumen.bat` 跑源码 |

`core/` 里所有东西只用标准库写的，没有 Flask / FastAPI / requests。

前端只有一个第三方脚本：`web/assets/qrcode.js`（qrcode-generator，MIT），
只用来画局域网二维码，声明见 `licenses/frontend-NOTICE.txt`。

---

## 9. 常见问题

**Q：界面显示「离线规则引擎」，不是模型输出？**
说明本机既没探测到外部端点，`models/` 下也没有 `.gguf`。把权重放进 `models/` 再点「重新探测」。

**Q：Windows Defender 一直弹「防火墙已阻止此应用的部分功能」，点了允许也没用？**

三个叠加原因：规则按**程序路径**匹配（换目录就失效）、在弹窗上点「取消」会留下
**拦截规则**（优先级高于放行）、以及**禁用 Defender 实时保护跟防火墙是两回事**。
Lumen 已经改成按**端口**放行并自动清理遗留拦截规则，桌面端「手机」面板里有
「一键放行局域网」按钮；正常情况下以后不会再弹。

**Q：怎么加自己的模型？**
1. 下载一个 `.gguf`（推荐 Q4_K_M 量化）；
2. 放进 `models/` 目录；
3. 重启 Lumen 或在设置 →「内置推理运行时」点「启动」。

也可以直接用界面里的「获取权重」一键下载，装完自动识别。
Lumen 会按**当前档位**挑权重（而不是「体积最大的那个」），切档位会自动换权重重启运行时。

**Q：磁盘空间不够？**
- `models/` 里的大权重可以删掉，用界面「权重仓库 → 删除」；
- ONNX 备用后端（约 3GB）可以用环境变量 `LUMEN_ONNX_DIR` 指到别的盘；
- 打包版自带 2GB 的 Core 3B 权重，其余档位按需下载。

**Q：手机连不上？**
1. 桌面端设置里开「允许局域网访问」；
2. 手机和电脑在同一 Wi-Fi；
3. 用 `http://<IP>:8770/m` 而不是 `https://`；
4. 部分公司/学校 Wi-Fi 开了 AP 隔离，换手机热点试。

**Q：换 Wi-Fi 后手机连不上了？**
用桌面端「手机」面板里的二维码重新扫一次即可——面板每次打开都会重新探测 IP 并刷新二维码。

**Q：手机用什么账号登录？**
首次运行默认账号是 `administrator`，默认密码 `lumen`（桌面端「设置 → 账号」里有醒目提示）。登录后请立刻改密码。

也可以在桌面端「手机同步」里生成**配对码**，手机端选「配对码」登录。

**Q：远控没反应？**
必须在**电脑端**点「允许」。另外确认没有切到别的用户会话（Windows 的 UIPI 会挡跨会话注入）。

**Q：想清空所有数据？**
关掉 Lumen，删掉 `data/` 目录即可（会话、账号、令牌全在里面）。模型文件在 `models/`，不受影响。

---

## 10. 合规与隐私

- 所有推理、文件读写、命令执行都在本机，**不上传任何数据**；
- 唯一的出网行为是「联网搜索」，开启时会在回答里显示引用来源；
- 远控必须本机逐次授权，且不抓屏上传；
- 守护服务不隐藏、不反杀、不 Rootkit。

第三方许可声明见 `licenses/`：

- `V1-Ultra-NOTICE.txt`
- `H1-Turbo-NOTICE.txt`（H1 Turbo 的 MIT 与 Apache-2.0 两条上游声明）
- `mini-se-NOTICE.txt`
- `lumen-core-NOTICE.txt`
- `frontend-NOTICE.txt`（前端二维码库 qrcode-generator，MIT）

---

## 11. 跨网中继（电脑在别处、手机用流量也能连）

局域网直连要求手机和电脑在同一 Wi-Fi。想"电脑在家开着、人在外面用手机流量连"，
靠 **Lumen Link 中继** 实现——电脑侧的 LumenGuard 主动出站拨号到中继（绕开 NAT/防火墙），
手机经中继隧道连回电脑，数据只在你和你的电脑之间流动，中继不落地内容。

**自托管中继（纯标准库，零依赖）：**

```bash
# 在一台有公网/可被手机访问的机器上：
python tools/relay_server.py --port 8772 --secret "你自定义的密钥"

# 冒烟测试（确认隧道通）：
python tools/relay_server.py --self-test
```

**电脑端开启：** 桌面端「手机同步 / 远控 → 跨网中继」填中继地址
（形如 `http://你的中继:8772`）与同一把密钥，保存后守护进程会自动重连。

**手机端连接：** 用手机浏览器打开**中继地址**，输入同一把密钥，
即跳转到 Lumen 手机端。之后：

- 查看电脑上的会话、发消息让电脑上的 AI 干活；
- 「远程控制」整台电脑（看屏幕 + 触控/键鼠反控，需电脑端逐次授权）；
- 主 AI 没开时，「设备」页会显示"主 AI 未运行"，点「**唤醒主 AI**」，
  由本机守护进程 LumenGuard 把它拉起来。

**安全模型：** 中继用 `--secret` 做共享密钥，错则 403；手机连接后拿会话 Cookie。
不想自己搭？留空 secret 也能跑，但任何知道地址的人都能连——仅适合本机自测。

> 仓库设为**私有**时，Releases 里的安装包与 APK 只有你自己能下；
> 模型权重继续走 hf-mirror 公开镜像，不依赖仓库可见性。

---

## 12. 许可

- Lumen 软件本体（含桌面端、LumenGuard 守护进程、手机端、中继服务）以 **MIT** 许可发布；
- 随包分发的**模型权重**各自携带上游许可证（gpt-oss / Qwen / DeepSeek 等），
  `licenses/` 下五份 NOTICE 原样保留、不可删除；
- 使用模型权重即视为接受其上游许可。
