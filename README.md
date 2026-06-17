# 超星自测题库 → PDF 生成器（chaoxing-quiz-pdf）

> 自动抓取超星学习通课程中的自测/章节测验题库，去重汇总后生成**可打印的 PDF 试卷**（题目 + 末尾参考答案），方便离线刷题。
>
> 兼容手机与电脑：手机在**微信 / QQ 内置浏览器**即可登录、抓题、打开/转发/下载 PDF。

---

## ✨ 功能特性

- **完整用户体系**：本地账号注册 / 登录（密码 SHA-256 加盐存储），可绑定并记住超星账号，下次免输密码。
- **超星账号登录**：复用学习通 `fanyalogin` 接口（AES 加密账号密码），支持 cookie 持久化。
- **三种抓题模式**：
  - **浏览器半自动**（主，推荐）：程序自动新建自测卷，并用 Playwright 打开真实浏览器进入答题页；遇到超星的**拖拽拼图验证码**时由用户手动拖一下，其余抓题/去重/出题全自动。可绕过反爬。
  - **自测（纯后台）**：直接走 `exam-ans/mooc2/exam` 接口抓自测题库——但答题页有滑块验证码，通常会被拦。
  - **章节测验**（备用）：遍历课程章节内的测验任务反复抓取去重。
- **AI 生成解析**：对「只有答案、没有解析」的题目，可配置 OpenAI 兼容接口（OpenAI / DeepSeek / Kimi / 智谱 / 通义 等），调用大模型自动补全解析；可一键测试连通性，也可关闭。
- **智能解析**：自动识别单选 / 多选 / 判断 / 填空 / 简答，内置**超星加密字体反解码**，尽力提取正确答案与解析。
- **可打印 PDF**：封面 + 题量统计 + 按题型分组的题目区（留作答空间）+ 末尾参考答案与解析区，内嵌中文字体。
- **移动端友好**：单页应用，自适应手机屏幕；PDF 支持在线预览（inline）与下载（attachment）。

---

## 🚀 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

> **浏览器半自动模式**还需下载 Chromium：
> ```bash
> python -m playwright install chromium
> ```

> **中文字体**：PDF 生成需要一个中文 TTF/OTF 字体。
> - Windows：仓库内已随附 `fonts/simhei.ttf`，开箱即用。
> - Linux / Docker：建议安装 `fonts-noto-cjk`（Dockerfile 已包含），或把 `NotoSansSC-Regular.ttf` 放进 `fonts/` 目录。

### 2. 启动服务

```bash
python app.py
# 默认 http://0.0.0.0:5000 ，可用 PORT 环境变量修改端口
```

浏览器（或手机微信/QQ）打开 `http://<服务器IP>:5000` 即可使用。

> **🔐 安全相关环境变量（生产部署务必设置）**
> | 变量 | 说明 |
> |------|------|
> | `FLASK_SECRET_KEY` | 会话签名密钥。**不设置则每次启动随机生成**（重启后登录态失效）。生产必须固定。 |
> | `APP_CRYPTO_KEY` | 加密超星密码的主密钥。不设置则在项目根目录生成 `.app_secret_key` 文件持有。 |
> | `CORS_ORIGINS` | 允许跨域携带凭证的来源（逗号分隔）。默认不开放跨域（建议前后端同源部署）。 |
> | `COOKIE_SECURE` | 设为 `1` 时 Session Cookie 仅经 HTTPS 传输（HTTPS 部署建议开启）。 |
> | `CX_DEBUG_DUMP` | 设为 `0` 关闭调试 HTML 落盘（含会话令牌，默认开启用于校准）。 |

> ⚠️ **浏览器半自动模式仅适合在「用户本机」运行**：它会在运行服务的机器上弹出 Chromium 让用户手动过验证码。
> 部署在远程服务器时用户看不到该窗口，且无显示器的服务器会因缺少图形环境无法启动浏览器。

### 3. 使用流程

1. 注册 / 登录本地账号。
2. 输入超星手机号与密码，登录超星。
3. 加载课程 → 选择课程 → 选择抓题模式与参数。
4. 点击「开始抓题并生成 PDF」，等待进度完成。
5. **在线打开** 或 **下载** PDF：
   - 微信/QQ 内：点「在线打开」后，再点右上角 `···` →「在浏览器打开 / 用其他应用打开」即可保存或转发。
   - 电脑浏览器：点「下载到本地」可选择保存位置。

---

## 🐳 Docker 部署

```bash
docker build -t chaoxing-quiz-pdf .
docker run -d -p 5000:5000 --name quiz-pdf chaoxing-quiz-pdf
```

---

## 📁 项目结构

```
chaoxing-quiz-pdf/
├── api/
│   ├── base.py          # 超星接口封装：登录 / 课程 / 章节 / 抓题（每实例独立 session，多用户隔离）
│   ├── cipher.py        # AES 加密（登录用）
│   ├── cookies.py       # 按用户隔离的 cookie 持久化
│   ├── decode.py        # HTML 解析：课程 / 章节 / 题目（含答案提取）
│   ├── font_decoder.py  # 超星加密字体解码器
│   ├── cxsecret_font.py # 字形哈希 → 原字符 映射
│   ├── database.py      # SQLite 用户体系
│   └── config.py        # 常量 / 请求头
├── quiz_fetcher.py      # 抓题策略：自测抽题 / 章节测验 累积去重
├── pdf_builder.py       # fpdf2 生成 PDF 试卷
├── ai_explainer.py      # AI 生成解析（OpenAI 兼容接口）
├── app.py               # Flask 后端（认证 / 抓题任务 / AI / PDF 下载）
├── web/index.html       # 移动优先单页前端（零构建）
├── resource/font_map_table.json  # 字体哈希映射表
├── fonts/               # 内嵌中文字体
├── requirements.txt
└── Dockerfile
```

---

## ⚙️ 抓题原理（自测，已按真实接口实现）

超星「自测」从课程题库**随机抽题**，没有公开的「导出整库」接口。本项目复刻自测的完整流程：

1. `GET  /exam-ans/mooc2/exam/exam-question-count` —— 查询题库可抽题量；
2. `POST /exam-ans/mooc2/exam/create-self-test`（`questionNum`=抽题数）—— 异步组卷，返回 `taskId`；
3. `GET  /exam-ans/mooc2/exam/selftest-autopapertask-status?taskId=` —— 轮询至 `taskStatus:"ok"`，拿到 `paperId`；
4. `GET  /exam-ans/mooc2/exam/exam-list` —— 解析列表里 `goTest(courseId, tId, relationId, endTime, paperId, isRetest, enc)`，按 `paperId` 定位本卷的 `tId / relationId / enc`；
5. `GET  /exam-ans/exam/lookPaper?...&isPreview=true`（或 `reVersionTestStartNew`）—— 取整卷题目 HTML，用 `decode_questions_info` 解析。

**反复新建自测 + 题干指纹去重**即可逼近完整题库（单卷上限常见 500 题）。
相关接口集中在 `api/base.py` 的 `Chaoxing.EXAM_HOST` 与 `*selftest*` 方法。
抓不到答案/解析的题，可开启「AI 生成解析」补全。

---

## 🔒 隐私与免责声明

- 你的本地账号密码经 SHA-256 加盐后存储；超星凭证仅保存在**运行该服务的本地数据库**中，不会上传任何第三方。
- 运行期文件（`users.db`、`sessions/`、`output/*.pdf`、日志）已在 `.gitignore` 中排除，不会被提交。
- 本项目仅供个人学习自测使用，请遵守超星平台服务条款与所在学校的相关规定，**请勿用于任何作弊或商业用途**。由使用本工具产生的一切后果由使用者自行承担。

---

## 🙏 致谢

超星接口对接、加密字体解码等逻辑参考并改编自 [Samueli924/chaoxing](https://github.com/Samueli924/chaoxing) 及相关社区项目。
