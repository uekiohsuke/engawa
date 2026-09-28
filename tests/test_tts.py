from engawa.ui.tts import clean_for_speech, has_speakable, split_sentences


def test_split_sentences_keeps_unfinished_rest():
    sentences, rest = split_sentences("おかえり。今日はどうだった？ 私はね")
    assert sentences == ["おかえり。", "今日はどうだった？"]
    assert rest == " 私はね"


def test_split_sentences_includes_closing_brackets_and_newlines():
    sentences, rest = split_sentences("「ほんと？」って思った！\n次は")
    assert sentences == ["「ほんと？」", "って思った！"]
    assert rest == "次は"


def test_streaming_pieces_join_into_sentences():
    buffer, spoken = "", []
    for piece in ["ねえマス", "ター。ちょっ", "といい？", "……別に"]:
        sentences, buffer = split_sentences(buffer + piece)
        spoken += sentences
    assert spoken == ["ねえマスター。", "ちょっといい？"]
    assert buffer == "……別に"


def test_clean_for_speech():
    assert clean_for_speech("(mock) **大事** な話。") == "大事 な話。"
    assert clean_for_speech("見て https://example.com すごい") == "見て  すごい"
    assert clean_for_speech("……うん。") == "…うん。"


def test_has_speakable():
    assert has_speakable("うん")
    assert not has_speakable("……")
    assert not has_speakable("！？")
