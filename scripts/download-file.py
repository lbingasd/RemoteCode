#!/usr/bin/env python3
"""Download a VSIX, NSSM archive or other optional asset with an operator-verified hash."""
import argparse
from pathlib import Path
from common import download

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--url", required=True)
p.add_argument("--sha256", required=True, help="从可信渠道核实；不能从不可信镜像同时取文件和 hash")
p.add_argument("--output", required=True, type=Path)
p.add_argument("--mirror")
args = p.parse_args()
download(args.url, args.output, args.sha256, args.mirror)
print("下载及 SHA-256 校验完成:", args.output.resolve())
