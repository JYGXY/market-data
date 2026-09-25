#!/usr/bin/env python3
"""提交前扫描：发现私人账户类敏感字样就拒绝。

词表用 Unicode 转义 / 拆分写法存放，这样本文件自身不含这些字样，
无需把自己列为例外，整个仓库一视同仁地扫描。

用法:
  python3 scripts/check_forbidden.py            # 扫描暂存区（git pre-commit 用）
  python3 scripts/check_forbidden.py --all      # 扫描仓库内全部已跟踪文件 + 未跟踪文件
  python3 scripts/check_forbidden.py FILE ...   # 扫描指定文件
  python3 scripts/check_forbidden.py --list     # 打印词表
退出码: 0 = 干净, 1 = 发现禁用字样
"""
import subprocess
import sys
from pathlib import Path

FORBIDDEN = [
    # 按要求的四个词（查看明文: python3 scripts/check_forbidden.py --list）
    "\u6301\u4ed3",
    "\u6210\u672c",
    "port" + "folio",
    "\u5b89\u5168\u4ef7",
    # 额外兜底（同属不公开信息）
    "\u4ed3\u4f4d",
    "\u89c2\u5bdf\u540d\u5355",
    "watch" + "list",
    "cost " + "basis",
]


def git_files(args):
    out = subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout
    return [f for f in out.split("\0") if f]


def staged_content(path):
    return subprocess.run(["git", "show", f":{path}"], capture_output=True, check=True).stdout


def scan(name, raw):
    text = raw.decode("utf-8", errors="ignore").lower()
    hits = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for w in FORBIDDEN:
            if w.lower() in line:
                hits.append((name, lineno, w))
    # 文件名本身也检查
    for w in FORBIDDEN:
        if w.lower() in name.lower():
            hits.append((name, 0, w))
    return hits


def main(argv):
    if argv and argv[0] == "--list":
        print("\n".join(FORBIDDEN))
        return 0
    hits = []
    if argv and argv[0] == "--all":
        files = git_files(["ls-files", "-z", "--cached", "--others", "--exclude-standard"])
        for f in files:
            p = Path(f)
            if p.is_file():
                hits += scan(f, p.read_bytes())
    elif argv:
        for f in argv:
            hits += scan(f, Path(f).read_bytes())
    else:
        for f in git_files(["diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR"]):
            hits += scan(f, staged_content(f))
    if hits:
        print("拒绝提交：发现不应出现在公开仓库的字样：", file=sys.stderr)
        for name, lineno, w in hits:
            where = f"{name}:{lineno}" if lineno else f"{name} (文件名)"
            print(f"  {where}  ->  {w!r}", file=sys.stderr)
        return 1
    print("check_forbidden: OK", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
