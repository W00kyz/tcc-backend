from app.domain.forms.hashing import question_content_hash
from app.domain.forms.models import QuestionType


def test_hash_is_stable_and_covers_the_four_fields() -> None:
    h1 = question_content_hash(
        prompt="Área limpa?", question_type=QuestionType.BOOLEAN, required=True, options=[]
    )
    # deterministic
    assert h1 == question_content_hash(
        prompt="Área limpa?", question_type=QuestionType.BOOLEAN, required=True, options=[]
    )
    assert len(h1) == 64  # sha256 hexdigest
    assert (
        question_content_hash(
            prompt="Área suja?", question_type=QuestionType.BOOLEAN, required=True, options=[]
        )
        != h1
    )
    assert (
        question_content_hash(
            prompt="Área limpa?", question_type=QuestionType.BOOLEAN, required=False, options=[]
        )
        != h1
    )
    assert (
        question_content_hash(
            prompt="Área limpa?", question_type=QuestionType.TEXT, required=True, options=[]
        )
        != h1
    )


def test_option_order_matters() -> None:
    a = question_content_hash(
        prompt="p", question_type=QuestionType.SINGLE_CHOICE, required=True, options=["x", "y"]
    )
    b = question_content_hash(
        prompt="p", question_type=QuestionType.SINGLE_CHOICE, required=True, options=["y", "x"]
    )
    assert a != b
