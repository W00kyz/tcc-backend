import hashlib
import json

from app.domain.forms.models import QuestionType


def question_content_hash(
    *, prompt: str, question_type: QuestionType, required: bool, options: list[str]
) -> str:
    """Spec Ruling 2 — covers prompt, type, options (in authored order) and required.

    order_index is deliberately excluded: reordering questions never invalidates answers.

    Example:
        question_content_hash(
            prompt="Área limpa?", question_type=QuestionType.BOOLEAN, required=True, options=[]
        )  # -> a stable 64-char sha256 hexdigest
    """
    canonical = json.dumps(
        {"prompt": prompt, "type": str(question_type), "required": required, "options": options},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
