from enum import StrEnum


class AppError(Exception):
    def __init__(
        self, code: str, message: str, status: int = 409, *, retryable=False, details=None
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable
        self.details = details or {}

    def as_dict(self):
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


class ConversationErrorCode(StrEnum):
    """The stable failure vocabulary of the conversation and message API.

    A code is a contract with the client, not a log line: the frontend branches on it and a
    test pins it, so the spelling is frozen once shipped. Every code carries exactly one
    user-facing message and exactly one status, both held in the two tables below, because a
    code that reaches a user with wording invented at the raise site is a code nobody can
    keep consistent.

    The one exception is `CONTEXT_LIMIT_EXCEEDED`, whose retryability is a property of why the
    limit was hit rather than of the code, so it is built by `context_limit_error()` from a
    reason instead of by `conversation_error()`. Its single message and status still live in
    the tables; only the raise path and the retryability verdict differ, and the tables are
    what the client-facing wording is read from either way.
    """

    IDEMPOTENCY_KEY_REQUIRED = "IDEMPOTENCY_KEY_REQUIRED"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    CONVERSATION_NOT_WRITABLE = "CONVERSATION_NOT_WRITABLE"
    INVALID_INTERRUPT_ANSWER = "INVALID_INTERRUPT_ANSWER"
    EXECUTION_RECOVERY_REQUIRED = "EXECUTION_RECOVERY_REQUIRED"
    PERSISTENCE_FAILED = "PERSISTENCE_FAILED"
    NEW_CONVERSATION_REQUIRED = "NEW_CONVERSATION_REQUIRED"
    CONTEXT_LIMIT_EXCEEDED = "CONTEXT_LIMIT_EXCEEDED"


# Why the context limit was hit, which is what decides whether a retry can help. It is a
# closed set rather than free text because the reason travels to the client in `details` and
# a caller that branches on it would otherwise be branching on a string nobody agreed to.
CONTEXT_LIMIT_REASON_SUMMARY_FAILED = "summary_failed"
CONTEXT_LIMIT_REASON_OVER_BUDGET = "over_budget"
CONTEXT_LIMIT_REASONS = frozenset(
    {CONTEXT_LIMIT_REASON_SUMMARY_FAILED, CONTEXT_LIMIT_REASON_OVER_BUDGET}
)

# This code has a message and a status like every other code -- both are in the tables below
# -- but not one retryability: that is decided by the reason, so it cannot be built from the
# fixed-table path. Listed here so `conversation_error()` refuses it loudly instead of
# guessing, leaving `context_limit_error()` as the only builder.
_EXPLAINED_BY_REASON = frozenset({ConversationErrorCode.CONTEXT_LIMIT_EXCEEDED})


CONVERSATION_ERROR_MESSAGES: dict[str, str] = {
    ConversationErrorCode.IDEMPOTENCY_KEY_REQUIRED: "请求缺少 Idempotency-Key，无法识别重复提交",
    ConversationErrorCode.IDEMPOTENCY_CONFLICT: "同一个幂等键被用于了不同的请求",
    ConversationErrorCode.CONVERSATION_NOT_WRITABLE: "该会话已归档或已删除，无法继续发送消息",
    ConversationErrorCode.INVALID_INTERRUPT_ANSWER: "当前等待回答的问题与本次提交不匹配",
    ConversationErrorCode.EXECUTION_RECOVERY_REQUIRED: "上一次执行未完成且无法安全恢复，请新建会话继续",
    ConversationErrorCode.PERSISTENCE_FAILED: "消息未能保存，本次请求未执行",
    ConversationErrorCode.NEW_CONVERSATION_REQUIRED: "开始新的目标需要新建会话",
    ConversationErrorCode.CONTEXT_LIMIT_EXCEEDED: "对话内容超出模型上下文限制，本次请求未执行",
}

# A missing key is a malformed request; everything else is a state conflict, except a
# storage failure, which is the one case a client may usefully retry.
CONVERSATION_ERROR_STATUS: dict[str, int] = {
    ConversationErrorCode.IDEMPOTENCY_KEY_REQUIRED: 400,
    ConversationErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ConversationErrorCode.CONVERSATION_NOT_WRITABLE: 409,
    ConversationErrorCode.INVALID_INTERRUPT_ANSWER: 409,
    ConversationErrorCode.EXECUTION_RECOVERY_REQUIRED: 409,
    ConversationErrorCode.PERSISTENCE_FAILED: 503,
    ConversationErrorCode.NEW_CONVERSATION_REQUIRED: 409,
    ConversationErrorCode.CONTEXT_LIMIT_EXCEEDED: 409,
}


def context_limit_error(reason: str) -> AppError:
    """Build the context limit failure for `reason`, which must name one of the two.

    The reason is not decoration: it is what tells the client whether sending the same
    message again is worth anything. A summary that failed on a timeout is transient and the
    turn can be retried; an over-budget ledger is arithmetic, and the identical request
    overflows identically. What the two tables above cannot supply is that verdict: they hold
    one message and one status per code, and no retryability, which is a property of the reason
    here rather than of the code. So this code is present in both tables -- its message in
    `CONVERSATION_ERROR_MESSAGES` and its status in `CONVERSATION_ERROR_STATUS`, which is what
    the call below reads -- while `conversation_error()` refuses it, because that one builder
    would have to pick a single retryability for both reasons. An unrecognised reason raises
    instead of defaulting for the same reason: a typo that fell back to "not retryable" would
    quietly strand a transient failure.
    """

    if reason not in CONTEXT_LIMIT_REASONS:
        allowed = ", ".join(sorted(CONTEXT_LIMIT_REASONS))
        raise ValueError(f"unknown context limit reason {reason!r}; allowed: {allowed}")

    return AppError(
        ConversationErrorCode.CONTEXT_LIMIT_EXCEEDED.value,
        CONVERSATION_ERROR_MESSAGES[ConversationErrorCode.CONTEXT_LIMIT_EXCEEDED],
        CONVERSATION_ERROR_STATUS[ConversationErrorCode.CONTEXT_LIMIT_EXCEEDED],
        retryable=reason == CONTEXT_LIMIT_REASON_SUMMARY_FAILED,
        details={"reason": reason},
    )


def conversation_not_found() -> AppError:
    """The one 404 for "no such conversation **of yours**".

    Deliberately identical for "does not exist" and "belongs to someone else": telling those
    apart hands an attacker an oracle for which conversation ids are real (design §7.1, the
    same rule `ConversationService._owned_in` follows). It lives here rather than in the
    service because `app/context` -- the ledger, the summary chain and the assembler -- now
    verifies ownership itself, and a second copy of the wording would drift from this one.
    """

    return AppError("NOT_FOUND", "会话不存在", 404)


def conversation_error(code: ConversationErrorCode, *, details: dict | None = None) -> AppError:
    """Build the one error a code may raise, so message and status cannot drift apart."""

    if code in _EXPLAINED_BY_REASON:
        raise ValueError(
            f"{code.value} carries a reason and has two retryability verdicts, so it cannot "
            "be built from the fixed tables; call context_limit_error(reason) instead"
        )

    return AppError(
        code.value,
        CONVERSATION_ERROR_MESSAGES[code],
        CONVERSATION_ERROR_STATUS[code],
        retryable=code is ConversationErrorCode.PERSISTENCE_FAILED,
        details=details,
    )
