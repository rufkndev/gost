import json
from types import SimpleNamespace
from typing import Any

from app.core import docx_io as dx
from app.core.classify.llm import LlmClassifier, build_payload, cost_of, gray_blocks, mask
from app.core.classify.resolve import apply_answers, apply_llm, pick_questions
from app.core.classify.rules import classify
from app.core.ir import BlockType, DocumentIR
from app.core.parser import parse
from tests.synth import Doc, title_page, tp

T = BlockType
LONG = "Это обычный абзац основного текста, в котором достаточно много слов для уверенности."


def sample_ir() -> DocumentIR:
    data = (
        Doc()
        .add(
            *title_page(),
            tp("Методика исследования", bold=True),  # серая зона
            tp(LONG),
            tp("Основная часть", bold=True, jc="center"),
            tp(LONG),
        )
        .build()
    )
    return classify(parse(dx.read_package(data)))


class FakeResponses:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


class FakeClient:
    """Имитация AsyncOpenAI: только client.responses.create."""

    def __init__(self, responses: list[Any]) -> None:
        self.responses = FakeResponses(responses)


def reply(
    labels: list[dict[str, Any]],
    model: str = "gpt-6-luna-2026-09-22",
    status: str = "completed",
    refusal: bool = False,
) -> Any:
    part = (
        SimpleNamespace(type="refusal", refusal="нет")
        if refusal
        else SimpleNamespace(type="output_text", text=json.dumps({"labels": labels}))
    )
    return SimpleNamespace(
        output=[SimpleNamespace(type="reasoning"), SimpleNamespace(type="message", content=[part])],
        output_text="" if refusal else json.dumps({"labels": labels}),
        usage=SimpleNamespace(input_tokens=2000, output_tokens=300),
        status=status,
        model=model,
    )


def test_mask_contacts_and_names() -> None:
    s = mask(
        "Иванов И. И., тел. +7 (912) 345-67-89, ivan@mail.ru, И.И. Петров, Сидоров Пётр Иванович"
    )
    assert "Иванов" not in s and "Петров" not in s and "Сидоров" not in s
    assert "[PHONE]" in s and "[EMAIL]" in s


def test_payload_never_contains_title_page() -> None:
    ir = sample_ir()
    gray = gray_blocks(ir, 0.0, 0.9)
    payload = json.dumps(build_payload(ir, gray), ensure_ascii=False)
    assert "МИСИС" not in payload and "Москва 2025" not in payload
    assert all(b.type != T.TITLE_PAGE for b in gray)


async def test_valid_labels_applied_and_unknown_dropped() -> None:
    ir = sample_ir()
    gray = gray_blocks(ir, 0.0, 0.9)
    target = gray[0].id
    client = FakeClient(
        [
            reply(
                [
                    {"id": target, "type": "heading", "level": 2, "confidence": 0.95},
                    {"id": "p9999", "type": "heading", "level": 1, "confidence": 0.9},  # чужой id
                ]
            )
        ]
    )
    clf = LlmClassifier(client, "gpt-6-luna", "gpt-6-sol")
    res = await clf.classify(ir, 0.0, 0.9)
    assert set(res.labels) == {target}
    # Имя модели с датой в ответе — цена всё равно Luna.
    assert res.calls[0].ok and res.cost_usd == round(cost_of("gpt-6-luna", 2000, 300), 6)
    assert cost_of("gpt-6-luna", 2000, 300) == (2000 * 0.10 + 300 * 0.50) / 1_000_000
    kw = client.responses.calls[0]
    assert kw["text"]["format"]["type"] == "json_schema" and kw["text"]["format"]["strict"]
    assert kw["store"] is False and kw["reasoning"] == {"effort": "low"}
    assert "МИСИС" not in kw["input"][0]["content"]


async def test_invalid_json_escalates_once() -> None:
    ir = sample_ir()
    target = gray_blocks(ir, 0.0, 0.9)[0].id
    bad = reply([])
    bad.output_text = "не JSON"
    client = FakeClient(
        [
            bad,
            reply(
                [{"id": target, "type": "body", "level": 0, "confidence": 0.9}], model="gpt-6-sol"
            ),
        ]
    )
    res = await LlmClassifier(client, "gpt-6-luna", "gpt-6-sol").classify(ir, 0.0, 0.9)
    assert [c.ok for c in res.calls] == [False, True]
    assert client.responses.calls[1]["model"] == "gpt-6-sol"
    assert target in res.labels


async def test_refusal_and_incomplete_escalate() -> None:
    ir = sample_ir()
    client = FakeClient([reply([], refusal=True), reply([], status="incomplete")])
    res = await LlmClassifier(client, "gpt-6-luna", "gpt-6-sol").classify(ir, 0.0, 0.9)
    assert [c.error for c in res.calls] == ["refusal", "incomplete"] and not res.labels


async def test_api_error_does_not_raise() -> None:
    ir = sample_ir()
    client = FakeClient([RuntimeError("down"), RuntimeError("down")])
    res = await LlmClassifier(client, "gpt-6-luna", "gpt-6-sol").classify(ir, 0.0, 0.9)
    assert res.labels == {} and len(res.calls) == 2 and not any(c.ok for c in res.calls)


async def test_budget_limits_batch() -> None:
    ir = sample_ir()
    client = FakeClient([reply([])])
    clf = LlmClassifier(client, "gpt-6-sol", "gpt-6-sol", max_cost_usd=0.00001)
    res = await clf.classify(ir, 0.0, 0.9)
    assert len(res.sent) <= 1 and res.overflow


def test_conflict_goes_to_user_and_answers_apply() -> None:
    ir = sample_ir()
    gray = gray_blocks(ir, 0.0, 0.9)
    b = gray[0]
    from app.core.classify.llm import Label

    conflicts = apply_llm(ir, {b.id: Label(id=b.id, type="body", level=0, confidence=0.95)}, 0.9)
    assert conflicts == [b.id]
    qs = pick_questions(ir, 0.6, conflicts, 5)
    q = next(q for q in qs if q.block_id == b.id)
    labels = [o["label"] for o in q.options]
    assert len(labels) == len(set(labels))
    assert "Обычный текст" in labels and any("Заголовок" in x for x in labels)
    apply_answers(ir, {b.id: ("heading", 1)})
    assert b.type == T.HEADING and b.level == 1 and b.source == "user"


def test_agreement_raises_confidence() -> None:
    ir = sample_ir()
    b = gray_blocks(ir, 0.0, 0.9)[0]
    from app.core.classify.llm import Label

    apply_llm(
        ir, {b.id: Label(id=b.id, type=b.type.value, level=b.level or 0, confidence=0.97)}, 0.9
    )
    assert b.confidence >= 0.9 and b.source == "llm"
