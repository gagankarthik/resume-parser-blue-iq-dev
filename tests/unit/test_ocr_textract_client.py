"""The Textract client is built once per region, not once per OCR call."""

from PIL import Image

from app.services.extraction import ocr_extractor


def test_textract_client_is_reused_across_calls(monkeypatch):
    built: list[str] = []

    def fake_client(service, region_name=None):
        built.append(service)
        return object()

    monkeypatch.setattr(ocr_extractor.boto3, "client", fake_client)
    monkeypatch.setattr(ocr_extractor, "_textract_image_bytes", lambda client, data: "line")
    ocr_extractor._textract_client.cache_clear()

    page = Image.new("L", (10, 10), 255)
    assert ocr_extractor._run_textract([page]) == "line"
    assert ocr_extractor._run_textract([page, page]) == "line\n\nline"
    assert built == ["textract"]
    ocr_extractor._textract_client.cache_clear()
