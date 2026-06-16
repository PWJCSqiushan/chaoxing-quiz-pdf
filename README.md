# 超星自测题库 → PDF 生成器（chaoxing-quiz-pdf）

> 自动抓取超星学习通课程中的自测/章节测验题库，去重汇总后生成**可打印的 PDF 试卷**（题目 + 末尾参考答案），方便离线刷题。
>
> 兼容手机与电脑：手机在**微信 / QQ 内置浏览器**即可登录、抓题、打开/转发/下载 PDF。

---

## ✨ 功能特性

- **完整用户体系**：本地账号注册 / 登录（密码 SHA-256 加盐存储），可绑定并记住超星账号，下次免输密码。
- **超星账号登录**：复用学习通 `fanyalogin` 接口（AES 加密账号密码），支持 cookie 持久化。
- **两种抓题模式**：
  - **自测抽题**（主，推荐）：调用超星「自测」独立功能（导航 `dataname="zc"` → `mooc2/exam/exam-list`），反复新建自测卷从课程题库随机抽题，按题干指纹自动去重累积，逼近完整题库。
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

> **中文字体**：PDF 生成需要一个中文 TTF/OTF 字体。
> - Windows：仓库内已随附 `fonts/simhei.ttf`，开箱即用。
> - Linux / Docker：建议安装 `fonts-noto-cjk`（Dockerfile 已包含），或把 `NotoSansSC-Regular.ttf` 放进 `fonts/` 目录。

### 2. 启动服务

```bash
python app.py
# 默认 http://0.0.0.0:5000 ，可用 PORT 环境变量修改端口
```

浏览器（或手机微信/QQ）打开 `http://<服务器IP>:5000` 即可使用。

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

## ⚙️ 抓题原理

超星「自测」是从课程题库中**随机抽题**，没有公开的「导出整个题库」接口。本项目据此采用：

1. 进入课程的「自测」模块（导航 `dataname="zc"` → `https://mooc1.chaoxing.com/mooc2/exam/exam-list`）；
2. 反复「新建自测」并设置抽题数量，让系统从题库随机抽题；
3. 用 `decode_questions_info` 解析每份自测卷的题干 / 选项 / 题型 / 答案；
4. 按**题干指纹去重累积**，把多次随机抽到的题目汇总为尽量完整的题库。

> 自测模块的创建 / 答题接口参数集中在 `api/base.py` 顶部的 `SELFTEST_*` 常量与对应方法中，
> 便于按真实抓包结果快速校准。备用的「章节测验」模式走 `mooc-ans/api/work` 接口。
> 抓不到答案的题目，可开启「AI 生成解析」补全，或在 PDF 中标注「（题库未提供）」。

---

## 🔒 隐私与免责声明

- 你的本地账号密码经 SHA-256 加盐后存储；超星凭证仅保存在**运行该服务的本地数据库**中，不会上传任何第三方。
- 运行期文件（`users.db`、`sessions/`、`output/*.pdf`、日志）已在 `.gitignore` 中排除，不会被提交。
- 本项目仅供个人学习自测使用，请遵守超星平台服务条款与所在学校的相关规定，**请勿用于任何作弊或商业用途**。由使用本工具产生的一切后果由使用者自行承担。

---

## 🙏 致谢

超星接口对接、加密字体解码等逻辑参考并改编自 [Samueli924/chaoxing](https://github.com/Samueli924/chaoxing) 及相关社区项目。
