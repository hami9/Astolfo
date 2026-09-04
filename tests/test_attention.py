from astolfo.attention import DISTRACTED_SHARE, Attention


def test_nothing_holds_it_at_first():
    attention = Attention()
    assert not attention.holds(-100)
    assert not attention.elsewhere(-100)
    assert attention.share_for(-100) == 1.0


def test_joining_a_conversation_claims_it():
    attention = Attention(hold=60)
    attention.claim(-100)
    assert attention.holds(-100)
    assert attention.elsewhere(-200)
    assert not attention.elsewhere(-100)


def test_the_other_chats_get_less_of_it():
    attention = Attention(hold=60)
    attention.claim(-100)
    assert attention.share_for(-200) == DISTRACTED_SHARE
    assert attention.share_for(-100) == 1.0, "the chat it is in is unaffected"


def test_being_spoken_to_ends_the_daydream():
    attention = Attention(hold=60)
    attention.claim(-100)
    attention.release(-100)
    assert attention.share_for(-200) == 1.0


def test_releasing_a_chat_that_does_not_hold_it_changes_nothing():
    attention = Attention(hold=60)
    attention.claim(-100)
    attention.release(-999)
    assert attention.holds(-100)


def test_it_wears_off():
    attention = Attention(hold=0)
    attention.claim(-100)
    assert not attention.holds(-100)
    assert attention.share_for(-200) == 1.0, "a zero hold switches the whole thing off"
