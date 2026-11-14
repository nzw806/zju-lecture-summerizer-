# 智云课堂课程总结 · ZJU Lecture Summarizer

> Paste a ZJU 智云课堂 (classroom.zju.edu.cn) replay URL → the tool fetches the
> lecture transcript, summarizes it section by section with any OpenAI-compatible AI, and
> produces a timestamped study note (`知识总结.md`).
>
> 粘贴一个浙大智云课堂回放地址，自动抓字幕（+ 登录后可抓 PPT 图片）→ AI 分段总结
> → 生成带时间戳的《知识总结.md》。**核心纯 Python 标准库；弹窗登录为可选增强，macOS / Windows 通用。**

![python](https://img.shields.io/badge/python-3.9+-blue) ![deps](https://img.shields.io/badge/核心-纯标准库-green) ![opt](https://img.shields.io/badge/登录-可选Playwright-lightgrey) ![license](https://img.shields.io/badge/license-MIT-lightgrey)

## 功能 Features

- **抓字幕**：登录后自动抓取整节课的语音识别文本（带登录态请求官方接口；匿名请求自 2026-11 起返回 403）
- **抓 PPT（图片+视觉转写文字层）**：登录一次后，自动按页下载整节课的 PPT 幻灯片图片与出现时间戳（平台实际给的是 AI 从视频切出的图片，**大多不带文字层**），生成 `ppt/manifest.md` 清单；若平台无文字层，则用 **DeepSeek V4.1 Flash 视觉模型逐页"看图转写"**生成文字（约 1 千 token/页），连同时间戳注入总结——总结模型按"同一时间轴"把字幕与正在展示的幻灯片对齐，纠同音误识别、补全字幕没讲全的定义与步骤
- **AI 课程画像（两层提示词）**：总结前先用字幕开头 + PPT 转写让 AI 识别"这是哪门课、本讲讲什么、关键术语词表、该侧重什么"（约 2 秒），生成本课**针对性提示词**；人工内置的提示词层只保留通用约束（结构/格式/时间戳）。识别结果写入总结头部，术语词表用于纠正字幕同音错字
- **可关掉课件抓取**：界面勾选「抓取课件 PPT」，不勾则完全跳过图片下载与视觉转写，只做字幕总结——**省钱模式**（视觉转写是主要 token 开销）
- **多家 API 可选**：默认 DeepSeek，内置智谱 GLM / 通义千问 / OpenAI 预设，也可填任意 OpenAI 兼容端点与模型名；换服务商不用改代码
- **单次请求总结**：整课字幕一次性交给 AI 生成完整讲义，前后一致、不并行；仅当超出模型上下文时自动退化为顺序分块+合并
- **课件参考**：可上传 pptx / docx / pdf / txt / md 课件，提取文字注入总结，校准术语、帮助抓住重点（PDF 需含文字层，扫描件建议先转文字）
- **下载交付**：完成后界面提供「下载总结文档」「下载全部资料（zip）」按钮，文件直接存到你自己的下载目录
- **登录一次、之后全自动**：点「登录智云课堂」弹出内置浏览器（Playwright）对着浙大官方页登录一次，登录态安全存进**系统钥匙串**（macOS Keychain / Windows DPAPI），之后自动复用，过期才需重登；不依赖你装的是哪个浏览器，Win/Mac 通用
- **本地网页界面**：双击启动，浏览器里填表、看实时日志、下载结果
- **隐私**：**API key 与登录 Cookie 均只存系统钥匙串**（OS 级加密），项目目录里没有任何明文密钥；二者**永不上传**，所有请求只发往智云课堂（及白名单 CDN）和你选定的 AI 服务商

> **依赖说明**：抓字幕、总结、钥匙串存储均为**纯 Python 标准库**。只有「弹窗登录」这一步需要可选依赖 Playwright（`pip install playwright && python -m playwright install chromium`）；不装也能用——改用「手动粘贴 Cookie」即可，依旧零依赖。

## 三种总结模式

| 模式 | 流程 | 需要 | 适用 |
|---|---|---|---|
| 只抓字幕 | 平台字幕接口 → 字幕全文 | 无 | 存档字幕、不耗额度 |
| 字幕 + AI 总结（默认） | 平台字幕 → AI 单次请求总结 | 任一支持的 API key | 绝大多数课 |
| 增强识别 + AI 总结 | 下载视频 → ffmpeg 抽音频 → **Whisper large-v3 重新识别**（中性提示+防幻觉）→ 总结 | key + ffmpeg + `pip install faster-whisper` | 口音重、平台字幕术语识别不准的课 |

增强识别模式里的 ffmpeg 可在界面一键下载（静态单文件，约 20MB，存于 `bin/`，不入库）；
Whisper 识别使用**中性提示词**（只在有课件时补本课术语，绝不写死某门课——写死的算法术语会
在音乐/静音段触发大段幻觉）；同时关闭跨窗口条件继承（`condition_on_previous_text=False`），
并检测大面积重复：一旦识别结果高度雷同（如反复输出"点赞订阅"），立即中止并提示，
**不会把垃圾字幕送进 AI 总结**。总结提示词已预设（速览表 / 分章详解 / 公式清单 /
考试信息 / 考点预测五段式），也可在界面「高级」里自行修改。

## 快速开始 Quick Start

**即下载即用**：克隆后双击一个文件即可，无需安装任何依赖（Python 3.9+ 自带环境就够）。

| 系统 | 操作 |
|---|---|
| macOS | 双击 `start.command`（首次若提示"无法打开"：右键 → 打开 → 仍要打开） |
| Windows | 双击 `start.bat`（没装 Python 会提示去官网下载，安装时勾选 Add to PATH） |
| Linux | `sh start.sh` |

启动后浏览器自动打开 http://127.0.0.1:8765 ，**先点「环境自检」**，
看 Python / 智云课堂连通性 / AI key / 智云登录态 / ffmpeg 五项的绿黄红灯，
再填表运行。端口被占用时会自动顺延到 8766–8770。
**注意：启动服务的那个终端窗口不要关**，关了服务就停；想停服务在终端按 Ctrl+C。

三个**不可避免的一次性摩擦**（操作系统行为，非代码问题）：

1. **AI key 需自备**（DeepSeek 默认，也可选智谱/千问/OpenAI 或自定义；一节课几毛钱）；
   不填则进入"只抓字幕"模式。
2. **首次点「登录」会弹出内置浏览器窗口** → 对着浙大官方页登录一次；
   登录态写入系统钥匙串时 macOS 会弹授权框 → 点"允许"（建议"始终允许"）。
   想用弹窗登录需先装可选依赖：`pip install playwright && python -m playwright install chromium`。
3. **未来高精 Whisper 模式**首次使用需联网下载 ffmpeg 与模型（约 3GB）。

手动方式（等价）：

```bash
python3 app.py          # 启动网页界面
python3 pipeline.py "<回放URL>"   # 纯命令行
```

在页面里：

1. 粘贴回放地址（形如 `https://classroom.zju.edu.cn/livingroom?course_id=...&sub_id=...`）；
2. 选 AI 服务商并填 API key（默认 DeepSeek，[platform.deepseek.com](https://platform.deepseek.com)；也可选智谱/千问/OpenAI 或自定义端点）。勾选「记住」后加密存入系统钥匙串，下次留空自动沿用；
3. 点「登录智云课堂（自动）」在弹窗里登录一次（**必登**：平台已收紧，字幕、PPT、视频直链均需登录态）；
4. 视需要勾选「抓取课件 PPT」（不勾省钱，只做字幕总结）；点「开始」，完成后点「下载总结文档」/「下载全部资料（zip）」把讲义和 PPT 保存到自己电脑。

也可以不用界面，直接命令行：

```bash
python3 pipeline.py "<回放URL>"
```

## 输出 Output

每次运行在工具目录 `output/<日期>_sub<课程ID>/` 暂存（同一天同一课复用目录），
**完成后在界面点下载按钮把文件取回自己电脑**：

```
知识总结.md            ← 最终讲义（带时间戳，可单独下载）
raw/transcript.txt             逐段带时间戳字幕全文
raw/transcript_by_minute.txt   按分钟合并的字幕
ppt/001_t125s.jpg …            （登录后）每页 PPT 幻灯片图片，文件名含出现秒数
ppt/manifest.md / .json        PPT 清单：时间戳 → 图片 → 文字层
ppt/ppt_text.txt               PPT 纯文字层（按页，供检索/注入总结）
raw/video.mp4 / audio.mp3      （增强识别模式）视频与音频
```

## 工作原理 How It Works

```
livingroom URL ──解析──▶ sub_id
                          │
        字幕接口 GET（带登录态）◀─┘
                          │
              按分钟合并 → 按 ~11k 字分块
                          │
        AI 并发 3 路 × N 块（分段笔记）
                          │
              AI 合并 → 知识总结.md
```

- 字幕接口：`courseapi/v3/web-socket/search-trans-result?sub_id=...`（需登录态；匿名 403，失败自动回退 interactive 后端）
- PPT 接口：`pptnote/v1/schedule/search-ppt?course_id=...&sub_id=...&page=...`（需登录）
  返回每页的 `pptimgurl`(原图)/`pptthumb`(缩略图)/`content`(文字层)/时间戳；主站为空时
  自动回退到 `interactivemeta.cmc.zju.edu.cn/pptnoteapi`。图片仅允许
  `*.zju.edu.cn / *.chinamcloud.* / *.chinamcache.com` 白名单域名，强制 https。
- 登录与鉴权：点「登录」用 Playwright 弹内置浏览器登录浙大统一身份认证 →
  读出会话 Cookie → 存进系统钥匙串（`secure_store.py`：macOS `security` 命令、
  Windows DPAPI、兜底 0600 文件；值先 base64 规避 `security` 对非 ASCII 的 hex 编码坑）。
  请求时带 Cookie，并从 Cookie 的 `_token`（URL 编码的 PHP 序列化串）解出
  `Authorization: Bearer <token>`（对齐参考项目 ZhiyunAssistant）。
  抓取阶段只读钥匙串、不开浏览器；密码只进浙大官方页，代码不读取、不保存。
- 总结提示词针对课堂场景定制：证明分步不跳步、公式上下分式、保留时间戳、单列考点与考试通知

## 常见问题 FAQ

- **需要登录吗？** 需要（2026-11 起平台收紧，字幕接口匿名也 403）。点「登录」一次即可，登录态存钥匙串自动复用；PPT 图片/文字层与视频直链同样依赖它。
- **登录安全吗？** 密码只进浙大官方页，本工具不读取、不保存密码，只拿登录后的 Cookie，且 Cookie 仅存系统钥匙串（OS 级加密），不写进项目文件、不上传。可随时点「清除登录态」一键删除。
- **弹窗登录打不开 / 报错？** 需先装可选依赖：`pip install playwright && python -m playwright install chromium`；不便安装就用「手动粘贴 Cookie」兜底。
- **花多少钱？** 一节课（约 2.5 小时）的总结大约消耗 3–5 万输入 token，
  按 DeepSeek 现价约 **¥0.1–0.2**（换其他家以各家计价为准）；「只抓字幕」模式完全免费。
- **点了按钮没反应？** 多半是服务没在跑或页面是旧的：重新双击启动文件，
  再回浏览器按 Cmd+R / F5 刷新页面。
- **macOS 首次运行弹钥匙串框？** 点"允许"（建议"始终允许"）。若报权限不足：
  系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 添加你的终端。
- **401 / 402 / 429？** 分别是 API key 错误（注意与所选服务商匹配）/ 余额不足 / 限流（脚本自动退避重试）。
- **直播中能跑吗？** 不建议，字幕不全；课后回放再跑。
- **macOS 官方 Python 报 SSL 证书错误？** 脚本会自动降级处理；根治可运行
  `/Applications/Python 3.x/Install Certificates.command`。

## 声明 Disclaimer

本项目仅供**个人学习**使用：抓取的是本人有权限访问的课程回放，频率极低（一节课一次请求级）。
请遵守浙大智云课堂的使用条款，**不要**用于批量抓取、传播课件或任何商业用途。
登录 Cookie 与 AI API key 都是你的凭证，**均只存于系统钥匙串**（OS 级加密，
首次读取 macOS 会弹授权框，建议"始终允许"）；项目目录不含任何明文密钥，
推送到仓库也不会泄露。旧版本曾把 key 明文存于 `config.json`——启动时会自动
迁移进钥匙串并抹除。请勿把课件/PPT 传播给他人或用于商业用途。

## 许可 License

[MIT](LICENSE)
