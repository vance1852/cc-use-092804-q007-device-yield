"""服务层可观察错误：API 据此返回业务错误码而不是底层计算异常。"""

from __future__ import annotations


class ServiceError(RuntimeError):
    code = "service_error"
    status = 400


class NotFound(ServiceError):
    code = "not_found"
    status = 404


class Conflict(ServiceError):
    code = "conflict"
    status = 409


class Forbidden(ServiceError):
    code = "forbidden"
    status = 403


class InvalidState(ServiceError):
    code = "invalid_state"
    status = 409


class ValidationFailed(ServiceError):
    code = "validation_failed"
    status = 422


class InvalidSample(ValidationFailed):
    """测量样本本身无效（非有限数值、未知器件、计划外频点等）。"""

    code = "invalid_sample"