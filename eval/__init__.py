"""
TelecomOps-Agent 的 evaluation 包。

职责：
  - runner:     批量运行 task，封装 tau2 Python API，生成 runs/<run_id>/
  - metrics:    从 SimulationRun 计算 per-task 指标与 run 级 summary
  - trace:      提取人类可读的对话轨迹（含 tool call 与其结果配对）
  - taxonomy:   失败分类法（12 类）
"""

from eval.taxonomy import FAILURE_TYPES

__all__ = ["FAILURE_TYPES"]
