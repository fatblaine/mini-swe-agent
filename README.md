# 手写你的第一个 Minimal SWE Agent

> 对应学习路线 **阶段 2（W2–W3，约 15h）**。
> 纪律：不用 LangChain / LangGraph / 任何 agent 框架。只用 `anthropic` SDK + 标准库，单文件，500 行以内。

本目录里的代码是**参考答案**，不是让你直接跑的。正确用法：新建一个空文件 `my_agent.py`，按下面 8 步自己敲一遍，每步做完跑对应的自测，卡住了再对照参考实现。

```
mini-swe-agent/
├── agent_v0.py          # 参考答案 v0：手写 parser（337 行）
├── agent_v1.py          # 参考答案 v1：native tool calling（143 行，复用 v0 的 executor）
├── make_tasks.py        # 生成 3 个埋了 bug 的小 repo
├── run_eval.py          # 跑评测：独立验证 + 记录轮数/token/成败
└── selftest/            # 离线测试：parser 刁难用例 + 用"剧本 LLM"测 loop，不花钱
```

---

## 0. 过关标准（先看终点）

```bash
python make_tasks.py
python run_eval.py --agent v0      # 3/3 通过
python run_eval.py --agent v1      # 3/3 通过
```

"通过"由 `run_eval.py` **独立判定**：重新跑一次 pytest，并检查 `tests/` 没被改过。agent 自己说"我修好了"不算数。

| 任务 | bug 在哪 | 考察点 |
|---|---|---|
| task1_median | `stats.py` 偶数长度中位数 | 最基本的 跑测试 → 读报错 → 改 → 重跑 |
| task2_slugify | `textutil/slug.py` 连字符处理 | 一次改动要同时满足多个断言 |
| task3_cart | 测试在 `cart.py`，bug 在 `pricing.py` | 必须用 search / read 跨文件追根因 |

---

## 1. 环境准备（10 分钟）

```bash
python -m venv .venv && source .venv/bin/activate
pip install anthropic pytest
export ANTHROPIC_API_KEY=sk-ant-...
export AGENT_MODEL=claude-sonnet-5        # 可选，默认就是它；想省钱可换 claude-haiku-4-5-20251001
python -m pytest selftest -q               # 应该 15 passed（不调 API）
```

---

## 2. 先在纸上画出 loop（30 分钟，不写代码）

整个 agent 就是这张图：

```
            ┌────────────────────────────────────────────────┐
            │  ① loop：for step in range(MAX_STEPS)           │
            │                                                 │
 task ──►  ② input 拼接 ──► ③ LLM ──► ④ parser ──► ⑤ executor │
            │   ▲  system + 工具说明 + 历史 + 观察       │     │
            │   └────────────── observation ◄─────────────┘     │
            │                                                 │
            │  出口：finish(且验证通过) / 超轮数 / 格式错误超限  │
            └────────────────────────────────────────────────┘
```

写下每个部件**坏掉时会表现成什么**，这是你以后调试任何 agent 的故障对照表：

| 部件 | 失效表现 |
|---|---|
| ① loop | 无限循环烧钱；模型说"完成了"就信了，实际没修好 |
| ② input | 模型不知道有哪些工具 / 格式；上下文爆掉；旧观察把新信息淹没 |
| ③ LLM | 429/500 直接让整个任务崩掉 |
| ④ parser | 一个多余的 ``` 或少一个闭合标签就卡死 |
| ⑤ executor | 一个异常把 loop 打崩；`cat` 一个大文件把上下文塞满；命令卡住不返回 |

---

## 3. Step 1 — executor：先写工具，完全不碰 LLM（2h）

**目标**：4 个工具 + 一个 `execute(ws, name, args) -> (观察文本, 是否出错)`。

| 工具 | 签名 | 必须做到 |
|---|---|---|
| bash | `bash(cmd)` | 返回 exit_code、stdout、stderr 三样；设超时 |
| read_file | `read_file(path, offset, limit)` | 带行号；限制一次最多 200 行；告诉模型还剩多少行 |
| write_file | `write_file(path, content)` | 写完如果是 `.py`，立刻 `compile()` 做语法检查并把结果写进返回值 |
| search | `search(pattern, glob)` | pattern 空 → 列文件；非空 → grep 出 `文件:行号: 内容`；结果封顶 50 条 |

这些限制都来自 SWE-agent 论文的 **ACI（Agent-Computer Interface）** 思想，理解为什么比会写更重要：

- **为什么限制行数**：人用 `cat` 看完大文件会自己跳读，模型不会。几千行一次性塞进上下文，它的注意力被稀释，而且后面每一轮都要为这些 token 付费。
- **为什么带行号**：模型需要精确引用"第 12 行"，没有行号它会自己数，经常数错。
- **为什么写完立刻回灌语法检查**：不这么做，模型会把一个缩进错误的文件写进去，下一步跑测试看到一堆 ImportError，然后被带偏去修不存在的问题。错误反馈离动作越近越好。
- **为什么 execute 要吞掉所有异常**：工具出错是**观察**，不是**崩溃**。`FileNotFoundError` 应该变成一段文字还给模型，让它自己改路径。

另外两个必须有的：
- `Workspace.path()` 把所有路径锁在工作目录内（最简陋的 sandbox）。
- `truncate()` 截断过长输出时**保头保尾**，因为 pytest 的关键报错在末尾。

**自测**：在 Python REPL 里手动调用每个工具，对着 `tasks/task1_median/repo` 跑一遍。然后：
```bash
python -m pytest selftest/test_loop_offline.py -k "escape or glob" -q
```

---

## 4. Step 2 — input：写 system prompt（1h）

**目标**：`build_system_prompt()` + `build_messages(task)`。

prompt 由四块拼成，按这个顺序：
1. **角色**：一句话，你是在真实仓库里修 bug 的 agent。
2. **交互协议**：每轮先写思考，再输出**恰好一个**工具调用，然后停。**不要自己写 observation**。
3. **工具说明**：从 `TOOLS` 字典自动生成，别手写两份（改了工具忘了改 prompt 是常见 bug）。
4. **工作规则**：先复现、不改测试、写前先读全、改完重跑再 finish、别重复同一动作。

历史的组织：`user(task)` → `assistant(思考+动作)` → `user(<observation>…</observation>)` → `assistant` → …

注意第 4 块里的每一条规则，都对应一个你之后会亲眼看到的失败。先写上，跑的时候观察模型是否遵守。

---

## 5. Step 3 — 手写 parser（3h，本路线最重要的一步）

**目标**：`parse_action(text) -> (thought, name, args, warning)`，失败抛 `ParseError`。

格式：
```xml
我先跑一下测试。
<tool name="write_file">
<path>stats.py</path>
<content>
def median(xs): ...
</content>
</tool>
```

你**一定**会撞上这些情况，每个都要有明确策略：

| 模型的输出 | 策略 |
|---|---|
| 整段包在 ` ```xml ... ``` ` 里 | 正则直接在全文里找 `<tool`，围栏自然被忽略 |
| `<content>` 里又包了一层 ` ```python ` | 只对 content 剥围栏 |
| 一次输出两个 `<tool>` | 只执行第一个，在观察里警告它 |
| 少了 `</tool>` | 抛 ParseError，错误信息回灌给模型，让它重来 |
| 完全没有工具调用（只在"思考"） | 同上 |
| `name='bash'` 单引号 | 正则兼容 |
| bash 命令里有 `>` `<` 重定向 | 参数正则用 `<(\w+)>(.*?)</\1>`，不会误伤 |
| content 缩进 | **不能** `.strip()`，只去掉标签旁边那一个换行 |

**两个核心设计决定**：

1. **错误回灌，而不是崩溃或静默修复**。解析失败时把原因写成 observation 发回去，模型下一轮通常能自我纠正。但要设预算（`MAX_FORMAT_ERRORS = 3`），连续失败就放弃。
2. **stop sequence**。调用 API 时设 `stop_sequences=["</tool>"]`，模型写完第一个动作就被强制截停，从根上消灭"一次两个动作"和"自己编 observation"。代价是返回的文本里没有 `</tool>`，你要自己补上。这就是 ReAct 论文实现里在 `Observation:` 处截停的同一个技巧。

**自测**：
```bash
python -m pytest selftest/test_parser.py -q     # 9 个刁难用例全绿
```

**必做实验**：接上真模型后，把 `USE_STOP_SEQUENCE` 改成 `False` 跑 task3 几次，记录你看到了什么。这是 ReAct 脆弱性的第一手经验，面试时这是能讲出细节的东西。

---

## 6. Step 4 — loop 与流程控制（3h）

**目标**：`run(task, workdir, llm, verify, max_steps) -> stats`。这是你后端的老本行：一个带状态、有出口、能重试的状态机。

骨架：
```python
for step in range(1, max_steps + 1):
    text = llm(system, compact(messages))       # ② ③
    try:    name, args = parse_action(text)     # ④
    except ParseError: 回灌错误; continue
    if name == "finish": 验证通过才 return
    obs = execute(ws, name, args)               # ⑤
    messages.append(observation)
return "max_steps"
```

需要逐一实现的控制点：

| 控制点 | 实现 | 防的是什么 |
|---|---|---|
| 最大轮数 | `MAX_STEPS = 30` | 无限循环烧钱 |
| **终止验证** | `finish` 时跑 `verify` 命令，失败就打回，继续循环 | 模型谎报完成（非常常见） |
| API 重试 | 429 / 500 / 连接错误做指数退避；把 SDK 自带重试关掉（`max_retries=0`）自己写一次 | 一次网络抖动毁掉整个任务 |
| 格式错误预算 | 连续 3 次解析失败就退出 | 模型进入格式混乱的死循环 |
| 原地打转检测 | 最近 3 个动作完全相同 → 在观察里插入提醒 | ReAct 典型失败：重复动作 |
| 上下文压缩 | `compact()`：只保留最近 6 条观察全文，更早的折叠成一行 | 上下文膨胀；旧报错干扰判断 |
| 统计 | 每轮累加 `input_tokens` / `output_tokens`，记录格式错误和工具错误数 | 没有数据就没法比较 v0 和 v1 |

**关键技巧：把 LLM 做成可注入的参数**。`run(..., llm=...)` 接收任何 `(system, messages) -> (text, stop_reason, in_tok, out_tok)` 的函数。这样你可以写一个"剧本 LLM"按顺序吐预设回复，零成本测试所有流程分支：

```bash
python -m pytest selftest/test_loop_offline.py -q
```

这 6 个测试分别覆盖：正常完成、谎报完成被打回、格式错误预算耗尽、打转检测 + 超轮数、路径越界、glob 匹配。**loop 的所有分支都应该先在离线测试里跑通，再接真模型。**

---

## 7. Step 5 — 接真模型，跑评测（2h）

```bash
python make_tasks.py
python agent_v0.py --workdir tasks/task1_median/repo \
    --task "stats.median 偶数长度结果不对，修复它，不要改测试" \
    --verify "python -m pytest -q"
```

先单跑一个，逐行看日志（💭 思考 / 🔧 动作 / 👀 观察）。然后：

```bash
python run_eval.py --agent v0
```

`run_eval.py` 每次从干净副本开始，结束后**独立**重跑测试并比较 `tests/` 的哈希，结果追加到 `results_v0.jsonl`。

**如果没有 3/3**，按这个顺序排查：
1. 看 `format_errors`：高 → 问题在 ② prompt 或 ④ parser。
2. 看日志里有没有同一个动作反复出现 → ① 打转，或 ⑤ 观察信息不够让它看到新东西。
3. 看它 write_file 后是否丢了原文件的其他函数 → prompt 里"先读全再写"没被遵守。
4. `tests_tampered` 为真 → 它在作弊，加强 prompt 规则，并且欣慰你的评测抓到了。

---

## 8. Step 6 — v1：换成 native tool calling（2h）

**目标**：只替换 ② 和 ④，① 和 ⑤ 原样复用。看 `agent_v1.py` 顶部的 `from agent_v0 import ... execute`。

变化点：

| | v0 | v1 |
|---|---|---|
| 工具说明 | 写在 system prompt 里的文字 | `tools=[{name, description, input_schema}]` |
| 模型输出 | 一段文本，你用正则解析 | `content` 里的 `tool_use` block，参数已是 JSON |
| 回传观察 | `user: <observation>...</observation>` | `user: [{"type":"tool_result","tool_use_id":...}]` |
| 一轮多个动作 | 你要禁止（stop sequence） | 允许，但**每个 tool_use 都必须有对应的 tool_result**，否则 API 报错 |
| 终止信号 | 自定义 `finish` 工具 | `stop_reason != "tool_use"` |
| 新增处理 | — | `stop_reason == "max_tokens"` 时让它继续 |
| 压缩 | 替换字符串 | 必须保持 block 结构和 `tool_use_id` 一一对应 |

```bash
python run_eval.py --agent v1
python run_eval.py --agent v0 --repeat 3 --quiet
python run_eval.py --agent v1 --repeat 3 --quiet
```

然后填这张表（这就是"框架和 API 替你省掉了什么"的量化答案）：

| 指标 | v0 | v1 | 差值 |
|---|---|---|---|
| 代码行数（去掉共享部分） | | | |
| 成功率（9 次） | | | |
| 平均轮数 | | | |
| 平均 input tokens | | | |
| 格式错误总数 | | 0（结构上不可能） | |

预期：v1 格式错误归零，平均轮数可能略少（并行调用）。但 loop、验证、重试、压缩、executor 这些**一行都没省** —— 这正是 agent 工程里真正的工作量所在，也是你用 LangGraph 时框架替你藏起来的东西。

---

## 9. 扩展练习（选做，按价值排序）

1. **轨迹落盘**：每轮把 messages 写成 JSON，出问题时能回放。这就是可观测性的起点。
2. **str_replace 工具**：`edit(path, old, new)` 替代整文件覆盖。对比两者在 task3 上的 token 消耗，体会 SWE-agent 为什么专门设计编辑命令。
3. **加一个更难的任务**：bug 在测试没覆盖的分支里，需要 agent 自己写一个复现用例。
4. **换模型对比**：`AGENT_MODEL=claude-haiku-4-5-20251001` 跑同一套评测，看小模型在哪个部件上先崩。

---

## 10. 做完之后

路线里写好的下一步：**对照实验** —— 把 工友通 的 Supervisor 拆掉，五个子 agent 合并成一个大 prompt 加全部工具，跑同一批评测用例。你刚手写完 loop，现在最清楚多 agent 到底多做了什么，也能用 `run_eval.py` 同样的思路（独立验证 + 记录轮数/token）去设计那个实验。
