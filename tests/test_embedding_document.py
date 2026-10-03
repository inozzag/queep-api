from datetime import UTC, datetime

from queep.ai import document
from queep.models import ImageInfo, Interpretation, Reference, SourceContent


def saved(**kwargs):
    now = datetime.now(UTC)
    base = dict(id="1", kind="link", title="", text="", url=None, status="ready",
                revision=1, created_at=now, updated_at=now)
    base.update(kwargs)
    return Reference.model_validate(base)


def test_site_embedding_says_what_it_is_and_what_it_is_for():
    ref = saved(
        url="https://www.indiehackers.com/",
        source=SourceContent(
            state="read", content_kind="page", url="https://www.indiehackers.com/",
            title="Indie Hackers", text="Histórias de quem constrói o próprio produto.",
            fetched_at=datetime.now(UTC)),
    )
    analysis = Interpretation(
        summary="Comunidade de fundadores.",
        portrait="Isto é um site com postagens no estilo blog para indie hackers.",
        topics=["indie hacking", "bootstrapping"],
        usefulness=["achar relatos de quem constrói um produto sozinho"],
        entities=["Indie Hackers"],
        coverage="partial",
    )
    text = document(ref, analysis)
    assert text.startswith("Isto é um site com postagens no estilo blog para indie hackers.")
    assert "Tipo: um site" in text
    assert "Serve para: achar relatos de quem constrói um produto sozinho" in text
    assert "Assuntos: indie hacking; bootstrapping" in text
    assert "Nomes: Indie Hackers" in text
    assert "Trecho: Histórias de quem constrói o próprio produto." in text
    assert "coverage" not in text and "limitations" not in text


def test_photo_embedding_describes_the_scene_when_portrait_is_missing():
    ref = saved(kind="image", title="Referência")
    analysis = Interpretation(
        summary="",
        appearance=["cadeira azul dentro de uma sala de estar"],
        coverage="full",
    )
    text = document(ref, analysis)
    assert text.startswith("Isto é uma foto de cadeira azul dentro de uma sala de estar.")
    assert "Tipo: uma foto" in text
    assert "Aparência: cadeira azul dentro de uma sala de estar" in text


def test_video_page_is_a_video_and_keeps_the_spoken_excerpt():
    ref = saved(
        url="https://www.youtube.com/watch?v=9PkMHkHN9dw",
        transcript="Neste vídeo eu explico como precificar um SaaS.",
        images=[ImageInfo(sha256="aa", content_type="image/jpeg", width=480, height=360,
                          byte_count=10, preview_kind="video")],
    )
    analysis = Interpretation(
        summary="Explicação de preço.",
        portrait="Isto é um vídeo em que se explica como cobrar por um software.",
        topics=["precificação"],
        coverage="partial",
    )
    text = document(ref, analysis)
    assert "Tipo: um vídeo" in text
    assert text.startswith("Isto é um vídeo em que se explica como cobrar por um software.")
    assert "Trecho: Neste vídeo eu explico como precificar um SaaS." in text
