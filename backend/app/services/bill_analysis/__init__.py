"""账单深度分析模块。

提供9个维度的深度分析：
1. 客户基础信息与风险标签
2. 交易活跃时段分析
3. 银行卡/账户使用统计
4. 交易类型占比分析
5. 月度收支趋势分析
6. 特殊风控交易清单
7. 高频交易对手分析
8. 专项交易明细
9. 流水检索说明
"""

from app.services.bill_analysis.analyzer import BillAnalyzer

__all__ = ["BillAnalyzer"]
