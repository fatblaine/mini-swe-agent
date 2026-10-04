"""
跑 3 个任务，独立验证并记录轮数 / token / 成败。
  python run_eval.py --agent v0
  python run_eval.py --agent v1 --repeat 3     # 每个任务跑 3 次，看失败率
  python run_eval.py --agent mine              # 跑你自己的 my_agent.py

关键纪律：agent 说自己成功不算数。这里重新跑一次测试，
并检查 tests/ 有没有被偷偷改过（agent 改测试让它通过是真实会发生的作弊）。
"""
import argparse
import hashlib
import importlib
import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path


def tests_hash(repo):
    h = hashlib.sha256()
    for f in sorted((repo / "tests").rglob("*.py")):
        h.update(f.read_bytes())
    return h.hexdigest()


def run_one(agent, task_dir, max_steps, quiet):
    spec = json.loads((task_dir / "task.json").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        shutil.copytree(task_dir / "repo", repo)          # 每次从干净副本开始
        before = tests_hash(repo)
        t0 = time.time()
        try:
            stats = agent.run(spec["issue"], repo, verify=spec["verify"], max_steps=max_steps,
                              log=(lambda *_: None) if quiet else print)
        except Exception as e:  # noqa: BLE001  API 彻底挂掉等
            stats = {"status": f"crash: {type(e).__name__}: {e}", "steps": 0, "input_tokens": 0, "output_tokens": 0}
        r = subprocess.run(spec["verify"], shell=True, cwd=repo, capture_output=True, text=True)
        tampered = tests_hash(repo) != before
        return {
            "task": task_dir.name,
            "agent_status": stats["status"],
            "tests_pass": r.returncode == 0,
            "tests_tampered": tampered,
            "success": r.returncode == 0 and not tampered,
            "steps": stats["steps"],
            "input_tokens": stats["input_tokens"],
            "output_tokens": stats["output_tokens"],
            "format_errors": stats.get("format_errors", 0),
            "tool_errors": stats.get("tool_errors", 0),
            "seconds": round(time.time() - t0, 1),
        }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", choices=["v0", "v1", "mine"], default="v0")
    ap.add_argument("--tasks", default="tasks")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--max-steps", type=int, default=30)
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()

    agent = importlib.import_module("my_agent" if a.agent == "mine" else f"agent_{a.agent}")
    rows = []
    for task_dir in sorted(Path(a.tasks).iterdir()):
        for i in range(a.repeat):
            print(f"\n══════ {task_dir.name}  (run {i + 1}/{a.repeat}, agent {a.agent}) ══════")
            row = run_one(agent, task_dir, a.max_steps, a.quiet)
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False))

    out = Path(f"results_{a.agent}.jsonl")
    with out.open("a", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps({**row, "agent": a.agent, "ts": time.strftime("%F %T")}, ensure_ascii=False) + "\n")

    print(f"\n{'task':<16}{'ok':<5}{'steps':>6}{'in_tok':>9}{'out_tok':>9}{'fmt_err':>9}  status")
    for r in rows:
        print(f"{r['task']:<16}{'✓' if r['success'] else '✗':<5}{r['steps']:>6}{r['input_tokens']:>9}"
              f"{r['output_tokens']:>9}{r['format_errors']:>9}  {r['agent_status']}"
              + ("  ⚠ 改了测试" if r["tests_tampered"] else ""))
    ok = sum(r["success"] for r in rows)
    print(f"\n通过 {ok}/{len(rows)}   结果已追加到 {out}")


if __name__ == "__main__":
    main()
