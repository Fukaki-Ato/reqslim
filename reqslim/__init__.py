"""reqslim - request reduction for ZCode.

One job: sit between ZCode and its upstream, and stop the requests that do not
need to be sent. Four mechanisms, tried in this order:

1. raw-body hash        identical bytes we already answered (retries, fan-out)
2. normalised key       same conversation modulo cache_control / tool-id churn
3. in-flight coalescing concurrent duplicates share one upstream call
4. failure memo         a 429/5xx is replayed instead of hammered

The proxy never rewrites a response, never trims context and never swaps
models, so unmounting it changes the request count and nothing else.

Run it::

    python -m reqslim.zcode --upstream https://open.bigmodel.cn/api/anthropic

Wire it into ZCode::

    python tools/install_zcode.py
"""

from __future__ import annotations

from .cache import ExactCache
from .types import LLMResponse, Usage

__version__ = "0.2.5"

__all__ = ["ExactCache", "LLMResponse", "Usage"]
