"""库内统一异常类型，全部继承 ValueError，便于调用方按需捕获。"""


class TemplateCompileError(ValueError):
    """模板词法扫描失败（未结束引号/注释、分号、问号、裸冒号等）。"""


class BindingError(ValueError):
    """绑定字典缺键/多键，或值类型不在支持范围内。"""


class PlanStoreError(ValueError):
    """计划发布冲突、计划文件缺失或计划内容不符合公开格式。"""


class BatchError(ValueError):
    """批次预检失败、批次不可变约束被违反等执行前错误。"""
