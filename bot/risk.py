"""Управление риском: размер позиции и условия остановки бота."""
from dataclasses import dataclass, field

# Доля маржи, которую разрешено задействовать (запас на комиссии и проскальзывание)
MARGIN_BUFFER = 0.95


def position_size(equity, entry, stop, risk_pct, leverage):
    """Размер позиции в базовой валюте.

    Риск фиксирован в % от ТЕКУЩЕГО депозита — после прибылей позиция растёт
    (сложный процент), после убытков уменьшается. Объём ограничен доступным
    плечом.
    """
    stop_dist = abs(entry - stop)
    if stop_dist <= 0 or equity <= 0 or entry <= 0:
        return 0.0
    by_risk = equity * risk_pct / 100 / stop_dist
    by_margin = equity * leverage * MARGIN_BUFFER / entry
    return min(by_risk, by_margin)


@dataclass
class RiskGuard:
    start_equity: float
    target_profit_pct: float
    max_drawdown_pct: float
    max_consecutive_losses: int
    consecutive_losses: int = 0
    trades: list = field(default_factory=list)

    def record_trade(self, pnl):
        self.trades.append(pnl)
        self.consecutive_losses = self.consecutive_losses + 1 if pnl < 0 else 0

    def stop_reason(self, equity):
        """Причина остановки или None, если торговлю можно продолжать."""
        change_pct = (equity - self.start_equity) / self.start_equity * 100
        if change_pct >= self.target_profit_pct:
            return f"цель достигнута: +{change_pct:.1f}%"
        if change_pct <= -self.max_drawdown_pct:
            return f"превышена просадка: {change_pct:.1f}%"
        if self.consecutive_losses >= self.max_consecutive_losses:
            return f"{self.consecutive_losses} убыточных сделок подряд"
        return None
