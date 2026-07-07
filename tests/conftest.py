import os
import sys

_SDK_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EXAMPLES = os.path.join(_SDK_ROOT, "examples")
for _p in (_SDK_ROOT, _EXAMPLES):
    if _p not in sys.path:
        sys.path.insert(0, _p)
