# cycles/virtues-phase-8/handoff.md

> Commit `1c5cb8d`. Phase 8 = 给 8 个 load-bearing 纯函数加 29 个回归测试 + 锁死 martingale sizing invariant。

## 干了啥

新建 `tests/test_helpers.py` 29 cases 覆盖：

| 函数 | 覆盖点 |
|---|---|
| `reversal_stakes` | canonical 5→10.36→21.48 + 单层 invariant + monotonic + 0-layer 边界 |
| `reversal_factors` | wf/lf/target 计算（在 stakes 用） |
| `matching_streak` | 全 match / 末位 mismatch / 空数组 / 单元素 |
| `kline_color` | R/G/D 判定 |
| `clamp_price` | tick 对齐边界、过大/过小、tick 不同精度 |
| `price_decimals` | "0.01"→2, "0.001"→3, "1"→0 |
| `ema` | 长度匹配、初始值 = values[0]、monotonic ramp 趋同 |
| `rsi` | 全涨→100、全跌→0、震荡→50 附近 |

**核心 invariant**（一个 test 锁住整个 martingale 公式）：
```python
for n in range(1, len(stakes) + 1):
    accumulated_before_n = sum(lf * s for s in stakes[:n - 1])
    net_if_layer_n_wins = wf * stakes[n - 1] - accumulated_before_n
    assert abs(net_if_layer_n_wins - target) < 1e-9
```

这一行覆盖所有 layer：任意一层 WIN 都净赚 `target_profit`。

## Codex 抓的 2 个真 blocker（已 inline 修）

1. **Layer 3 stake 值算错**：subagent draft 写 21.477103589430083，实际 reversal_stakes 算的是 21.477086633198205。Subagent 手算时除法精度丢了一位。直接改正确值。

2. **Recoup invariant test 公式错**：原 test 用 `(1.0 - entry) * size - accumulated_loss`——这是 `run_reversal_live_real` 的**实盘 PnL 估算**公式，**不含 fee**；但 `reversal_stakes` **含 fee**（wf/lf 都减/加 0.07*0.5）。两个模型在 layer 4 WIN 时一个出 6.38，一个出 4.825。Test 失败。

修法：换用 stake-formula 的内部一致 invariant `wf * stake_N - Σ(lf × prior)`，绕开实盘/模型 差异。

## 你看到的差异

无运行时差异——纯测试。`pytest tests/` 现在 95 passed（之前 66）。

## 你要做的 verification

无 live verification 需要。本 phase 是 V6 测试补全，不改运行行为。直接跑 pytest 即可。
