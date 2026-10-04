"""
Minimal SWE Agent — v1（native tool calling 版）

和 v0 对比：executor（⑤）与 loop 骨架（①）原样复用，
只换掉了 ②input 的工具说明 和 ④output parser —— 这两块由 API 替你做了。
"""
import argparse
import json
import sys
import time

from agent_v0 import MAX_STEPS, MODEL, KEEP_RECENT_OBS, Workspace, execute

# ② input：工具说明从"写在 prompt 里的文字"变成"JSON Schema"
TOOL_SCHEMAS = [
    {"name": "bash", "description": "在仓库根目录执行 shell 命令，返回 exit_code/stdout/stderr。不要运行交互式命令。",
     "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]}},
    {"name": "read_file", "description": "带行号读取文件的一段，一次最多 200 行。",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "相对路径"},
         "offset": {"type": "integer", "description": "起始行，从 1 开始", "default": 1},
         "limit": {"type": "integer", "description": "行数，最多 200", "default": 100}},
         "required": ["path"]}},
    {"name": "write_file", "description": "整个覆盖写入文件。先读全再写回完整内容。.py 文件写完自动做语法检查。",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "search", "description": "pattern 为空时按 glob 列出文件；否则在匹配文件中做正则 grep，返回 文件:行号: 内容。",
     "input_schema": {"type": "object", "properties": {
         "pattern": {"type": "string", "default": ""}, "glob": {"type": "string", "default": "**/*"}}}},
]

SYSTEM_PROMPT = """你是一个在真实代码仓库里修 bug 的软件工程 agent。
1. 先复现：运行测试，读报错，再定位代码。
2. 不要修改 tests/ 下的测试文件。
3. write_file 会整个覆盖文件：先读全再写回完整内容。
4. 改完必须重新运行测试确认通过。全部通过后，不再调用工具，直接用一段话总结你改了什么。"""


def make_llm(model=MODEL, max_retries=4):
    import anthropic
    client = anthropic.Anthropic(max_retries=0)
    retryable = (anthropic.RateLimitError, anthropic.APIConnectionError,
                 anthropic.InternalServerError, anthropic.APITimeoutError)

    def call(system, messages):
        for attempt in range(max_retries + 1):
            try:
                return client.messages.create(model=model, max_tokens=4096, system=system,
                                              tools=TOOL_SCHEMAS, messages=messages)
            except retryable as e:
                if attempt == max_retries:
                    raise
                print(f"  [retry] {type(e).__name__}", file=sys.stderr)
                time.sleep(2 ** attempt)
    return call


def compact(messages, keep=KEEP_RECENT_OBS):
    """同 v0 的思路，但要保持 tool_result 的结构（tool_use_id 必须一一对应）。"""
    result_idx = [i for i, m in enumerate(messages)
                  if m["role"] == "user" and isinstance(m["content"], list)]
    old = set(result_idx[:-keep]) if len(result_idx) > keep else set()
    out = []
    for i, m in enumerate(messages):
        if i in old:
            m = {"role": "user", "content": [
                {**b, "content": "[较早的观察已折叠]"} if b.get("type") == "tool_result" else b
                for b in m["content"]]}
        out.append(m)
    return out


def run(task, workdir, llm=None, verify=None, max_steps=MAX_STEPS, log=print):
    llm = llm or make_llm()
    ws = Workspace(workdir)
    messages = [{"role": "user", "content": f"<task>\n{task}\n</task>\n\n工作目录是仓库根目录: {ws.root}"}]
    stats = {"steps": 0, "input_tokens": 0, "output_tokens": 0,
             "format_errors": 0, "tool_errors": 0, "status": "running"}
    last_actions = []

    for step in range(1, max_steps + 1):
        stats["steps"] = step
        resp = llm(SYSTEM_PROMPT, compact(messages))
        stats["input_tokens"] += resp.usage.input_tokens
        stats["output_tokens"] += resp.usage.output_tokens
        # SDK 返回的 block 对象可以直接放回 messages；转成 dict 便于日志和序列化
        content = [b.model_dump(exclude_none=True) for b in resp.content]
        messages.append({"role": "assistant", "content": content})

        for b in content:
            if b["type"] == "text" and b["text"].strip():
                log(f"[{step}] 💭 {b['text'][:200]}")

        tool_uses = [b for b in content if b["type"] == "tool_use"]

        # ── 终止条件：模型不再调用工具 = 声称完成 → 验证 ──
        if resp.stop_reason != "tool_use" or not tool_uses:
            if resp.stop_reason == "max_tokens":
                messages.append({"role": "user", "content": "你的输出被截断了，请继续。"})
                continue
            if verify:
                out, _ = execute(ws, "bash", {"cmd": verify})
                if not out.startswith("[exit_code] 0"):
                    log(f"[{step}] ⚠ 声称完成但验证失败，打回")
                    messages.append({"role": "user", "content": f"验证命令 `{verify}` 失败：\n{out}\n任务没有完成，继续。"})
                    continue
            stats["status"] = "finished"
            stats["summary"] = "".join(b.get("text", "") for b in content)
            return stats

        # ── 执行：一轮可能有多个 tool_use，每个都必须回一个 tool_result ──
        results = []
        for tu in tool_uses:
            log(f"[{step}] 🔧 {tu['name']} {json.dumps(tu['input'], ensure_ascii=False)[:160]}")
            args = {k: str(v) for k, v in tu["input"].items()}   # 复用 v0 的字符串参数约定
            obs, is_err = execute(ws, tu["name"], args)
            stats["tool_errors"] += is_err

            sig = (tu["name"], json.dumps(tu["input"], sort_keys=True))
            last_actions = (last_actions + [sig])[-3:]
            if len(last_actions) == 3 and len(set(last_actions)) == 1:
                obs += "\n\n[系统提醒] 你已经连续 3 次执行完全相同的动作，结果不会改变。换一个思路。"

            log(f"[{step}] 👀 {obs[:300]}{'...' if len(obs) > 300 else ''}\n")
            results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": obs, "is_error": is_err})
        messages.append({"role": "user", "content": results})

    stats["status"] = "max_steps"
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--verify", default=None)
    ap.add_argument("--max-steps", type=int, default=MAX_STEPS)
    a = ap.parse_args()
    print(json.dumps(run(a.task, a.workdir, verify=a.verify, max_steps=a.max_steps), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
