"""共享业务异常基类.

所有 App 的业务异常继承 BusinessError，API 层可统一捕获。
"""


class BusinessError(Exception):
    """业务异常基类.

    Args:
        message: 人类可读的错误描述
        code: 机器可读的错误代码，用于客户端逻辑判断
    """

    def __init__(self, message: str, code: str = "business_error") -> None:
        self.message = message
        self.code = code
        super().__init__(self.message)
