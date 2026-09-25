"""确定性计算引擎包。

硬性边界：本包内任何模块不得导入 app.services.llm。
数值只能由 pandas 在本包内计算，LLM 不参与运算。
"""
