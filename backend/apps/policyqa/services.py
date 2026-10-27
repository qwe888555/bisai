"""政策问答：关键词检索 + 单次模型调用 + 四种降级状态。

status 语义（契约 4.6）：
- ANSWERED    模型给出答复，且 citations 均属于本次检索到的政策集；
- NO_EVIDENCE 检索不到足够依据，不拼造政策，返回 fallbackPolicies；
- UNAVAILABLE 模型超时或失败，返回政策列表，不泄露密钥与厂商原始错误；
- DISABLED    开关关闭，直接返回政策列表，不影响其他业务。
"""

from __future__ import annotations

import re
from datetime import datetime, time
from typing import Any, Optional

from django.utils import timezone

from apps.common import conf
from apps.common.context import get_request_id
from apps.common.errors import BUSINESS_VALIDATION_FAILED, ApiError
from apps.common.ids import new_policy_id

from .models import PolicyDocument

MAX_CITATIONS = 3
MAX_EXCERPT_CHARS = 1000

_ID = re.compile(r"\d{17}[\dXx]")
_PHONE = re.compile(r"\b1\d{10}\b")
_TOKEN = re.compile(r"[\u4e00-\u9fa5]{2,}|[A-Za-z0-9]{2,}")
_STOP_TOKENS = {"请问", "什么", "怎么", "如何", "哪些", "可以", "需要", "是否", "我们", "问题"}


def _tokens(text: str) -> set[str]:
    """极简分词：中文按 2-gram，英文数字按词；不引第三方分词库。"""
    tokens: set[str] = set()
    for match in _TOKEN.finditer(text or ""):
        word = match.group()
        if re.fullmatch(r"[\u4e00-\u9fa5]+", word):
            if len(word) == 2:
                tokens.add(word)
            else:
                tokens.update(word[i : i + 2] for i in range(len(word) - 1))
        else:
            tokens.add(word.lower())
    return {t for t in tokens if t not in _STOP_TOKENS}


def _candidate_documents(event_id: Optional[str], season: Optional[str]):
    now = timezone.now()
    queryset = PolicyDocument.objects.filter(enabled=True, published_at__isnull=False)
    if event_id:
        queryset = queryset.filter(event_id__in=[event_id, ""])
    if season:
        queryset = queryset.filter(season__in=[season, ""])
    candidates = [
        doc
        for doc in queryset
        if (doc.valid_from is None or doc.valid_from <= now)
        and (doc.valid_to is None or doc.valid_to >= now)
    ]
    return candidates


def _rank(question: str, documents: list[PolicyDocument]) -> list[PolicyDocument]:
    keywords = _tokens(question)
    if not keywords:
        return documents[:MAX_CITATIONS]
    scored: list[tuple[int, PolicyDocument]] = []
    for doc in documents:
        title_tokens = _tokens(doc.title)
        body_tokens = _tokens(f"{doc.summary} {doc.body}")
        score = 3 * len(keywords & title_tokens) + len(keywords & body_tokens)
        if score > 0:
            scored.append((score, doc))
    scored.sort(key=lambda pair: (-pair[0], pair[1].id))
    return [doc for _, doc in scored[:MAX_CITATIONS]]


def _citation(doc: PolicyDocument, excerpt: str) -> dict[str, Any]:
    content_id = doc.source_content_id or doc.pk
    return {
        "contentId": content_id,
        "title": doc.title,
        "revision": int(doc.revision or 1),
        "excerpt": excerpt[:MAX_EXCERPT_CHARS],
        "publicUrl": f"/api/v1/public/contents/{content_id}",
    }


def _excerpt(doc: PolicyDocument) -> str:
    text = (doc.body or "").strip().replace("\n", " ")
    return text[:MAX_EXCERPT_CHARS]


def _fallback(documents: list[PolicyDocument]) -> list[dict[str, Any]]:
    return [_citation(doc, _excerpt(doc)) for doc in documents[:MAX_CITATIONS]]


def _to_datetime(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if timezone.is_aware(value) else timezone.make_aware(value)
    return timezone.make_aware(datetime.combine(value, time.min))


def sync_policy_document(content) -> Optional[PolicyDocument]:
    """把 CMS 的 POLICY 内容同步为问答可检索的政策快照。

    CMS 发布/修改/下线/删除内容时由 events 服务调用；只有 PUBLISHED 状态可检索。
    非 POLICY 类型直接忽略，返回值仅用于调试。
    """
    if getattr(content, "content_type", "") != "POLICY":
        return None
    document = PolicyDocument.objects.filter(source_content_id=content.pk).first()
    if content.status != "PUBLISHED" or getattr(content, "deleted_at", None):
        if document is not None and document.enabled:
            document.enabled = False
            document.save(update_fields=["enabled", "updated_at"])
        return document

    body = (getattr(content, "body_text", "") or getattr(content, "body_html", "") or "").strip()
    if not body:
        return document  # 空正文不建快照，避免问答引用空文本

    if document is None:
        document = PolicyDocument(id=new_policy_id(), source_content_id=content.pk)
    document.title = content.title
    document.summary = content.summary or ""
    document.body = body
    document.event_id = content.event_id or ""
    document.revision = int(content.revision or 1)
    document.enabled = True
    document.published_at = content.published_at or timezone.now()
    document.valid_from = _to_datetime(content.policy_effective_from)
    document.valid_to = _to_datetime(content.policy_effective_to)
    document.save()
    return document


def _reject_personal_info(question: str) -> None:
    if _ID.search(question) or _PHONE.search(question):
        # 含身份证/手机号：拒绝外发，不送模型
        raise ApiError(
            BUSINESS_VALIDATION_FAILED,
            "问题包含身份证号或手机号等个人信息，请去除后再提问",
            [{"field": "question", "reason": "包含个人信息，已拒绝外发"}],
        )


def _ask_model(question: str, documents: list[PolicyDocument]) -> Optional[str]:
    """返回模型答复；超时/失败/未配置返回 None（调用方降级为 UNAVAILABLE）。"""
    if not (conf.AI_ENABLED and conf.AI_BASE_URL and conf.AI_API_KEY and conf.AI_MODEL):
        return None
    try:
        import httpx
    except ImportError:  # pragma: no cover
        return None

    context = "\n\n".join(
        f"【{doc.title}（修订 {doc.revision}）】{_excerpt(doc)}" for doc in documents
    )
    messages = [
        {
            "role": "system",
            "content": (
                "你是省级中小学足球赛事系统的政策问答助手。"
                "只能依据提供的政策原文回答，不得编造、不得推测。"
                "如果原文不足以回答，只回复：NO_EVIDENCE"
            ),
        },
        {"role": "user", "content": f"政策原文：\n{context}\n\n问题：{question}"},
    ]
    try:
        with httpx.Client(timeout=conf.AI_TIMEOUT_SECONDS) as client:
            response = client.post(
                f"{conf.AI_BASE_URL.rstrip('/')}/chat/completions",
                json={"model": conf.AI_MODEL, "messages": messages, "temperature": 0},
                headers={"Authorization": f"Bearer {conf.AI_API_KEY}"},
            )
        if response.status_code >= 400:
            return None
        body = response.json()
        answer = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return answer.strip() or None
    except Exception:  # noqa: BLE001 - 依赖不可用一律降级，不外抛厂商错误
        return None


def answer(question: str, event_id: Optional[str] = None, season: Optional[str] = None) -> dict[str, Any]:
    _reject_personal_info(question)

    documents = _candidate_documents(event_id, season)
    ranked = _rank(question, documents)
    fallback = _fallback(documents)

    if not conf.AI_ENABLED:
        return {
            "status": "DISABLED",
            "answer": "政策问答当前已关闭，请查阅下列政策原文。",
            "citations": [],
            "fallbackPolicies": fallback,
            "aiGenerated": False,
            "requestId": get_request_id(),
        }

    if not ranked:
        # 零命中通常是与政策/报名无关的问题：明确边界，不给无关政策卡片
        return {
            "status": "NO_EVIDENCE",
            "answer": "本问答仅支持校园足球政策与报名相关问题，未找到相关依据。请改问政策条款或报名流程。",
            "citations": [],
            "fallbackPolicies": [],
            "aiGenerated": False,
            "requestId": get_request_id(),
        }

    model_answer = _ask_model(question, ranked)
    if model_answer is None:
        return {
            "status": "UNAVAILABLE",
            "answer": "问答服务暂时不可用，请稍后重试，或直接查阅下列政策原文。",
            "citations": [],
            "fallbackPolicies": fallback,
            "aiGenerated": False,
            "requestId": get_request_id(),
        }

    if "NO_EVIDENCE" in model_answer.upper():
        return {
            "status": "NO_EVIDENCE",
            "answer": "未在已发布的政策中找到足够依据，请查阅相关政策原文或咨询组织方。",
            "citations": [],
            "fallbackPolicies": fallback,
            "aiGenerated": True,
            "requestId": get_request_id(),
        }

    return {
        "status": "ANSWERED",
        "answer": model_answer,
        "citations": [_citation(doc, _excerpt(doc)) for doc in ranked],
        "fallbackPolicies": [],
        "aiGenerated": True,
        "requestId": get_request_id(),
    }
