"""Portrait preamble stays in front of the v8 embedding text."""

from datetime import UTC, datetime

from queep.ai import ANALYSIS_VERSION, SPACE, document, interpretation_schema, normalize_output
from queep.models import Interpretation, Reference, SourceContent
from queep.portrait import portrait_line


def _page():
    now = datetime.now(UTC)
    return Reference(
        id="a" * 32,
        kind="link",
        title="Saved",
        text="",
        url="https://saved.design/",
        status="ready",
        revision=1,
        created_at=now,
        updated_at=now,
        source=SourceContent(
            content_kind="page",
            state="read",
            url="https://saved.design/",
            title="Saved",
            text="Curated design tools for the team.",
            fetched_at=now,
        ),
    )


def test_portrait_preamble_leads_the_document_and_keeps_the_page():
    analysis = Interpretation(
        summary="Um diretório de referências de design.",
        usefulness=["guardar referências"],
        topics=["design"],
        entities=["Saved"],
        appearance=["grade de cartões"],
        coverage="partial",
        limitations=["coverage stays in the legacy body"],
        portrait="Isto é um site de referências de design.",
    )
    text = document(_page(), analysis)
    assert text.startswith("Isto é um site de referências de design.")
    assert "Tipo: um site" in text
    assert "Serve para: guardar referências" in text
    assert "Assuntos: design" in text
    assert "Nomes: Saved" in text
    assert "Aparência: grade de cartões" in text
    assert "Página: Curated design tools for the team." in text
    head = text.split("Resumo:", 1)[0]
    assert "coverage" not in head
    assert "limitations" not in head
    assert ANALYSIS_VERSION == "queep-interpretation-v8"
    assert SPACE == "gemini-embedding-2:768:document-v1"


def test_missing_portrait_falls_back_to_the_item_phrase():
    analysis = Interpretation(summary="Grade de projetos.", coverage="partial")
    assert portrait_line(_page(), analysis).startswith("Isto é um site. Grade de projetos.")


def test_portrait_stays_optional_for_the_v8_prompt():
    schema = interpretation_schema()
    assert "portrait" in schema["properties"]
    assert "portrait" not in schema["required"]
    assert "summary" in schema["required"]
    payload = Interpretation(summary="Oi", coverage="full").model_dump()
    payload.pop("portrait")
    cleaned = normalize_output(payload, schema)
    assert cleaned["portrait"] == ""
    kept = normalize_output({**payload, "portrait": "Isto é um site."}, schema)
    assert kept["portrait"] == "Isto é um site."
