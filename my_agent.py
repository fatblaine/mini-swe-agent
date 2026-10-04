"""
My Minimal SWE Agent — 手写版（照着 README 的 Step 1~5 一步步填空）
只依赖 anthropic SDK + Python 标准库。

五个部件：
  ① task / loop 与流程控制 ...... run()                     → README Step 4
  ② input（大拼接 prompt）...... build_system_prompt() 等   → README Step 2
  ③ LLM ......................... make_llm()                 → README Step 4（重试）
  ④ output parser ............... parse_action()             → README Step 3
  ⑤ executor（工具）............. Workspace / TOOLS / execute() → README Step 1

自测（默认测的就是本文件）：
  python -m pytest selftest -q
  python run_eval.py --agent mine

函数名、参数名、返回值格式要和 agent_v0.py 保持一致，否则 selftest 调不到。
卡住超过 30 分钟再去看 agent_v0.py 里对应的那一个函数。
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

MODEL = os.environ.get("AGENT_MODEL", "claude-sonnet-5-5")
MAX_STEPS = 20             # 最大轮数：防止无限循环烧钱
MAX_FORMAT_ERRORS = 3      # 连续格式错误上限
MAX_OBS_CHARS = 6000       # 单条观察的截断上限
KEEP_RECENT_OBS = 6        # 只保留最近 N 条观察的全文，更早的折叠
USE_STOP_SEQUENCE = True   # 必做实验：改成 False 跑 task3，看模型会怎么乱


# ═════════════════════════ ⑤ executor（Step 1）═════════════════════════
class Workspace:
    """所有文件操作都被锁在 root 目录内（最简陋的 sandbox）。"""

    def __init__(self, root):
        self.root = Path(root).resolve()

    def path(self, rel):
        """把相对路径解析成绝对路径；跳出 root 时抛 ValueError（信息里要含"越界"）。"""
        # resolve 后检查 root 是否是它自己或它的祖先
        abs_path = (self.root / rel).resolve()
        if not abs_path.is_relative_to(self.root):
            raise ValueError(f"路径越界，只能访问工作目录内的文件: {rel}")
        return abs_path

    def read_file(self, path):
        """读取文件内容，如果文件不存在则抛 FileNotFoundError。"""
        return self.path(path).read_text(encoding="utf-8")

    def write_file(self, path, content):
        """写入文件内容，如果父目录不存在则创建。"""
        file_path = self.path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")


def truncate(text, limit=MAX_OBS_CHARS):
    """超长时保头保尾，中间写明省略了多少字符。为什么保尾？pytest 的关键报错在末尾。"""
    # TODO
    if len(text) <= limit:
        return text
    head_len = limit // 2
    tail_len = limit - head_len - 20  # 留 20 个字符给省略提示
    return text[:head_len] + f"\n...省略 {len(text) - head_len - tail_len} 个字符...\n" + text[-tail_len:]


def tool_bash(ws, cmd, timeout="60"):
    """在 ws.root 执行 shell 命令。
    返回包含 "[exit_code] N"、stdout、stderr 的文本；超时要返回提示而不是抛异常。
    注意：loop 里验证 finish 时靠 out.startswith("[exit_code] 0") 判断成功。
    （参数从 parser 来，都是字符串，记得 int(timeout)）"""
    # TODO
    try:
        result = subprocess.run(cmd, shell=True, cwd=ws.root,
                                capture_output=True, text=True, timeout=int(timeout),
                                # Windows 默认不是 UTF-8：统一按 UTF-8 解码，解不了的字符替换掉而不是报错
                                encoding="utf-8", errors="replace",
                                env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        return f"[exit_code] {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    except subprocess.TimeoutExpired:
        return f"[exit_code] -1\nstdout:\n\nstderr:\nCommand timed out after {timeout} seconds."


def tool_read_file(ws, path, offset="1", limit="100"):
    """带行号读取 [offset, offset+limit) 行；limit 最多 200；告诉模型还剩多少行、下次 offset 填几。"""
    # 文件不存在 / 越界时直接抛异常，由 execute 统一接住并标记为出错
    content = ws.read_file(path)
    lines = content.splitlines()
    offset = max(1, int(offset))          # offset=0 或负数时，切片会从末尾取，必须兜底
    limit = max(1, min(int(limit), 200))  # ACI：一次最多看 200 行
    selected_lines = lines[offset - 1:offset - 1 + limit]
    if not selected_lines:
        return f"[{path}] 共 {len(lines)} 行，offset={offset} 已超出文件末尾。"
    end = offset + len(selected_lines) - 1
    remaining_lines = len(lines) - end
    if remaining_lines > 0:
        more = f"（还有 {remaining_lines} 行，用 offset={end + 1} 继续读）"
    else:
        more = "（已到文件末尾）"
    body = "\n".join(f"{i:>5}| {line}" for i,
                     line in enumerate(selected_lines, start=offset))
    return f"[{path}] 第 {offset}-{end} 行，共 {len(lines)} 行 {more}\n{body}"


def tool_write_file(ws, path, content):
    """整文件覆盖写入（父目录不存在要创建）。
    如果是 .py：立刻 compile() 做语法检查，把 OK / 错误行号写进返回值。"""
    # 写入失败（如越界）直接抛异常，由 execute 统一接住
    ws.write_file(path, content)
    msg = f"已写入 {path}（{len(content.splitlines())} 行）"
    if path.endswith(".py"):
        try:
            compile(content, path, 'exec')
            msg += "\n[syntax] OK"
        except SyntaxError as e:
            # 错误放在最显眼的位置，否则模型看到"已写入"就以为没问题
            return (f"[syntax ERROR] {path} 第 {e.lineno} 行: {e.msg}\n"
                    f"文件已写入但无法运行，请立刻修正。")
    return msg


SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules", ".venv"}


def tool_search(ws, pattern="", glob="**/*"):
    """pattern 为空 → 按 glob 列出文件；否则在匹配文件里正则 grep，输出 "文件:行号: 内容"。
    结果最多 50 条；跳过 SKIP_DIRS；正则写错要返回提示而不是抛异常。
    坑：fnmatch("stats.py", "**/*.py") 是 False，根目录下的文件也要能匹配到。"""
    try:
        if pattern:
            regex = re.compile(pattern)
        else:
            regex = None
    except re.error as e:
        return f"Invalid regex pattern: {e}"

    short_glob = glob.removeprefix("**/")   # 让 **/*.py 也能匹配根目录下的 stats.py
    hits = []
    for f in sorted(ws.root.rglob("*")):
        rel_parts = f.relative_to(ws.root).parts
        if not f.is_file() or SKIP_DIRS & set(rel_parts):   # 跳过目录本身和 .git 等无关目录
            continue
        rel = f.relative_to(ws.root).as_posix()             # 统一用 / 分隔，例如 textutil/slug.py
        if not (fnmatch(rel, glob) or fnmatch(rel, short_glob) or fnmatch(f.name, short_glob)):
            continue
        if regex is None:                                   # 没给 pattern：只列文件
            hits.append(rel)
        else:                                               # 给了 pattern：逐行搜索
            try:
                text = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):           # 二进制文件等读不了的，跳过
                continue
            for n, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    hits.append(f"{rel}:{n}: {line.strip()}")
        if len(hits) >= 50:
            break

    if not hits:
        return "没有匹配结果。"
    tail = "\n（结果超过 50 条，已截断，请缩小范围）" if len(hits) >= 50 else ""
    return "\n".join(hits[:50]) + tail


# name -> (函数, 参数说明)。参数说明同时用来生成 system prompt —— 只写一份。
# finish 没有函数（None），由 loop 特殊处理。
TOOLS = {
    "bash": (tool_bash,
             "<cmd>要执行的命令</cmd> <timeout>超时秒数,默认60,可省略</timeout>  "
             "在仓库根目录执行，返回 exit_code/stdout/stderr。shell 是 Windows cmd：不要用 ls/cat/grep，"
             "看文件用 read_file、找代码用 search。不要运行交互式或常驻命令。"),
    "read_file": (tool_read_file,
                  "<path>相对路径</path> <offset>起始行,默认1,可省略</offset> <limit>行数,默认100,最多200,可省略</limit>  "
                  "带行号读取文件的一段，会告诉你还剩多少行、下次 offset 填几。"),
    "write_file": (tool_write_file,
                   "<path>相对路径</path> <content>完整的新文件内容</content>  "
                   "整个覆盖写入，不是追加：先 read_file 读全，再写回完整内容。.py 文件写完会自动做语法检查。"),
    "search": (tool_search,
               "<pattern>正则,可空</pattern> <glob>文件通配,默认**/*,可省略</glob>  "
               "pattern 为空时列出匹配 glob 的文件；否则在这些文件里搜索，返回 文件:行号: 内容，最多 50 条。"),
    "finish": (None,
               "<summary>你改了什么、为什么</summary>  "
               "重新运行测试并确认全部通过后才能调用，结束任务。"),
}


def execute(ws, name, args):
    """调用 TOOLS[name]，返回 (观察文本, 是否出错)。
    永远不能抛异常：未知工具、参数不对、FileNotFoundError、越界……全部变成文字还给模型。
    正常结果记得过一遍 truncate()。"""
    if name not in TOOLS:
        return f"Unknown tool: {name}. 可用工具: {', '.join(TOOLS)}", True
    func, _ = TOOLS[name]
    try:
        if func is None:
            return "finish tool should not be executed directly.", True
        obs = func(ws, **args)
        return truncate(obs), False
    except FileNotFoundError:   # 默认信息带本机绝对路径，换成模型看得懂的相对路径
        return f"Error executing tool {name}: 文件不存在: {args.get('path')}", True
    except Exception as e:
        return f"Error executing tool {name}: {e}", True


# ═════════════════════════ ② input（Step 2）═════════════════════════
def build_system_prompt():
    """四块按顺序拼：角色 → 交互协议（先思考，再恰好一个工具调用，不要自己写 observation）
    → 工具说明（从 TOOLS 自动生成）→ 工作规则（先复现、不改测试、先读全再写、改完重跑再 finish、别重复）。"""
    # TODO
    raise NotImplementedError("Step 2: build_system_prompt")


def build_messages(task, workdir):
    """初始 messages：一条 user 消息，包含任务描述和工作目录。"""
    # TODO
    raise NotImplementedError("Step 2: build_messages")


def compact(messages, keep=KEEP_RECENT_OBS):
    """上下文压缩：以 "<observation>" 开头的 user 消息，只保留最近 keep 条全文，更早的折叠成一行。
    返回新列表，不要修改原 messages。"""
    # TODO
    raise NotImplementedError("Step 4: compact")


# ═════════════════════════ ③ LLM（Step 4）═════════════════════════
def make_llm(model=MODEL, max_retries=4):
    """返回 call(system, messages) -> (text, stop_reason, input_tokens, output_tokens)。
    - anthropic.Anthropic(max_retries=0)，关掉 SDK 自带重试，自己写指数退避
    - 可重试：RateLimitError / APIConnectionError / InternalServerError / APITimeoutError
    - USE_STOP_SEQUENCE 为 True 时传 stop_sequences=["</tool>"]"""
    import anthropic  # noqa: F401  放在函数里，离线测试不需要装好 API key
    # TODO
    raise NotImplementedError("Step 4: make_llm")


# ═════════════════════════ ④ output parser（Step 3）═════════════════════════
class ParseError(Exception):
    pass


def parse_action(text):
    """返回 (thought, tool_name, args, warning)，失败抛 ParseError。
    必须过 selftest/test_parser.py 的 9 个用例，错误信息里要含测试期望的关键词：
      - 有 "<tool" 但没闭合 → 信息含"结构不完整"
      - 完全没有工具调用   → 信息含"没有找到"
      - 参数没用 <名>..</名> 包起来 → 信息含"参数"
      - 一次两个 <tool>   → 只取第一个，warning 里写"你一次输出了 2 个..."
      - content：保留缩进（不能 strip），只去掉标签旁一个换行，并剥掉 ```python 围栏
      - name='bash' 单引号也要认；bash 命令里的 > < 不能误伤"""
    # TODO
    raise NotImplementedError("Step 3: parse_action")


# ═════════════════════════ ① loop 与流程控制（Step 4）═════════════════════════
def run(task, workdir, llm=None, verify=None, max_steps=MAX_STEPS, log=print):
    """主循环，返回 stats 字典：
      {"steps", "input_tokens", "output_tokens", "format_errors", "tool_errors", "status", ...}
    status 取值："finished" / "format_error_limit" / "max_steps"

    每一轮：
      1. text, stop_reason, tin, tout = llm(system, compact(messages))，累加 token
      2. stop_reason == "stop_sequence" 时把 "</tool>" 补回 text
      3. parse_action；失败 → format_errors+1，连续 MAX_FORMAT_ERRORS 次就退出，否则把错误作为 observation 回灌
      4. name == "finish" → 有 verify 就跑一遍，失败则回灌（信息里含"验证命令"）并继续；通过才 return
      5. execute → 统计 tool_errors
      6. 最近 3 个动作完全相同 → 在观察末尾追加提醒（含"连续 3 次"）
      7. 观察以 "<observation>\\n...\\n</observation>" 形式 append 成 user 消息
    llm 是可注入参数：selftest 会传一个"剧本 LLM"进来，零成本测试所有分支。"""
    llm = llm or make_llm()
    # TODO
    raise NotImplementedError("Step 4: run")


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
