"""领域与服务错误类型。"""


class AdaptiveExamError(Exception):
    """所有自适应测评错误的基类。"""


class ValidationError(AdaptiveExamError):
    """领域对象或入参校验失败。"""


class NotFoundError(AdaptiveExamError):
    """引用的实体、版本或会话不存在。"""


class VersionConflictError(AdaptiveExamError):
    """同一实体的同一修订号被重复登记。"""


class SessionClosedError(AdaptiveExamError):
    """会话已结束，不能再执行该操作。"""


class PendingSelectionError(AdaptiveExamError):
    """存在尚未作答的候选题，需先凭原请求标识续考。"""

    def __init__(self, request_id: str, token: str, item_id: str):
        super().__init__("存在待作答的已选题目，请携带原请求标识续考")
        self.request_id = request_id
        self.token = token
        self.item_id = item_id


class IdempotencyError(AdaptiveExamError):
    """请求标识被用于不同的操作或不同的载荷。"""


class RegradePreconditionError(AdaptiveExamError):
    """不满足受控重评的前置条件。"""
