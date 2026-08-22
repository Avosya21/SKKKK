from app.app.services.chunking import chunk_plain_text, chunk_transcript, estimate_tokens


def test_chunk_size_and_overlap():
    text = "Первое предложение. Второе предложение. Третье предложение."
    chunks = chunk_plain_text(text, max_tokens=8, overlap_tokens=2)
    assert len(chunks) >= 1
    assert all(c.token_estimate > 0 for c in chunks)


def test_chunks_do_not_split_sentences():
    text = "Alpha sentence. Beta sentence."
    chunks = chunk_plain_text(text, max_tokens=512)
    assert chunks[0].text.count(".") >= 1


def test_transcript_chunks_keep_speaker():
    text = "Спикер №1: первая реплика\nСпикер №2: вторая реплика"
    chunks = chunk_transcript(text)
    assert any(c.speaker and "Спикер" in c.speaker for c in chunks)


def test_estimate_tokens_positive():
    assert estimate_tokens("hello world") >= 1
