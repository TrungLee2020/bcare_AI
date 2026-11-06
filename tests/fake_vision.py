"""Client OpenAI giả cho các test đọc ảnh: trả kết quả theo tên schema."""

from types import SimpleNamespace

from app.services.documents.schemas import Classification, Medication, PrescriptionExtraction
from tests.fake_openai import make_answer


def make_classification(**overrides) -> Classification:
    base = dict(doc_type="prescription", readable=True, confidence=0.95)
    base.update(overrides)
    return Classification(**base)


def make_prescription(**overrides) -> PrescriptionExtraction:
    base = dict(
        ocr_text="BỆNH VIỆN CHỢ RẪY\nĐƠN THUỐC\n1. Paracetamol 500mg x 10 viên\nUống 1 viên khi sốt",
        doc_date="2026-11-03",
        facility="BV Chợ Rẫy",
        doctor="BS. Nguyễn An",
        patient_name="Nguyễn Văn A",
        patient_birth_year=1980,
        patient_gender="Nam",
        uncertain_fields=[],
        diagnosis_as_written="Viêm họng cấp",
        medications=[
            Medication(
                name="Paracetamol", strength="500mg", form="viên", quantity="10 viên",
                instructions_as_written="Uống 1 viên khi sốt", duration=None,
            )
        ],
        follow_up_date=None,
        notes=None,
    )
    base.update(overrides)
    return PrescriptionExtraction(**base)


class FakeVision:
    def __init__(self, classification=None, extraction=None, classify_error=None, extract_error=None, answer=None):
        self.classification = classification or make_classification()
        self.extraction = extraction or make_prescription()
        self.classify_error = classify_error
        self.extract_error = extract_error
        self.answer = answer or make_answer()
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def names(self) -> list[str]:
        return [c["response_format"]["json_schema"]["name"] for c in self.calls]

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        name = kwargs["response_format"]["json_schema"]["name"]
        if name == "document_classification":
            if self.classify_error:
                raise self.classify_error
            result = self.classification
        elif name == "document_extraction":
            if self.extract_error:
                raise self.extract_error
            result = self.extraction
        else:
            result = self.answer
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=result.model_dump_json()), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=300, completion_tokens=100),
        )
