# 智云课堂课程总结 · ZJU Lecture Summarizer

> 选择智云课堂日期与课堂，或粘贴回放 URL → 自动获取字幕 → 使用配置的 OpenAI 兼容模型生成带时间戳的学习笔记。
>
> 支持 Windows / macOS / Linux；基础模式只用 Python 标准库。课程发现需要本人有效的智云课堂 Cookie，AI 总结使用本机配置的接口、模型和 API key。

![python](https://img.shields.io/badge/python-3.9+-blue) ![deps](https://img.shields.io/badge/依赖-零（纯标准库）-green) ![license](https://img.shields.io/badge/license-MIT-lightgrey)

## 功能 Features

- **获取我的课程**：按日期读取本人智云课堂课程，选择具体课堂后直接进入字幕流程，无需手动复制 `livingroom` URL。
- **抓字幕**：调用智云课堂字幕接口，保存逐段和按分钟合并的时间戳全文。
- **AI 总结**：支持 OpenAI 兼容 Base URL、模型名和 API key；整课单次总结，超出上下文时顺序分块合并。
- **课件参考**：可上传 pptx / docx / txt / md 课件，提取文字注入总结，校准术语、帮助抓住重点。
- **下载交付**：完成后提供总结文档、字幕全文和全部资料 zip 下载。
- **Cookie 选择**：支持 Chrome / Edge 自动尝试读取，也支持手动粘贴智云课堂请求头 Cookie。
- **本地网页界面**：双击启动，浏览器里选择课程、查看实时日志、下载结果。
- **隐私**：Cookie 与 API key 只存在本机 `config.json`（权限 600）；Cookie 不发送给 AI 服务，也不提交到 Git。

## 三种总结模式

| 模式 | 流程 | 需要 | 适用 |
|---|---|---|---|
| 只抓字幕 | 平台字幕接口 → 字幕全文 | 无；手动 URL 可不填 Cookie | 存档字幕、不耗额度 |
| 字幕 + AI 总结（默认） | 平台字幕 → OpenAI 兼容模型总结 | AI key；选择课程需要 Cookie | 绝大多数课 |
| 增强识别 + AI 总结 | 下载视频 → ffmpeg 抽音频 → **Whisper large-v3 重新识别** → 总结 | Cookie、AI key、ffmpeg、`pip install faster-whisper` | 口音重、平台字幕术语识别不准的课 |

增强识别模式里的 ffmpeg 可在界面一键下载，存于 `bin/`，不入库；Whisper 识别时自动带入课程术语提示，减少专业词汇误识别。总结提示词已预设，也可在界面「高级」里自行修改。

## 快速开始 Quick Start

**即下载即用**：克隆后双击一个文件即可，无需安装额外依赖（Python 3.9+ 自带环境就够）。

| 系统 | 操作 |
|---|---|
| macOS | 双击 `start.command` |
| Windows | 双击 `start.bat` |
| Linux | `sh start.sh` |

启动后浏览器自动打开 http://127.0.0.1:8765，先点「环境自检」，确认 Python / 智云课堂连通性 / AI 接口 / 浏览器登录态 / ffmpeg 状态。端口被占用时会自动顺延到 8766–8770。

一次性准备：

1. 在 Chrome 或 Edge 登录智云课堂；
2. 将 `classroom.zju.edu.cn` 请求头中的 Cookie 粘贴到本地页面，或尝试自动读取；
3. 在页面填写 OpenAI 兼容 Base URL、模型和 API key，勾选记住后只保存在本机。

手动启动方式：

```bash
python3 app.py
python3 pipeline.py "<回放URL>"
```

在页面里：

1. 粘贴 Cookie，选择日期，点击「获取我的课程」；
2. 选择课程和具体课堂，按需检查回放状态；
3. 点击「使用此课堂」，再选择「只抓字幕」或「字幕 + AI 总结」；
4. 点击「开始」，完成后下载总结文档。

也可以继续使用备用入口：直接粘贴包含 `sub_id` 的 `livingroom` 回放地址。课程发现和回放状态查询不会调用 AI。

## 输出 Output

每次运行在工具目录 `output/<日期>_sub<课程ID>/` 暂存，完成后在界面点下载按钮：

```text
知识总结.md                    ← 最终讲义（带时间戳）
raw/transcript.txt             ← 逐段带时间戳字幕全文
raw/transcript_by_minute.txt   ← 按分钟合并的字幕
raw/ppt.json                   ← 若 Cookie 有效且课程提供 PPT
raw/video.mp4 / audio.mp3      ← 增强识别模式
```

## 工作原理 How It Works

```text
Cookie ──本地提取 _token──▶ 智云课堂课程接口
                                │
                     课程 / 课堂选择（按日期）
                                │
                     sub_id ──▶ 字幕接口
                                │
                     按分钟合并 → AI 总结
                                │
                     知识总结.md + 字幕资料
```

- 课程接口：`courseapi/v2/course-live/get-my-course-day?day=YYYY-MM-DD`
- 回放状态：`courseapi/v3/portal-home-setting/get-sub-info?course_id=...&sub_id=...`
- 字幕接口：`courseapi/v3/web-socket/search-trans-result?sub_id=...&tenant_code=112`
- 课程与回放查询只在本机携带 Cookie；Cookie、Bearer Token 不发送给 AI。
- AI 请求使用页面或 `config.json` 中配置的 OpenAI 兼容 Base URL 与模型。

## 常见问题 FAQ

- **需要登录吗？** 手动粘贴回放 URL 时，抓字幕通常不需要 Cookie；使用“获取我的课程”、回放状态检查、PPT 文字层或增强识别时需要有效智云课堂 Cookie。
- **Chrome 自动读取失败怎么办？** 新版浏览器可能锁定 Cookie 数据库或使用应用绑定加密；按页面展开的说明，从 `classroom.zju.edu.cn` 请求头手动复制 Cookie。不要把 Cookie 发给别人。
- **课程查询失败怎么办？** 先确认日期、Cookie 是否来自智云课堂域名；401/403 表示登录态失效，接口结构变化则保留手动 URL 备用入口。
- **直播中能跑吗？** 不建议，字幕和保存回放可能尚未生成；课后回放再跑。
- **AI 请求发往哪里？** 只发往页面中配置的 OpenAI 兼容 Base URL；课程 Cookie 和 Bearer Token 不会发送给 AI。

## 声明 Disclaimer

本项目仅供**个人学习**使用：抓取的是本人有权限访问的课程回放，频率极低（一节课一次请求级）。
请遵守浙大智云课堂的使用条款，**不要**用于批量抓取、传播课件或任何商业用途。
Cookie 是你的登录凭证，请勿将 `config.json` 提交到任何仓库或发给他人（`.gitignore` 已默认排除）。

## 许可 License

[MIT](LICENSE)
