# 复用 tale_of_xxt Skill

这份说明面向想在自己 Codex 环境中复用 `tale_of_xxt` skill 的用户。

## 这个 Skill 做什么

输入一个豆瓣播客单集链接，workflow 会：

1. 从豆瓣单集接口读取元数据和音频地址。
2. 下载音频，并按 2 分钟时间窗切成多个 chunk。
3. 调用 OpenRouter 的 Qwen3-ASR 模型转写每个 chunk。
4. 汇总成 `{时间窗 + ASR 内容}` 的 `asr.md`。
5. 基于 ASR 生成粗时间粒度总结和结构化内容大纲。
6. 对关键专名做外部校正，并在最终文件里列出参考来源。

## 安装方式

把整个 skill 目录复制到目标机器的 Codex skills 目录：

```bash
mkdir -p ~/.codex/skills
cp -R tale_of_xxt ~/.codex/skills/
```

安装后应当是这个结构：

```text
~/.codex/skills/tale_of_xxt/
├── SKILL.md
├── README.md
├── references/
│   └── manifest-schema.md
└── scripts/
    └── tale_of_xxt_workflow.py
```

确认脚本可执行：

```bash
chmod +x ~/.codex/skills/tale_of_xxt/scripts/tale_of_xxt_workflow.py
```

如果 Codex 当前会话已经启动，建议重开一个 Codex 会话，让新 skill 被重新发现。

## 依赖

目标环境需要：

- Codex
- Python 3
- `ffmpeg` 和 `ffprobe`
- 可访问豆瓣音频地址和 OpenRouter API
- OpenRouter API key

检查依赖：

```bash
python3 --version
ffmpeg -version
ffprobe -version
```

macOS 可用 Homebrew 安装 ffmpeg：

```bash
brew install ffmpeg
```

## 配置 OpenRouter API Key

不要把 API key 写进脚本或 markdown 文件。推荐在 shell 环境中设置：

```bash
export OPENROUTER_API_KEY="sk-or-v1-..."
```

如果希望长期生效，可写入自己的 shell 配置文件，例如 `~/.zshrc`：

```bash
echo 'export OPENROUTER_API_KEY="sk-or-v1-..."' >> ~/.zshrc
source ~/.zshrc
```

调用 skill 前，Codex 应优先检查是否存在 `OPENROUTER_API_KEY`。如果没有，应先提醒用户配置 key，而不是继续跑 ASR。

## 验证安装

运行：

```bash
python3 ~/.codex/skills/tale_of_xxt/scripts/tale_of_xxt_workflow.py --help
```

能看到这些子命令即表示脚本可用：

```text
prepare
transcribe
build-asr
ranges
validate-asr
run
```

## 基本使用

推荐让 Codex 自动使用这个 skill。可以这样对 Codex 说：

```text
请使用 tale_of_xxt skill，基于这个豆瓣播客单集链接生成粗时间粒度总结和结构化大纲：
<豆瓣播客单集链接>
```

也可以手动跑机械步骤。

先准备音频和 manifest：

```bash
python3 ~/.codex/skills/tale_of_xxt/scripts/tale_of_xxt_workflow.py prepare \
  --url "<豆瓣播客单集链接>" \
  --workdir "./tale_of_xxt_work"
```

短音频可直接转写：

```bash
python3 ~/.codex/skills/tale_of_xxt/scripts/tale_of_xxt_workflow.py transcribe \
  --manifest "./tale_of_xxt_work/manifest.json" \
  --workers 2
```

生成 ASR markdown：

```bash
python3 ~/.codex/skills/tale_of_xxt/scripts/tale_of_xxt_workflow.py build-asr \
  --manifest "./tale_of_xxt_work/manifest.json"
```

然后让 Codex 根据 `asr.md` 和 `summary_prompt.md` 写最终的 `coarse_summary_outline.md`。

## Multi-Agent ASR 用法

长音频建议使用 Codex 的 multi-agent 能力并行转写。

主 agent 先运行：

```bash
python3 ~/.codex/skills/tale_of_xxt/scripts/tale_of_xxt_workflow.py prepare \
  --url "<豆瓣播客单集链接>" \
  --workdir "./tale_of_xxt_work"
```

计算分段：

```bash
python3 ~/.codex/skills/tale_of_xxt/scripts/tale_of_xxt_workflow.py ranges \
  --manifest "./tale_of_xxt_work/manifest.json" \
  --agents 4
```

然后主 agent 使用 `multi_agent_v1.spawn_agent`，把不同 chunk 范围分给不同 worker。每个 worker 必须写入独立文件，例如：

```bash
python3 ~/.codex/skills/tale_of_xxt/scripts/tale_of_xxt_workflow.py transcribe \
  --manifest "./tale_of_xxt_work/manifest.json" \
  --start 0 \
  --end 23 \
  --output "./tale_of_xxt_work/asr_results/part_000_023.jsonl" \
  --workers 2
```

不要让多个 worker 写同一个 JSONL 文件。

所有 worker 完成后，主 agent 校验：

```bash
python3 ~/.codex/skills/tale_of_xxt/scripts/tale_of_xxt_workflow.py validate-asr \
  --manifest "./tale_of_xxt_work/manifest.json" \
  --input ./tale_of_xxt_work/asr_results/part_*.jsonl
```

校验通过后合并生成 `asr.md`：

```bash
python3 ~/.codex/skills/tale_of_xxt/scripts/tale_of_xxt_workflow.py build-asr \
  --manifest "./tale_of_xxt_work/manifest.json" \
  --input ./tale_of_xxt_work/asr_results/part_*.jsonl
```

## 最终输出要求

最终文件通常命名为：

```text
coarse_summary_outline.md
```

应包含：

- `专名校正说明`
- `粗时间粒度内容总结`
- `内容大纲（含最早出现时间）`
- `外部校正参考`

总结应当是编辑后的 episode guide，不是 ASR 逐句复述。关键名词要尽量校正：

- 店铺、餐厅、酒吧、厂牌、地点：优先查大众点评、美团、地图、品牌官网。
- 书影音、文艺作品、人名：优先查豆瓣。
- 酒款和酒厂：可查品牌官网、电商页、Untappd、RateBeer 等。

## 分享时不要包含

分享这份 skill 给别人时，建议只分享：

- `SKILL.md`
- `README.md`
- `scripts/tale_of_xxt_workflow.py`
- `references/manifest-schema.md`

不要分享：

- OpenRouter API key
- 已下载音频
- ASR 结果
- 临时工作目录
- 包含个人路径或隐私信息的产物

## 常见问题

如果提示没有 OpenRouter API key：

```text
需要 OpenRouter API key 才能调用 Qwen3-ASR。
```

设置 `OPENROUTER_API_KEY` 后重新运行。

如果提示找不到 `ffmpeg`：

```bash
brew install ffmpeg
```

如果 ASR 缺片段：

1. 运行 `validate-asr` 看缺哪些 chunk。
2. 只重跑缺失范围的 `transcribe`。
3. 再运行 `build-asr`。
