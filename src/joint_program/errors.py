"""履约协同服务的领域错误。"""


class DomainError(Exception):
    """领域错误基类。"""


class ValidationError(DomainError):
    """输入参数不合法。"""


class PermissionDeniedError(DomainError):
    """操作超出合作方权限。"""


class NotFoundError(DomainError):
    """引用的对象不存在。"""


class StateError(DomainError):
    """当前状态不允许该操作。"""


class StaleDecisionError(StateError):
    """基于过期版本的决定（并发修订、落后基线），不得污染较新的决定。"""


class SignatureError(DomainError):
    """会签无效：已过期、内容与最新条款不一致或重复会签。"""


class IncompleteError(StateError):
    """会签不齐或草案不完整，不能生效。"""


class StoreCorruptedError(DomainError):
    """事件日志哈希链校验失败。"""
