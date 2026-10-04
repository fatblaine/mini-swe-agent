"""
Minimal SWE Agent — v0（手写 parser 版）
只依赖 anthropic SDK + Python 标准库。

五个部件在本文件中的位置：
  ① task / loop 与流程控制 ...... run()
  ② input（大拼接 prompt）...... SYSTEM_PROMPT / build_messages()
  ③ LLM ......................... call_llm()
  ④ output parser ............... parse_action()
  ⑤ executor（工具）............. Workspace / TOOLS / execute()

用法：
  python agent_v0.py --workdir tasks/task1_median --task "修复失败的测试" --verify "python -m pytest -q"
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from fnmatch import fnmatch
from pathlib import Path

MODEL = os.environ.get("AGENT_MODEL", "claude-sonnet-5")
MAX_STEPS = 30             # 最大轮数：防止无限循环烧钱
MAX_FORMAT_ERRORS = 3      # 连续格式错误上限
MAX_OBS_CHARS = 6000       # 单条观察的截断上限
KEEP_RECENT_OBS = 6        # 只保留最近 N 条观察的全文，更早的折叠
USE_STOP_SEQUENCE = True   # 关掉它，亲眼看模型"一次吐两个动作 / 自己编观察"


# ═════════════════════════ ⑤ executor ═════════════════════════
class Workspace:
    """所有文件操作都被锁在 root 目录内（最简陋的 sandbox）。"""

    def __init__(self, root):
        self.root = Path(root).resolve()

    def path(self, rel):
        full = (self.root / rel).resolve()
        if full != self.root and self.root not in full.parents:
            raise ValueError(f"路径越界，只能访问工作目录内的文件: {rel}")
        return full


def truncate(text, limit=MAX_OBS_CHARS):
    """保头保尾：报错信息通常在末尾，文件开头通常有 import。"""
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n\n... [中间省略 {len(text) - limit} 字符] ...\n\n{text[-half:]}"


def tool_bash(ws, cmd, timeout="60"):
    try:
        r = subprocess.run(cmd, shell=True, cwd=ws.root, capture_output=True,
                           text=True, timeout=int(timeout))
    except subprocess.TimeoutExpired:
        return f"[exit_code] TIMEOUT（超过 {timeout}s，命令被杀掉。不要运行交互式或常驻命令）"
    return f"[exit_code] {r.returncode}\n[stdout]\n{r.stdout}\n[stderr]\n{r.stderr}"


def tool_read_file(ws, path, offset="1", limit="100"):
    lines = ws.path(path).read_text(encoding="utf-8").splitlines()
    offset = max(1, int(offset))
    limit = max(1, min(int(limit), 200))        # ACI：一次最多看 200 行
    chunk = lines[offset - 1: offset - 1 + limit]
    body = "\n".join(f"{i:>5}| {line}" for i, line in enumerate(chunk, start=offset))
    end = offset + len(chunk) - 1
    more = f"（还有 {len(lines) - end} 行，用 offset={end + 1} 继续读）" if end < len(lines) else "（已到文件末尾）"
    return f"[{path}] 第 {offset}-{end} 行，共 {len(lines)} 行 {more}\n{body}"


def tool_write_file(ws, path, content):
    p = ws.path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if not content.endswith("\n"):
        content += "\n"
    p.write_text(content, encoding="utf-8")
    msg = f"已写入 {path}（{len(content.splitlines())} 行）"
    if path.endswith(".py"):                     # ACI：写完立刻回灌语法检查
        try:
            compile(content, path, "exec")
            msg += "\n[syntax] OK"
        except SyntaxError as e:
            msg += f"\n[syntax] ERROR 第 {e.lineno} 行: {e.msg}\n文件已写入但无法运行，请立刻修正。"
    return msg


SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv"}


def tool_search(ws, pattern="", glob="**/*"):
    """pattern 为空 → 只按 glob 列文件；否则在匹配的文件里做正则 grep。"""
    try:
        rx = re.compile(pattern) if pattern else None
    except re.error as e:
        return f"正则表达式无效: {e}"
    hits = []
    for f in sorted(ws.root.rglob("*")):
        rel = f.relative_to(ws.root).as_posix()
        if not f.is_file() or SKIP_DIRS & set(f.relative_to(ws.root).parts):
            continue
        g = glob.removeprefix("**/")            # 让 **/*.py 也能匹配根目录下的 a.py
        if not (fnmatch(rel, glob) or fnmatch(rel, g) or fnmatch(f.name, g)):
            continue
        if rx is None:
            hits.append(rel)
            continue
        try:
            for n, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if rx.search(line):
                    hits.append(f"{rel}:{n}: {line.strip()}")
        except (UnicodeDecodeError, OSError):
            continue
        if len(hits) >= 50:
            break
    if not hits:
        return "没有匹配结果。"
    tail = "\n（结果超过 50 条，已截断，请缩小范围）" if len(hits) >= 50 else ""
    return "\n".join(hits[:50]) + tail


TOOLS = {
    # name: (函数, 参数说明) —— 参数说明同时用于生成 prompt
    "bash": (tool_bash, "<cmd>要执行的 shell 命令</cmd>  在工作目录执行，返回 exit_code/stdout/stderr。不要运行交互式命令。"),
    "read_file": (tool_read_file, "<path>相对路径</path> <offset>起始行,默认1</offset> <limit>行数,默认100,最多200</limit>  带行号读取文件的一段。"),
    "write_file": (tool_write_file, "<path>相对路径</path> <content>完整的新文件内容</content>  整个覆盖写入。.py 文件写完会自动做语法检查。"),
    "search": (tool_search, "<pattern>正则,可空</pattern> <glob>文件通配,默认**/*</glob>  pattern 为空时列出文件，否则 grep，返回 文件:行号: 内容。"),
    "finish": (None, "<summary>你改了什么、为什么</summary>  确认测试全部通过后调用，结束任务。"),
}


def execute(ws, name, args):
    """把一切异常都变成观察文本还给模型——executor 永远不能让 loop 崩掉。"""
    if name not in TOOLS or TOOLS[name][0] is None:
        return f"[tool error] 未知工具 {name!r}。可用: {', '.join(TOOLS)}", True
    try:
        return truncate(TOOLS[name][0](ws, **args)), False
    except TypeError as e:
        return f"[tool error] 参数不对: {e}", True
    except Exception as e:  # noqa: BLE001
        return f"[tool error] {type(e).__name__}: {e}", True


# ═════════════════════════ ② input ═════════════════════════
def build_system_prompt():
    tool_docs = "\n".join(f"- {name}: {doc}" for name, (_, doc) in TOOLS.items())
    return f"""你是一个在真实代码仓库里修 bug 的软件工程 agent。

## 工作方式
每一轮你先用一两句话写出思考，然后输出**恰好一个**工具调用，然后停下，等待 <observation>。
不要自己编写 <observation>，那是系统给你的。

## 工具调用格式（严格遵守）
<tool name="工具名">
<参数名>参数值</参数名>
</tool>

示例：
我先看看测试怎么失败的。
<tool name="bash">
<cmd>python -m pytest -q</cmd>
</tool>

## 可用工具
{tool_docs}

## 规则
1. 先复现：运行测试，读报错，再定位代码。
2. 不要修改 tests/ 下的测试文件，要修的是源码。
3. write_file 会整个覆盖文件：先 read_file 读全，再写回完整内容。
4. 改完必须重新运行测试确认通过，再调用 finish。
5. 同样的动作不要重复做，没有进展就换个思路。"""


def build_messages(task, workdir):
    return [{"role": "user", "content": f"<task>\n{task}\n</task>\n\n工作目录是仓库根目录: {workdir}"}]


def compact(messages, keep=KEEP_RECENT_OBS):
    """最简单的上下文压缩：更早的观察折叠成一行，只保留最近 keep 条全文。"""
    obs_idx = [i for i, m in enumerate(messages) if m["role"] == "user" and m["content"].startswith("<observation>")]
    old = set(obs_idx[:-keep]) if len(obs_idx) > keep else set()
    return [
        {"role": m["role"], "content": f"<observation>[较早的观察已折叠，原长 {len(m['content'])} 字符]</observation>"}
        if i in old else m
        for i, m in enumerate(messages)
    ]


# ═════════════════════════ ③ LLM ═════════════════════════
def make_llm(model=MODEL, max_retries=4):
    import anthropic
    client = anthropic.Anthropic(max_retries=0)   # 关掉 SDK 自带重试，自己实现一遍
    retryable = (anthropic.RateLimitError, anthropic.APIConnectionError,
                 anthropic.InternalServerError, anthropic.APITimeoutError)

    def call(system, messages):
        for attempt in range(max_retries + 1):
            try:
                resp = client.messages.create(
                    model=model, max_tokens=4096, system=system, messages=messages,
                    stop_sequences=["</tool>"] if USE_STOP_SEQUENCE else [],
                )
                text = "".join(b.text for b in resp.content if b.type == "text")
                return text, resp.stop_reason, resp.usage.input_tokens, resp.usage.output_tokens
            except retryable as e:
                if attempt == max_retries:
                    raise
                wait = 2 ** attempt
                print(f"  [retry] {type(e).__name__}，{wait}s 后第 {attempt + 1} 次重试", file=sys.stderr)
                time.sleep(wait)
    return call


# ═════════════════════════ ④ output parser ═════════════════════════
class ParseError(Exception):
    pass


TOOL_RE = re.compile(r'<tool\s+name\s*=\s*["\']?(\w+)["\']?\s*>(.*?)</tool>', re.S)
PARAM_RE = re.compile(r"<(\w+)>(.*?)</\1>", re.S)
FENCE_RE = re.compile(r"^\s*```[\w+-]*\n(.*?)\n?```\s*$", re.S)


def strip_fence(value):
    """模型常把代码再包一层 ```python ... ```，剥掉它。"""
    m = FENCE_RE.match(value)
    return m.group(1) if m else value


def parse_action(text):
    """返回 (thought, tool_name, args, warning)。解析失败抛 ParseError，错误信息会回灌给模型。"""
    matches = TOOL_RE.findall(text)
    if not matches:
        if "<tool" in text:
            raise ParseError("检测到 <tool 但结构不完整（缺少 </tool>，或 name 属性写错）。")
        raise ParseError("没有找到工具调用。每一轮必须包含恰好一个 <tool name=\"...\">...</tool>。")
    name, body = matches[0]
    args = {}
    for key, value in PARAM_RE.findall(body):
        if key == "content":        # 代码内容：保留缩进，只去掉标签旁的换行和 markdown 围栏
            value = value.removeprefix("\n").removesuffix("\n")
            args[key] = strip_fence(value)
        else:
            args[key] = value.strip()
    if name != "finish" and not args and body.strip():
        raise ParseError(f"工具 {name} 的参数没有用 <参数名>...</参数名> 包起来。")
    warning = ""
    if len(matches) > 1:
        warning = f"[注意] 你一次输出了 {len(matches)} 个工具调用，只执行了第一个。每轮只能调用一个工具。"
    thought = text[: text.find("<tool")].strip()
    return thought, name, args, warning


# ═════════════════════════ ① loop 与流程控制 ═════════════════════════
def run(task, workdir, llm=None, verify=None, max_steps=MAX_STEPS, log=print):
    llm = llm or make_llm()
    ws = Workspace(workdir)
    system = build_system_prompt()
    messages = build_messages(task, ws.root)
    stats = {"steps": 0, "input_tokens": 0, "output_tokens": 0,
             "format_errors": 0, "tool_errors": 0, "status": "running"}
    format_streak, last_actions = 0, []

    for step in range(1, max_steps + 1):
        stats["steps"] = step
        text, stop_reason, tin, tout = llm(system, compact(messages))
        stats["input_tokens"] += tin
        stats["output_tokens"] += tout
        if stop_reason == "stop_sequence":
            text += "</tool>"                     # stop 序列本身不会出现在输出里，补回来
        messages.append({"role": "assistant", "content": text or "(空)"})

        # ── 解析 ──
        try:
            thought, name, args, warning = parse_action(text)
            format_streak = 0
        except ParseError as e:
            stats["format_errors"] += 1
            format_streak += 1
            log(f"[{step}] ✗ 格式错误: {e}")
            if format_streak >= MAX_FORMAT_ERRORS:
                stats["status"] = "format_error_limit"
                return stats
            messages.append({"role": "user", "content": f"<observation>\n[格式错误] {e}\n请按系统提示中的格式重新输出。\n</observation>"})
            continue

        log(f"[{step}] 💭 {thought[:200]}")
        log(f"[{step}] 🔧 {name} {json.dumps({k: v[:80] for k, v in args.items()}, ensure_ascii=False)}")

        # ── 终止条件：模型声称完成 → 不信，先验证 ──
        if name == "finish":
            if verify:
                out, _ = execute(ws, "bash", {"cmd": verify})
                if not out.startswith("[exit_code] 0"):
                    log(f"[{step}] ⚠ 声称完成但验证失败，打回")
                    messages.append({"role": "user", "content": f"<observation>\n你调用了 finish，但验证命令 `{verify}` 失败：\n{out}\n任务没有完成，继续。\n</observation>"})
                    continue
            stats["status"] = "finished"
            stats["summary"] = args.get("summary", "")
            return stats

        # ── 执行 ──
        obs, is_err = execute(ws, name, args)
        stats["tool_errors"] += is_err

        # ── 原地打转检测（ReAct 的典型失败模式）──
        sig = (name, json.dumps(args, sort_keys=True))
        last_actions = (last_actions + [sig])[-3:]
        if len(last_actions) == 3 and len(set(last_actions)) == 1:
            obs += "\n\n[系统提醒] 你已经连续 3 次执行完全相同的动作，结果不会改变。停下来，换一个思路。"
        if warning:
            obs = warning + "\n" + obs

        log(f"[{step}] 👀 {obs[:300]}{'...' if len(obs) > 300 else ''}\n")
        messages.append({"role": "user", "content": f"<observation>\n{obs}\n</observation>"})

    stats["status"] = "max_steps"
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--verify", default=None, help="finish 时执行的验证命令，失败会打回")
    ap.add_argument("--max-steps", type=int, default=MAX_STEPS)
    a = ap.parse_args()
    stats = run(a.task, a.workdir, verify=a.verify, max_steps=a.max_steps)
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
