"""中国大陆 18 位身份证号校验（GB 11643 / ISO 7064 MOD 11-2）。"""

from __future__ import annotations

import re

_WEIGHTS = (7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2)
_CHECK_CODES = "10X98765432"


def valid_id_number(value: str) -> bool:
    """18 位身份证 + ISO 7064 MOD 11-2 校验位。"""
    text = (value or "").strip().upper()
    if not re.fullmatch(r"\d{17}[\dX]", text):
        return False
    total = sum(int(text[index]) * _WEIGHTS[index] for index in range(17))
    return _CHECK_CODES[total % 11] == text[17]
