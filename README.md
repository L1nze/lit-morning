# 📚 LitMorning（lit-zaobao）· 每日论文早报

每天自动抓取**你研究方向**的新文献：多源检索 → 关键词 + 语义打分 → 期刊分级/分区标注 → LLM 中文摘要 → 推送到微信/邮件，并生成可订阅的网页归档。

纯 Python 标准库，**零第三方依赖**（3.10+），克隆即用；任何学科都能用，改一处配置即可换成你的方向。

## 功能一览

| 模块 | 能力 |
|---|---|
| 数据源 | arXiv / OpenAlex / Crossref / Scopus，并发抓取，单源故障不影响整体 |
| 筛选 | 关键词打分、负向词一票否决、跨源去重、SQLite 已读去重 |
| 期刊 | 静态分级（顶刊/权威加分，可设门槛）+ 中科院/JCR/IF 分区接口标注（EasyScholar / OneScholar） |
| 摘要 | LLM 中文三句话摘要，9 家服务商可切换、四级兜底、按论文 id 缓存（重跑不重复花钱） |
| 个性化 | 点赞 → 关键词画像加权；配 embedding 后叠加语义加分与漏网捞回（可选） |
| 推送 | Server酱 / PushPlus / 企业微信 / WxPusher / 邮件，主备双通道互备，超长自动分片 |
| 归档 | 每日 HTML / Markdown / JSON + 历史导航页 + RSS，一键发布到 GitHub Pages |
| 其他 | 周报统计、摘要双语翻译（8 引擎）、桌面 GUI（可打包 exe）、全链路自检 `doctor` |

## 快速开始

```bash
git clone https://github.com/L1nze/lit-morning.git
cd lit-morning
python bot.py init                    # 生成 config.json
python bot.py today --no-push --open  # 本地试跑一轮，浏览器里看效果
```

然后编辑 `config.json`，把示例方向换成你自己的（就这三处）：

```jsonc
"research_focus": "你的研究方向（用于生成摘要提示词）",
"app_tagline": "你的日报副标题",
"keywords": {
  "topic_terms":   ["..."],   // 主题词：命中才算相关（权重 ×3，可设为必须）
  "method_terms":  ["..."],   // 方法词（×2）
  "bonus_terms":   ["..."],   // 加分词（×1）
  "exclude_terms": ["..."]    // 负向词：一票否决，剔掉不想要的噪声
}
```

> 旧版配置键名会自动兼容迁移，无需手动改。

### 可选增强（不配也能用，配了更好）

| 想要 | 配置 | 说明 |
|---|---|---|
| 微信推送 | `push.serverchan_sendkey` | [Server酱](https://sct.ftqq.com) 免费 5 条/天；`push.fallback_provider` 可设备通道（企微 / WxPusher / 邮件等，凭据都在 `push` 节） |
| LLM 中文摘要 | `llm.providers.<服务商>.api_key` | 内置智谱/DeepSeek/Kimi/百炼/OpenAI 等 9 家预设；`python bot.py models` 查看/切换/测试 |
| 期刊分区 | `journal_rank.*` | EasyScholar 免费 Key，或 OneScholar；日报直接标注中科院分区/JCR/IF |
| 语义召回 | 配好 LLM key 即自动启用 | 或 `llm.embedding_model` 显式指定；`semantic.enabled: false` 可关 |
| 网页归档 | `site_url` | 填 GitHub Pages 地址后，推送超长自动附「完整版」链接 |

配好后：

```bash
python bot.py push-test   # 测试推送通道
python bot.py today       # 正式跑一轮：抓取 → 筛选 → 摘要 → 落盘 → 推送
```

## 每天自动跑（三选一）

1. **GitHub Actions（推荐，电脑完全不用开）**：推到你的 GitHub → Settings → Secrets 添加 `LITZB_SERVERCHAN_SENDKEY`（可选 `LITZB_LLM_API_KEY` / `LITZB_LLM_ACTIVE`）；**研究方向与关键词**同样走 Secrets（`config.json` 不入库）：`LITZB_RESEARCH_FOCUS` + `LITZB_TOPIC_TERMS` / `LITZB_METHOD_TERMS` / `LITZB_BONUS_TERMS` / `LITZB_EXCLUDE_TERMS`（词组逗号分隔），不配则用内置示例方向 → 每天 08:37（北京时间）自动出报并归档回仓库。再到 Settings → Pages 把 Source 设为 **GitHub Actions**，即得归档站 + RSS（把地址填进 `site_url` 生效完整版链接）。
2. **Windows 桌面版**：`build-exe.cmd` 打包单文件 exe，窗口里「⏰ 每日推送」一键注册计划任务，换机器拷走 exe 即部署。
3. **命令行**：`python bot.py daemon --at 08:30` 常驻定时，或 `register-task.cmd` 注册计划任务（错过开机自动补跑）。

## 常用命令

```bash
python bot.py today [--no-push] [--days 5] [--limit 20] [--open]  # 出今日早报
python bot.py like <论文id或链接>      # 点赞 → 个性化加权；unlike 撤销
python bot.py weekly [--days 7]       # 周报：收录趋势/来源/期刊层次/热词
python bot.py doctor                  # 全链路自检（逐环节真实网络检查）
python bot.py models [--use p/m] [--test]   # 查看/切换/测试 LLM 服务商
python bot.py rank "期刊名"           # 查期刊分区（不带参数 = 测试分区服务）
python bot.py serve / gui / stats / push-test / likes
```

桌面 GUI：`python bot.py gui`——论文阅读页（双语摘要、期刊徽章、翻译）、历史日报、设置框（服务商/推送/数据源/分区一站式配置）。

## 测试

```bash
python -m unittest discover -s tests   # 260+ 用例
```

## License

MIT
