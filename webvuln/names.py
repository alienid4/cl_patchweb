"""人名正規化（V2.111）：AD 名單匯入、同名確認、預覽、寄信找收件人共用同一套，避免各算各的。

  norm_name：去頭尾空白 → 半形／全形空白一律收成一個 → 中文字之間的空白拿掉 → 取開頭連續的中文字
             「陳小美 AMY」→ 陳小美；「李小華　EVA」→ 李小華；「周 志明」→ 周志明；「svc_backup」→ ''
  owner_key：比對鍵；有中文名就用中文名，否則（英文名、群組名）用空白收斂後的原字串
"""
from __future__ import annotations

import re
from typing import Any

_CJK = "㐀-䶿一-鿿豈-﫿"
_WS = re.compile(r"[\s　﻿​‌‍⁠]+")
_CJK_GAP = re.compile(r"(?<=[%s]) (?=[%s])" % (_CJK, _CJK))
_LEAD = re.compile(r"[%s][%s·．・]*" % (_CJK, _CJK))


def collapse(s: Any) -> str:
    return _WS.sub(" ", "" if s is None else str(s)).strip()


def norm_name(s: Any) -> str:
    t = _CJK_GAP.sub("", collapse(s))
    m = _LEAD.match(t)
    return m.group(0) if m else ""


def owner_key(s: Any) -> str:
    return norm_name(s) or collapse(s)
