"""部件质量域的可观察业务错误。

服务层只抛出这些错误，HTTP 层据此映射状态码与错误码，
避免把 ValueError/ZeroDivisionError 等底层计算异常暴露给质量人员。
"""


class ComponentError(RuntimeError):
    code = "component_error"
    status = 400


class NotFound(ComponentError):
    code = "not_found"
    status = 404


class Conflict(ComponentError):
    code = "conflict"
    status = 409


class InvalidSample(ComponentError):
    """测量样本本身无效（非有限数值、字段缺失等）。"""

    code = "invalid_sample"
    status = 422


class InvalidLotState(ComponentError):
    """批次台账与分析前提不一致，例如器件登记数量与声明数量不符。"""

    code = "invalid_lot_state"
    status = 422
