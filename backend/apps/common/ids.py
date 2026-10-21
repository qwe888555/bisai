"""业务 ID 生成：不透明字符串前缀 ID。

契约要求 ID 为不透明字符串（最长 64），不能用长整型承载，也不能拿身份证
当主键。统一前缀便于排查与日志识别，例如 acc_ / org_ / msg_ / file_。
"""

from __future__ import annotations

import uuid

PREFIX_ACCOUNT = "acc"
PREFIX_ORGANIZATION = "org"
PREFIX_DICTIONARY = "dic"
PREFIX_MESSAGE = "msg"
PREFIX_FILE = "file"
PREFIX_IDENTITY = "idc"
PREFIX_AUDIT = "log"
PREFIX_INVITATION = "inv"
PREFIX_POLICY = "pol"
PREFIX_IDEMPOTENCY = "idm"


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def new_account_id() -> str:
    return new_id(PREFIX_ACCOUNT)


def new_organization_id() -> str:
    return new_id(PREFIX_ORGANIZATION)


def new_dictionary_id() -> str:
    return new_id(PREFIX_DICTIONARY)


def new_message_id() -> str:
    return new_id(PREFIX_MESSAGE)


def new_file_id() -> str:
    return new_id(PREFIX_FILE)


def new_identity_id() -> str:
    return new_id(PREFIX_IDENTITY)


def new_invitation_id() -> str:
    return new_id(PREFIX_INVITATION)


def new_policy_id() -> str:
    return new_id(PREFIX_POLICY)


def new_idempotency_id() -> str:
    return new_id(PREFIX_IDEMPOTENCY)
