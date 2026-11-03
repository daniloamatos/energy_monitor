

import math
from decimal import Decimal, ROUND_HALF_UP


def _round(value):
    return float(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def number(value, decimals=2, signed=False):
    if value is None or not math.isfinite(value):
        return "—"
    return format(value, f"{'+' if signed else ''},.{decimals}f").translate(str.maketrans(",.", ".,"))


def _short(value):
    return number(value).rstrip("0").rstrip(",")


def compact(value, signed=False, integer=False):
    if value is None or not math.isfinite(value):
        return "—"
    magnitude = abs(value)
    sign = "-" if value < 0 else "+" if signed else ""
    if magnitude >= 1000:
        rounded = Decimal(str(magnitude)).quantize(Decimal('1') if integer else Decimal('0.01'), rounding=ROUND_HALF_UP)
        remainder = int(rounded)
        parts = []
        for scale, unit in ((10**12, 'T'), (10**9, 'B'), (10**6, 'M'), (1000, 'K')):
            block, remainder = divmod(remainder, scale)
            if block:
                parts.append(f'{block}{unit}')
        fraction = rounded - int(rounded)
        if remainder or fraction:
            tail = Decimal(remainder) + fraction
            parts.append(str(tail).rstrip('0').rstrip('.') if fraction else str(remainder))
        return sign + ' '.join(parts).replace('.', ',')
    return number(value, 0 if integer else 2, signed=signed)


def count(value):
    return compact(value, integer=True)
