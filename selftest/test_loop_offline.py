"""不花一分钱测 loop：用一个"剧本 LLM"代替真模型，验证流程控制是否正确。"""
import importlib
import os
import shutil
from pathlib import Path

import make_tasks

# 默认测你自己的 my_agent.py；想对照参考答案：AGENT_IMPL=agent_v0
agent = importlib.import_module(os.environ.get("AGENT_IMPL", "my_agent"))

FIXED = '''def mean(xs):
    if not xs:
        raise ValueError("empty")
    return sum(xs) / len(xs)


def median(xs):
    if not xs:
        raise ValueError("empty")
    s = sorted(xs)
    mid = len(s) // 2
    if len(s) % 2 == 0:
        return (s[mid - 1] + s[mid]) / 2
    return s[mid]
'''


def scripted(replies):
    """按顺序吐出预设回复；模拟 stop_sequence 行为（去掉结尾 </tool>）。"""
    it = iter(replies)
    seen = []

    def llm(system, messages):
        seen.append(messages)
        text = next(it)
        if text.endswith("</tool>"):
            return text[: -len("</tool>")], "stop_sequence", 100, 20
        return text, "end_turn", 100, 20
    llm.seen = seen
    return llm


def fresh_repo(tmp_path):
    make_tasks.main(tmp_path / "tasks")
    repo = tmp_path / "repo"
    shutil.copytree(tmp_path / "tasks" / "task1_median" / "repo", repo)
    return repo


def test_happy_path(tmp_path):
    repo = fresh_repo(tmp_path)
    llm = scripted([
        '跑测试\n<tool name="bash"><cmd>python -m pytest -q</cmd></tool>',
        '读源码\n<tool name="read_file"><path>stats.py</path></tool>',
        f'修复\n<tool name="write_file"><path>stats.py</path><content>\n{FIXED}</content></tool>',
        '<tool name="finish"><summary>fixed even-length median</summary></tool>',
    ])
    stats = agent.run("fix", repo, llm=llm, verify="python -m pytest -q", log=lambda *_: None)
    assert stats["status"] == "finished" and stats["steps"] == 4
    assert stats["input_tokens"] == 400


def test_premature_finish_is_rejected(tmp_path):
    repo = fresh_repo(tmp_path)
    llm = scripted([
        '<tool name="finish"><summary>done!</summary></tool>',       # 撒谎
        f'<tool name="write_file"><path>stats.py</path><content>\n{FIXED}</content></tool>',
        '<tool name="finish"><summary>really done</summary></tool>',
    ])
    stats = agent.run("fix", repo, llm=llm, verify="python -m pytest -q", log=lambda *_: None)
    assert stats["status"] == "finished" and stats["steps"] == 3
    assert "验证命令" in llm.seen[1][-1]["content"]


def test_format_error_budget(tmp_path):
    repo = fresh_repo(tmp_path)
    llm = scripted(["我在想……"] * 5)
    stats = agent.run("fix", repo, llm=llm, log=lambda *_: None)
    assert stats["status"] == "format_error_limit" and stats["format_errors"] == 3


def test_loop_detection_and_max_steps(tmp_path):
    repo = fresh_repo(tmp_path)
    llm = scripted(['<tool name="bash"><cmd>ls</cmd></tool>'] * 10)
    stats = agent.run("fix", repo, llm=llm, max_steps=4, log=lambda *_: None)
    assert stats["status"] == "max_steps"
    assert "连续 3 次" in llm.seen[3][-1]["content"]


def test_path_escape_blocked(tmp_path):
    ws = agent.Workspace(fresh_repo(tmp_path))
    obs, err = agent.execute(ws, "read_file", {"path": "../../etc/passwd"})
    assert err and "越界" in obs


def test_search_glob_matches_root_files(tmp_path):
    ws = agent.Workspace(fresh_repo(tmp_path))
    assert "stats.py:7:" in agent.tool_search(ws, "def median", "**/*.py")
