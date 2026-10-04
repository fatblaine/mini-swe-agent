"""parser 的"刁难"用例。先把这些跑绿，再接真模型。  python -m pytest selftest -q"""
import importlib
import os

import pytest

# 默认测你自己的 my_agent.py；想对照参考答案：AGENT_IMPL=agent_v0
agent = importlib.import_module(os.environ.get("AGENT_IMPL", "my_agent"))
ParseError, parse_action = agent.ParseError, agent.parse_action


def test_normal():
    t, name, args, w = parse_action('先跑测试。\n<tool name="bash">\n<cmd>python -m pytest -q</cmd>\n</tool>')
    assert (t, name, args, w) == ("先跑测试。", "bash", {"cmd": "python -m pytest -q"}, "")


def test_wrapped_in_markdown_fence():
    text = '好的\n```xml\n<tool name="read_file">\n<path>stats.py</path>\n</tool>\n```'
    assert parse_action(text)[1:3] == ("read_file", {"path": "stats.py"})


def test_content_keeps_indentation_and_strips_code_fence():
    text = ('<tool name="write_file">\n<path>a.py</path>\n<content>\n```python\n'
            'def f():\n    return 1\n```\n</content>\n</tool>')
    assert parse_action(text)[2]["content"] == "def f():\n    return 1"


def test_two_actions_only_first_executed():
    text = '<tool name="bash"><cmd>ls</cmd></tool>\n<tool name="bash"><cmd>pwd</cmd></tool>'
    _, _, args, warning = parse_action(text)
    assert args == {"cmd": "ls"} and "2 个" in warning


def test_missing_close_tag():
    with pytest.raises(ParseError, match="结构不完整"):
        parse_action('<tool name="bash">\n<cmd>ls</cmd>\n')


def test_no_action():
    with pytest.raises(ParseError, match="没有找到"):
        parse_action("我觉得 bug 在 median 里。")


def test_shell_redirects_survive():
    assert parse_action('<tool name="bash"><cmd>echo a > out.txt && cat < out.txt</cmd></tool>')[2] == \
        {"cmd": "echo a > out.txt && cat < out.txt"}


def test_single_quoted_name():
    assert parse_action("<tool name='search'><pattern>def median</pattern></tool>")[1] == "search"


def test_raw_body_without_param_tags_is_rejected():
    with pytest.raises(ParseError, match="参数"):
        parse_action('<tool name="bash">ls -la</tool>')
