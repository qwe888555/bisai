"""政策问答：关键词检索 + 单次模型调用 + 四种降级状态。

status 语义（契约 4.6）：
- ANSWERED    模型给出答复，且 citations 均属于本次检索到的政策集；
- NO_EVIDENCE 检索不到足够依据，不拼造政策，返回 fallbackPolicies；
- UNAVAILABLE 模型超时或失败，返回政策列表，不泄露密钥与厂商原始错误；
- DISABLED    开关关闭，直接返回政策列表，不影响其他业务。

日常问题（问候、网站操作指引、足球常识）走通用助手分支：answerMode=GENERAL，
citations 为空，答复不带政策依据，不得编造政策条款与时间节点。
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

# 命中这些词说明在问政策/报名，必须走政策原文检索，不允许用通用知识作答
_POLICY_KEYWORDS: tuple[str, ...] = (
    "报名", "资格", "材料", "审核", "规程", "赛程", "赛事", "参赛",
    "名额", "学籍", "年龄", "晋级", "出线", "抽签", "赛制", "名次",
    "证书", "体检", "保险", "经费", "收费", "政策", "公示", "截止",
    "组别", "分组", "户籍", "籍贯", "转学", "禁赛", "处罚",
)


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
        # 没有有效关键词（如“1+1”“你好”）说明与政策无关，不硬塞政策原文
        return []
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
                "但涉及平台报名办理（怎么报名、要交哪些材料、在哪个入口）时，"
                "以平台实际规则优先：平台为在线填报，报名附件只有球队报名表与名单（盖章）、学籍证明两类，"
                "体检表、保险单、照片、户籍等不是平台在线报名必交项；"
                "不要把政策文件中的材料清单整段当作平台必交材料，政策原文只作补充依据。"
                "如果原文不足以回答，只回复：NO_EVIDENCE。"
                "否则答复用简体中文，先给结论再分点说明；分点用“- ”起行，"
                "小标题用“### ”起行，不要输出表格、代码块和一级/二级标题，单段不超过 120 字。"
            ),
        },
        {"role": "user", "content": f"政策原文：\n{context}\n\n问题：{question}"},
    ]
    try:
        with httpx.Client(timeout=conf.AI_TIMEOUT_SECONDS) as client:
            response = client.post(
                f"{conf.AI_BASE_URL.rstrip('/')}/chat/completions",
                json={
                    "model": conf.AI_MODEL,
                    "messages": messages,
                    "temperature": 0,
                    # GLM-5.3-Flash 始终思考、不接受 thinking 关闭（400/1210）；
                    # 用 reasoning_effort=low 压低推理开销，避免超出
                    # AI_TIMEOUT_SECONDS 后拿到空 content 降级 UNAVAILABLE
                    "reasoning_effort": "low",
                },
                headers={"Authorization": f"Bearer {conf.AI_API_KEY}"},
            )
        if response.status_code >= 400:
            return None
        body = response.json()
        answer = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return answer.strip() or None
    except Exception:  # noqa: BLE001 - 依赖不可用一律降级，不外抛厂商错误
        return None


# 问操作步骤/故障（怎么、在哪、打不开……）属于平台使用问题，交给通用助手
_OPERATION_HINT = re.compile(
    r"怎么|如何|怎样|在哪|哪里|为什么|为啥|打不开|找不到|登录不上|用不了|失败|报错|页面|按钮|操作"
)


def _is_policy_question(question: str) -> bool:
    """判断是否在问政策/报名：是则必须有原文依据，不能用通用知识作答。"""
    text = question or ""
    if _OPERATION_HINT.search(text):
        return False
    return any(word in text for word in _POLICY_KEYWORDS)


_GENERAL_SYSTEM = (
    "你是省级中小学足球赛事平台的智能助手，服务学校端、球队端、市州端和省级用户。"
    "你可以回答日常问题、平台操作指引和足球常识。"
    "已知平台事实：报名单只能由学校端提交，球队端、市州端和省级只做材料准备与审核；"
    "账号由上级账号签发邀请后注册，人员主档由学校维护；"
    "材料在材料中心上传，审核结果和通知在消息中心查看；"
    "本人可在个人中心修改密码，忘记密码需联系上级账号或管理员重置。"
    "平台报名是在线填报，不需要提交纸质件；报名附件只有球队报名表与名单（盖章）、学籍证明两类，"
    "体检表、保险单、照片、户籍等不是平台在线报名必交项，赛事另有要求的以组别配置和平台通知为准。"
    "除此之外不要臆造功能入口、按钮位置或流程细节，不确定就提示联系上级账号或管理员。"
    "涉及赛事政策条款、报名资格、时间节点、名额和审核结论时，只说明“请以平台发布的政策原文为准”，"
    "不得编造具体条款、日期、数字或结论。"
    "语气友好，简体中文，200 字以内，不使用表情符号；不知道就直接说不知道。"
    "需要分点时用“- ”起行，不要输出表格与代码块。"
)

# 用户在问“平台报名要交什么材料”：按平台实际办理规则作答，
# 不能把政策文件里的材料清单整段搬过来（政策要求 ≠ 平台在线报名必交项）。
_PLATFORM_MATERIAL_QUESTION = re.compile(
    r"(报名|参赛|注册).{0,10}(材料|资料|证件|东西|交什么|提交什么)"
    r"|(材料|资料).{0,10}(报名|参赛|注册)"
)
_POLICY_DOC_HINT = re.compile(r"政策|规程|文件|条款|办法|第[一二三四五六七八九十\d]+条")


def _platform_enrollment_answer() -> str:
    from apps.enrollments.models import MATERIAL_TYPES

    attachments = "、".join(label for _, label in MATERIAL_TYPES)
    return (
        "平台在线报名不需要提交政策文件里列的全部纸质材料。实际只需两步：\n"
        "1. 在报名单中填写赛事、组别、参赛球队和领队联系方式；\n"
        f"2. 上传报名附件：{attachments}。\n"
        "体检表、保险单、照片、户籍等不是平台在线报名的必交项；"
        "如该赛事另有要求，以组别配置和平台通知为准。"
    )



def _ask_general(question: str) -> Optional[str]:
    """通用助手答复；未配置/超时/失败返回 None。"""
    if not (conf.AI_ENABLED and conf.AI_BASE_URL and conf.AI_API_KEY and conf.AI_MODEL):
        return None
    try:
        import httpx
    except ImportError:  # pragma: no cover
        return None

    messages = [
        {"role": "system", "content": _GENERAL_SYSTEM},
        {"role": "user", "content": question},
    ]
    try:
        with httpx.Client(timeout=conf.AI_TIMEOUT_SECONDS) as client:
            response = client.post(
                f"{conf.AI_BASE_URL.rstrip('/')}/chat/completions",
                json={
                    "model": conf.AI_MODEL,
                    "messages": messages,
                    "temperature": 0.3,
                    "reasoning_effort": "low",
                },
                headers={"Authorization": f"Bearer {conf.AI_API_KEY}"},
            )
        if response.status_code >= 400:
            return None
        body = response.json()
        answer_text = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return answer_text.strip() or None
    except Exception:  # noqa: BLE001 - 依赖不可用一律降级
        return None


def _general_result(question: str, fallback: list[dict[str, Any]]) -> dict[str, Any]:
    """日常问题答复：标注非政策依据，避免被当成政策结论。"""
    reply = _ask_general(question)
    if reply is None:
        return {
            "status": "UNAVAILABLE",
            "answer": "问答服务暂时不可用，请稍后重试，或直接查阅下列政策原文。",
            "citations": [],
            "fallbackPolicies": fallback,
            "aiGenerated": False,
            "requestId": get_request_id(),
        }
    return {
        "status": "ANSWERED",
        "answer": reply,
        "answerMode": "GENERAL",
        "notice": "该答复未引用政策原文，仅供参考；政策与报名要求以平台发布为准。",
        "citations": [],
        "fallbackPolicies": [],
        "aiGenerated": True,
        "requestId": get_request_id(),
    }


def answer(question: str, event_id: Optional[str] = None, season: Optional[str] = None) -> dict[str, Any]:
    _reject_personal_info(question)

    # 平台报名材料类问题：优先按平台实际办理规则回答，避免模型把政策条款里的
    # 体检、保险、照片等材料清单当成平台必交项。明确在问政策条款时仍走政策检索。
    if _PLATFORM_MATERIAL_QUESTION.search(question) and not _POLICY_DOC_HINT.search(question):
        return {
            "status": "ANSWERED",
            "answer": _platform_enrollment_answer(),
            "answerMode": "GENERAL",
            "notice": "该答复为平台办理规则说明，未引用政策条款；政策与报名要求以平台发布为准。",
            "citations": [],
            "fallbackPolicies": [],
            "aiGenerated": False,
            "requestId": get_request_id(),
        }

    documents = _candidate_documents(event_id, season)
    ranked = _rank(question, documents)
    fallback = _fallback(documents)
    policy_intent = _is_policy_question(question)

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
        if not policy_intent:
            # 日常问题/网站操作指引：走通用助手，不走政策检索
            return _general_result(question, fallback)
        # 政策/报名问题但零命中：不拼造政策，明确边界
        return {
            "status": "NO_EVIDENCE",
            "answer": "未在已发布的政策中找到与问题匹配的条款，请换一种问法或咨询组织方。",
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
        if not policy_intent:
            # 命中政策文本但实际是日常问题，交给通用助手回答
            return _general_result(question, fallback)
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
