import sys
from pathlib import Path

# 让 tests 可以 import 仓库根下的 app 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
