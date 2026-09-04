from astolfo import interest


def _score(text: str, **kwargs) -> float:
    return interest.rate(text, **kwargs).score


def test_an_open_question_beats_a_sign_off():
    assert _score("بچه‌ها کسی میدونه فردا بازی کیه؟") > _score("مرسی")
    assert _score("guys anyone up for a game?") > _score("ok")


def test_it_stays_out_of_two_other_people_talking():
    """The single biggest reason it used to look like it was interrupting."""
    alone = _score("این گربه خیلی نازه")
    threaded = _score("این گربه خیلی نازه", in_thread=True)
    assert threaded < alone - 0.3


def test_it_does_not_talk_twice_in_a_row():
    assert _score("عه جدی؟", spoke_last=True) < _score("عه جدی؟")


def test_media_and_things_it_likes_raise_it():
    assert _score("", has_media=True) > _score("")
    assert _score("یه بچه گربه دیدم") > _score("یه فرم اداری پر کردم")


def test_a_running_joke_in_the_notes_raises_it():
    plain = _score("درباره هیپوگریف حرف میزدیم")
    remembered = _score("درباره هیپوگریف حرف میزدیم", notes="هیپوگریف شوخی همیشگی گروهه")
    assert remembered > plain


def test_the_score_stays_in_range():
    everything = interest.rate("بچه‌ها گربه!! کسی هست؟؟", has_media=True, notes="گربه")
    nothing = interest.rate("ok", in_thread=True, spoke_last=True)
    assert 0.0 <= nothing.score <= everything.score <= 1.0
    assert everything.reason and nothing.reason


def test_zero_talkativeness_never_joins():
    hot = interest.Interest(1.0, "everything")
    assert not interest.worth_joining(hot, 0.0)
    assert not interest.worth_joining(interest.Interest(0.0, "nothing"), 1.0)


def test_the_setting_moves_the_bar_rather_than_deciding_alone():
    dull = interest.Interest(0.2, "not much")
    lively = interest.Interest(0.9, "an open question")
    # Without the jitter the decision is a pure comparison, which is what is tested.
    assert not interest.worth_joining(dull, 0.3, jitter=False)
    assert interest.worth_joining(lively, 0.3, jitter=False)
    # Turned up, even a dull message gets answered; turned down, even a good one does not.
    assert interest.worth_joining(dull, 0.9, jitter=False)
    assert not interest.worth_joining(lively, 0.05, jitter=False)
